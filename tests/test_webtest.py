"""/webtest: the model starts the project's web app and tests it with the Browser tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cmcoder.runtime.commands import run_command
from cmcoder.webtest import package_manager, split_request, start_hints, webtest_prompt

from .conftest import API_KEY
from .helpers import make_session


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def test_start_hints_come_from_the_project(tmp_path: Path) -> None:
    assert start_hints(tmp_path) == []
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite", "build": "vite build", "start": "node app.js"}})
    )
    assert start_hints(tmp_path) == ["npm run dev  (vite)", "npm run start  (node app.js)"]
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert package_manager(tmp_path) == "pnpm"
    assert start_hints(tmp_path)[0].startswith("pnpm dev")
    (tmp_path / "manage.py").write_text("")
    assert "python manage.py runserver" in start_hints(tmp_path)


def test_a_broken_package_json_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json")
    assert start_hints(tmp_path) == []
    (tmp_path / "package.json").write_text("[1, 2]")
    assert start_hints(tmp_path) == []


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("", (None, "")),
        ("add a todo", (None, "add a todo")),
        ("http://localhost:3000", ("http://localhost:3000", "")),
        ("localhost:5173 log in and out", ("localhost:5173", "log in and out")),
        ("localhost is slow", (None, "localhost is slow")),
    ],
)
def test_split_request(arg: str, expected: tuple[str | None, str]) -> None:
    assert split_request(arg) == expected


def test_the_prompt_starts_the_app_or_uses_the_running_one(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    starting = webtest_prompt(tmp_path, "check the login")
    assert "npm run dev  (vite)" in starting and "in the background" in starting
    assert "check the login" in starting and "kill its process id" in starting
    assert "Browser tool" in starting and "screenshot" in starting
    running = webtest_prompt(tmp_path, "http://localhost:3000 add a todo")
    assert "already running at http://localhost:3000" in running and "do not start it" in running
    assert "add a todo" in running and "kill" not in running
    plain = webtest_prompt(tmp_path / "empty", "")
    assert "each page you can reach" in plain


async def test_the_command_sends_the_prompt_and_respects_the_switch(
    mock_server, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    session = make_session(server.base_url, project)
    try:
        result = await run_command(session, "/webtest the login page")
        assert result.send and "the login page" in result.send
    finally:
        await session.agent.close()
    off = make_session(server.base_url, project, settings={"browser": {"enabled": False}})
    try:
        said = await run_command(off, "/webtest")
        assert said.send is None and "Browser tool is off" in said.lines[0]
    finally:
        await off.agent.close()
