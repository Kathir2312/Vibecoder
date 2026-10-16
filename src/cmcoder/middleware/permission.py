"""Ask the user before a tool runs: permission rules plus LangGraph interrupts.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `AgentMiddleware.awrap_tool_call(request, handler)`: wraps every tool call
  the agent makes. `request.tool_call` holds `{"name", "args", "id"}`. Calling
  `await handler(request)` runs the tool (and any inner middleware). Not
  calling it and returning a `ToolMessage` instead means the tool never runs,
  and the model sees our message as the tool's result.

* `langgraph.types.interrupt(value)`: pauses the whole graph run and hands
  `value` to whoever is running it (our runtime/session.py, which shows the
  permission prompt). The run is resumed with `Command(resume=answer)`, and
  `interrupt()` then *returns* `answer`.

  How resuming works matters: LangGraph re-runs the interrupted node from its
  start. This middleware therefore runs a second time for the same tool call,
  and this time `interrupt()` returns the saved answer instead of pausing. So
  everything before `interrupt()` must be safe to run twice (checking rules
  is), and side effects belong after it.

  Interrupts need a checkpointer (the graph's state is saved while it waits);
  agents/main.py always gives one.

WHY NOT LANGCHAIN'S BUILT-IN `HumanInTheLoopMiddleware`?
It asks for configured tool names (approve / edit / reject). cmcoder needs
much more: allow/ask/deny rules (`Bash(npm test:*)`, `Edit(src/**)`),
permission modes, protected paths, secret files and high-risk commands that
always ask. That logic is the battle-tested `PermissionPolicy` from
cmcoderagent (core/permissions.py), unchanged. This class only connects it to
LangGraph.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolCall, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from ..core.permissions import Decision, PermissionPolicy, suggest_rules
from ..tools.base import Tool, ToolContext

# The `kind` field of our interrupt payload, so the runtime can tell
# permission requests from other interrupts (IDE tools in Phase 2).
PERMISSION_KIND = "permission"


def denied(call: ToolCall, text: str) -> ToolMessage:
    """A tool result that tells the model the call didn't run."""
    return ToolMessage(
        content=text, tool_call_id=call["id"] or "", name=call["name"], status="error"
    )


class PermissionMiddleware(AgentMiddleware):
    """Checks every tool call against the permission policy; asks when needed.

    `interactive=False` (`cmcoder -p`, CI): nobody can answer, so calls that
    would ask are denied with a message saying how to allow them.
    """

    def __init__(
        self,
        policy: PermissionPolicy,
        ctx: ToolContext,
        tools: dict[str, Tool],
        *,
        interactive: bool,
        on_rule_saved: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.policy = policy
        self.ctx = ctx
        # Not `self.tools`: on AgentMiddleware that attribute means "extra tools
        # this middleware adds to the agent".
        self.known_tools = tools
        self.interactive = interactive
        self.on_rule_saved = on_rule_saved

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        tool = self.known_tools.get(call["name"])
        if tool is None:
            return await handler(request)  # MCP tools etc.; ToolNode reports unknown names
        try:
            args = tool.Input.model_validate(call["args"])
        except ValidationError:
            # Let LangChain's tool validation produce its usual error for the model.
            return await handler(request)

        check = self.policy.check(tool, args, self.ctx)
        if check.decision == Decision.DENY:
            return denied(call, f"Permission denied: {check.reason}")
        if check.decision == Decision.ALLOW:
            return await handler(request)

        # Decision.ASK
        label = tool.describe(args, self.ctx)
        rules = suggest_rules(tool, args, self.ctx)
        rule = ", ".join(rules)
        if not self.interactive:
            if check.high_risk:
                reason = (
                    f"{label} is a high-risk command ({check.reason}) and needs a person's "
                    "approval each time; it cannot run in non-interactive mode."
                )
            else:
                reason = (
                    f"{label} needs approval, which is not available in non-interactive mode. "
                    f'Allow it with --allowedTools "{rule}" or a less strict --permission-mode.'
                )
            return denied(call, f"Permission denied: {reason}")

        change = tool.proposed_change(args, self.ctx)
        can_remember = not check.high_risk and not self.policy.allow_rules_locked
        # ---- the graph pauses here until the user answers ----
        answer: dict[str, Any] = interrupt(
            {
                "kind": PERMISSION_KIND,
                "tool_use_id": call["id"] or "",
                "name": tool.name,
                "label": label,
                "input": args.model_dump(),
                "suggested_rule": rule,
                "reason": check.reason,
                "can_remember": can_remember,
                "change": None
                if change is None
                else {"path": str(change.path), "before": change.before, "after": change.after},
            }
        )
        # ---- resumed: `answer` is what Command(resume=...) carried ----
        if not answer.get("allow"):
            feedback = str(answer.get("feedback") or "").strip()
            text = "The user denied this action."
            text += (
                f" Their feedback: {feedback}"
                if feedback
                else " Stop and wait for the user's instructions."
            )
            return denied(call, text)
        if answer.get("remember") and can_remember:
            for r in rules:
                self.policy.add_allow(r)
                if self.on_rule_saved:
                    self.on_rule_saved(r)
        return await handler(request)
