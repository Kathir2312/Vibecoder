"""Code search in a session: the CodeSearch tool, automatic code context
(middleware/code_context.py) and /index."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import load_settings
from cmcoder.protocol import events as ev
from cmcoder.rag.index import open_index
from cmcoder.runtime.commands import run_command
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import of_type
from .test_rag_index import AUTH


@pytest.fixture
def env(mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A project with code, a gateway with an embedding model, and settings for both."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    (project / "auth.py").write_text(AUTH)
    (project / "notes.md").write_text("# Deploy\n\nDeploys run from the release branch.\n")
    server = mock_server([])
    monkeypatch.setenv("CMCODER_BASE_URL", server.base_url)
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    monkeypatch.setenv("CMCODER_MODEL", "qwen3-27b")
    monkeypatch.setenv("CMCODER_EMBEDDING_MODEL", "text-embedding-3-small")
    return server


async def build_index(project: Path) -> None:
    index = open_index(load_settings(project), project)
    assert index is not None
    try:
        await index.update()
    finally:
        await index.close()


def session(project: Path, **kw: Any) -> Session:
    agent = build_agent(load_settings(project), AgentOptions(cwd=project, interactive=False, **kw))
    return Session(agent)


async def run(s: Session, prompt: str) -> list[ev.Event]:
    return [e async for e in s.run(prompt)]


def sent_to_model(server: Any, i: int = -1) -> str:
    return "\n".join(str(m.get("content")) for m in server.state.requests[i]["messages"])


async def test_code_search_tool(env: Any, project: Path) -> None:
    await build_index(project)
    env.state.script = [
        {
            "tool_calls": [
                {"name": "CodeSearch", "arguments": {"query": "refresh expired auth token"}}
            ]
        },
        {"content": "It's refresh_auth_token in auth.py."},
    ]
    s = session(project)
    try:
        assert "CodeSearch" in s.agent.tools
        events = await run(s, "ok")  # too short for automatic context
    finally:
        await s.agent.close()
    assert "Code search: this project is indexed" in sent_to_model(env, 0)
    result = next(e for e in of_type(events, ev.ToolResult) if e.name == "CodeSearch")
    assert "auth.py:" in result.content and "refresh_auth_token" in result.content
    assert not result.is_error and not of_type(events, ev.PermissionDenied)


async def test_automatic_context(env: Any, project: Path) -> None:
    await build_index(project)
    env.state.script = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    s = session(project)
    try:
        first = await run(s, "how is the expired auth token refreshed?")
        context = of_type(first, ev.CodeContext)[0]
        assert context.items[0].path == "auth.py" and context.tokens > 0
        sent = sent_to_model(env)
        assert "<system-reminder>" in sent and "def refresh_auth_token" in sent
        # The same code isn't sent twice in one conversation; short messages get none.
        second = await run(s, "how is the expired auth token refreshed?")
        assert not of_type(second, ev.CodeContext)
        third = await run(s, "thanks")
        assert not of_type(third, ev.CodeContext)
        # The history (and /rewind) give back what the user wrote, without the code.
        history = [h.text for h in await s.history() if h.role == "user"]
        assert history[0] == "how is the expired auth token refreshed?"
    finally:
        await s.agent.close()


async def test_off_or_limited(env: Any, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No index yet: no tool, and no warning unless code search was asked for.
    s = session(project)
    assert "CodeSearch" not in s.agent.tools and not s.agent.startup_warnings
    await s.agent.close()
    monkeypatch.setenv("CMCODER_RAG", "on")
    s = session(project)
    assert any("no index yet" in w for w in s.agent.startup_warnings)
    await s.agent.close()
    # Automatic context can be turned off, keeping the tool.
    await build_index(project)
    (project / ".cmcoder").mkdir()
    (project / ".cmcoder" / "settings.json").write_text(
        '{"rag": {"autoContext": {"enabled": false}}}'
    )
    env.state.script = [{"content": "a"}]
    s = session(project)
    try:
        events = await run(s, "how is the expired auth token refreshed?")
        assert "CodeSearch" in s.agent.tools
        assert not of_type(events, ev.CodeContext)
    finally:
        await s.agent.close()


async def test_a_failing_index_never_stops_the_turn(env: Any, project: Path) -> None:
    await build_index(project)
    env.state.embedding_models = []  # the embedding model is gone from the gateway
    env.state.script = [{"content": "answer"}, {"content": "again"}]
    s = session(project)
    try:
        events = await run(s, "how is the expired auth token refreshed?")
        assert events[-1].result == "answer"
        warnings = [e.message for e in of_type(events, ev.Warning)]
        assert any("Code search isn't answering" in w for w in warnings)
        events = await run(s, "and how are invoices parsed then?")
        assert not of_type(events, ev.Warning)  # said once
    finally:
        await s.agent.close()


async def test_edits_and_new_files_are_found(env: Any, project: Path) -> None:
    await build_index(project)
    (project / "billing.py").write_text(
        "def compute_vat_for_invoice(total):\n    return total * 0.2\n"
    )
    env.state.script = [
        {"tool_calls": [{"name": "Read", "arguments": {"file_path": "auth.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "Edit",
                    "arguments": {
                        "file_path": "auth.py",
                        "old_string": "def parse_invoice_total(lines):",
                        "new_string": "def parse_invoice_grand_total(lines):",
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "CodeSearch", "arguments": {"query": "invoice grand total"}}]},
        {"tool_calls": [{"name": "CodeSearch", "arguments": {"query": "vat for invoice"}}]},
        {"content": "done"},
    ]
    s = session(project, permission_mode="acceptEdits")
    try:
        # The session brings the index up to date in the background (billing.py is new).
        index = s.agent.code_index
        assert index is not None, s.agent.startup_warnings
        for _ in range(100):
            if not index.updating:
                break
            await asyncio.sleep(0.05)
        events = await run(s, "ok")
    finally:
        await s.agent.close()
    results = [e.content for e in of_type(events, ev.ToolResult) if e.name == "CodeSearch"]
    assert "parse_invoice_grand_total" in results[0]  # the agent's own edit
    assert "compute_vat_for_invoice" in results[1]  # a file added before the session


async def test_subagents_search_too(env: Any, project: Path) -> None:
    await build_index(project)
    s = session(project)
    try:
        from cmcoder.agents.subagents import BUILT_IN_AGENTS, allowed

        explore = BUILT_IN_AGENTS["explore"]
        assert allowed("CodeSearch", explore.tools) and "CodeSearch" in s.agent.tools
        assert s.agent.subagents is not None and s.agent.subagents.code_index is not None
    finally:
        await s.agent.close()


async def test_index_command_turns_code_search_on_mid_session(env: Any, project: Path) -> None:
    env.state.script = [{"content": "a"}]
    s = session(project)
    try:
        assert "CodeSearch" not in s.agent.tools
        assert (await run_command(s, "/index status")).lines == [
            "No index for this project yet: /index builds it."
        ]
        lines = (await run_command(s, "/index")).lines
        assert lines[0].startswith("Indexed 2 files") and "on for the rest" in lines[1]
        assert "CodeSearch" in s.agent.tools and s.agent.code_index is not None
        await run(s, "hi")  # the rebuilt graph, the same conversation
        assert "Code search: this project is indexed" in sent_to_model(env)
        status = (await run_command(s, "/index status")).lines
        assert any(line.startswith("Files, pieces      2 files") for line in status)
        assert (await run_command(s, "/index nonsense")).lines[0].startswith("Usage")
    finally:
        await s.agent.close()
