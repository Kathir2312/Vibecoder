"""Auto memory: notes kept between sessions, only from what the user says."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.notes import MAX_BODY, MemoryStore, NoteError, looks_like_secret
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
    assert store.prompt_block() and "Ok" in store.prompt_block()
    assert MemoryStore(tmp_path / "empty").prompt_block() is None


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
    assert "Notes from earlier sessions" in system_text(server) or any(
        "Run pytest -x -q." in str(r["messages"][0]) for r in server.requests[-1:]
    )


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
