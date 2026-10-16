"""End to end: the LangGraph agent fixes a bug against the scripted mock server.

The mock server replays model replies in order (see cmcoder/testing/mock_server.py),
so these tests exercise the real HTTP path: ChatOpenAI -> httpx -> server,
streaming, tool calls, middleware, interrupts and checkpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def bug_fix_script() -> list[dict]:
    return [
        {"tool_calls": [{"name": "Read", "arguments": {"file_path": "app.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "Edit",
                    "arguments": {
                        "file_path": "app.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                }
            ]
        },
        {"content": "Fixed `add` in app.py.", "reasoning": "The function subtracts."},
    ]


async def test_fixes_a_bug_end_to_end(mock_server, project: Path) -> None:
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(bug_fix_script())
    session = make_session(server.base_url, project, permission_mode="acceptEdits")
    try:
        events = await run_turn(session, "add() is wrong, fix it")
    finally:
        await session.agent.close()

    assert "return a + b" in (project / "app.py").read_text()
    result = events[-1]
    assert isinstance(result, ev.Result) and not result.is_error
    assert result.result == "Fixed `add` in app.py."
    assert [t.name for t in of_type(events, ev.ToolUse)] == ["Read", "Edit"]
    assert not any(t.is_error for t in of_type(events, ev.ToolResult))
    # Reasoning arrives in reasoning_content and is kept apart from the answer.
    assert "subtracts" in "".join(d.text for d in of_type(events, ev.ReasoningDelta))
    # Reasoning is never sent back to the model on later requests.
    assert all("subtracts" not in str(r) for r in server.requests)
    # The system prompt went first, with our tools' JSON schemas.
    first = server.requests[0]
    assert first["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["tools"]} >= {"Read", "Edit", "Bash"}
    assert first["max_tokens"] <= 8192


async def test_permission_prompt_pauses_and_resumes(mock_server, project: Path) -> None:
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(bug_fix_script())
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req)
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask)  # default mode: edits ask
    try:
        events = await run_turn(session, "fix add")
    finally:
        await session.agent.close()
    assert [r.name for r in asked] == ["Edit"]
    assert asked[0].change is not None and "a + b" in asked[0].change.after
    # The prompt is shown after the tool_use event it belongs to.
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("ToolUse") < kinds.index("PermissionRequest")
    assert "return a + b" in (project / "app.py").read_text()


async def test_denied_edit_is_reported_to_the_model(mock_server, project: Path) -> None:
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(bug_fix_script())

    async def ask(req: ev.PermissionRequest) -> dict:
        return {"allow": False, "feedback": "use a different name"}

    session = make_session(server.base_url, project, ask=ask)
    try:
        events = await run_turn(session, "fix add")
    finally:
        await session.agent.close()
    assert "return a - b" in (project / "app.py").read_text()  # unchanged
    denied = [r for r in of_type(events, ev.ToolResult) if r.name == "Edit"]
    assert denied and denied[0].is_error and "use a different name" in denied[0].content
    # The model saw the denial as the tool's result on its next request.
    tool_msgs = [m for m in server.requests[-1]["messages"] if m["role"] == "tool"]
    assert "use a different name" in tool_msgs[-1]["content"]


async def test_non_interactive_mode_denies_instead_of_asking(mock_server, project: Path) -> None:
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(bug_fix_script())
    session = make_session(server.base_url, project)  # no ask: like `cmcoder -p`
    try:
        events = await run_turn(session, "fix add")
    finally:
        await session.agent.close()
    edit = [r for r in of_type(events, ev.ToolResult) if r.name == "Edit"][0]
    assert edit.is_error and "--allowedTools" in edit.content
    assert "return a - b" in (project / "app.py").read_text()


async def test_high_risk_command_always_asks_even_in_bypass_mode(
    mock_server, project: Path
) -> None:
    server = mock_server(
        [
            {"tool_calls": [{"name": "Bash", "arguments": {"command": "rm -rf build"}}]},
            {"content": "ok"},
        ]
    )
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req)
        return {"allow": False}

    session = make_session(server.base_url, project, ask=ask, permission_mode="bypassPermissions")
    try:
        await run_turn(session, "clean up")
    finally:
        await session.agent.close()
    assert len(asked) == 1 and not asked[0].can_remember


async def test_conversation_continues_on_the_same_thread(mock_server, project: Path) -> None:
    server = mock_server([{"content": "First answer."}, {"content": "Second answer."}])
    session = make_session(server.base_url, project)
    try:
        await run_turn(session, "one")
        events = await run_turn(session, "two")
    finally:
        await session.agent.close()
    assert events[-1].result == "Second answer."
    # The checkpointer kept turn one; the second request carries the history.
    contents = [m.get("content") for m in server.requests[-1]["messages"]]
    assert "one" in contents and "First answer." in contents and "two" in contents


async def test_think_tags_are_split_from_the_answer(mock_server, project: Path) -> None:
    server = mock_server(
        [{"content": "The answer is 4.", "reasoning": "2+2 is 4"}], think_tags=True
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "2+2?")
    finally:
        await session.agent.close()
    msg = of_type(events, ev.AssistantMessage)[-1]
    assert msg.text == "The answer is 4." and "2+2 is 4" in msg.reasoning
    assert "<think>" not in "".join(d.text for d in of_type(events, ev.AssistantDelta))
