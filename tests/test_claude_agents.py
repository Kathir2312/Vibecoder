"""Agents in Claude Code's folders and with Claude Code's model names work as they are.

`npx playwright init-agents --loop=claude` writes `.claude/agents/playwright-test-*.md` with
`model: sonnet` and `color: green`; cmcoder reads that folder (and `~/.claude/agents`), so nothing
is copied or edited, and `sonnet` is never sent to the gateway as a model name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.subagents import (
    AgentDefinition,
    agent_infos,
    listed,
    load_agents,
    model_ref,
    untrusted_project_agents,
)
from cmcoder.config.settings import claude_dir, config_dir
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, mock_settings, of_type, run_turn

# What `npx playwright init-agents --loop=claude` writes (Playwright 1.65), shortened.
PLAYWRIGHT_PLANNER = """\
---
name: playwright-test-planner
description: Use this agent when you need to create comprehensive test plan for a web application or website
tools: Glob, Grep, Read, LS, mcp__playwright-test__browser_click, mcp__playwright-test__browser_navigate, mcp__playwright-test__browser_snapshot
model: sonnet
color: green
---

You are an expert web test planner. Explore the app, then save the plan under specs/.
"""


def agent_file(folder: Path, name: str, *, model: str = "", tools: str = "Read") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", f"description: The {name} agent.", f"tools: {tools}"]
    if model:
        lines.append(f"model: {model}")
    path = folder / f"{name}.md"
    path.write_text("---\n" + "\n".join(lines) + f"\n---\nYou are the {name}.\n", encoding="utf-8")
    return path


def definition(model: str | None) -> AgentDefinition:
    return AgentDefinition("a", "An agent.", "Do it.", "user", model=model)


def test_playwrights_agents_in_the_claude_folder_work_without_a_copy(project: Path) -> None:
    folder = project / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "playwright-test-planner.md").write_text(PLAYWRIGHT_PLANNER, encoding="utf-8")
    found = load_agents(project, True)["playwright-test-planner"]
    assert found.origin == "project" and found.folder == ".claude/agents"
    assert found.tools is not None and "Read" in found.tools
    assert listed("mcp__playwright-test__browser_click", found.tools)
    assert not listed("mcp__playwright-test__browser_drag", found.tools)  # not in its list
    assert found.model == "sonnet"
    assert model_ref(mock_settings("http://127.0.0.1:1/v1"), found) is None  # your own model


def test_a_projects_claude_agents_wait_for_trust_like_its_own(project: Path) -> None:
    agent_file(project / ".claude" / "agents", "scout")
    assert "scout" not in load_agents(project, False)
    assert untrusted_project_agents(project) == ["scout"]
    shown = {i.name: i for i in agent_infos(project, False)}
    assert not shown["scout"].usable and "cmcoder trust" in shown["scout"].hint
    assert "scout" in load_agents(project, True)


def test_your_claude_folder_is_read_and_a_cmcoder_folder_wins_a_name_clash(
    project: Path,
) -> None:
    agent_file(claude_dir() / "agents", "mine", model="haiku")
    agent_file(claude_dir() / "agents", "both", tools="Grep")
    agent_file(config_dir() / "agents", "both", tools="Glob")
    agent_file(project / ".claude" / "agents", "clash", tools="Read")
    agent_file(project / ".cmcoder" / "agents", "clash", tools="Grep")
    agent_file(project / ".claude" / "agents", "yours", tools="Read")
    agent_file(config_dir() / "agents", "yours", tools="Glob")
    found = load_agents(project, True)
    assert found["mine"].origin == "user" and found["mine"].folder.endswith("/agents")
    assert found["both"].tools == ["Glob"]  # ~/.cmcoder/agents over ~/.claude/agents
    assert found["clash"].tools == ["Grep"]  # .cmcoder/agents over .claude/agents
    assert (
        found["yours"].tools == ["Glob"] and found["yours"].origin == "user"
    )  # you over a project


@pytest.mark.parametrize("name", ["sonnet", "opus", "Sonnet", " opus ", "inherit", "", None])
def test_sonnet_and_opus_mean_the_main_model(name: str | None) -> None:
    settings = mock_settings("http://127.0.0.1:1/v1", smallFastModel="mock:qwen3-7b")
    assert model_ref(settings, definition(name)) is None


def test_haiku_means_the_small_model_and_other_names_are_left_alone() -> None:
    settings = mock_settings("http://127.0.0.1:1/v1", smallFastModel="mock:qwen3-7b")
    assert model_ref(settings, definition("haiku")) == "mock:qwen3-7b"
    assert model_ref(settings, definition("small")) == "mock:qwen3-7b"
    assert model_ref(settings, definition("mock:qwen3-27b")) == "mock:qwen3-27b"
    assert model_ref(settings, definition("qwen-custom")) == "qwen-custom"
    assert model_ref(mock_settings("http://127.0.0.1:1/v1"), definition("haiku")) is None


async def test_a_subagent_with_claudes_model_names_runs_on_your_models(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    agent_file(claude_dir() / "agents", "reviewer", model="sonnet")
    agent_file(claude_dir() / "agents", "scout", model="haiku")
    server = mock_server(
        [
            {"tool_calls": [{"id": "t1", "name": "Task", "arguments": _task("reviewer")}]},
            {"content": "Looks fine."},
            {"tool_calls": [{"id": "t2", "name": "Task", "arguments": _task("scout")}]},
            {"content": "Found it."},
            {"content": "Both done."},
        ]
    )
    session = make_session(server.base_url, project, settings={"smallFastModel": "mock:qwen3-7b"})
    try:
        events = await run_turn(session, "review and scout")
    finally:
        await session.agent.close()
    models = [r["model"] for r in server.requests]
    assert "sonnet" not in models and "haiku" not in models
    assert models[1] == "qwen3-27b"  # `model: sonnet`: the main model
    assert models[3] == "qwen3-7b"  # `model: haiku`: the small one
    results = {r.id: r for r in of_type(events, ev.ToolResult)}
    assert results["t1"].content == "Looks fine." and results["t2"].content == "Found it."


def _task(agent: str) -> dict[str, str]:
    return {"description": agent, "prompt": f"Do the {agent} job", "subagent_type": agent}
