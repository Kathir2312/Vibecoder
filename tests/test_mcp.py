"""MCP servers through langchain-mcp-adapters, end to end."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import McpServerConfig, approve, config_fingerprint
from cmcoder.mcp import McpManager, status_lines, tool_name
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import mock_settings, of_type, run_turn

DEMO = str(Path(__file__).parent / "mcp_servers" / "demo_server.py")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    monkeypatch.setenv("DEMO_SECRET", "s3cret")


def demo_config(**extra: object) -> dict:
    return {"command": sys.executable, "args": [DEMO], **extra}


def call(tool: str, **args: object) -> dict:
    return {"tool_calls": [{"name": tool, "arguments": args}]}


def test_tool_names() -> None:
    assert tool_name("git hub", "list.issues") == "mcp__git_hub__list_issues"


async def test_mcp_tools_reach_the_model_and_run(mock_server, project: Path) -> None:
    server = mock_server(
        [
            call("mcp__demo__add", a=2, b=3),
            call("mcp__demo__fail", reason="test"),
            call("mcp__demo__env", name="TOKEN"),
            {"content": "2 + 3 = 5."},
        ]
    )
    settings = mock_settings(
        server.base_url,
        mcpServers={"demo": demo_config(env={"TOKEN": "${DEMO_SECRET}"})},
        permissions={"allow": ["mcp__demo"]},  # a whole server in one rule
    )
    mcp = McpManager(settings, project)
    assert await mcp.start(None) == []
    agent = build_agent(settings, AgentOptions(cwd=project, interactive=False), mcp=mcp)
    session = Session(agent)
    try:
        events = await run_turn(session, "add 2 and 3")
        assert any("demo: connected" in line for line in status_lines(mcp))
    finally:
        await agent.close()
        await mcp.close()
    results = {r.name: r for r in of_type(events, ev.ToolResult)}
    assert results["mcp__demo__add"].content == "5" and not results["mcp__demo__add"].is_error
    assert (
        results["mcp__demo__fail"].is_error
        and "failed on purpose" in results["mcp__demo__fail"].content
    )
    assert results["mcp__demo__env"].content == "s3cret"  # ${VAR} expanded for the server
    tools = {t["function"]["name"]: t["function"] for t in server.requests[0]["tools"]}
    assert tools["mcp__demo__add"]["description"].startswith("[MCP server demo]")
    assert "a" in tools["mcp__demo__add"]["parameters"]["properties"]


async def test_mcp_tools_need_permission(mock_server, project: Path) -> None:
    server = mock_server([call("mcp__demo__shout", text="hi"), {"content": "ok"}])
    settings = mock_settings(server.base_url, mcpServers={"demo": demo_config()})
    mcp = McpManager(settings, project)
    await mcp.start(None)
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict:
        asked.append(req)
        return {"allow": True}

    agent = build_agent(settings, AgentOptions(cwd=project), mcp=mcp)
    session = Session(agent, ask=ask)
    try:
        events = await run_turn(session, "shout")
    finally:
        await agent.close()
        await mcp.close()
    assert [r.name for r in asked] == ["mcp__demo__shout"]
    assert asked[0].suggested_rule == "mcp__demo__shout"
    assert of_type(events, ev.ToolResult)[0].content == "HI"


async def test_project_servers_need_approval(project: Path, mock_server) -> None:
    server = mock_server([])
    settings = mock_settings(server.base_url)
    settings.project_mcp_servers = {"proj": McpServerConfig.model_validate(demo_config())}
    mcp = McpManager(settings, project)
    await mcp.start(None)  # nobody to approve
    assert mcp.servers[0].status == "not approved" and not mcp.langchain_tools()
    await mcp.close()

    approve(project, "mcp:proj", config_fingerprint(settings.project_mcp_servers["proj"]))
    mcp = McpManager(settings, project)
    await mcp.start(None)
    try:
        assert mcp.servers[0].status == "connected"
    finally:
        await mcp.close()


async def test_blocked_and_failing_servers(project: Path, mock_server) -> None:
    server = mock_server([])
    settings = mock_settings(
        server.base_url,
        mcpServers={"demo": demo_config(), "broken": {"command": "no-such-mcp-server-xyz"}},
    )
    settings.denied_mcp_servers = ["demo"]
    mcp = McpManager(settings, project)
    warnings = await mcp.start(None)
    await mcp.close()
    states = {s.name: s.status for s in mcp.servers}
    assert states == {"demo": "blocked", "broken": "failed"}
    assert "broken" in warnings[0] and "not found" in warnings[0]


async def test_tool_timeout(mock_server, project: Path) -> None:
    server = mock_server([call("mcp__demo__slow", seconds=5), {"content": "gave up"}])
    settings = mock_settings(
        server.base_url,
        mcpServers={"demo": demo_config(toolTimeout=0.5)},
        permissions={"allow": ["mcp__demo"]},
    )
    mcp = McpManager(settings, project)
    await mcp.start(None)
    agent = build_agent(settings, AgentOptions(cwd=project, interactive=False), mcp=mcp)
    try:
        events = await run_turn(Session(agent), "be slow")
    finally:
        await agent.close()
        await mcp.close()
    slow = of_type(events, ev.ToolResult)[0]
    assert slow.is_error and "timed out" in slow.content
