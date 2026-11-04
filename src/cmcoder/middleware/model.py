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
from langchain.agents.middleware import AgentMiddleware, AgentState, ClearToolUsesEdit
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ..models.chat import REASONING_KEY, ModelBundle
from ..models.overflow import is_context_overflow
from ..models.reasoning import split_think
from ..models.windows import SOURCE, parse_context_window, save_window
from ..protocol import events as ev
from ..tools.base import output_budget_chars, truncate_middle

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
    """Copies of `messages` with all but the newest `keep` tool outputs replaced.

    LangChain's own edit does it (the one `ContextEditingMiddleware` applies at 60%
    of the window); trigger=0 makes it clear now."""
    edited = list(messages)
    ClearToolUsesEdit(trigger=0, keep=keep, placeholder=CLEARED).apply(
        edited, count_tokens=count_tokens_approximately
    )
    return edited


def shorten_tool_outputs(messages: Sequence[AnyMessage], limit: int) -> list[AnyMessage]:
    """Copies of `messages` with every tool output longer than `limit` characters
    cut in the middle (the start and the end are what matters most)."""
    return [
        m.model_copy(update={"content": truncate_middle(m.content, limit)})
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and len(m.content) > limit
        else m
        for m in messages
    ]


# Overflow recovery, in the order tried, each on top of the one before: what to do
# to the request, and the most output tokens to ask for with it (a smaller reply
# leaves room when the server's window is smaller than we thought).
RECOVERY = (
    ("old tool outputs cleared", None),
    ("older messages summarised", 2048),
    ("long tool outputs shortened", 1024),
)
SHORTEST_OUTPUT = 1500  # characters: shorter than this isn't worth shortening to


class ModelProfileMiddleware(AgentMiddleware):
    """Apply the model profile to each call and keep within the context window.

    * `max_tokens` per call: servers like vLLM reject a request when prompt +
      `max_tokens` is larger than the window. With a 32K Qwen3 window that
      happens quickly, so it is set to the room that is actually left.
    * Calibration: after each reply, the server's real prompt token count
      (`AIMessage.usage_metadata`) tunes our characters-per-token estimate.
    * Window learning: if the server says how long it allows ("maximum context
      length is N", "... tokens > N maximum"), N is saved (models/windows.py).
    * Recovery: a request the server refuses as too long (in whatever words:
      models/overflow.py) is retried with less in it, step by step: old tool
      outputs cleared, older messages summarised (`summarise`, from the
      compaction middleware, which then writes the summary into the
      conversation), long tool outputs shortened. Only if all of that fails
      does the user see an error.
    """

    def __init__(
        self,
        bundle: ModelBundle,
        on_window: Callable[[int], None] | None = None,
        summarise: Callable[[list[AnyMessage]], Awaitable[list[AnyMessage] | None]] | None = None,
    ) -> None:
        super().__init__()
        self.bundle = bundle
        self.on_window = on_window  # tells others (compaction) about a learned window
        self.summarise = summarise  # CompactionMiddleware.emergency; None for subagents
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
        except openai.APIStatusError as e:
            if not is_context_overflow(e):
                raise
            response = await self._recover(request, e, settings, handler)
        self._calibrate(request, response)
        return response

    def _learn(self, error: BaseException) -> int | None:
        """The window the server names in its error, if it does: saved and passed on."""
        window = parse_context_window(str(error))
        if window is not None:
            b = self.bundle
            b.set_context_window(window, SOURCE)
            save_window(b.base_url, b.name, window)
            if self.on_window:
                self.on_window(window)
        return window

    async def _recover(
        self,
        request: ModelRequest,
        error: openai.APIStatusError,
        settings: dict[str, Any],
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Retry a request the server refused as too long, with less in it each time."""
        known = self._learn(error) is not None
        # "...you requested N tokens": N - our max_tokens is the server's own
        # count of the prompt, better than our estimate.
        server_prompt = None
        if m := _REQUESTED.search(str(error)):
            server_prompt = int(m.group(1)) - int(settings["max_tokens"])
        current, last = request, error
        for step, (what, cap) in enumerate(RECOVERY):
            if step == 0:
                current = current.override(
                    messages=clear_old_tool_outputs(current.messages, KEEP_TOOL_OUTPUTS)
                )
            elif step == 1 and self.summarise is not None:
                shorter = await self.summarise(list(current.messages))
                if shorter is not None:
                    current = current.override(messages=shorter)
            elif step == 2:
                sizes = [
                    len(m.content)
                    for m in current.messages
                    if isinstance(m, ToolMessage) and isinstance(m.content, str)
                ]
                limit = max(
                    SHORTEST_OUTPUT,
                    min(
                        output_budget_chars(self.bundle.profile.context_window),
                        max(sizes, default=0) // 2,
                    ),
                )
                current = current.override(messages=shorten_tool_outputs(current.messages, limit))
            retry = self._settings(current, server_prompt if step == 0 else None)
            if cap is not None:
                retry["max_tokens"] = min(retry["max_tokens"], cap)
            try:
                response = await handler(current.override(model_settings=retry))
            except openai.APIStatusError as again:
                if not is_context_overflow(again):
                    raise
                known = self._learn(again) is not None or known
                last = again
                continue
            if not known and self.on_window:
                # An estimate: compact before the prompt gets as big as the one refused.
                self.on_window(int(self._prompt_tokens(request) * 0.9))
            _notice(f"The conversation was too long for the model: {what}, and cmcoder went on.")
            return response
        raise last

    def _prompt_tokens(self, request: ModelRequest) -> int:
        system = [request.system_message] if request.system_message else []
        chars = count_chars(system + list(request.messages), list(request.tools))
        return int(chars / self.chars_per_token)


def _notice(text: str) -> None:
    """Tell the user in the chat (a warning event on the custom stream), if there is one."""
    try:
        get_stream_writer()({"type": "event", "event": ev.Warning(message=text).model_dump()})
    except RuntimeError:  # not inside a graph run: nobody to tell
        pass


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
