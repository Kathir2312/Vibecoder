"""A long task must reach `maxTurns` instead of dying with LangGraph's GraphRecursionError.

`create_agent` makes every middleware hook a graph node, so one model call takes about nine
graph steps; a fixed budget of "4 per turn" ended a task after 24 tool calls (the default
`maxTurns` is 50). The budget is now sized from the graph (agents/limits.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphRecursionError

from cmcoder.agents.limits import recursion_limit_for
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def glob(i: int) -> dict[str, Any]:
    return {"tool_calls": [{"name": "Glob", "arguments": {"pattern": f"nothing{i}*.zz"}}]}


def test_the_budget_follows_the_graph_not_a_fixed_number() -> None:
    graph = SimpleNamespace(nodes={"__start__": 0, "model": 0, "tools": 0, "A.before_model": 0})
    assert recursion_limit_for(graph, 10) == 10 * 3 + 25  # the node named __start__ isn't a step
    assert recursion_limit_for(SimpleNamespace(nodes={}), 10) == 10 * 2 + 25


async def test_a_task_runs_to_max_turns_without_a_recursion_error(
    mock_server, project: Path
) -> None:
    # Far more calls than the old budget (30 * 4 + 20 = 140 steps, about 15 calls) allowed.
    server = mock_server([glob(i) for i in range(40)] + [{"content": "done"}])
    session = make_session(server.base_url, project, settings={"maxTurns": 30})
    try:
        events = await run_turn(session, "look around a lot")
    finally:
        await session.agent.close()
    assert not of_type(events, ev.Error), of_type(events, ev.Error)
    (result,) = of_type(events, ev.Result)
    assert result.subtype == "success" and not result.is_error
    assert (
        len(server.requests) == 30
    )  # stopped by maxTurns (ModelCallLimitMiddleware), not by a crash
    # The real graph is bigger than 2 or 4 steps per turn, which is why the old budget failed.
    assert session.agent.recursion_limit > session.agent.max_turns * 4 + 20


async def test_the_step_limit_is_a_stop_not_a_crash_and_the_conversation_goes_on(
    mock_server, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock_server([{"content": "carrying on"}])
    session = make_session(server.base_url, project)
    original = session._stream

    async def endless(*a: Any, **k: Any) -> Any:
        raise GraphRecursionError("Recursion limit of 220 reached without hitting a stop condition")
        yield  # pragma: no cover - makes this an async generator

    try:
        monkeypatch.setattr(session, "_stream", endless)
        events = await run_turn(session, "never ends")
        assert not of_type(events, ev.Error)  # no raw "GraphRecursionError: ..." in the chat
        (warning,) = of_type(events, ev.Warning)
        assert "step budget" in warning.message and "continue" in warning.message
        (result,) = of_type(events, ev.Result)
        assert result.subtype == "max_turns" and result.is_error
        # "continue" works: a new budget, the same conversation.
        monkeypatch.setattr(session, "_stream", original)
        events = await run_turn(session, "continue")
        assert of_type(events, ev.Result)[0].subtype == "success"
        assert of_type(events, ev.Result)[0].result == "carrying on"
    finally:
        await session.agent.close()


async def test_a_subagent_gets_its_own_budget(mock_server, project: Path) -> None:
    task = {
        "tool_calls": [
            {
                "id": "task1",
                "name": "Task",
                "arguments": {
                    "description": "look",
                    "prompt": "Look around a lot.",
                    "subagent_type": "general-purpose",
                },
            }
        ]
    }
    server = mock_server(
        [task] + [glob(i) for i in range(40)] + [{"content": "report"}, {"content": "ok"}]
    )
    session = make_session(server.base_url, project, settings={"subagentMaxTurns": 30})
    try:
        events = await run_turn(session, "delegate")
    finally:
        await session.agent.close()
    assert not of_type(events, ev.Error), of_type(events, ev.Error)
    inner = [e for e in of_type(events, ev.ToolUse) if e.parent_tool_use_id]
    assert len(inner) == 30  # the subagent's own maxTurns ended it
    assert of_type(events, ev.Result)[0].subtype == "success"
