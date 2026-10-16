"""Custom slash commands, skills and MCP prompts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import config_dir
from cmcoder.mcp import McpManager
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import run_command
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import make_session, mock_settings, of_type, run_turn

DEMO = str(Path(__file__).parent / "mcp_servers" / "demo_server.py")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


async def test_custom_command_expands_with_its_allowed_tools(mock_server, project: Path) -> None:
    commands = config_dir() / "commands"
    commands.mkdir(parents=True)
    (commands / "note.md").write_text(
        "---\ndescription: Write a note\nallowed-tools: Write\n---\nWrite $1 into notes.txt."
    )
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "notes.txt", "content": "hi"}}
                ]
            },
            {"content": "Noted."},
        ]
    )
    session = make_session(server.base_url, project)  # no one to ask: -p style
    try:
        help_text = "\n".join((await run_command(session, "/help")).lines)
        assert "/note" in help_text and "Write a note" in help_text
        result = await run_command(session, "/note hello")
        assert result.send == "Write hello into notes.txt." and result.allow == ["Write"]
        events = await run_turn_with(session, result.send, result.allow)
        # The next turn has no special rules any more.
        assert session.agent.policy.turn_allow == []
    finally:
        await session.agent.close()
    assert (project / "notes.txt").read_text() == "hi"
    assert not any(r.is_error for r in of_type(events, ev.ToolResult))


async def run_turn_with(session: Session, prompt: str, allow: list[str]) -> list[ev.Event]:
    return [e async for e in session.run(prompt, allow=allow)]


async def test_skills_are_listed_and_loaded(mock_server, project: Path) -> None:
    skill = config_dir() / "skills" / "release-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Writes release notes in our format.\n---\n"
        "1. Use git log.\n2. Group by type."
    )
    (skill / "template.md").write_text("## Added\n## Fixed\n")
    server = mock_server(
        [
            {"tool_calls": [{"name": "Skill", "arguments": {"skill": "release-notes"}}]},
            {"content": "Following the skill."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "write release notes")
    finally:
        await session.agent.close()
    system = server.requests[0]["messages"][0]["content"]
    assert "release-notes: Writes release notes in our format." in system
    loaded = of_type(events, ev.ToolResult)[0]
    assert "Group by type." in loaded.content and "template.md" in loaded.content


async def test_mcp_prompt_as_a_command(mock_server, project: Path) -> None:
    server = mock_server([])
    settings = mock_settings(
        server.base_url, mcpServers={"demo": {"command": sys.executable, "args": [DEMO]}}
    )
    mcp = McpManager(settings, project)
    await mcp.start(None)
    agent = build_agent(settings, AgentOptions(cwd=project, interactive=False), mcp=mcp)
    session = Session(agent)
    try:
        assert "/mcp__demo__review" in "\n".join((await run_command(session, "/help")).lines)
        result = await run_command(session, "/mcp__demo__review app.py")
    finally:
        await agent.close()
        await mcp.close()
    assert result.send == "Please review app.py for bugs."
