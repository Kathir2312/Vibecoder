"""Subagents: the Task tool runs a nested agent graph inside the main one."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.config.settings import config_dir
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def call(tool: str, call_id: str | None = None, **args: object) -> dict:
    c: dict = {"name": tool, "arguments": args}
    if call_id:
        c["id"] = call_id
    return {"tool_calls": [c]}


async def test_explore_subagent_reports_back(mock_server, project: Path) -> None:
    (project / "auth.py").write_text("def refresh_token():\n    pass\n")
    server = mock_server(
        [
            call(
                "Task",
                "task1",
                description="find refresh",
                prompt="Where is refresh_token?",
                subagent_type="explore",
            ),
            call("Grep", pattern="refresh_token"),  # the subagent searches
            {"content": "refresh_token is defined in auth.py:1."},  # its report
            {"content": "It's in auth.py, line 1."},  # the main agent's answer
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "where is refresh_token?")
    finally:
        await session.agent.close()
    assert events[-1].result == "It's in auth.py, line 1."
    task = [r for r in of_type(events, ev.ToolResult) if r.name == "Task"][0]
    assert "auth.py:1" in task.content
    nested = [e for e in of_type(events, ev.ToolUse) if e.parent_tool_use_id == "task1"]
    assert [e.name for e in nested] == ["Grep"]
    states = [s.state for s in of_type(events, ev.SubagentStatus)]
    assert states[0] == "queued" and "running" in states and states[-1] == "done"
    # The subagent's own words never stream as the main agent's reply.
    assert "auth.py:1." not in "".join(d.text for d in of_type(events, ev.AssistantDelta))
    # The subagent has its own conversation and only its allowed tools.
    sub = server.requests[1]
    assert {t["function"]["name"] for t in sub["tools"]} == {"Read", "Glob", "Grep"}
    assert "read-only search subagent" in sub["messages"][0]["content"]
    assert "Where is refresh_token?" in sub["messages"][1]["content"]


async def test_permission_prompt_inside_a_subagent(mock_server, project: Path) -> None:
    server = mock_server(
        [
            call("Task", "task1", description="write it", prompt="Create out.txt with 'x'."),
            call("Write", file_path="out.txt", content="x"),
            {"content": "Created out.txt."},
            {"content": "Done: out.txt exists."},
        ]
    )
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req)
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask)
    try:
        events = await run_turn(session, "make out.txt")
    finally:
        await session.agent.close()
    # The subagent's interrupt paused the main graph; the answer resumed both.
    assert [r.name for r in asked] == ["Write"]
    assert (project / "out.txt").read_text() == "x"
    assert events[-1].result == "Done: out.txt exists."
    numbers = {s.number for s in of_type(events, ev.SubagentStatus)}
    assert numbers == {1}  # the same run before and after the prompt


async def test_denial_inside_a_subagent_ends_the_turn(mock_server, project: Path) -> None:
    server = mock_server(
        [
            call("Task", "task1", description="write it", prompt="Create out.txt with 'x'."),
            call("Write", file_path="out.txt", content="x"),
            {"content": "never sent"},
        ]
    )

    async def ask(req: ev.PermissionRequest) -> dict:
        return {"allow": False}

    session = make_session(server.base_url, project, ask=ask)
    try:
        events = await run_turn(session, "make out.txt")
    finally:
        await session.agent.close()
    assert len(server.requests) == 2  # neither the subagent nor the main agent goes on
    assert events[-1].subtype == "interrupted"
    task = [r for r in of_type(events, ev.ToolResult) if r.name == "Task"][0]
    assert task.is_error and "denied" in task.content
    assert of_type(events, ev.SubagentStatus)[-1].state == "stopped"
    assert not (project / "out.txt").exists()


async def test_a_subagent_keeps_what_it_read_across_a_prompt(mock_server, project: Path) -> None:
    """On resume LangGraph runs the Task tool again; the subagent must keep its
    tool context (here: that it read the file), or the approved Write fails."""
    (project / "notes.txt").write_text("old\n")
    server = mock_server(
        [
            call("Task", "task1", description="update", prompt="Replace notes.txt with new."),
            call("Read", file_path="notes.txt"),
            call("Write", file_path="notes.txt", content="new\n"),
            {"content": "Updated notes.txt."},
            {"content": "Done."},
        ]
    )

    async def ask(req: ev.PermissionRequest) -> dict:
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask)
    try:
        events = await run_turn(session, "update notes")
    finally:
        await session.agent.close()
    write = next(r for r in of_type(events, ev.ToolResult) if r.name == "Write")
    assert not write.is_error, write.content
    assert (project / "notes.txt").read_text() == "new\n"
    assert session.agent.subagents is not None and not session.agent.subagents.contexts


async def test_parallel_subagents(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Task",
                        "id": "a",
                        "arguments": {"description": "one", "prompt": "Say one."},
                    },
                    {
                        "name": "Task",
                        "id": "b",
                        "arguments": {"description": "two", "prompt": "Say two."},
                    },
                ]
            },
            {"content": "report"},
            {"content": "report"},
            {"content": "Both done."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "do two things")
    finally:
        await session.agent.close()
    tasks = [r for r in of_type(events, ev.ToolResult) if r.name == "Task"]
    assert sorted(r.id for r in tasks) == ["a", "b"] and all(r.content == "report" for r in tasks)
    assert events[-1].result == "Both done."


async def test_custom_agent_definition_and_unknown_type(mock_server, project: Path) -> None:
    agents = config_dir() / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\ndescription: Reviews code\ntools: Read\n---\nYou review code carefully."
    )
    server = mock_server(
        [
            call(
                "Task", "t1", description="review", prompt="Review a.py", subagent_type="reviewer"
            ),
            {"content": "Looks fine."},
            call("Task", "t2", description="x", prompt="y", subagent_type="nope"),
            {"content": "ok"},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "review")
    finally:
        await session.agent.close()
    main_tools = {t["function"]["name"]: t["function"] for t in server.requests[0]["tools"]}
    assert "reviewer: Reviews code" in main_tools["Task"]["description"]
    assert {t["function"]["name"] for t in server.requests[1]["tools"]} == {"Read"}
    results = {r.id: r for r in of_type(events, ev.ToolResult)}
    assert results["t1"].content == "Looks fine."
    assert results["t2"].is_error and "Unknown subagent_type" in results["t2"].content
