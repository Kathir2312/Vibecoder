"""When the agent is stuck it has to stop and rethink (middleware/rethink.py): three failed
tool calls in a row get a reminder to say what went wrong and try something different; still
failing after two rethinks, the model loses its tools and must report to the user."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from cmcoder.middleware.rethink import BUDGET, REPORT, RETHINK_KEY, STUCK, RethinkMiddleware
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def missing(i: int) -> dict[str, Any]:
    """A call that fails: reading a file that isn't there."""
    return {"tool_calls": [{"name": "Read", "arguments": {"file_path": f"nope{i}.txt"}}]}


def glob(i: int) -> dict[str, Any]:
    return {"tool_calls": [{"name": "Glob", "arguments": {"pattern": f"nothing{i}*.zz"}}]}


def last_user_text(request: dict[str, Any]) -> str:
    message = request["messages"][-1]
    assert message["role"] == "user", message
    return str(message["content"])


async def run(mock_server: Any, project: Path, script: list[dict[str, Any]], **settings: Any):
    server = mock_server(script)
    session = make_session(server.base_url, project, settings=settings or None)
    try:
        events = await run_turn(session, "do it")
        history = await session.history()
    finally:
        await session.agent.close()
    return server, events, history


async def test_three_failed_calls_in_a_row_ask_for_a_rethink(mock_server, project: Path) -> None:
    server, events, history = await run(
        mock_server, project, [missing(1), missing(2), missing(3), {"content": "Trying Glob."}]
    )
    assert len(server.requests) == 4
    assert "reminder" not in str(server.requests[2]["messages"][-1]).lower()  # after two: no
    text = last_user_text(server.requests[3])  # after three: yes
    assert "You are stuck" in text and "last 3 tool calls in a row failed" in text
    assert "different" in text and "Read" in text
    assert [w.message for w in of_type(events, ev.Warning) if "Stuck" in w.message]
    assert of_type(events, ev.Result)[0].subtype == "success"
    # The reminder is the model's, not a message the user wrote: no chat bubble for it.
    assert not [h for h in history if "stuck" in h.text.lower() and h.role == "user"]


async def test_still_failing_after_a_rethink_the_model_must_report(
    mock_server, project: Path
) -> None:
    script = [missing(i) for i in range(6)] + [{"content": "I am stuck on missing files."}]
    server, events, _ = await run(mock_server, project, script)
    assert len(server.requests) == 7
    assert "You are stuck" in last_user_text(server.requests[3])  # the first rethink
    report = last_user_text(server.requests[6])  # the second streak: report
    assert "Stop using tools now" in report and "rethought once" in report
    assert not server.requests[6].get("tools"), "no tools for the report"
    assert server.requests[3].get("tools"), "tools stay for a rethink"
    assert of_type(events, ev.Result)[0].result == "I am stuck on missing files."


async def test_a_success_in_between_is_not_stuck(mock_server, project: Path) -> None:
    script = [missing(1), missing(2), glob(1), missing(3), missing(4), {"content": "ok"}]
    server, events, _ = await run(mock_server, project, script)
    assert not [w for w in of_type(events, ev.Warning) if "Stuck" in w.message]
    assert "stuck" not in str(server.requests[-1]["messages"][-1]).lower()


async def test_a_reminder_when_most_model_calls_are_used(mock_server, project: Path) -> None:
    script = [glob(i) for i in range(9)] + [{"content": "done"}]
    server, events, _ = await run(mock_server, project, script, maxTurns=10)
    assert len(server.requests) == 10
    text = last_user_text(server.requests[8])  # before the 9th call, 8 of 10 are used
    assert "8 of the 10 model calls" in text
    assert "model calls" not in str(server.requests[9]["messages"][-1])  # only once
    assert [w for w in of_type(events, ev.Warning) if "model calls are used" in w.message]


async def test_it_can_be_switched_off(mock_server, project: Path) -> None:
    script = [missing(1), missing(2), missing(3), {"content": "ok"}]
    server, events, _ = await run(mock_server, project, script, rethink={"enabled": False})
    assert "stuck" not in str(server.requests[3]["messages"][-1]).lower()
    assert not [w for w in of_type(events, ev.Warning) if "Stuck" in w.message]


def _fail(i: int) -> list[Any]:
    return [
        AIMessage("", tool_calls=[{"name": "Bash", "args": {"command": f"c{i}"}, "id": f"c{i}"}]),
        ToolMessage("Exit code 1\nboom", tool_call_id=f"c{i}", name="Bash", status="error"),
    ]


def _ok(i: int) -> list[Any]:
    return [
        AIMessage("", tool_calls=[{"name": "Read", "args": {}, "id": f"r{i}"}]),
        ToolMessage("fine", tool_call_id=f"r{i}", name="Read"),
    ]


def test_the_decision_depends_only_on_the_messages() -> None:
    mw = RethinkMiddleware(after_failures=3, max_rethinks=1, max_turns=50)
    user = HumanMessage("fix it")
    assert mw.decide([user]) is None  # nothing to judge yet
    assert mw.decide([user, *_fail(1), *_fail(2)]) is None
    first = mw.decide([user, *_fail(1), *_fail(2), *_fail(3)])
    assert first is not None and first.additional_kwargs[RETHINK_KEY] == STUCK
    assert "<system-reminder>" in str(first.content) and "Exit code 1" in str(first.content)
    after = [user, *_fail(1), *_fail(2), *_fail(3), first]
    # The count starts again after a reminder: two more failures aren't a streak...
    assert mw.decide([*after, *_fail(4), *_fail(5)]) is None
    # ...three more are, and the rethink did not help: report.
    second = mw.decide([*after, *_fail(4), *_fail(5), *_fail(6)])
    assert second is not None and second.additional_kwargs[RETHINK_KEY] == REPORT
    # A new message from the user is a new turn: nothing carries over.
    assert mw.decide([*after, HumanMessage("try again")]) is None


def test_a_success_after_a_rethink_makes_the_next_streak_a_new_problem() -> None:
    mw = RethinkMiddleware(after_failures=3, max_rethinks=1)
    user = HumanMessage("fix it")
    first = mw.decide([user, *_fail(1), *_fail(2), *_fail(3)])
    assert first is not None
    history = [user, *_fail(1), *_fail(2), *_fail(3), first, *_ok(1), *_fail(4), *_fail(5)]
    again = mw.decide([*history, *_fail(6)])
    assert again is not None and again.additional_kwargs[RETHINK_KEY] == STUCK  # not REPORT


def test_two_rethinks_when_asked_for() -> None:
    mw = RethinkMiddleware(after_failures=2, max_rethinks=2)
    user = HumanMessage("go")
    first = mw.decide([user, *_fail(1), *_fail(2)])
    assert first is not None and first.additional_kwargs[RETHINK_KEY] == STUCK
    h = [user, *_fail(1), *_fail(2), first, *_fail(3), *_fail(4)]
    second = mw.decide(h)
    assert second is not None and second.additional_kwargs[RETHINK_KEY] == STUCK
    third = mw.decide([*h, second, *_fail(5), *_fail(6)])
    assert third is not None and third.additional_kwargs[RETHINK_KEY] == REPORT


def test_the_budget_reminder_comes_once() -> None:
    mw = RethinkMiddleware(max_turns=10, budget_fraction=0.8)
    turn: list[Any] = [HumanMessage("go")]
    for i in range(7):
        turn += [
            AIMessage("", tool_calls=[{"name": "Glob", "args": {}, "id": f"g{i}"}]),
            ToolMessage("ok", tool_call_id=f"g{i}", name="Glob"),
        ]
    assert mw.decide(turn) is None  # 7 of 10
    turn += [
        AIMessage("", tool_calls=[{"name": "Glob", "args": {}, "id": "g7"}]),
        ToolMessage("ok", tool_call_id="g7", name="Glob"),
    ]
    note = mw.decide(turn)
    assert note is not None and note.additional_kwargs[RETHINK_KEY] == BUDGET
    assert mw.decide([*turn, note]) is None  # not again
