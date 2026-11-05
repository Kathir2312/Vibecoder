"""The IDE's own browser tools (VS Code's Integrated Browser) offered to the model as they are:
the extension sends their names and schemas, every call travels back as an `ide_tool_request`,
screenshots come back as pictures, and the engine's own Browser tool steps aside."""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.ide_browser import IdeToolSet, looks_read_only, object_schema, tool_name
from cmcoder.protocol.messages import IdeToolDef
from cmcoder.runtime.commands import webtest_command
from cmcoder.runtime.session import Session
from cmcoder.tools.base import ToolResult

from .conftest import API_KEY
from .helpers import mock_settings
from .test_stdio import Agent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs cmcoder as a subprocess")


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


PNG = _png()
SHOT = f"data:image/png;base64,{PNG}"

DEFS = [
    {
        "name": "open_browser_page",
        "description": "Open a URL in the integrated browser.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The address"}},
            "required": ["url"],
        },
    },
    {
        "name": "screenshot_page",
        "description": "Take a screenshot of the page.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": args}]}


def tool_names(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools", [])}


async def test_the_ides_browser_tools_are_offered_and_called_over_stdio(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(
        [
            call("ide_open_browser_page", url="http://localhost:3000"),
            call("ide_screenshot_page"),
            {"content": "The page works."},
        ]
    )
    agent = await Agent.start(project, server)
    try:
        await agent.until("system_init")
        await agent.send(type="ide_capabilities", tools=[], tool_defs=DEFS)
        await agent.send(type="user_message", text="test the page")
        # Opening a page asks first (it goes somewhere); the answer is the user's.
        ask = await agent.until("permission_request")
        assert ask["name"] == "ide_open_browser_page"
        await agent.send(type="permission_response", request_id=ask["request_id"], allow=True)
        opened = await agent.until("ide_tool_request")
        assert opened["name"] == "open_browser_page"  # the IDE's own name, not ours
        assert opened["input"] == {"url": "http://localhost:3000"}
        await agent.send(
            type="ide_tool_result", request_id=opened["request_id"], content="Opened tab 1."
        )
        # A screenshot only looks: no question. Its picture goes to the chat.
        shot = await agent.until("ide_tool_request")
        assert shot["name"] == "screenshot_page"
        await agent.send(
            type="ide_tool_result",
            request_id=shot["request_id"],
            content="Took a screenshot.",
            images=[SHOT, "javascript:alert(1)", "data:image/svg+xml;base64,PHN2Zy8+"],
        )
        assert (await agent.until("result"))["subtype"] == "success"
        results = {e["name"]: e for e in agent.events if e["type"] == "tool_result"}
        assert "Opened tab 1." in results["ide_open_browser_page"]["content"]
        assert not results["ide_screenshot_page"]["is_error"]
        (shown,) = results["ide_screenshot_page"]["images"]  # only the real image type
        assert shown.startswith("data:image/")
    finally:
        await agent.close()
    # What the model was offered: the IDE's tools with their schemas, and not our Browser tool.
    first = server.requests[0]
    assert {"ide_open_browser_page", "ide_screenshot_page"} <= tool_names(first)
    assert "Browser" not in tool_names(first)
    spec = next(t for t in first["tools"] if t["function"]["name"] == "ide_open_browser_page")
    assert spec["function"]["parameters"]["required"] == ["url"]
    assert "integrated browser" in spec["function"]["description"]


async def test_an_ide_tool_that_fails_is_an_error_for_the_model(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([call("ide_screenshot_page"), {"content": "I could not."}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("system_init")
        await agent.send(type="ide_capabilities", tools=[], tool_defs=DEFS)
        await agent.send(type="user_message", text="screenshot")
        request = await agent.until("ide_tool_request")
        await agent.send(
            type="ide_tool_result",
            request_id=request["request_id"],
            content="There is no page open.",
            is_error=True,
        )
        assert (await agent.until("result"))["subtype"] == "success"
        (result,) = [e for e in agent.events if e["type"] == "tool_result"]
        assert result["is_error"] and "no page open" in result["content"]
    finally:
        await agent.close()
    assert "no page open" in str(server.requests[-1]["messages"][-1])


async def test_without_the_ides_tools_the_engines_browser_stays(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([{"content": "hi"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("system_init")
        await agent.send(type="user_message", text="hello")
        await agent.until("result")
    finally:
        await agent.close()
    names = tool_names(server.requests[0])
    assert not [n for n in names if n.startswith("ide_")]
    assert "Browser" in names  # Playwright is installed with cmcoder


def test_names_schemas_and_the_read_only_guess() -> None:
    taken: set[str] = set()
    assert tool_name("open_browser_page", taken) == "ide_open_browser_page"
    assert tool_name("open.browser page", taken) == "ide_open_browser_page_2"  # sanitised, unique
    assert tool_name("ide_click", taken) == "ide_click"
    assert len(tool_name("x" * 200, taken)) <= 64
    assert object_schema({}) == {"type": "object", "properties": {}}
    assert object_schema({"$schema": "x", "properties": {"a": {"type": "string"}}})[
        "properties"
    ] == {"a": {"type": "string"}}
    for name in ("readPage", "screenshot_page", "Screenshot"):
        assert looks_read_only(name)
    for name in ("openBrowserPage", "click_element", "type_in_page", "runPlaywrightCode"):
        assert not looks_read_only(name)


async def test_a_tool_set_runs_its_tools_through_the_call_it_was_given() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def call_ide(name: str, data: dict[str, Any]) -> ToolResult:
        seen.append((name, data))
        return ToolResult("x" * 100)

    tools = IdeToolSet([IdeToolDef(**DEFS[0])], call_ide, max_chars=40)
    (tool,) = tools.langchain_tools()
    out = await tool.ainvoke({"url": "http://localhost"})
    assert seen == [("open_browser_page", {"url": "http://localhost"})]
    assert len(str(out)) < 100  # cut to what the context can take
    (stand_in,) = tools.permission_tools()
    assert stand_in.name == "ide_open_browser_page" and not stand_in.read_only


async def test_webtest_uses_the_ides_browser_when_it_has_one(project: Path) -> None:
    async def call_ide(name: str, data: dict[str, Any]) -> ToolResult:  # pragma: no cover
        return ToolResult("")

    agent = build_agent(
        mock_settings("http://127.0.0.1:1/v1"),
        AgentOptions(cwd=project),
        ide_browser=IdeToolSet([IdeToolDef(**d) for d in DEFS], call_ide),
    )
    try:
        assert agent.ide_tool_names == ["ide_open_browser_page", "ide_screenshot_page"]
        sent = webtest_command(Session(agent), "http://localhost:3000 the login").send
        assert sent is not None
        assert "browser tools (ide_open_browser_page, ide_screenshot_page" in sent
        assert "Browser tool" not in sent and "http://localhost:3000" in sent
    finally:
        await agent.close()
