"""Auto memory: notes kept between sessions, only from what the user says."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.notes import MAX_BODY, MAX_NOTES, Memory, MemoryStore, NoteError, looks_like_secret
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def system_text(server) -> str:
    first = server.requests[0]["messages"][0]
    content = first["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)


def test_save_list_update_forget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.save("Test command", "Run pytest -x -q before committing.")
    store.save("Style", "Use type hints everywhere.")
    store.save("Test command", "Run pytest -x -q --lf.")  # same title: updates
    assert [n.title for n in store.notes()] == ["Style", "Test command"]
    assert "--lf" in store.index_text() and "-q before" not in store.index_text()
    assert (store.dir / "MEMORY.md").read_text().count("\n") == 2
    assert store.forget("1").title == "Style"
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
def test_secrets_are_refused(tmp_path: Path, text: str) -> None:
    assert looks_like_secret(text)
    with pytest.raises(NoteError):
        MemoryStore(tmp_path).save("Creds", text)


def test_limits_and_privacy(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m")
    with pytest.raises(NoteError):
        store.save("Long", "x" * (MAX_BODY + 1))
    store.save("Ok", "fine")
    assert (store.dir / "ok.md").stat().st_mode & 0o077 == 0
    block = Memory(store, MemoryStore(tmp_path / "u", "user")).prompt_block()
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
        titles = [n.title for n in MemoryStore.for_project(project).notes()]
        assert titles == ["Test command"]
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
    MemoryStore.for_project(project).save("Old", "stale fact")
    session = make_session(server.base_url, project)
    try:
        result = await run_command(session, "/memory forget 1")
    finally:
        await session.agent.close()
    assert result.lines == ["Forgot: Old"]
    assert MemoryStore.for_project(project).notes() == []


def test_agent_notes_are_pruned_first_and_duplicates_merge(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m")
    store.save("Mine", "a user fact")
    for i in range(MAX_NOTES - 1):
        store.save(f"auto {i}", f"fact {i}", "agent")
    store.save("Brand new", "one more fact", "agent")  # full: the oldest auto note goes
    titles = [n.title for n in store.notes()]
    assert len(titles) == MAX_NOTES and "Mine" in titles and "auto 0" not in titles
    store.save("Same fact", "a user fact")  # same text as "Mine": one note, not two
    assert len(store.notes()) == MAX_NOTES
    full = MemoryStore(tmp_path / "f")
    for i in range(MAX_NOTES):
        full.save(f"u{i}", f"fact {i}")
    with pytest.raises(NoteError):
        full.save("extra", "more")


def test_agent_notes_are_project_only_and_marked(tmp_path: Path) -> None:
    user = MemoryStore(tmp_path / "u", "user")
    with pytest.raises(NoteError):
        user.save("x", "y", "agent")
    project = MemoryStore(tmp_path / "p")
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
    memory = Memory.for_project(project)
    assert [(n.title, n.source) for n in memory.project.notes()] == [("Lint", "agent")]
    assert [(n.title, n.source) for n in memory.user.notes()] == [("Style", "user")]
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
    assert Memory.for_project(project).project.notes() == []
