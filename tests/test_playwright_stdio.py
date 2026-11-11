"""/playwright as an IDE chat uses it: over `--protocol stdio`, with a scripted model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .test_stdio import Agent, write

URL = "http://localhost:5080"


async def test_the_ide_lists_the_commands_and_answers_the_status_itself(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")  # the chat asks when "/" is typed
        listed = await agent.until("command_list")
        names = {c["name"] for c in listed["commands"]}
        # The slash list offers what sends a request to the model too, not only what is answered here.
        assert {"playwright", "webtest", "review", "init", "turns", "model"} <= names
        shown = next(c for c in listed["commands"] if c["name"] == "playwright")
        assert "setup" in shown["argument_hint"] and "heal" in shown["argument_hint"]

        await agent.send(type="user_message", text="/playwright")
        text = (await agent.until("assistant_message"))["text"]
        assert "Playwright tests for this project's web app" in text and "Next:" in text
        assert (await agent.until("result"))["subtype"] == "success"

        await agent.send(type="user_message", text="/playwright map")  # before set-up: says why
        assert "Not yet" in (await agent.until("assistant_message"))["text"]
        await agent.until("result")
        assert len(server.requests) == 0  # neither went to the model
    finally:
        assert await agent.close() == 0


async def test_setup_writes_its_files_without_a_question(mock_server: Any, project: Path) -> None:
    """The step's own folder needs no permission question; the model is asked to write there."""
    server = mock_server(
        [
            write("playwright-tests/package.json", '{"name": "playwright-tests"}\n'),
            write("playwright-tests/tests/seed.spec.ts", "// seed\n"),
            {"content": "Created the Playwright project."},
        ]
    )
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text=f"/playwright setup {URL}")
        result = await agent.until("result")
        assert result["subtype"] == "success", result
        asked = [e for e in agent.events if e["type"] == "permission_request"]
        assert asked == [], "writing inside playwright-tests/ asked a question"
        assert (project / "playwright-tests" / "package.json").read_text().startswith("{")
        sent = json.dumps(server.requests[0]["messages"])
        assert "Set up Playwright tests for this project" in sent
        assert "playwright.config.ts" in sent and "http://localhost:5080" in sent
    finally:
        assert await agent.close() == 0


async def test_a_step_never_writes_outside_its_folder_unasked(
    mock_server: Any, project: Path
) -> None:
    """The rules cover playwright-tests/ and nothing else: a write to the app asks."""
    server = mock_server([write("src/app.py", "print('changed')\n"), {"content": "done"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        (project / "playwright-tests").mkdir()
        await agent.send(type="user_message", text=f"/playwright setup {URL}")
        asked = await agent.until("permission_request")
        assert asked["name"] == "Write" and asked["input"]["file_path"].endswith("app.py")
        await agent.send(type="permission_response", request_id=asked["request_id"], allow=False)
        await agent.until("result")
        assert (
            not (project / "src" / "app.py").exists()
            or "changed" not in (project / "src" / "app.py").read_text()
        )
    finally:
        assert await agent.close() == 0
