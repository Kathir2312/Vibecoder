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


async def test_a_step_runs_as_its_agent_and_the_agent_from_before_comes_back(
    mock_server: Any, project: Path
) -> None:
    """`/playwright plan` is done by the planner agent: the chat's Agent list shows it for that one
    request (its own instructions, only the tools its job needs), and then the agent that was in
    use is back, so the next question isn't answered by a planner that can't edit your files."""
    import shutil

    if shutil.which("node") is None:
        import pytest

        pytest.skip("Node.js is needed for /playwright")
    folder = project / "playwright-tests"
    (folder / "specs").mkdir(parents=True)
    (folder / "tests").mkdir()
    (folder / "package.json").write_text("{}")
    (folder / "playwright.config.ts").write_text(
        'export default { use: { baseURL: "http://localhost:5080" } };\n'
    )
    (folder / "node_modules" / "@playwright" / "test").mkdir(parents=True)
    server = mock_server(
        [
            write("playwright-tests/specs/site.md", "# Test plan\n"),
            {"content": "The plan has 1 test."},
            {"content": "Done."},
        ]
    )
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="set_agent", agent="explore")  # the user's own choice
        assert (await agent.until("agent_changed"))["agent"] == "explore"
        await agent.until("agent_list")
        await agent.send(type="user_message", text="/playwright plan")
        seen: list[dict[str, Any]] = []
        while (event := await agent.next())["type"] != "result":
            seen.append(event)
        changes = [e for e in seen if e["type"] == "agent_changed"]
        assert [(c["agent"], c["reason"]) for c in changes] == [
            ("playwright-planner", "step"),
            ("explore", "restore"),
        ]
        order = [e["type"] for e in seen]
        assert order.index("agent_changed") < order.index("tool_use") < len(order) - 1
        lists = [e["current"] for e in seen if e["type"] == "agent_list"]
        assert lists == ["playwright-planner", "explore"]  # the dropdown moves there and back
        assert event["subtype"] == "success"
        assert (project / "playwright-tests" / "specs" / "site.md").exists()
        # What the model saw: the planner's own instructions, and not a tool the planner has no use for.
        first = server.requests[0]
        system = json.dumps(first["messages"][0])
        assert "You are a test planner" in system
        offered = {t["function"]["name"] for t in first["tools"]}
        assert {"Read", "Write", "Browser"} <= offered and not offered & {"Bash", "Edit"}
        asked = [e for e in agent.events if e["type"] == "permission_request"]
        assert asked == []  # the planner writes its plan without a question

        await agent.send(type="user_message", text="and now a plain question")
        await agent.until("result")
        second = {t["function"]["name"] for t in server.requests[2]["tools"]}
        assert "Edit" not in second  # `explore` is read-only: the planner is gone
        assert "You are a test planner" not in json.dumps(server.requests[2]["messages"][0])
    finally:
        assert await agent.close() == 0
