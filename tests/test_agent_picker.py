"""Talking to one of the agents for a whole conversation (`set_agent`, `--agent`): a subagent
definition's instructions, tools and model replace the default's, and the UI gets a list to
choose from (`agent_list`), with a project's agents greyed out until the project is trusted."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.agents.subagents import agent_infos, listed, short_description
from cmcoder.config.settings import set_project_trust

from .conftest import API_KEY
from .helpers import mock_settings
from .test_stdio import Agent

PLANNER = """\
---
name: planner
description: Use this agent when you need to create a comprehensive test plan for a web application. Examples: <example>Context: A user wants tests. user: 'plan it' assistant: 'I'll use the planner'. <commentary>Yes.</commentary></example>
tools: Glob, Grep, Read, Write, mcp__playwright-test__browser_click
model: sonnet-for-claude
color: green
---
You are an expert web test planner. Explore the app, then write the plan to specs/.
"""
EVERYTHING = """\
---
name: helper
description: Helps with anything.
---
You are a helper.
"""


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def add_agents(project: Path) -> None:
    folder = project / ".cmcoder" / "agents"
    folder.mkdir(parents=True)
    (folder / "planner.md").write_text(PLANNER.replace("sonnet-for-claude", "inherit"))
    (folder / "helper.md").write_text(EVERYTHING)


def tool_names(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools", [])}


def system_text(request: dict[str, Any]) -> str:
    return str(request["messages"][0]["content"])


def test_a_menu_line_without_the_examples() -> None:
    line = short_description(PLANNER.split("---")[1].split("description: ")[1].split("\ntools:")[0])
    assert (
        line
        == "Use this agent when you need to create a comprehensive test plan for a web application."
    )
    assert short_description("Short one.") == "Short one."
    long = short_description("word " * 100)
    assert len(long) <= 140 and long.endswith("…")
    assert short_description("<example>only</example>") == ""


def test_the_tools_an_agent_lists() -> None:
    wanted = ["Read", "mcp__playwright-test"]
    assert listed("Read", wanted) and not listed("Write", wanted)
    assert listed("mcp__playwright-test__browser_click", wanted)  # a whole MCP server
    assert not listed("mcp__other__x", wanted)
    assert listed("anything", None)  # no list: every tool
    assert listed("ide_open_browser_page", ["Read", "ide_*"])  # a prefix: the IDE's browser tools
    assert not listed("Bash", ["Read", "ide_*"]) and not listed("idea", ["ide_*"])


def test_the_list_puts_the_projects_agents_first_and_greys_out_untrusted_ones(
    project: Path,
) -> None:
    add_agents(project)
    trusted = agent_infos(project, True)
    assert [(i.name, i.origin, i.usable) for i in trusted] == [
        ("helper", "project", True),
        ("planner", "project", True),
        ("explore", "built-in", True),
        ("general-purpose", "built-in", True),
        ("playwright-generator", "built-in", True),
        ("playwright-healer", "built-in", True),
        ("playwright-mapper", "built-in", True),
        ("playwright-planner", "built-in", True),
        ("playwright-runner", "built-in", True),
    ]
    # The Playwright agents have a heading of their own in the list, the others their origin's.
    assert {i.name for i in trusted if i.group == "Playwright"} == {
        n for n in (i.name for i in trusted) if n.startswith("playwright-")
    }
    assert all(i.group == "" for i in trusted if not i.name.startswith("playwright-"))
    assert trusted[1].description.startswith("Use this agent when you need to create")
    assert "<example>" not in trusted[1].description
    untrusted = {i.name: i for i in agent_infos(project, False)}
    assert not untrusted["planner"].usable and "cmcoder trust" in untrusted["planner"].hint
    assert untrusted["explore"].usable
    assert "critic" not in untrusted


def test_an_agent_replaces_the_instructions_the_tools_and_the_todo_list(project: Path) -> None:
    add_agents(project)
    settings = mock_settings("http://127.0.0.1:1/v1")
    settings.project_trusted = True
    default = build_agent(settings, AgentOptions(cwd=project))
    planner = build_agent(settings, AgentOptions(cwd=project, agent="planner"))
    everything = build_agent(settings, AgentOptions(cwd=project, agent="helper"))
    assert default.persona is None and planner.persona == "planner"
    assert {"Read", "Bash", "Edit"} <= set(default.tools)
    # The planner lists Glob, Grep, Read, Write and one MCP tool: nothing else.
    assert set(planner.tools) == {"Glob", "Grep", "Read", "Write"}
    assert {"Read", "Bash", "Edit"} <= set(everything.tools)  # no tools: line: all of them


def test_an_unknown_or_untrusted_agent_is_refused(project: Path) -> None:
    add_agents(project)
    settings = mock_settings("http://127.0.0.1:1/v1")
    with pytest.raises(ValueError, match="No agent named 'nope'"):
        build_agent(settings, AgentOptions(cwd=project, agent="nope"))
    with pytest.raises(ValueError, match="No agent named 'planner'"):  # the project isn't trusted
        build_agent(settings, AgentOptions(cwd=project, agent="planner"))
    with pytest.raises(ValueError, match="No agent named 'critic'"):
        build_agent(settings, AgentOptions(cwd=project, agent="critic"))


async def test_over_stdio_the_conversation_becomes_the_agent_and_back(
    mock_server: Any, project: Path
) -> None:
    add_agents(project)
    set_project_trust(project, True)
    server = mock_server([{"content": "Plan written."}, {"content": "Back to normal."}])
    agent = await Agent.start(project, server)
    try:
        init = await agent.until("system_init")
        assert "agents" in init["features"]
        listing = await agent.until("agent_list")
        assert listing["current"] is None
        assert [a["name"] for a in listing["agents"]][:2] == ["helper", "planner"]
        # Choose the planner: a notice, the new list, and the next message goes to it.
        await agent.send(type="set_agent", agent="planner")
        changed = await agent.until("agent_changed")
        assert changed["agent"] == "planner"
        assert (await agent.until("agent_list"))["current"] == "planner"
        await agent.send(type="user_message", text="plan the login page")
        assert (await agent.until("result"))["result"] == "Plan written."
        # Back to the default: its own instructions and tools again.
        await agent.send(type="set_agent", agent=None)
        assert (await agent.until("agent_changed"))["agent"] is None
        await agent.send(type="user_message", text="and now?")
        assert (await agent.until("result"))["result"] == "Back to normal."
    finally:
        await agent.close()
    as_planner, as_default = server.requests
    assert "expert web test planner" in system_text(as_planner)
    assert "talking with the user directly" in system_text(as_planner)
    assert "You are cmcoder" not in system_text(as_planner)
    assert tool_names(as_planner) == {"Glob", "Grep", "Read", "Write"}
    assert "expert web test planner" not in system_text(as_default)
    assert {"Bash", "Edit", "write_todos", "Task"} <= tool_names(as_default)
    # The conversation is one: the planner's turn is still in what the default sees.
    assert "plan the login page" in str(as_default["messages"])


async def test_over_stdio_a_wrong_choice_is_an_error_not_a_switch(
    mock_server: Any, project: Path
) -> None:
    add_agents(project)  # the project is NOT trusted
    agent = await Agent.start(project, mock_server([{"content": "x"}]))
    try:
        await agent.until("system_init")
        listing = await agent.until("agent_list")
        planner = next(a for a in listing["agents"] if a["name"] == "planner")
        assert planner["usable"] is False and "cmcoder trust" in planner["hint"]
        await agent.send(type="set_agent", agent="planner")
        error = await agent.until("error")
        assert error["kind"] == "invalid_agent" and "cmcoder trust" in error["message"]
        await agent.send(type="set_agent", agent="nope")
        error = await agent.until("error")
        assert error["kind"] == "invalid_agent" and "Choose one of" in error["message"]
        assert not [e for e in agent.events if e["type"] == "agent_changed"]
    finally:
        await agent.close()


async def test_over_stdio_it_cannot_change_during_a_turn(mock_server: Any, project: Path) -> None:
    add_agents(project)
    set_project_trust(project, True)
    write = {"tool_calls": [{"name": "Write", "arguments": {"file_path": "a.txt", "content": "x"}}]}
    agent = await Agent.start(project, mock_server([write, {"content": "done"}]))
    try:
        await agent.until("system_init")
        await agent.send(type="user_message", text="write it")
        ask = await agent.until("permission_request")  # the turn waits for the user's answer
        await agent.send(type="set_agent", agent="planner")
        error = await agent.until("error")
        assert error["kind"] == "busy" and "during a turn" in error["message"]
        await agent.send(type="permission_response", request_id=ask["request_id"], allow=False)
        await agent.until("result")
    finally:
        await agent.close()


def test_the_command_line_can_start_as_an_agent(mock_server: Any, project: Path) -> None:
    add_agents(project)
    set_project_trust(project, True)
    server = mock_server([{"content": "Planned."}])
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(project),
        "CMCODER_CONFIG_DIR": __import__("os").environ["CMCODER_CONFIG_DIR"],
        "CMCODER_BASE_URL": server.base_url,
        "CMCODER_API_KEY": API_KEY,
        "CMCODER_MODEL": "qwen3-27b",
        "CMCODER_SANDBOX": "off",
    }
    run = subprocess.run(
        [sys.executable, "-m", "cmcoder", "--agent", "planner", "-p", "plan it"],
        cwd=project, env=env, capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert run.returncode == 0 and "Planned." in run.stdout, run.stderr
    assert "expert web test planner" in system_text(server.requests[0])
    bad = subprocess.run(
        [sys.executable, "-m", "cmcoder", "--agent", "nope", "-p", "x"],
        cwd=project, env=env, capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert bad.returncode != 0 and "No agent named 'nope'" in bad.stdout + bad.stderr
