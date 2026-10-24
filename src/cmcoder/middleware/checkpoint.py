"""Save a file's contents before Write or Edit changes it, for `/rewind`.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* **`awrap_tool_call` with a side effect before the tool runs**: take the
  snapshot, then `await handler(request)`.
* **Middleware order.** The first middleware in `create_agent`'s list is the
  outermost wrapper. This one comes after cmcoder's other tool wrappers
  (loop guard, permission, hooks) in agents/main.py, so it sees only calls
  they let through.
* **`request.runtime.context`**: per-run data (agents/context.py) reaching a
  tool hook; here the turn number and the snapshot store.
* **What a LangGraph checkpoint is *not***: it saves graph state, never files
  on disk. Undoing code is the application's job.

Two kinds of "checkpoint" meet in `/rewind`:

* LangGraph checkpoints (the checkpointer) hold the *conversation* at every
  step. Rewinding the conversation is LangGraph time travel
  (runtime/session.py).
* File snapshots (core/checkpoints.py, from cmcoderagent) hold the *code*
  as it was before the agent changed it. LangGraph doesn't know about files
  on disk, so this middleware takes them.

It is cmcoder's innermost tool wrapper: it only runs once the permission check has
let the call through, right before the tool writes. The snapshot store and
the turn number come from the run's context (agents/context.py).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import ValidationError

from ..agents.context import RunContext
from ..tools.base import Tool, ToolContext

FILE_EDIT_TOOLS = {"Write", "Edit"}


class FileCheckpointMiddleware(AgentMiddleware):
    def __init__(self, ctx: ToolContext, tools: dict[str, Tool]) -> None:
        super().__init__()
        self.ctx = ctx
        self.known_tools = tools

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        run = request.runtime.context if request.runtime is not None else None
        tool = self.known_tools.get(call["name"])
        if call["name"] in FILE_EDIT_TOOLS and tool is not None and isinstance(run, RunContext):
            try:
                target = tool.permission_target(tool.Input.model_validate(call["args"]), self.ctx)
            except ValidationError:
                target = None
            if isinstance(target, Path):
                try:
                    run.checkpoints.capture(run.turn, target)
                except OSError:
                    pass  # a missing snapshot must not block the edit
        return await handler(request)
