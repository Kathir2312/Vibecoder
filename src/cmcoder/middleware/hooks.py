"""Hooks: the user's shell commands on agent events, as LangChain middleware.

The hook runner itself (core/hooks.py, from cmcoderagent) runs a command with
the event as JSON on stdin and reads its answer: exit 2 blocks, JSON can
decide. This file connects it to the agent loop:

| Hook          | Where                                                      |
|---------------|------------------------------------------------------------|
| PreToolUse    | middleware/permission.py (it changes the permission decision) |
| PostToolUse   | `HooksMiddleware.awrap_tool_call`, after the tool ran        |
| Stop          | `HooksMiddleware.aafter_agent`: a block sends the agent back to work |
| UserPromptSubmit, SessionStart, Notification | runtime/session.py (outside the graph) |
| PreCompact    | middleware/compaction.py                                   |

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `aafter_agent` runs once, when the agent is about to finish. Decorated
  with `@hook_config(can_jump_to=["model"])`, it may return
  `{"jump_to": "model"}` (plus new messages) to send the agent back to the
  model instead of ending. That is how a Stop hook says "not done yet".
* `get_stream_writer()`: anything a node or middleware writes with it comes
  out of `graph.astream(..., stream_mode="custom")`. We use it to show hook
  warnings in the UI without putting them in the conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from ..core.hooks import ApproveHookFn, HookRunner

MAX_STOP_BLOCKS = 3  # a Stop hook can't keep a turn going forever


def warn(message: str) -> None:
    """Show a warning in the UI (the session turns custom events into `warning`)."""
    try:
        get_stream_writer()({"type": "warning", "message": message})
    except RuntimeError:  # not inside a graph run (e.g. a unit test)
        pass


class HooksMiddleware(AgentMiddleware):
    def __init__(self, runner: HookRunner, approve_fn: ApproveHookFn | None = None) -> None:
        super().__init__()
        self.runner = runner
        self.approve_fn = approve_fn
        self._stop_blocks = 0

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        self._stop_blocks = 0  # a new user turn
        return None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if not self.runner.has("PostToolUse") or not isinstance(result, ToolMessage):
            return result
        call = request.tool_call
        outcome = await self.runner.run(
            "PostToolUse",
            {
                "tool_name": call["name"],
                "tool_input": call["args"],
                "tool_response": {"content": result.text, "is_error": result.status == "error"},
            },
            match=call["name"],
            approve_fn=self.approve_fn,
        )
        for w in outcome.warnings:
            warn(w)
        notes = ([f"A PostToolUse hook objected: {outcome.reason}"] if outcome.block else []) + (
            outcome.context
        )
        if not notes:
            return result
        return result.model_copy(update={"content": result.text + "\n\n" + "\n".join(notes)})

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        if not self.runner.has("Stop"):
            return None
        last = state["messages"][-1] if state["messages"] else None
        outcome = await self.runner.run(
            "Stop",
            {
                "stop_hook_active": self._stop_blocks > 0,
                "last_assistant_message": last.text if isinstance(last, AIMessage) else "",
            },
            approve_fn=self.approve_fn,
        )
        for w in outcome.warnings:
            warn(w)
        if not outcome.block or self._stop_blocks >= MAX_STOP_BLOCKS:
            return None
        self._stop_blocks += 1
        reason = outcome.reason or "a Stop hook says the work isn't finished"
        return {
            "messages": [HumanMessage(f"[Stop hook] Keep working: {reason}")],
            "jump_to": "model",
        }
