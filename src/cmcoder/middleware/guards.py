"""Small guards that keep mid-sized open models (Qwen3) on track.

Each class is a short example of one middleware hook:

* `ToolCallRepairMiddleware.aafter_model`: runs right after each model reply
  and can change it before the tools run. Returning `{"messages": [msg]}`
  updates the graph state. Because `msg` has the *same id* as the reply, the
  `add_messages` reducer replaces the old message instead of appending a new
  one.
* `BashFileWorkMiddleware` and `LoopGuardMiddleware`: `awrap_tool_call` that
  answers for the tool without running it (returns a `ToolMessage`).

Ported from cmcoderagent's hand-written loop (core/agent.py::_run_call).
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from ..core.steer import file_work_redirect
from .permission import call_key, denied

MAX_IDENTICAL_CALLS = 3

ToolHandler = Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse model-produced JSON arguments, repairing common mistakes.

    Handles code fences, trailing commas, Python-style dicts and double-encoded
    JSON, which smaller models produce now and then.
    """
    text = (raw or "").strip()
    if not text:
        return {}, None
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value: Any = None
    error: str | None = None
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", fenced)):
        try:
            value = json.loads(candidate, strict=False)
            error = None
            break
        except ValueError as e:
            error = str(e)
    if error is not None:
        try:
            value = ast.literal_eval(fenced)
            error = None
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
    if error is not None:
        return None, f"arguments are not valid JSON ({error})"
    if isinstance(value, str):
        try:
            value = json.loads(value, strict=False)
        except ValueError:
            pass
    if not isinstance(value, dict):
        return None, "arguments must be a JSON object"
    return value, None


class ToolCallRepairMiddleware(AgentMiddleware):
    """Fix broken tool calls before LangChain rejects them.

    When the model's arguments aren't valid JSON, `ChatOpenAI` puts the call in
    `AIMessage.invalid_tool_calls`, and the agent would answer it with an
    error. Most of these are easy to repair, which saves a whole model turn.
    We also give calls without an id a stable one (some servers leave it out),
    because every `ToolMessage` must point at its call's id.
    """

    async def aafter_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return None
        calls = [dict(c) for c in last.tool_calls]
        still_invalid = []
        changed = False
        for bad in last.invalid_tool_calls:
            args, _error = parse_tool_arguments(bad.get("args") or "")
            if args is None or not bad.get("name"):
                still_invalid.append(bad)
                continue
            calls.append(
                {"name": bad["name"], "args": args, "id": bad.get("id"), "type": "tool_call"}
            )
            changed = True
        for c in calls:
            if not c.get("id"):
                c["id"] = f"call_{uuid.uuid4().hex[:12]}"
                changed = True
        if not changed:
            return None
        fixed = last.model_copy(update={"tool_calls": calls, "invalid_tool_calls": still_invalid})
        return {"messages": [fixed]}


class BashFileWorkMiddleware(AgentMiddleware):
    """Steer plain file work in Bash (`cat > f << EOF`, `sed -i`) to the file tools.

    Qwen3 likes to edit files through the shell, which skips read-before-write
    checks and floods permission prompts. The first time, we answer with a
    hint instead of running the command; if the model sends the same command
    again, it runs (the model may have a good reason).
    """

    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled
        self._redirected: str | None = None
        # Calls already let through: LangGraph re-runs the tool node when it
        # resumes after a permission prompt, and the same call must not be
        # redirected the second time.
        self._passed: set[str] = set()

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: ToolHandler
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        call_id = call_key(call)
        if not self.enabled or call["name"] != "Bash" or call_id in self._passed:
            return await handler(request)
        command = str(call["args"].get("command", ""))
        hint = file_work_redirect(command)
        if hint and command != self._redirected:
            self._redirected = command
            return denied(
                call,
                f"{hint} If the shell is really needed here, say why and send the same command again.",
            )
        self._redirected = None
        self._passed.add(call_id)
        return await handler(request)


class LoopGuardMiddleware(AgentMiddleware):
    """Stop the model from calling the same tool with the same arguments forever."""

    def __init__(self, limit: int = MAX_IDENTICAL_CALLS) -> None:
        super().__init__()
        self.limit = limit
        self._last: str | None = None
        self._count = 0
        self._seen_ids: set[str] = set()

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: ToolHandler
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        call_id = call_key(call)
        if call_id not in self._seen_ids:  # count each call once, even when re-run
            self._seen_ids.add(call_id)
            key = json.dumps([call["name"], call["args"]], sort_keys=True, default=str)
            self._count = self._count + 1 if key == self._last else 1
            self._last = key
        if self._count >= self.limit:
            return denied(
                call,
                f"You have called {call['name']} with identical arguments {self._count} times "
                "in a row. Stop repeating it and try a different approach.",
            )
        return await handler(request)


class PlainToolTextMiddleware(AgentMiddleware):
    """Turn tool results made of content blocks into plain text.

    MCP tools (langchain-mcp-adapters) return LangChain content blocks:
    `[{"type": "text", "text": "5", "id": ...}]`. OpenAI's own API accepts
    that for tool messages, but many self-hosted servers expect a string, and
    the UIs show text. A post-processing tool wrapper: call the handler,
    then change the `ToolMessage` it returns.
    """

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: ToolHandler
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if isinstance(result, ToolMessage) and isinstance(result.content, list):
            return result.model_copy(update={"content": result.text})
        return result
