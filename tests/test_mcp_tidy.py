"""What cmcoder does with an MCP server's tools and results: the tool filter and counts, the
warning for a server with very many tools, the cap on a result, pictures that go to the chat and
not to the model, and the longer first start of a package runner (npx).

A fake browser server (tests/mcp_servers/browser_server.py) has Playwright-like tools."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import McpServerConfig, McpToolFilter, approve, config_fingerprint
from cmcoder.mcp import McpManager, status_lines
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import mock_settings, of_type, run_turn

FAKE = str(Path(__file__).parent / "mcp_servers" / "browser_server.py")
ALL = {
    "mcp__fake__browser_navigate",
    "mcp__fake__browser_snapshot",
    "mcp__fake__browser_big_snapshot",
    "mcp__fake__browser_cookie_list",
    "mcp__fake__browser_take_screenshot",
}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def fake(**env: str) -> dict[str, Any]:
    return {"command": sys.executable, "args": [FAKE], "env": env}


def names(manager: McpManager) -> set[str]:
    return {t.name for t in manager.langchain_tools()}


async def allow(_: ev.PermissionRequest) -> dict[str, Any]:
    return {"allow": True}


async def started(url: str, project: Path, servers: dict[str, Any], **extra: Any) -> McpManager:
    manager = McpManager(mock_settings(url, mcpServers=servers, **extra), project)
    await manager.start(None)
    return manager


# --- the filter and the counts --------------------------------------------------------------


def test_a_filter_keeps_what_allow_matches_and_deny_does_not() -> None:
    rule = McpToolFilter(allow=["browser_*"], deny=["browser_cookie_*"])
    assert rule.keeps("browser_click") and rule.keeps("browser_snapshot")
    assert not rule.keeps("browser_cookie_get") and not rule.keeps("test_run")
    assert McpToolFilter().keeps("anything")  # nothing set: everything
    assert not McpToolFilter(allow=[]).keeps("anything")  # an empty allow list: nothing
    assert McpToolFilter(deny=["test_*"]).keeps("browser_click")


async def test_the_filter_cuts_the_tools_and_the_status_says_how_many(
    mock_server: Any, project: Path
) -> None:
    url = mock_server([]).base_url
    manager = await started(
        url,
        project,
        {"fake": fake()},
        mcpToolFilters={"fake": {"allow": ["browser_*"], "deny": ["browser_cookie_*", "*_big_*"]}},
    )
    try:
        assert names(manager) == {
            "mcp__fake__browser_navigate",
            "mcp__fake__browser_snapshot",
            "mcp__fake__browser_take_screenshot",
        }
        line = status_lines(manager)[0]
        assert "3 of 5 tools (filtered)" in line and "tokens in every request" in line
        assert manager.servers[0].tokens > 0
    finally:
        await manager.close()


async def test_an_unfiltered_server_says_how_many_tools_it_has(
    mock_server: Any, project: Path
) -> None:
    manager = await started(mock_server([]).base_url, project, {"fake": fake()})
    try:
        assert names(manager) == ALL
        assert "5 tools, about" in status_lines(manager)[0]
    finally:
        await manager.close()


async def test_the_filter_applies_to_a_projects_own_server_too(
    mock_server: Any, project: Path
) -> None:
    settings = mock_settings(
        mock_server([]).base_url, mcpToolFilters={"proj": {"allow": ["browser_snapshot"]}}
    )
    config = McpServerConfig.model_validate(fake())
    settings.project_mcp_servers = {"proj": config}
    approve(project, "mcp:proj", config_fingerprint(config))
    manager = McpManager(settings, project)
    await manager.start(None)
    try:
        assert names(manager) == {"mcp__proj__browser_snapshot"}
    finally:
        await manager.close()


async def test_a_server_with_very_many_tools_gets_a_warning_until_it_is_filtered(
    mock_server: Any, project: Path
) -> None:
    url = mock_server([]).base_url
    servers = {"fake": fake(FAKE_FILLER="45")}
    big = McpManager(mock_settings(url, mcpServers=servers), project)
    warnings = await big.start(None)
    await big.close()
    assert len(warnings) == 1
    assert "fake adds 50 tools" in warnings[0] and "mcpToolFilters" in warnings[0]
    cut = McpManager(
        mock_settings(url, mcpServers=servers, mcpToolFilters={"fake": {"allow": ["browser_*"]}}),
        project,
    )
    assert await cut.start(None) == []  # filtered: nothing to warn about
    await cut.close()


# --- results: the size cap and pictures --------------------------------------------------------


def call(tool: str, call_id: str = "t1") -> dict[str, Any]:
    return {"tool_calls": [{"id": call_id, "name": tool, "arguments": {}}]}


async def run_with(
    mock_server: Any, project: Path, tool: str, **settings: Any
) -> tuple[list[ev.Event], Any]:
    server = mock_server([call(tool), {"content": "done"}])
    s = mock_settings(server.base_url, mcpServers={"fake": fake()}, **settings)
    manager = McpManager(s, project)
    await manager.start(None)
    agent = build_agent(s, AgentOptions(cwd=project), mcp=manager)
    try:
        events = await run_turn(Session(agent, ask=allow), "go")
    finally:
        await agent.close()
        await manager.close()
    return events, server


async def test_a_huge_result_is_cut_to_the_budget_and_says_so(
    mock_server: Any, project: Path
) -> None:
    events, server = await run_with(mock_server, project, "mcp__fake__browser_big_snapshot")
    (result,) = of_type(events, ev.ToolResult)
    assert 240_000 > len(result.content) <= 31_000
    assert "characters truncated" in result.content
    seen = str(server.requests[1]["messages"][-1]["content"])
    assert len(seen) < 31_000  # what the model got on its next call


async def test_a_picture_goes_to_the_chat_and_never_to_the_model(
    mock_server: Any, project: Path
) -> None:
    events, server = await run_with(mock_server, project, "mcp__fake__browser_take_screenshot")
    (result,) = of_type(events, ev.ToolResult)
    assert len(result.images) == 1 and result.images[0].startswith("data:image/")
    assert "Screenshot of viewport" in result.content and "You can't see it" in result.content
    sent = str(server.requests[1]["messages"])
    assert "image_url" not in sent and "base64" not in sent  # no pixels went to the model


def test_a_picture_with_nowhere_to_go_is_left_out_with_a_note() -> None:
    from mcp.types import CallToolResult, ImageContent, TextContent

    manager = McpManager(mock_settings("http://127.0.0.1:9"), Path("."))
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
    result = CallToolResult(
        content=[
            TextContent(type="text", text="shot"),
            ImageContent(type="image", data=png, mimeType="image/png"),
            ImageContent(type="image", data="%%%not base64%%%", mimeType="image/png"),
            ImageContent(type="image", data=png, mimeType="application/x-evil"),
        ]
    )
    out = manager.tidy(result)  # not inside a tool call: nothing to hand the picture to
    assert [b.type for b in out.content] == ["text", "text"]
    assert "left out" in out.content[1].text
    assert manager._pictures is None or not list(manager._pictures.iterdir())


def test_a_short_plain_result_is_returned_as_it_is() -> None:
    from mcp.types import CallToolResult, TextContent

    manager = McpManager(mock_settings("http://127.0.0.1:9"), Path("."))
    result = CallToolResult(content=[TextContent(type="text", text="small")])
    assert manager.tidy(result) is result


# --- the first start of a package runner -------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "args", "seconds"),
    [
        ("npx", ["-y", "@playwright/mcp@latest"], 120.0),
        ("npx.cmd", ["@playwright/mcp"], 120.0),
        ("uvx", ["some-server"], 120.0),
        ("pnpm", ["dlx", "some-server"], 120.0),
        ("npm", ["exec", "some-server"], 120.0),
        ("node", ["server.js"], 30.0),
        ("python", ["server.py"], 30.0),
        ("npm", ["run", "start"], 30.0),
    ],
)
def test_a_package_runner_gets_longer_to_start_for_the_first_time(
    command: str, args: list[str], seconds: float
) -> None:
    assert (
        McpServerConfig.model_validate({"command": command, "args": args}).startup_seconds
        == seconds
    )


def test_a_startup_timeout_you_set_always_wins() -> None:
    cfg = McpServerConfig.model_validate({"command": "npx", "args": ["x"], "startupTimeout": 20})
    assert cfg.startup_seconds == 20
