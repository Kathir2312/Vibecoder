"""Middleware around the model call: per-model settings, context budget, reasoning.

LANGCHAIN CONCEPTS IN THIS FILE

* `awrap_model_call(request, handler)`: wraps every call to the chat model.
  `request` is a `ModelRequest` (model, messages, system message, tools,
  `model_settings`). `request.override(...)` returns a changed *copy*: requests
  are immutable, so middlewares can't change things behind each other's backs.
  `model_settings` are passed to the model call (`model.bind_tools(tools,
  **model_settings)`), which is how we set `max_tokens` per call.
* `aafter_model`: post-processes the reply in the graph state (see guards.py).

Order: create_agent applies middlewares outer to inner in list order, so the
first middleware in the list sees the request first and the response last.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import openai
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime

from ..models.chat import REASONING_KEY, ModelBundle
from ..models.reasoning import split_think
from ..models.windows import SOURCE, parse_context_window, save_window

# Rough size of a token in characters, for code and English, until the
# server's own counts calibrate it (see ModelProfileMiddleware).
CHARS_PER_TOKEN = 3.5
MIN_OUTPUT_TOKENS = 512
SAFETY_MARGIN = 256  # tokens kept free for the server's chat template
_REQUESTED = re.compile(r"you requested (\d+) tokens", re.I)
CLEARED = "[Old tool output cleared to fit the context window.]"
KEEP_TOOL_OUTPUTS = 3  # the newest tool outputs survive a retry after an overflow


def count_chars(messages: Sequence[BaseMessage], tools: Sequence[Any]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.text) if isinstance(m.content, list) else len(str(m.content))
        if isinstance(m, AIMessage) and m.tool_calls:
            chars += len(json.dumps([c["args"] for c in m.tool_calls], default=str))
    for t in tools:
        chars += len(getattr(t, "description", "") or "") + 200  # name + JSON schema
    return chars


def clear_old_tool_outputs(messages: Sequence[AnyMessage], keep: int) -> list[AnyMessage]:
    """Copies of `messages` with all but the newest `keep` tool outputs replaced."""
    tool_idx = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    old = set(tool_idx[:-keep] if keep else tool_idx)
    return [
        m.model_copy(update={"content": CLEARED}) if i in old else m for i, m in enumerate(messages)
    ]


class ModelProfileMiddleware(AgentMiddleware):
    """Apply the model profile to each call and keep within the context window.

    * `max_tokens` per call: servers like vLLM reject a request when prompt +
      `max_tokens` is larger than the window. With a 32K Qwen3 window that
      happens quickly, so it is set to the room that is actually left.
    * Calibration: after each reply, the server's real prompt token count
      (`AIMessage.usage_metadata`) tunes our characters-per-token estimate.
    * Window learning: if the server still says "maximum context length is N",
      N is saved (models/windows.py) and the call is retried once with old
      tool outputs cleared, in this request only (the state isn't changed).
    """

    def __init__(self, bundle: ModelBundle, on_window: Callable[[int], None] | None = None) -> None:
        super().__init__()
        self.bundle = bundle
        self.on_window = on_window  # tells others (compaction) about a learned window
        self.chars_per_token = CHARS_PER_TOKEN

    def _settings(self, request: ModelRequest, prompt_tokens: int | None = None) -> dict[str, Any]:
        p = self.bundle.profile
        if prompt_tokens is None:
            system = [request.system_message] if request.system_message else []
            chars = count_chars(system + list(request.messages), list(request.tools))
            prompt_tokens = int(chars / self.chars_per_token)
        room = p.context_window - prompt_tokens - SAFETY_MARGIN
        settings = dict(request.model_settings)
        # CmcoderChatModel sends this as `max_tokens` or `max_completion_tokens`.
        settings["max_tokens"] = max(MIN_OUTPUT_TOKENS, min(p.max_output, room))
        if request.tools:
            # Several tool calls in one reply, or one at a time (per model).
            settings["parallel_tool_calls"] = p.parallel_tool_calls
        return settings

    def _calibrate(self, request: ModelRequest, response: ModelResponse) -> None:
        reply = next((m for m in response.result if isinstance(m, AIMessage)), None)
        usage = reply.usage_metadata if reply is not None else None
        if not usage or usage.get("input_tokens", 0) < 200:
            return
        system = [request.system_message] if request.system_message else []
        ratio = (
            count_chars(system + list(request.messages), list(request.tools))
            / usage["input_tokens"]
        )
        if 1.5 <= ratio <= 8:  # ignore nonsense counts
            self.chars_per_token = 0.7 * self.chars_per_token + 0.3 * ratio

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        system = request.system_message
        if system is not None and isinstance(system.content, list):
            # Middleware such as TodoListMiddleware appends its instructions to
            # the system message as content blocks. Self-hosted servers often
            # want one plain string there.
            request = request.override(system_message=SystemMessage(system.text))
        settings = self._settings(request)
        try:
            response = await handler(request.override(model_settings=settings))
        except openai.BadRequestError as e:
            window = parse_context_window(str(e))
            if window is None:
                raise
            b = self.bundle
            b.set_context_window(window, SOURCE)
            save_window(b.base_url, b.name, window)
            if self.on_window:
                self.on_window(window)
            # "...you requested N tokens": N - our max_tokens is the server's
            # own count of the prompt, better than our estimate.
            server_prompt = None
            if m := _REQUESTED.search(str(e)):
                server_prompt = int(m.group(1)) - int(settings["max_tokens"])
            smaller = request.override(
                messages=clear_old_tool_outputs(request.messages, KEEP_TOOL_OUTPUTS)
            )
            retry = self._settings(smaller, server_prompt)
            response = await handler(smaller.override(model_settings=retry))
        self._calibrate(request, response)
        return response


class ReasoningMiddleware(AgentMiddleware):
    """Move `<think>...</think>` text out of the reply into `reasoning_content`.

    Qwen's guidance is not to send old reasoning back to the model, and the
    user shouldn't see it as the answer. After this runs, `AIMessage.content`
    holds only the visible answer.
    """

    async def aafter_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not isinstance(last.content, str):
            return None
        visible, reasoning = split_think(last.content)
        if not reasoning:
            return None
        extra = dict(last.additional_kwargs)
        extra[REASONING_KEY] = "\n".join(x for x in (extra.get(REASONING_KEY), reasoning) if x)
        return {
            "messages": [last.model_copy(update={"content": visible, "additional_kwargs": extra})]
        }
