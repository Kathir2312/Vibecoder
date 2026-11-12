"""ExitPlanMode: the agent's plan, for the user to approve before anything is changed.

In plan mode the agent can only look. When it has a plan it calls this tool instead of describing
the plan in a message: the permission middleware (middleware/permission.py `_exit_plan_mode`) pauses
the run with a LangGraph `interrupt()` and the chat shows the plan as a card the user can approve,
edit or send back with a note. On approval plan mode ends and the steps become the todo list.

This module is the tool's schema and the two small conversions around it (steps to todos, an edited
list of lines back to steps). The tool itself changes nothing: the pause and the mode switch are the
middleware's, so that nothing can run before the user has said yes.

LangChain / LangGraph: the interrupt and the state update (`Command(update={"todos": ...})`) are
LangGraph's; the todo list is LangChain's `TodoListMiddleware` state. Nothing here duplicates them.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import Tool, ToolContext, ToolInput, ToolResult

MAX_STEPS = 30


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(
        min_length=1,
        description="One step, as an action: 'Add a status filter to the Index action'.",
    )
    files: list[str] = Field(
        default_factory=list, description="The files this step changes or adds, if you know them."
    )


class PlanInput(ToolInput):
    title: str = Field(
        min_length=1, description="What the plan is for, in a few words: 'Orders status filter'."
    )
    steps: list[PlanStep] = Field(
        min_length=1, max_length=MAX_STEPS, description="The steps, in the order to do them."
    )
    out_of_scope: str = Field("", description="What you will not touch, if that is worth saying.")


class ExitPlanModeTool(Tool):
    name = "ExitPlanMode"
    description = (
        "Plan mode is read-only: you can look at the project but not change it. When you have "
        "looked enough and know what to do, call this with your plan (a title and the steps, "
        "in order, naming the files each one changes) INSTEAD of describing the plan in a "
        "message and asking whether to go ahead. The user reads the plan, may edit it, and "
        "approves or sends it back with a note. If it is approved plan mode ends and you carry "
        "the steps out in order (they are your todo list); if it is sent back, change the plan "
        "as they ask and call this again. Do not call it when you are not in plan mode."
    )
    Input = PlanInput
    read_only = True

    def describe(self, args: PlanInput, ctx: ToolContext) -> str:
        return f"Plan: {args.title}"

    async def run(self, args: PlanInput, ctx: ToolContext) -> ToolResult:
        n = len(args.steps)
        return ToolResult(
            "The user approved the plan. Plan mode is over: carry it out now, step by step and in "
            f"order. Its {n} step{'s' if n != 1 else ''} are your todo list; mark each one done as "
            "you finish it.",
            summary=f"plan approved ({n} steps)",
        )


def todos_from(steps: list[PlanStep]) -> list[dict[str, Any]]:
    """The plan as the todo list: the first step is the one in progress."""
    return [
        {"content": s.text, "status": "in_progress" if i == 0 else "pending"}
        for i, s in enumerate(steps)
    ]


_NUMBERING = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")


def steps_from_lines(text: str, original: list[PlanStep]) -> list[PlanStep]:
    """The steps the user edited: one per line, numbering and bullets dropped. A line that is an
    original step unchanged keeps that step's files. Empty or absurd input leaves the plan as it was."""
    lines = [_NUMBERING.sub("", line).strip() for line in text.splitlines()]
    lines = [line[:500] for line in lines if line][:MAX_STEPS]
    if not lines:
        return original
    files = {s.text: s.files for s in original}
    return [PlanStep(text=line, files=files.get(line, [])) for line in lines]
