"""Slash commands, `#` memory notes, --continue and managed settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.cli.main import main_app, sub_app
from cmcoder.config.settings import SettingsError, load_settings
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import add_memory, run_command
from cmcoder.runtime.store import SessionStore

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


async def test_commands(mock_server, project: Path) -> None:
    server = mock_server([{"content": "one"}, {"content": "two"}])
    async with SessionStore.open(project) as store:
        session = make_session(server.base_url, project, store=store)
        try:
            assert any("/rewind" in line for line in (await run_command(session, "/help")).lines)
            await run_turn(session, "first message")
            await session.close()

            assert (await run_command(session, "/mode plan")).lines == ["Mode: plan"]
            assert session.agent.policy.mode == "plan"
            assert "Unknown permission mode" in (await run_command(session, "/mode nope")).lines[0]

            listing = (await run_command(session, "/rewind")).lines
            assert "first message" in listing[0]
            rewound = await run_command(session, "/rewind 1 conversation")
            assert rewound.prompt == "first message"

            sessions = (await run_command(session, "/resume")).lines
            assert "Fix the login bug" in sessions[0]
            assert (await run_command(session, "/todos")).lines == ["No todo list."]
            assert "window" in (await run_command(session, "/context")).lines[0]
            assert (await run_command(session, "/exit")).exit
            assert "Unknown command" in (await run_command(session, "/nope")).lines[0]
        finally:
            await session.agent.close()


def test_memory_note_is_appended(project: Path) -> None:
    (project / "CMCODER.md").write_text("# Rules\n- tabs, not spaces")
    add_memory(project, " use pytest ")
    assert (project / "CMCODER.md").read_text() == "# Rules\n- tabs, not spaces\n- use pytest\n"


def test_continue_in_print_mode(
    mock_server, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock_server([{"content": "Noted 42."}, {"content": "It was 42."}])
    monkeypatch.setenv("CMCODER_BASE_URL", server.base_url)
    monkeypatch.setenv("CMCODER_MODEL", "qwen3-27b")
    monkeypatch.chdir(project)
    runner = CliRunner()
    assert runner.invoke(main_app, ["-p", "remember 42"]).exit_code == 0
    result = runner.invoke(main_app, ["-p", "what number?", "--continue"])
    assert result.exit_code == 0, result.output
    assert "It was 42." in result.stdout
    assert "remember 42" in str(server.requests[-1]["messages"])
    listing = runner.invoke(sub_app, ["sessions"])
    assert "1 turns" in listing.stdout or "2 turns" in listing.stdout


def _managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr("cmcoder.config.settings.managed_settings_path", lambda: path)


def test_managed_settings_disable_bypass(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _managed(
        tmp_path,
        monkeypatch,
        {
            "providers": {"corp": {"baseUrl": "http://127.0.0.1:9/v1"}},
            "model": "corp:qwen3-27b",
            "permissions": {"disableBypassPermissionsMode": "disable"},
        },
    )
    settings = load_settings(project)
    with pytest.raises(SettingsError, match="disabled"):
        build_agent(settings, AgentOptions(cwd=project, permission_mode="bypassPermissions"))


async def test_managed_deny_rule_applies_to_the_agent(
    mock_server, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Bash", "arguments": {"command": "curl https://example.com"}}
                ]
            },
            {"content": "ok"},
        ]
    )
    _managed(
        tmp_path,
        monkeypatch,
        {
            "providers": {"corp": {"baseUrl": server.base_url}},
            "model": "corp:qwen3-27b",
            "permissions": {"deny": ["Bash(curl:*)"]},
        },
    )
    from cmcoder.runtime.session import Session

    agent = build_agent(
        load_settings(project), AgentOptions(cwd=project, permission_mode="bypassPermissions")
    )
    session = Session(agent)
    try:
        events = await run_turn(session, "fetch it")
    finally:
        await agent.close()
    bash = of_type(events, ev.ToolResult)[0]
    assert bash.is_error and "Permission denied" in bash.content
