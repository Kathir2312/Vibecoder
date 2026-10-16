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
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.runtime import Runtime

from ..models.chat import REASONING_KEY
from ..models.reasoning import split_think
from ..providers.profiles import ModelProfile

# Rough size of a token in characters, for code and English. cmcoderagent
# calibrated this from the server's own counts; Phase 1 brings that back.
CHARS_PER_TOKEN = 3.5
MIN_OUTPUT_TOKENS = 512
SAFETY_MARGIN = 256  # tokens kept free for the server's chat template


def estimate_tokens(messages: Sequence[BaseMessage], tools: Sequence[Any]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.text) if isinstance(m.content, list) else len(str(m.content))
        if isinstance(m, AIMessage) and m.tool_calls:
            chars += len(json.dumps([c["args"] for c in m.tool_calls], default=str))
    for t in tools:
        chars += len(getattr(t, "description", "") or "") + 200  # name + JSON schema
    return int(chars / CHARS_PER_TOKEN)


class ModelProfileMiddleware(AgentMiddleware):
    """Apply the model profile to each call and keep within the context window.

    Servers like vLLM reject a request when prompt + `max_tokens` is larger
    than the window. With a 32K Qwen3 window that happens quickly, so
    `max_tokens` is set per call to the room that is actually left.
    """

    def __init__(self, profile: ModelProfile) -> None:
        super().__init__()
        self.profile = profile

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        p = self.profile
        system = [request.system_message] if request.system_message else []
        used = estimate_tokens(system + list(request.messages), list(request.tools))
        room = p.context_window - used - SAFETY_MARGIN
        settings = dict(request.model_settings)
        # CmcoderChatModel sends this as `max_tokens` or `max_completion_tokens`.
        settings["max_tokens"] = max(MIN_OUTPUT_TOKENS, min(p.max_output, room))
        if request.tools:
            # Several tool calls in one reply, or one at a time (per model).
            settings["parallel_tool_calls"] = p.parallel_tool_calls
        return await handler(request.override(model_settings=settings))


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
