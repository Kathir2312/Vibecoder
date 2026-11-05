"""Make the agent rethink when it is stuck, instead of repeating itself until the budget is gone.

A mid-sized model that meets an error often tries the same thing again, or three slightly
different things that all fail for the same reason, until `maxTurns` runs out. A person (or
Copilot's and Claude's agents) stops, says what went wrong and picks another way. This
middleware does that stopping for the model, in three steps:

1. **Three tool calls in a row failed** (any tools, any arguments): a reminder is added before
   the next model call that asks for a short account of what was tried and a *different*
   approach, or a question to the user when something is missing.
2. **Still failing after a rethink** (three more failures; `maxRethinks`): the model's tools are
   taken away for one call and it is told to report to the user: what it was doing, what it tried, the error, its guess at the
   cause, what it needs. That ends the turn with something useful, not with an exhausted budget.
3. **80% of the model calls used** (`maxTurns`): one reminder to finish what matters most.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* **`abefore_model`**: a hook that runs before every model call; returning `{"messages": [m]}`
  appends `m` to the conversation (the `add_messages` reducer).
* **`awrap_model_call` + `request.override(tools=[])`**: change what one model call is given
  without touching the saved state. Here: no tools, so the model has to answer in words.
* **`get_stream_writer()`**: sends the "↻ Stuck" warning to the UI while the graph runs.

LangChain has no middleware for stuck detection, so the counting is plain code (a function of
the messages, no state of its own: it works the same after `--resume`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

# Marks the reminders this middleware adds ("stuck", "report" or "budget"). They are user-role
# messages made of one <system-reminder> block, so every UI hides them (runtime/session.py).
RETHINK_KEY = "cmcoder_rethink"

STUCK = "stuck"
REPORT = "report"
BUDGET = "budget"


def kind_of(message: AnyMessage) -> str | None:
    """The reminder kind of one of our reminders, else None."""
    if isinstance(message, HumanMessage):
        kind = message.additional_kwargs.get(RETHINK_KEY)
        return kind if isinstance(kind, str) else None
    return None


def this_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
    """The messages since the user's last real message (our own reminders aren't one)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and kind_of(m) is None:
            return messages[i + 1 :]
    return messages


def failure_streak(turn: list[AnyMessage]) -> tuple[list[ToolMessage], int]:
    """(the failed tool results at the end of the turn, the rethinks that did not help).

    The streak is the failures since the last success or the last rethink: a rethink gets a
    fresh chance, so counting starts again after it. The second number is how many rethinks
    came one after the other with no success in between; a success means the rethink worked
    (or at least moved things), so a later streak is a new problem and gets a rethink too.
    """
    streak: list[ToolMessage] = []
    rethinks = 0
    current = True  # still before the newest rethink
    for m in reversed(turn):
        kind = kind_of(m)
        if kind in (STUCK, REPORT):
            rethinks += kind == STUCK
            current = False
        elif isinstance(m, ToolMessage):
            if m.status != "error":
                break
            if current:
                streak.append(m)
    return streak, rethinks


def _first_line(text: str, width: int = 140) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= width else line[: width - 1] + "…"


def stuck_text(streak: list[ToolMessage]) -> str:
    names = ", ".join(sorted({m.name or "a tool" for m in streak}))
    last = _first_line(streak[0].text)
    return (
        f"You are stuck: the last {len(streak)} tool calls in a row failed ({names}). "
        f"The latest error: {last}\n"
        "Do not repeat them. Before your next tool call, write 2-3 sentences: what you tried "
        "and why you think it failed. Then name one approach that is clearly different from "
        "what you tried (another tool, another file, reading the error more closely, a smaller "
        "step) and follow it. If something is missing, such as information, a file or access, "
        "say so and ask the user instead of retrying."
    )


def report_text(streak: list[ToolMessage], rethinks: int) -> str:
    last = _first_line(streak[0].text)
    times = "once" if rethinks == 1 else f"{rethinks} times"
    return (
        f"You rethought {times} and your tool calls keep failing. The latest error: "
        f"{last}\n"
        "Stop using tools now and reply to the user in plain words: what you were trying to do, "
        "what you tried, the error you keep getting, your best guess at the cause, and what you "
        "need from them (a decision, a file, access) or what you would try next."
    )


def budget_text(calls: int, limit: int) -> str:
    return (
        f"You have used {calls} of the {limit} model calls allowed for this request. Finish what "
        "matters most now. If the work is not done, say plainly what is left. Do not start new "
        "exploration."
    )


def reminder(kind: str, text: str) -> HumanMessage:
    return HumanMessage(
        f"<system-reminder>\n{text}\n</system-reminder>", additional_kwargs={RETHINK_KEY: kind}
    )


class RethinkMiddleware(AgentMiddleware):
    """Stuck detection: ask for a rethink, then a report, instead of retrying to the limit."""

    def __init__(
        self,
        *,
        after_failures: int = 3,
        max_rethinks: int = 1,
        budget_fraction: float = 0.8,
        max_turns: int | None = None,
    ) -> None:
        super().__init__()
        self.after_failures = after_failures
        self.max_rethinks = max_rethinks
        self.budget_fraction = budget_fraction
        self.max_turns = max_turns

    def decide(self, messages: list[AnyMessage]) -> HumanMessage | None:
        """The reminder to add before the next model call, if any (a pure function)."""
        turn = this_turn(messages)
        if not turn or not isinstance(turn[-1], ToolMessage):
            return None  # the model is answering the user, or a reminder, not a tool result
        streak, rethinks = failure_streak(turn)
        if len(streak) >= self.after_failures:
            if rethinks >= self.max_rethinks:
                return reminder(REPORT, report_text(streak, rethinks))
            return reminder(STUCK, stuck_text(streak))
        if self.max_turns and self.budget_fraction > 0:
            calls = sum(1 for m in turn if m.type == "ai")
            if calls >= self.budget_fraction * self.max_turns and not any(
                kind_of(m) == BUDGET for m in turn
            ):
                return reminder(BUDGET, budget_text(calls, self.max_turns))
        return None

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        note = self.decide(list(state["messages"]))
        if note is None:
            return None
        kind = note.additional_kwargs[RETHINK_KEY]
        words = {
            STUCK: "↻ Stuck: the last tool calls in a row failed. Rethinking before trying again.",
            REPORT: "↻ Still stuck after rethinking: asking the model to report to you instead "
            "of trying again.",
            BUDGET: "⏱ Most of this request's model calls are used: asking the model to finish "
            "what matters most.",
        }[kind]
        try:
            get_stream_writer()({"type": "warning", "message": words})
        except Exception:  # outside a streamed run (a unit test): the reminder still goes in
            pass
        return {"messages": [note]}

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if request.messages and kind_of(request.messages[-1]) == REPORT:
            request = request.override(tools=[])  # it has to answer in words
        return await handler(request)
