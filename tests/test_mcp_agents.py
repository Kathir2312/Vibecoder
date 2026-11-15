"""MCP tools in an agent file's `tools:` list, and a project's `.mcp.json` servers.

This is the shape of Playwright's own agents (`init-agents --loop=claude`): each lists exactly
the `mcp__playwright-test__browser_*` tools it may use, and the server comes from the project's
`.mcp.json`, which is approved before it first starts. A fake server (tests/mcp_servers) stands
in for Playwright's.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import McpServerConfig, claude_dir, config_fingerprint, is_approved
from cmcoder.mcp import McpManager, status_lines
from cmcoder.mcp.manager import McpServer
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import mock_settings, run_turn

DEMO = str(Path(__file__).parent / "mcp_servers" / "demo_server.py")
DEMO_TOOLS = {f"mcp__demo__{n}" for n in ("add", "shout", "fail", "env", "slow")}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def demo_config() -> dict[str, Any]:
    return {"command": sys.executable, "args": [DEMO]}


def agent_file(name: str, tools: str) -> None:
    folder = claude_dir() / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: The {name}.\ntools: {tools}\nmodel: sonnet\n---\n"
        f"You are the {name}.\n",
        encoding="utf-8",
    )


def task(agent: str, call_id: str) -> dict[str, Any]:
    args = {"description": agent, "prompt": f"Do the {agent} job", "subagent_type": agent}
    return {"tool_calls": [{"id": call_id, "name": "Task", "arguments": args}]}


def names(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools", [])}


async def test_a_subagent_gets_only_the_mcp_tools_its_file_lists(
    mock_server: Any, project: Path
) -> None:
    agent_file("checker", "Read, mcp__demo__add, mcp__demo__shout")  # two tools of the server
    agent_file("wide", "mcp__demo")  # the whole server, and nothing else
    server = mock_server(
        [task("checker", "t1"), {"content": "ok"}, task("wide", "t2"), {"content": "ok"}]
        + [{"content": "done"}]
    )
    settings = mock_settings(server.base_url, mcpServers={"demo": demo_config()})
    mcp = McpManager(settings, project)
    await mcp.start(None)
    agent = build_agent(settings, AgentOptions(cwd=project), mcp=mcp)
    try:
        await run_turn(Session(agent), "go")
    finally:
        await agent.close()
        await mcp.close()
    main, checker, _, wide, _ = server.requests
    assert names(main) >= DEMO_TOOLS  # the main agent has them all
    assert names(checker) == {"Read", "mcp__demo__add", "mcp__demo__shout"}
    assert names(wide) == DEMO_TOOLS


async def test_an_agent_you_talk_to_gets_only_the_mcp_tools_its_file_lists(
    mock_server: Any, project: Path
) -> None:
    agent_file("checker", "Read, mcp__demo__add")
    server = mock_server([{"content": "hello"}])
    settings = mock_settings(server.base_url, mcpServers={"demo": demo_config()})
    mcp = McpManager(settings, project)
    await mcp.start(None)
    agent = build_agent(settings, AgentOptions(cwd=project, agent="checker"), mcp=mcp)
    try:
        await run_turn(Session(agent), "hi")
    finally:
        await agent.close()
        await mcp.close()
    assert names(server.requests[0]) == {"Read", "mcp__demo__add"}


async def test_an_unapproved_project_server_is_skipped_and_says_how_to_approve(
    mock_server: Any, project: Path
) -> None:
    agent_file("checker", "Read, mcp__proj__add")
    server = mock_server([task("checker", "t1"), {"content": "ok"}, {"content": "done"}])
    settings = mock_settings(server.base_url)
    config = McpServerConfig.model_validate(demo_config())
    settings.project_mcp_servers = {"proj": config}
    mcp = McpManager(settings, project)
    warnings = await mcp.start(None)  # `-p`: nobody to ask
    assert mcp.servers[0].status == "not approved" and not mcp.langchain_tools()
    assert "cmcoder mcp approve proj" in warnings[0]
    assert any("not approved" in line for line in status_lines(mcp))
    agent = build_agent(settings, AgentOptions(cwd=project), mcp=mcp)
    try:
        await run_turn(Session(agent), "go")
    finally:
        await agent.close()
        await mcp.close()
    assert names(server.requests[1]) == {"Read"}  # the tool it lists isn't there: no guessing
    assert not is_approved(project, "mcp:proj", config_fingerprint(config))


async def test_the_chat_asks_and_an_approved_server_stays_approved(
    mock_server: Any, project: Path
) -> None:
    agent_file("checker", "Read, mcp__proj__add")
    server = mock_server([task("checker", "t1"), {"content": "ok"}, {"content": "done"}])
    settings = mock_settings(server.base_url)
    config = McpServerConfig.model_validate(demo_config())
    settings.project_mcp_servers = {"proj": config}
    asked: list[str] = []

    async def approve_it(s: McpServer) -> bool:  # what the permission card answers
        asked.append(s.name)
        return True

    mcp = McpManager(settings, project)
    assert await mcp.start(approve_it) == []
    assert asked == ["proj"] and mcp.servers[0].status == "connected"
    agent = build_agent(settings, AgentOptions(cwd=project), mcp=mcp)
    try:
        await run_turn(Session(agent), "go")
    finally:
        await agent.close()
        await mcp.close()
    assert names(server.requests[1]) == {"Read", "mcp__proj__add"}
    again = McpManager(settings, project)  # a later session: not asked again
    await again.start(None)
    try:
        assert again.servers[0].status == "connected"
    finally:
        await again.close()
