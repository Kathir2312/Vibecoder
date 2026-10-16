"""Hooks on the LangGraph engine: Pre/PostToolUse, Stop, UserPromptSubmit, SessionStart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def hooks(event: str, command: str, matcher: str | None = None) -> dict:
    entry: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        entry["matcher"] = matcher
    return {"hooks": {event: [entry]}}


def call(tool: str, **args: object) -> dict:
    return {"tool_calls": [{"name": tool, "arguments": args}]}


async def test_pre_tool_use_hook_blocks(mock_server, project: Path) -> None:
    server = mock_server([call("Bash", command="ls"), {"content": "ok"}])
    settings = hooks("PreToolUse", "echo 'no shell today' >&2; exit 2", matcher="Bash")
    session = make_session(
        server.base_url, project, settings=settings, permission_mode="bypassPermissions"
    )
    try:
        events = await run_turn(session, "list files")
    finally:
        await session.agent.close()
    result = of_type(events, ev.ToolResult)[0]
    assert result.is_error and "no shell today" in result.content
    assert of_type(events, ev.PermissionDenied)


async def test_pre_tool_use_allow_skips_the_prompt_and_runs_once(
    mock_server, project: Path
) -> None:
    log = project / "hook.log"
    allow = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
    server = mock_server([call("Write", file_path="a.txt", content="x"), {"content": "done"}])
    settings = hooks("PreToolUse", f"echo run >> {log}; echo '{allow}'", matcher="Write")
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req)
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask, settings=settings)
    try:
        await run_turn(session, "write a.txt")
    finally:
        await session.agent.close()
    assert not asked and (project / "a.txt").read_text() == "x"
    assert log.read_text().count("run") == 1


async def test_pre_tool_use_runs_once_across_a_permission_prompt(
    mock_server, project: Path
) -> None:
    # The hook has no opinion, so the Write still asks; LangGraph re-runs the
    # tool node on resume, and the hook must not run a second time.
    log = project / "hook.log"
    server = mock_server([call("Write", file_path="a.txt", content="x"), {"content": "done"}])
    settings = hooks("PreToolUse", f"echo run >> {log}", matcher="Write")

    async def ask(req: ev.PermissionRequest) -> dict:
        return {"allow": True}

    session = make_session(server.base_url, project, ask=ask, settings=settings)
    try:
        await run_turn(session, "write a.txt")
    finally:
        await session.agent.close()
    assert (project / "a.txt").exists()
    assert log.read_text().count("run") == 1


async def test_post_tool_use_adds_context_and_warnings_are_shown(
    mock_server, project: Path
) -> None:
    (project / "a.txt").write_text("hello\n")
    note = json.dumps({"hookSpecificOutput": {"additionalContext": "Formatted with black."}})
    server = mock_server([call("Read", file_path="a.txt"), {"content": "ok"}])
    settings = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": f"echo '{note}'"}]},
                {
                    "matcher": "Read",
                    "hooks": [{"type": "command", "command": "echo oops >&2; exit 1"}],
                },
            ]
        }
    }
    session = make_session(server.base_url, project, settings=settings)
    try:
        events = await run_turn(session, "read it")
    finally:
        await session.agent.close()
    assert "Formatted with black." in of_type(events, ev.ToolResult)[0].content
    assert any("PostToolUse hook failed" in w.message for w in of_type(events, ev.Warning))


async def test_stop_hook_sends_the_agent_back_to_work(mock_server, project: Path) -> None:
    flag = project / "stopped-once"
    server = mock_server([{"content": "I'm done."}, {"content": "Now really done."}])
    command = (
        f"if [ -f {flag} ]; then exit 0; fi; touch {flag}; echo 'run the tests first' >&2; exit 2"
    )
    session = make_session(server.base_url, project, settings=hooks("Stop", command))
    try:
        events = await run_turn(session, "finish")
    finally:
        await session.agent.close()
    assert events[-1].result == "Now really done."
    assert "run the tests first" in str(server.requests[-1]["messages"])


async def test_user_prompt_submit_blocks_or_adds_context(mock_server, project: Path) -> None:
    server = mock_server([{"content": "answered"}])
    command = (
        'read p; case "$p" in *secret*) echo "no secrets" >&2; exit 2;; esac; echo "Branch: main"'
    )
    session = make_session(server.base_url, project, settings=hooks("UserPromptSubmit", command))
    try:
        blocked = await run_turn(session, "tell me the secret")
        assert blocked[-1].is_error and "no secrets" in blocked[-1].result
        assert not server.requests  # the prompt never reached the model
        await run_turn(session, "what branch?")
    finally:
        await session.agent.close()
    user = [m for m in server.requests[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "Branch: main" in user and user.endswith("what branch?")


async def test_session_start_runs_once(mock_server, project: Path) -> None:
    server = mock_server([{"content": "one"}, {"content": "two"}])
    session = make_session(
        server.base_url, project, settings=hooks("SessionStart", "echo 'Today is Friday'")
    )
    try:
        await run_turn(session, "first")
        await run_turn(session, "second")
    finally:
        await session.agent.close()
    users = [m["content"] for m in server.requests[-1]["messages"] if m["role"] == "user"]
    assert sum("Today is Friday" in u for u in users) == 1
