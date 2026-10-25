"""The team StateGraph: PM → developer → tester (↺ developer) → DevOps."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.agents.team import TEAM_GRAPH
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def call(tool: str, **args: object) -> dict:
    return {"tool_calls": [{"name": tool, "arguments": args}]}


TEAM_ON = {"team": {"enabled": True}, "permissions": {"allow": ["Read"]}}


def tool_names(request: dict) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools", [])}


async def test_the_graph_has_the_team_shape() -> None:
    graph = TEAM_GRAPH.get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert {("pm", "developer"), ("developer", "tester"), ("tester", "developer")} <= edges
    assert {("tester", "devops"), ("devops", "__end__"), ("__start__", "pm")} <= edges


async def test_a_failed_test_goes_back_to_the_developer(mock_server, project: Path) -> None:
    server = mock_server(
        [
            call("Team", feature="greet(name) returns 'Hello, <name>!'"),
            # pm
            call("Write", file_path="docs/stories/greet.md", content="# Greet\n"),
            call(
                "Stories",
                file="docs/stories/greet.md",
                summary="One story: greet a user by name.",
                criteria=["greet('Ann') == 'Hello, Ann!'"],
                needs_devops=True,
            ),
            # developer, round 1 (a bug)
            call("Write", file_path="greet.py", content="def greet(n):\n    return 'Hi ' + n\n"),
            {"content": "Changed greet.py: added greet()."},
            # tester, round 1
            call(
                "TestReport",
                verdict="fail",
                summary="greet is wrong.",
                failures=[
                    {
                        "test": "test_greet",
                        "error": "'Hi Ann' != 'Hello, Ann!'",
                        "where": "greet.py:2",
                    }
                ],
            ),
            # developer, round 2
            call("Read", file_path="greet.py"),
            call(
                "Write",
                file_path="greet.py",
                content="def greet(n):\n    return f'Hello, {n}!'\n",
            ),
            {"content": "Fixed greet.py: the greeting text."},
            # tester, round 2
            call("TestReport", verdict="pass", summary="greet passes."),
            # devops
            {"content": "CI already runs every test: nothing to change."},
            # the lead
            {"content": "The team built greet()."},
        ]
    )
    asked: list[str] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req.name)
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask, settings=TEAM_ON)
    try:
        events = await run_turn(session, "build greet with the team")
    finally:
        await session.agent.close()

    assert events[-1].result == "The team built greet()."
    assert (project / "greet.py").read_text() == "def greet(n):\n    return f'Hello, {n}!'\n"
    # Every Write in every role asked, through interrupts two graphs deep, and
    # resuming never ran a finished role again (the script would be off).
    assert asked == ["Write", "Write", "Write"]
    assert len(server.requests) == 12
    # Each role is its own Task card, in the graph's order.
    roles = [
        u.input["subagent_type"]
        for u in of_type(events, ev.ToolUse)
        if u.name == "Task" and u.parent_tool_use_id is None
    ]
    assert roles == ["pm", "developer", "tester", "developer", "tester", "devops"]
    # The tester's failure reached the developer's second round.
    assert "'Hi Ann' != 'Hello, Ann!'" in str(server.requests[6]["messages"])
    # The PM gets its role's tools plus the Stories schema; the lead's result.
    assert tool_names(server.requests[1]) == {"Read", "Glob", "Grep", "Write", "Stories"}
    team_result = next(r for r in of_type(events, ev.ToolResult) if r.name == "Team")
    assert team_result.content.startswith("Team result: PASS after 2 developer round(s).")
    assert "CI already runs every test" in team_result.content


async def test_out_of_rounds_stops_with_the_failure(mock_server, project: Path) -> None:
    server = mock_server(
        [
            call("Team", feature="f", stories_file="docs/stories/f.md"),  # skips the PM
            {"content": "Did it."},  # developer
            call("TestReport", verdict="fail", summary="Broken.", failures=[]),  # tester
            {"content": "The team couldn't finish."},  # the lead (no devops, no retry)
        ]
    )
    session = make_session(
        server.base_url,
        project,
        settings={**TEAM_ON, "team": {"enabled": True, "maxRounds": 1}},
    )
    try:
        events = await run_turn(session, "build f with the team")
    finally:
        await session.agent.close()
    team_result = next(r for r in of_type(events, ev.ToolResult) if r.name == "Team")
    assert "FAIL: the tests still fail after 1 of 1 developer rounds." in team_result.content
    assert "docs/stories/f.md" in str(server.requests[1]["messages"])
    assert len(server.requests) == 4


async def test_the_team_is_off_by_default(mock_server, project: Path) -> None:
    server = mock_server([{"content": "hi"}])
    session = make_session(server.base_url, project)
    try:
        assert "off" in (await run_command(session, "/team build x")).lines[0]
        await run_turn(session, "hi")
    finally:
        await session.agent.close()
    assert "Team" not in tool_names(server.requests[0])
    assert "Task" in tool_names(server.requests[0])
