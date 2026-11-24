"""The team StateGraph: PM → developer → tester (↺ developer) → DevOps."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.agents.team import TEAM_GRAPH, route_after_test
from cmcoder.agents.team import TestReport as TeamTestReport
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


def team_call(call_id: str, **args: object) -> dict:
    return {"tool_calls": [{"id": call_id, "name": "Team", "arguments": args}]}


def team_results(events: list) -> list[str]:
    return [r.content for r in of_type(events, ev.ToolResult) if r.name == "Team"]


def test_only_a_failing_test_sends_the_developer_back() -> None:
    state = {"round": 1, "max_rounds": 3}
    fail = TeamTestReport(verdict="fail", summary="x")
    untested = TeamTestReport(verdict="untested", summary="no tests here")
    assert route_after_test({**state, "test": fail}) == "developer"  # type: ignore[typeddict-item]
    assert route_after_test({**state, "test": untested}) == "__end__"  # type: ignore[typeddict-item]
    assert route_after_test({**state, "round": 3, "test": fail}) == "__end__"  # type: ignore[typeddict-item]


async def test_a_tester_that_gives_no_verdict_ends_the_loop_untested(
    mock_server, project: Path
) -> None:
    """The screenshot's loop: a tester out of steps was counted as a failed test, so the
    developer was sent back to fix nothing, round after round."""
    server = mock_server(
        [
            team_call("t1", feature="f", stories_file="docs/stories/f.md"),
            {"content": "Did it."},  # developer
            {"content": "I looked around and ran out of steps."},  # tester: no TestReport
            {"content": "The change is untested."},  # the lead
        ]
    )
    session = make_session(server.base_url, project, settings=TEAM_ON)
    try:
        events = await run_turn(session, "build f with the team")
    finally:
        await session.agent.close()
    (result,) = team_results(events)
    assert result.startswith("Team result: UNTESTED")
    assert "ended without a verdict" in result and "do not start the team again" in result
    assert len(server.requests) == 4  # no second developer round, no devops


async def test_the_tester_can_say_it_has_nothing_to_run(mock_server, project: Path) -> None:
    server = mock_server(
        [
            team_call("t1", feature="f", stories_file="docs/stories/f.md"),
            {"content": "Did it."},
            call("TestReport", verdict="untested", summary="There is no test project."),
            {"content": "No tests exist."},
        ]
    )
    session = make_session(server.base_url, project, settings=TEAM_ON)
    try:
        events = await run_turn(session, "build f with the team")
    finally:
        await session.agent.close()
    (result,) = team_results(events)
    assert result.startswith("Team result: UNTESTED")
    assert "There is no test project." in result
    assert len(server.requests) == 4


async def test_the_team_runs_once_per_request_and_again_in_the_next(
    mock_server, project: Path
) -> None:
    """The screenshot's three runs in one request (15 subagents, 177 minutes)."""
    server = mock_server(
        [
            team_call("t1", feature="f", stories_file="docs/stories/f.md"),
            {"content": "Did it."},  # developer
            call("TestReport", verdict="fail", summary="Broken.", failures=[]),  # tester
            team_call(
                "t2", feature="f, once more", stories_file="docs/stories/f.md"
            ),  # the lead tries again
            {"content": "It still fails; here is what is left."},  # the lead, after the refusal
            # the next request
            team_call("t3", feature="f, as discussed", stories_file="docs/stories/f.md"),
            {"content": "Fixed it."},  # developer
            call("TestReport", verdict="pass", summary="All green."),  # tester
            {"content": "Done."},  # the lead
        ]
    )
    session = make_session(
        server.base_url, project, settings={**TEAM_ON, "team": {"enabled": True, "maxRounds": 1}}
    )
    try:
        first = await run_turn(session, "build f with the team")
        refused_at = len(server.requests)
        second = await run_turn(session, "try it again")
    finally:
        await session.agent.close()
    first_results = team_results(first)
    assert first_results[0].startswith("Team result: FAIL")
    assert "already run in this request" in first_results[1]
    assert refused_at == 5  # lead, developer, tester, lead (refused), lead: no second run
    assert team_results(second)[0].startswith("Team result: PASS")  # a new request may run it


async def test_the_team_command_asks_for_one_run_without_planning_first(
    mock_server, project: Path
) -> None:
    server = mock_server([])
    session = make_session(server.base_url, project, settings=TEAM_ON)
    try:
        sent = (await run_command(session, "/team add an export button")).send
    finally:
        await session.agent.close()
    assert sent is not None and sent.endswith("add an export button")
    assert "without planning" in sent and "do not run the team again" in sent
