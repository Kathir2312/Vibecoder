"""Auto memory: notes kept between sessions, only from what the user says."""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore
from langgraph.store.sqlite import SqliteStore

from cmcoder.notes import (
    MAX_BODY,
    MAX_NOTES,
    Memory,
    MemoryStore,
    NoteError,
    looks_like_secret,
    open_store,
)
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def mem_store(scope: str = "project") -> MemoryStore:
    return MemoryStore(InMemoryStore(), ("t", scope), scope)


def notes_of(project: Path, scope: str = "project") -> list[tuple[str, str]]:
    """What the durable store holds, read after the session has closed."""
    memory = Memory.for_project(project)
    try:
        store = memory.user if scope == "user" else memory.project
        return [(n.title, n.source) for n in store.notes()]
    finally:
        memory.close()


def system_text(server) -> str:
    first = server.requests[0]["messages"][0]
    content = first["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)


def test_save_list_update_forget() -> None:
    store = mem_store()
    store.save("Test command", "Run pytest -x -q before committing.")
    store.save("Style", "Use type hints everywhere.")
    store.save("Test command", "Run pytest -x -q --lf.")  # same title: updates
    assert [n.title for n in store.notes()] == ["Style", "Test command"]
    assert "--lf" in store.index_text() and "-q before" not in store.index_text()
    assert store.forget("Style").title == "Style"
    assert store.forget("nothing") is None
    assert [n.title for n in store.notes()] == ["Test command"]


@pytest.mark.parametrize(
    "text",
    [
        "my key is sk-abcdefghijklmnopqrstuvwx",
        "password = hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----",
        "token: ghp_abcdefghijklmnopqrstuv",
    ],
)
def test_secrets_are_refused(text: str) -> None:
    assert looks_like_secret(text)
    with pytest.raises(NoteError):
        mem_store().save("Creds", text)


def test_limits_and_prompt_block() -> None:
    store = mem_store()
    with pytest.raises(NoteError):
        store.save("Long", "x" * (MAX_BODY + 1))
    store.save("Ok", "fine")
    memory = Memory(InMemoryStore(), "p")
    memory.project.save("Ok", "fine")
    block = memory.prompt_block()
    assert "Ok" in block and "Remember" in block and "Recall" in block
    assert "About the user" not in block


async def test_remember_needs_a_real_quote_then_shows_next_session(
    mock_server, project: Path
) -> None:
    server = mock_server(
        [
            {  # a made-up quote is refused
                "tool_calls": [
                    {
                        "name": "Remember",
                        "arguments": {"title": "Fake", "note": "x", "quote": "the user loves tabs"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "Remember",
                        "arguments": {
                            "title": "Test command",
                            "note": "Run pytest -x -q.",
                            "quote": "always run pytest -x -q",
                        },
                    }
                ]
            },
            {"content": "Noted."},
            {"content": "Hello again."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        await run_turn(session, "Please always run pytest -x -q here, remember that.")
        assert [n.title for n in session.agent.memory.project.notes()] == ["Test command"]
        listing = (await run_command(session, "/memory")).lines
        assert any("Test command" in line for line in listing)
    finally:
        await session.agent.close()
    second = make_session(server.base_url, project)
    try:
        await run_turn(second, "hi")
    finally:
        await second.agent.close()
    assert "Run pytest -x -q." in str(server.requests[-1]["messages"][0])


async def test_memory_off_has_no_tools_and_command_says_so(mock_server, project: Path) -> None:
    server = mock_server([{"content": "ok"}])
    session = make_session(server.base_url, project, settings={"memory": {"enabled": False}})
    try:
        await run_turn(session, "hi")
        assert "Memory is off" in (await run_command(session, "/memory")).lines[0]
    finally:
        await session.agent.close()
    names = {t["function"]["name"] for t in server.requests[0].get("tools", [])}
    assert "Remember" not in names


async def test_forget_command(mock_server, project: Path) -> None:
    server = mock_server([{"content": "ok"}])
    seed = Memory.for_project(project)
    seed.project.save("Old", "stale fact")
    seed.close()
    session = make_session(server.base_url, project)
    try:
        result = await run_command(session, "/memory forget 1")
    finally:
        await session.agent.close()
    assert result.lines == ["Forgot: Old"]
    assert notes_of(project) == []


def test_agent_notes_are_pruned_first_and_duplicates_merge() -> None:
    store = mem_store()
    store.save("Mine", "a user fact")
    for i in range(MAX_NOTES - 1):
        store.save(f"auto {i}", f"fact {i}", "agent")
    store.save("Brand new", "one more fact", "agent")  # full: the oldest auto note goes
    titles = [n.title for n in store.notes()]
    assert len(titles) == MAX_NOTES and "Mine" in titles and "auto 0" not in titles
    store.save("Same fact", "a user fact")  # same text as "Mine": one note, not two
    assert len(store.notes()) == MAX_NOTES
    full = mem_store()
    for i in range(MAX_NOTES):
        full.save(f"u{i}", f"fact {i}")
    with pytest.raises(NoteError):
        full.save("extra", "more")


def test_agent_notes_are_project_only_and_marked() -> None:
    user = mem_store("user")
    with pytest.raises(NoteError):
        user.save("x", "y", "agent")
    project = mem_store()
    project.save("Lint", "ruff check src", "agent")
    assert "(learned by cmcoder)" in project.index_text()
    assert project.notes()[0].source == "agent"


async def test_learned_and_user_notes_through_tools(mock_server, project: Path) -> None:
    call = lambda **a: {"tool_calls": [{"name": "Remember", "arguments": a}]}  # noqa: E731
    server = mock_server(
        [
            call(title="Lint", note="Lint with ruff check src."),  # learned itself
            call(title="Bad", note="x", scope="user"),  # user scope needs a quote
            call(title="Style", note="Type hints.", scope="user", quote="i like type hints"),
            {"tool_calls": [{"name": "Recall", "arguments": {"note": "Lint"}}]},
            {"content": "Done."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        await run_turn(session, "I like type hints in all my projects.")
    finally:
        await session.agent.close()
    assert notes_of(project) == [("Lint", "agent")]
    assert notes_of(project, "user") == [("Style", "user")]
    assert "Lint with ruff" in str(server.requests[-1]["messages"])  # Recall's answer


async def test_auto_off_only_saves_what_the_user_says(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {"tool_calls": [{"name": "Remember", "arguments": {"title": "L", "note": "n"}}]},
            {"content": "ok"},
        ]
    )
    session = make_session(server.base_url, project, settings={"memory": {"auto": False}})
    try:
        await run_turn(session, "hi")
    finally:
        await session.agent.close()
    assert notes_of(project) == []


def test_the_durable_store_is_a_private_sqlite_file_shared_by_projects(
    tmp_path: Path, project: Path
) -> None:
    first = Memory.for_project(project)
    assert isinstance(first.backend, SqliteStore)
    first.project.save("Fact", "kept on disk")
    first.user.save("Pref", "for everyone", "user")
    first.close()
    db = Path(__import__("os").environ["CMCODER_CONFIG_DIR"]) / "memory.db"
    assert db.exists() and db.stat().st_mode & 0o077 == 0
    other = tmp_path / "other"
    other.mkdir()
    again = Memory.for_project(project)
    elsewhere = Memory.for_project(other)
    try:
        assert [n.title for n in again.project.notes()] == ["Fact"]  # survives a restart
        assert elsewhere.project.notes() == []  # another project's notes are separate
        assert [n.title for n in elsewhere.user.notes()] == ["Pref"]  # the user's are shared
    finally:
        again.close()
        elsewhere.close()


def test_open_store_creates_the_folder(tmp_path: Path) -> None:
    store = open_store(tmp_path / "new" / "m.db")
    store.put(("a",), "k", {"v": 1})
    assert store.get(("a",), "k").value == {"v": 1}  # type: ignore[union-attr]


async def test_init_makes_the_model_learn_the_project_and_save_notes(
    mock_server, project: Path
) -> None:
    (project / "README.md").write_text("# Demo\nA tiny app.\n")
    remember = lambda **a: {"tool_calls": [{"name": "Remember", "arguments": a}]}  # noqa: E731
    server = mock_server(
        [
            {"tool_calls": [{"name": "Read", "arguments": {"file_path": "README.md"}}]},
            remember(title="Purpose", note="Demo is a tiny app."),
            remember(title="Test command", note="Run pytest -q."),
            {"content": "Saved 2 notes."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        result = await run_command(session, "/init the CLI")
        assert result.send and "Focus on: the CLI" in result.send and "Remember" in result.send
        await run_turn(session, result.send)
    finally:
        await session.agent.close()
    assert notes_of(project) == [("Purpose", "agent"), ("Test command", "agent")]


async def test_init_lists_existing_notes_and_respects_the_switches(
    mock_server, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    seed = Memory.for_project(project)
    seed.project.save("Purpose", "old text", "agent")
    seed.close()
    session = make_session(server.base_url, project)
    try:
        result = await run_command(session, "/init")
        assert result.send and "Purpose" in result.send
    finally:
        await session.agent.close()
    for setting, text in (({"enabled": False}, "Memory is off"), ({"auto": False}, "auto")):
        s = make_session(server.base_url, project, settings={"memory": setting})
        try:
            r = await run_command(s, "/init")
            assert r.send is None and text in r.lines[0]
        finally:
            await s.agent.close()
