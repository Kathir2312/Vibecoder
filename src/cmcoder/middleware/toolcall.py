"""Tell a running tool which call it is (for live output and "Stop this command"), and
carry what it leaves for the chat onto the tool message.

A LangChain middleware: `awrap_tool_call` runs around each tool call, in the same task as
the tool, so a `ContextVar` set here is visible to the tool's code (tools/progress.py
explains why a tool needs it).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ..tools.progress import call_artifact, current_call


class ToolCallContextMiddleware(AgentMiddleware):
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        token = current_call.set((str(call.get("id") or ""), str(call.get("name") or "")))
        left: dict[str, object] = {}
        artifact_token = call_artifact.set(left)
        try:
            result = await handler(request)
        finally:
            current_call.reset(token)
            call_artifact.reset(artifact_token)
        if left and isinstance(result, ToolMessage):
            # What the tool left for the chat, for a failed call too (see progress.py).
            existing = result.artifact if isinstance(result.artifact, dict) else {}
            result.artifact = {**left, **existing}
        return result
