"""Phase 1 features on the LangGraph engine, end to end against the mock gateway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cmcoder.config.settings import config_dir
from cmcoder.protocol import events as ev
from cmcoder.runtime.store import SessionStore

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


# --- saved sessions (AsyncSqliteSaver) ------------------------------------------------


async def test_session_is_saved_and_resumed(mock_server, project: Path) -> None:
    server = mock_server([{"content": "First."}, {"content": "Second."}])
    small = {"smallFastModel": "mock:qwen3-7b"}  # titles come from the small model
    async with SessionStore.open(project) as store:
        first = make_session(server.base_url, project, store=store, settings=small)
        await run_turn(first, "remember the number 7")
        await first.close()  # waits for the background title
        thread = first.session_id
        await first.agent.close()

    # A new process: a new agent and session, the same database.
    async with SessionStore.open(project) as store:
        sessions = await store.list()
        assert [s.thread_id for s in sessions] == [thread]
        assert sessions[0].title == "Fix the login bug"  # the mock's canned title
        second = make_session(server.base_url, project, store=store)
        await second.resume(thread)
        assert second.turn == 1
        events = await run_turn(second, "what was the number?")
        await second.agent.close()
    assert events[-1].result == "Second."
    contents = [m.get("content") for m in server.requests[-1]["messages"]]
    assert "remember the number 7" in contents and "First." in contents
    assert len(server.state.title_requests) == 1  # one title, not one per turn


async def test_session_database_is_owner_only(project: Path) -> None:
    import os
    import stat

    async with SessionStore.open(project) as store:
        assert store.persistent
    db = next((config_dir() / "projects").glob("*/sessions.db"))
    if os.name == "posix":
        assert stat.S_IMODE(db.stat().st_mode) == 0o600


async def test_persist_sessions_off_keeps_nothing(project: Path) -> None:
    async with SessionStore.open(project, persist=False) as store:
        assert not store.persistent and await store.list() == []
    assert not (config_dir() / "projects").exists()


# --- todos (TodoListMiddleware) ----------------------------------------------------------


async def test_todo_list_is_streamed_and_kept_in_state(mock_server, project: Path) -> None:
    todos = [
        {"content": "Write tests", "status": "in_progress"},
        {"content": "Run them", "status": "pending"},
    ]
    server = mock_server(
        [
            {"tool_calls": [{"name": "write_todos", "arguments": {"todos": todos}}]},
            {"content": "Planned."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "plan it")
        assert of_type(events, ev.TodoUpdate)[-1].todos == todos
        assert await session.todos() == todos
    finally:
        await session.agent.close()


# --- context management ------------------------------------------------------------------------


async def test_auto_compaction_summarises_with_the_small_model(mock_server, project: Path) -> None:
    lines = [f"line {i}: some text to fill the context window up" for i in range(400)]
    (project / "big.txt").write_text("\n".join(lines))  # ~20K chars
    server = mock_server(
        [
            {"tool_calls": [{"name": "Read", "arguments": {"file_path": "big.txt"}}]},
            {"content": "It is a big file."},
            {"content": "Still here."},
        ]
    )
    small_window = {
        "modelProfiles": [{"match": "*", "contextWindow": 6000}],
        "autoCompactThreshold": 0.3,
    }
    session = make_session(server.base_url, project, settings=small_window)
    try:
        first = await run_turn(session, "read big.txt")
        assert not of_type(first, ev.Compacted)
        events = await run_turn(session, "and now?")  # the history is too big now
        compacted = of_type(events, ev.Compacted)
        assert compacted and compacted[0].trigger == "auto"
        assert compacted[0].summarized_messages > 0
        assert compacted[0].tokens_after < compacted[0].tokens_before
        assert server.state.summary_requests  # the summary prompt went to the gateway
        messages = await session.messages()
        assert messages[0].content.startswith("Here is a summary")
        # The latest user request survives word for word.
        assert any(m.content == "and now?" for m in messages)
        assert events[-1].result == "Still here."
    finally:
        await session.agent.close()


async def test_manual_compact(mock_server, project: Path) -> None:
    server = mock_server([{"content": "one"}, {"content": "two"}, {"content": "three"}])
    session = make_session(server.base_url, project)
    try:
        for p in ("a", "b", "c"):
            await run_turn(session, p)
        before = len(await session.messages())
        result = await session.compact("the file names")
        assert result is not None and result.trigger == "manual"
        assert len(await session.messages()) < before
        assert "the file names" in str(server.state.summary_requests[-1])
    finally:
        await session.agent.close()


async def test_context_window_is_learned_from_the_server(mock_server, project: Path) -> None:
    # The profile assumes 32K; the server really has 6K and says so in its error.
    server = mock_server(
        [{"content": "fits now"}], enforce_context=True, context_windows={"qwen3-27b": 6000}
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "hello")
    finally:
        await session.agent.close()
    assert events[-1].result == "fits now"
    assert session.agent.bundle.profile.context_window == 6000
    saved = json.loads((config_dir() / "context-windows.json").read_text())
    assert [e["tokens"] for e in saved.values()] == [6000]


# --- tool calls as text (Qwen without a tool parser) -------------------------------------------

TEXT_CALL = '<tool_call>\n{"name": "Read", "arguments": {"file_path": "a.txt"}}\n</tool_call>'


async def test_text_tool_calls_are_parsed(mock_server, project: Path) -> None:
    (project / "a.txt").write_text("alpha\n")
    server = mock_server([{"content": TEXT_CALL}, {"content": "It says alpha."}])
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "read a.txt")
    finally:
        await session.agent.close()
    read = [r for r in of_type(events, ev.ToolResult) if r.name == "Read"]
    assert read and "alpha" in read[0].content
    assert "<tool_call>" not in "".join(d.text for d in of_type(events, ev.AssistantDelta))


async def test_prompted_tool_mode_sends_no_tools(mock_server, project: Path) -> None:
    (project / "a.txt").write_text("alpha\n")
    server = mock_server([{"content": TEXT_CALL}, {"content": "It says alpha."}])
    prompted = {"modelProfiles": [{"match": "*", "toolCalling": "prompted"}]}
    session = make_session(server.base_url, project, settings=prompted)
    try:
        events = await run_turn(session, "read a.txt")
    finally:
        await session.agent.close()
    assert events[-1].result == "It says alpha."
    first, second = server.requests[0], server.requests[1]
    assert "tools" not in first and "<tools>" in first["messages"][0]["content"]
    # The tool result went back as text, the way Qwen's template writes it.
    assert any("<tool_response>" in str(m.get("content")) for m in second["messages"])


# --- /rewind: files (snapshots) + conversation (LangGraph time travel) ---------------------------


async def test_rewind_restores_files_and_conversation(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "a.txt", "content": "one\n"}}
                ]
            },
            {"content": "Wrote one."},
            {
                "tool_calls": [
                    {
                        "name": "Edit",
                        "arguments": {
                            "file_path": "a.txt",
                            "old_string": "one",
                            "new_string": "two",
                        },
                    }
                ]
            },
            {"content": "Changed to two."},
            {"content": "Fresh answer after the rewind."},
        ]
    )
    session = make_session(server.base_url, project, permission_mode="acceptEdits")
    try:
        await run_turn(session, "write one")
        await run_turn(session, "change it to two")
        assert (project / "a.txt").read_text() == "two\n"
        points = await session.rewind_points()
        assert [(p.turn, p.files_changed) for p in points] == [(1, 1), (2, 1)]

        result = await session.rewind(2)
        assert (project / "a.txt").read_text() == "one\n"  # the code is back
        assert result.prompt == "change it to two"  # to edit and send again
        texts = [m.content for m in await session.messages()]
        assert "write one" in texts and "change it to two" not in texts

        events = await run_turn(session, "something else")
        assert events[-1].result == "Fresh answer after the rewind."
        # The request after the rewind doesn't carry the rewound turn.
        assert "change it to two" not in str(server.requests[-1]["messages"])
    finally:
        await session.agent.close()


async def test_rewind_first_turn_deletes_created_file(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "new.txt", "content": "x"}}
                ]
            },
            {"content": "Created."},
        ]
    )
    session = make_session(server.base_url, project, permission_mode="acceptEdits")
    try:
        await run_turn(session, "create new.txt")
        await session.rewind(1)
        assert not (project / "new.txt").exists()
        assert await session.messages() == []
    finally:
        await session.agent.close()


async def test_clear_starts_a_new_thread(mock_server, project: Path) -> None:
    server = mock_server([{"content": "a"}, {"content": "b"}])
    session = make_session(server.base_url, project)
    try:
        await run_turn(session, "zebra")
        old = session.session_id
        await session.clear()
        assert session.session_id != old and await session.messages() == []
        await run_turn(session, "second")
        assert "zebra" not in str(server.requests[-1]["messages"])
    finally:
        await session.agent.close()


def test_learned_windows_age_and_the_first_format(tmp_path: Path) -> None:
    from cmcoder.models import windows

    path = config_dir() / "context-windows.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"https://gw/v1|old": 8192}))  # the first format: a number
    assert windows.learned_window("https://gw/v1", "old") == (8192, windows.SOURCE)
    windows.save_window("https://gw/v1", "unknown", None, windows.PROBE_SOURCE)
    assert windows.learned_window("https://gw/v1", "unknown") is None  # no answer: no window
    assert windows.learned_entry("https://gw/v1", "unknown") is not None  # but don't re-probe
    data = json.loads(path.read_text())
    data["https://gw/v1|unknown"]["at"] -= 2 * 24 * 3600  # two days later: probe again
    path.write_text(json.dumps(data))
    assert windows.learned_entry("https://gw/v1", "unknown") is None
