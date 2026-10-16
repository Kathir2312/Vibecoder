"""Critique: a critic agent checks the answer before the user sees it.

On with `/critic on`, `--critic` or `critic.enabled` (off by default, as in
cmcoderagent). When the main agent is about to finish, a read-only `critic`
subagent gets the user's request, the draft answer, what the agent did this
turn and the turn's diff, checks them against the files, and gives a verdict.
Pass: the answer is shown. Fail: the problems go back to the main agent,
which fixes them and answers again, up to `critic.maxRounds`; after that the
answer is shown marked "not validated".

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `aafter_agent` + `@hook_config(can_jump_to=["model"])`: the same hook a
  Stop hook uses (middleware/hooks.py). Returning `{"jump_to": "model"}` with
  a new message turns "the agent is done" into "one more round".
* **Structured output**: the critic is a `create_agent` graph with
  `response_format=ToolStrategy(Verdict)`. LangChain gives the model a tool
  shaped like the `Verdict` schema; when the model calls it, the run ends and
  the parsed object is in the state's `structured_response`. No parsing of
  free text, and the schema's field descriptions tell the model what to fill.
* `get_stream_writer()` in middleware: the critic's progress (its tool calls,
  `subagent_status`, the `review_result`) reaches the UI on the custom stream.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from ..agents.context import RunContext
from ..agents.subagents import CRITIC_NAME, AgentDefinition, SubagentRuntime, run_agent
from ..core.checkpoints import Checkpoints
from ..protocol import events as ev
from ..sensitive import is_secret
from .compaction import latest_request

CRITIC_MAX_TURNS = 30  # model calls the critic may make per review
CHARS_PER_TOKEN = 4
PROMPT_SHARE = 0.45  # of the window, for the review prompt (the critic reads more itself)

CRITIC_PROMPT = """\
You are the reviewer (critic) of cmcoder, a coding assistant. Before an answer
is shown to the user, you check it. You can read the project (Read, Grep,
Glob) but not change anything.

Check:
- Does the answer do what the user asked, completely?
- Is what it says true? Verify claims against the files instead of trusting
  them: the code it says it changed, the functions it names, the results it
  reports.
- Are the changes correct: bugs, missing cases, broken callers, an edit that
  doesn't do what the answer says?
- Does it claim work it didn't do?

Report real problems the user would want fixed, not style preferences or
optional extras. If the answer is right, pass it.

End by calling the Verdict tool exactly once: verdict "pass" or "fail", a
one-sentence summary, and for "fail" each problem with what is wrong, where
(file:line when you can) and how to fix it."""

CRITIC_DEFINITION = AgentDefinition(
    CRITIC_NAME,
    "Reviews the answer before the user sees it (when critique is on).",
    CRITIC_PROMPT,
    "built-in",
    tools=["Read", "Glob", "Grep", "CodeSearch"],
)


class Issue(BaseModel):
    severity: Literal["high", "medium", "low"] = Field(
        "medium", description="high: wrong or broken; medium: incomplete; low: minor"
    )
    problem: str = Field(description="What is wrong")
    where: str = Field("", description="file:line, or where in the answer")
    fix: str = Field("", description="How to fix it")


class Verdict(BaseModel):
    """Give your verdict on the answer: pass, or fail with the problems to fix."""

    verdict: Literal["pass", "fail"]
    summary: str = Field(description="One sentence: why it passes or fails")
    issues: list[Issue] = Field(default_factory=list, description="For fail: each problem to fix")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 60)] + f"\n... [{len(text) - limit + 60} more characters]"


def turn_diff(checkpoints: Checkpoints, turn: int, root: Path) -> str:
    """This turn's changes to the project's files (from the file snapshots), as a
    unified diff. Secret files and files outside the project are left out."""
    seen: set[str] = set()
    parts: list[str] = []
    for entry in checkpoints.entries:
        if entry.turn != turn or entry.path in seen or entry.skipped:
            continue
        seen.add(entry.path)
        path = Path(entry.path)
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if is_secret(path, root):
            continue
        try:
            before = checkpoints._get(entry.blob).decode() if entry.blob else ""
            after = path.read_bytes().decode() if path.is_file() else ""
        except (OSError, UnicodeDecodeError, KeyError):
            parts.append(f"(changed: {rel}, not shown)")
            continue
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}" if entry.blob else "/dev/null",
            tofile=f"b/{rel}" if path.exists() else "/dev/null",
            lineterm="",
        )
        parts.append("\n".join(diff))
    return "\n\n".join(p for p in parts if p)


def turn_actions(messages: list[AnyMessage]) -> str:
    """The turn's tool calls and the first lines of their results."""
    results = {m.tool_call_id: m.text for m in messages if isinstance(m, ToolMessage)}
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for call in m.tool_calls:
            args = " ".join(str(call["args"]).split())
            out = (results.get(call["id"] or "") or "").strip().splitlines()
            first = f" -> {out[0][:160]}" if out else ""
            lines.append(f"- {call['name']}({args[:200]}){first}")
    return "\n".join(lines) or "(no tool calls)"


def fix_request(review: Verdict, round_: int, max_rounds: int) -> str:
    listed = (
        "\n".join(
            f"- [{i.severity}] {i.problem}"
            + (f" ({i.where})" if i.where else "")
            + (f" Fix: {i.fix}" if i.fix else "")
            for i in review.issues
        )
        or f"- {review.summary}"
    )
    return (
        f"<system-reminder>A reviewer checked your answer before the user saw it and found "
        f"problems (review {round_} of {max_rounds}):\n{listed}\n\n"
        "Fix them with your tools, then give your complete final answer again (the user "
        "hasn't seen the previous one). Don't mention the review.</system-reminder>"
    )


class CriticMiddleware(AgentMiddleware):
    """Reviews the final answer; `enabled` can be switched during a session."""

    def __init__(self, rt: SubagentRuntime, *, enabled: bool, max_rounds: int, window: int) -> None:
        super().__init__()
        self.rt = rt
        self.enabled = enabled
        self.max_rounds = max_rounds
        self.window = window
        self._round = 0
        self._earlier: list[Issue] = []

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        self._round, self._earlier = 0, []  # a new user turn
        return None

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        messages: list[AnyMessage] = list(state["messages"])
        last = messages[-1] if messages else None
        if not self.enabled or not isinstance(last, AIMessage) or last.tool_calls:
            return None
        writer = get_stream_writer()
        self._round += 1
        request = latest_request(messages)
        start = messages.index(request) if request is not None else 0
        context = runtime.context if isinstance(runtime.context, RunContext) else None
        diff = turn_diff(context.checkpoints, context.turn, self.rt.root) if context else ""
        call_id = f"review-{context.turn if context else 0}-{self._round}"
        label = f"Review(the answer, round {self._round}/{self.max_rounds})"
        writer(
            {
                "type": "event",
                "event": ev.ToolUse(
                    id=call_id, name="Task", input={"subagent_type": CRITIC_NAME}, label=label
                ).model_dump(),
            }
        )
        try:
            result = await run_agent(
                self.rt,
                CRITIC_DEFINITION,
                f"Review the answer (round {self._round}/{self.max_rounds})",
                self._prompt(request.text if request else "", last.text, messages[start:], diff),
                writer,
                call_id,
                response_format=ToolStrategy(Verdict),
                max_turns=CRITIC_MAX_TURNS,
            )
            verdict = result.structured if isinstance(result.structured, Verdict) else None
        except Exception as e:  # a broken review never loses the answer
            verdict = None
            summary = f"The reviewer failed: {type(e).__name__}: {e}"
        else:
            summary = verdict.summary if verdict else "The reviewer gave no verdict."
        state_name: Literal["pass", "fail", "none"] = verdict.verdict if verdict else "none"
        final = state_name != "fail" or self._round >= self.max_rounds
        writer(
            {
                "type": "event",
                "event": ev.ToolResult(
                    id=call_id, name="Task", content=summary, is_error=state_name != "pass"
                ).model_dump(),
            }
        )
        writer(
            {
                "type": "event",
                "event": ev.ReviewResult(
                    round=self._round,
                    max_rounds=self.max_rounds,
                    verdict=state_name,
                    final=final,
                    summary=summary,
                    issues=[
                        ev.ReviewIssue(**i.model_dump())
                        for i in (verdict.issues if verdict else [])
                    ],
                ).model_dump(),
            }
        )
        if final or verdict is None:
            return None
        self._earlier = list(verdict.issues)
        return {
            "messages": [HumanMessage(fix_request(verdict, self._round, self.max_rounds))],
            "jump_to": "model",
        }

    def _prompt(self, request: str, draft: str, turn: list[AnyMessage], diff: str) -> str:
        budget = int(self.window * CHARS_PER_TOKEN * PROMPT_SHARE)
        request = _clip(request, budget // 8)
        draft = _clip(draft, budget // 4)
        actions = _clip(turn_actions(turn), budget // 8)
        used = len(request) + len(draft) + len(actions) + 1500
        diff_text = (
            f"```diff\n{_clip(diff, max(2000, budget - used))}\n```"
            if diff
            else "(no files changed this turn)"
        )
        again = ""
        if self._earlier:
            listed = "\n".join(f"- [{i.severity}] {i.problem} ({i.where})" for i in self._earlier)
            again = (
                "\n\n## Problems found in the previous round\n"
                f"The agent was asked to fix these; check that it did:\n{listed}"
            )
        return (
            "Review this answer before it is shown to the user.\n\n"
            f"## The user's request\n{request}\n\n"
            f"## The answer (draft)\n{draft}\n\n"
            f"## What the agent did this turn\n{actions}\n\n"
            f"## Files changed this turn\n{diff_text}{again}\n\n"
            "Read the files you need to check it, then call Verdict."
        )
