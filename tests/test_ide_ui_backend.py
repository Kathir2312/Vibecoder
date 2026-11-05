"""What the IDE chat needs from the engine for its consent card and Bash run card:
why and where a request is asked, a session-only "always allow", the exit code and time
of a command, its output while it runs, and "Stop this command"."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.protocol import events as ev
from cmcoder.tools import progress

from .conftest import API_KEY
from .helpers import make_session
from .test_stdio import Agent, write

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="these commands use sleep and POSIX quoting"
)


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


NO_SANDBOX = {"sandbox": {"enabled": False}}  # a command that writes asks, as it does for users


def bash(command: str) -> dict[str, Any]:
    return {"tool_calls": [{"name": "Bash", "arguments": {"command": command}}]}


class Asked:
    """An `ask` function that records every request and answers as told."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.requests: list[ev.PermissionRequest] = []
        self.answers = list(answers) or [{"allow": True}]

    async def __call__(self, request: ev.PermissionRequest) -> dict[str, Any]:
        self.requests.append(request)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


async def run(session: Any, prompt: str) -> list[ev.Event]:
    return [e async for e in session.run(prompt)]


async def test_a_command_request_says_where_why_and_for_whom(mock_server, project: Path) -> None:
    server = mock_server([bash("touch hi.txt"), {"content": "done"}])
    asked = Asked({"allow": True})
    session = make_session(server.base_url, project, ask=asked, settings=NO_SANDBOX)
    try:
        await run(session, "say hi")
    finally:
        await session.agent.close()
    (request,) = asked.requests
    assert await asyncio.to_thread(lambda: Path(request.cwd or "").resolve() == project.resolve())
    assert request.reasons == ["not on your allow list"]
    assert request.high_risk is False and request.can_remember is True
    assert request.agent is None


async def test_a_high_risk_command_is_marked_and_never_remembered(
    mock_server, project: Path
) -> None:
    server = mock_server([bash("rm -rf build"), {"content": "done"}])
    asked = Asked({"allow": False})
    session = make_session(server.base_url, project, ask=asked, settings=NO_SANDBOX)
    try:
        await run(session, "clean")
    finally:
        await session.agent.close()
    (request,) = asked.requests
    assert request.high_risk is True and request.can_remember is False
    assert request.reasons and request.reasons[0].lower().startswith("high-risk")


async def test_an_edit_request_names_the_mode(mock_server, project: Path) -> None:
    server = mock_server([write("a.py", "x = 1\n"), {"content": "done"}])
    asked = Asked({"allow": False})
    session = make_session(server.base_url, project, ask=asked, settings=NO_SANDBOX)
    try:
        await run(session, "write it")
    finally:
        await session.agent.close()
    assert asked.requests[0].reasons == ["edits ask first in default mode"]
    assert asked.requests[0].cwd is None


async def test_always_allow_for_the_session_is_not_saved_to_settings(
    mock_server, project: Path
) -> None:
    saved: list[str] = []
    server = mock_server(
        [
            bash("touch same.txt"),
            {"content": "first"},
            bash("touch same.txt"),
            {"content": "second"},
        ]
    )
    asked = Asked({"allow": True, "remember_session": True})
    session = make_session(
        server.base_url, project, ask=asked, on_rule_saved=saved.append, settings=NO_SANDBOX
    )
    try:
        await run(session, "one")
        assert [r.label for r in asked.requests] == ["Bash(touch same.txt)"]
        await run(session, "two")  # the same rule covers it: no second question
    finally:
        await session.agent.close()
    assert len(asked.requests) == 1
    assert saved == []  # nothing was written to the user's settings
    assert any("Bash(touch" in str(r) for r in session.agent.policy.allow)


async def test_a_session_only_answer_to_a_high_risk_request_does_nothing(
    mock_server, project: Path
) -> None:
    server = mock_server(
        [bash("rm -rf build"), {"content": "a"}, bash("rm -rf build"), {"content": "b"}]
    )
    asked = Asked({"allow": True, "remember_session": True})
    session = make_session(server.base_url, project, ask=asked, settings=NO_SANDBOX)
    try:
        await run(session, "one")
        await run(session, "two")
    finally:
        await session.agent.close()
    assert len(asked.requests) == 2  # asked again: high-risk commands are never remembered


async def test_a_bash_result_carries_exit_code_time_and_length(mock_server, project: Path) -> None:
    server = mock_server([bash("touch r.txt; echo one; echo two; (exit 3)"), {"content": "done"}])
    session = make_session(
        server.base_url, project, ask=Asked({"allow": True}), settings=NO_SANDBOX
    )
    try:
        events = await run(session, "run it")
    finally:
        await session.agent.close()
    (result,) = [e for e in events if isinstance(e, ev.ToolResult) and e.name == "Bash"]
    assert result.exit_code == 3 and result.is_error
    assert result.total_lines == 2 and (result.duration_ms or 0) >= 0
    assert result.stopped is False
    sent = str(server.requests[-1]["messages"])
    assert "duration_ms" not in sent and "total_lines" not in sent  # not for the model


async def test_output_arrives_while_the_command_runs(mock_server, project: Path) -> None:
    server = mock_server(
        [bash("touch l.txt; echo a; sleep 1; echo b; sleep 1; echo c"), {"content": "done"}]
    )
    session = make_session(
        server.base_url, project, ask=Asked({"allow": True}), settings=NO_SANDBOX
    )
    seen: list[tuple[str, float]] = []
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        events = []
        async for e in session.run("go"):
            events.append(e)
            if isinstance(e, ev.ToolProgress):
                seen.append((e.text, loop.time() - started))
    finally:
        await session.agent.close()
    progress_events = [e for e in events if isinstance(e, ev.ToolProgress)]
    assert "".join(e.text for e in progress_events) == "a\nb\nc\n"
    use = next(e for e in events if isinstance(e, ev.ToolUse) and e.name == "Bash")
    assert {e.id for e in progress_events} == {use.id}
    assert progress_events[0].elapsed_ms < progress_events[-1].elapsed_ms
    first_result = next(
        i for i, e in enumerate(events) if isinstance(e, ev.ToolResult) and e.name == "Bash"
    )
    assert all(events.index(e) < first_result for e in progress_events)  # before the result
    assert seen[0][1] < seen[-1][1] - 0.5  # the first part came well before the last


async def test_stop_this_command_ends_it_and_the_turn_goes_on(mock_server, project: Path) -> None:
    server = mock_server(
        [bash("touch s.txt; echo started; sleep 30"), {"content": "I see it was stopped."}]
    )
    session = make_session(
        server.base_url, project, ask=Asked({"allow": True}), settings=NO_SANDBOX
    )
    events: list[ev.Event] = []
    stopped_at: float | None = None
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        async for e in session.run("run it"):
            events.append(e)
            if isinstance(e, ev.ToolProgress) and stopped_at is None:
                assert progress.stop(e.id) is True
                stopped_at = loop.time() - started
    finally:
        await session.agent.close()
    assert stopped_at is not None
    (result,) = [e for e in events if isinstance(e, ev.ToolResult) and e.name == "Bash"]
    assert result.stopped is True and result.is_error
    assert "started" in result.content and "stopped this command" in result.content
    assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
    assert events[-1].result == "I see it was stopped."  # the model carried on
    assert loop.time() - started < 20  # not the 30 s


def test_stopping_something_that_is_not_running_is_a_no_op() -> None:
    assert progress.stop("nothing") is False
    progress.register_stop("c1", lambda: None)
    assert progress.stop("c1") is True
    progress.unregister_stop("c1")
    assert progress.stop("c1") is False


async def test_over_stdio_a_session_only_answer_and_stop_tool(mock_server, project: Path) -> None:
    server = mock_server([bash("touch s.txt; echo started; sleep 30"), {"content": "stopped, ok"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("system_init")
        await agent.send(type="user_message", text="run it")
        ask = await agent.until("permission_request")
        assert ask["reasons"] == ["not on your allow list"] and ask["high_risk"] is False
        assert ask["cwd"] and ask["agent"] is None
        await agent.send(
            type="permission_response", request_id=ask["request_id"], allow=True,
            remember_session=True,
        )  # fmt: skip
        live = await agent.until("tool_progress")
        assert live["text"].startswith("started") and live["id"] == ask["tool_use_id"]
        await agent.send(type="stop_tool", tool_use_id=live["id"])
        result = await agent.until("tool_result")
        assert result["stopped"] is True and result["is_error"] is True
        assert (await agent.until("result"))["subtype"] == "success"
        # Stopping what has finished is said, not silently ignored.
        await agent.send(type="stop_tool", tool_use_id="gone")
        assert "no longer running" in (await agent.until("warning"))["message"]
    finally:
        await agent.close()
