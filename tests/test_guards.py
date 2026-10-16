"""Middleware guards for weaker models: argument repair, loops, Bash file work."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.middleware.guards import parse_tool_arguments
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('{"a": 1,}', {"a": 1}),
        ("{'a': True}", {"a": True}),
        ('"{\\"a\\": 1}"', {"a": 1}),
        ("", {}),
    ],
)
def test_parse_tool_arguments_repairs(raw: str, expected: dict) -> None:
    assert parse_tool_arguments(raw) == (expected, None)


def test_parse_tool_arguments_rejects_non_objects() -> None:
    assert parse_tool_arguments("[1, 2]")[0] is None
    assert parse_tool_arguments("{nope")[0] is None


async def test_broken_json_arguments_are_repaired(mock_server, project: Path) -> None:
    (project / "notes.txt").write_text("hello\n")
    server = mock_server(
        [
            # Trailing comma: invalid JSON, so ChatOpenAI files it under invalid_tool_calls.
            {"tool_calls": [{"name": "Read", "arguments": '{"file_path": "notes.txt",}'}]},
            {"content": "It says hello."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "read notes")
    finally:
        await session.agent.close()
    result = [r for r in of_type(events, ev.ToolResult) if r.name == "Read"]
    assert result and not result[0].is_error and "hello" in result[0].content


async def test_identical_calls_are_stopped(mock_server, project: Path) -> None:
    call = {"tool_calls": [{"name": "Glob", "arguments": {"pattern": "*.py"}}]}
    server = mock_server([call, call, call, {"content": "giving up"}])
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "find python files")
    finally:
        await session.agent.close()
    results = of_type(events, ev.ToolResult)
    assert [r.is_error for r in results] == [False, False, True]
    assert "identical arguments" in results[-1].content


async def test_bash_file_writes_are_steered_to_write(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Bash", "arguments": {"command": "cat > a.txt << EOF\nhi\nEOF"}}
                ]
            },
            {"content": "ok"},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "make a.txt")
    finally:
        await session.agent.close()
    bash = of_type(events, ev.ToolResult)[0]
    assert bash.is_error and "Write" in bash.content
    assert not (project / "a.txt").exists()
