"""The command line: `cmcoder -p` against the mock server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cmcoder.cli.main import main_app

from .conftest import API_KEY


@pytest.fixture
def configured(mock_server, project: Path, monkeypatch: pytest.MonkeyPatch):
    def make(script: list[dict]):
        server = mock_server(script)
        monkeypatch.setenv("CMCODER_BASE_URL", server.base_url)
        monkeypatch.setenv("CMCODER_MODEL", "qwen3-27b")
        monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
        monkeypatch.chdir(project)
        return server

    return make


def test_print_mode_text(configured) -> None:
    configured([{"content": "Hello from the agent."}])
    result = CliRunner().invoke(main_app, ["-p", "hi"])
    assert result.exit_code == 0, result.output
    assert "Hello from the agent." in result.stdout


def test_print_mode_stream_json(configured) -> None:
    configured([{"content": "Done."}])
    result = CliRunner().invoke(main_app, ["-p", "hi", "--output-format", "stream-json"])
    assert result.exit_code == 0, result.output
    types = [json.loads(line)["type"] for line in result.stdout.splitlines() if line.strip()]
    assert types[0] == "system_init" and types[-1] == "result"
    assert "assistant_delta" in types and "assistant_message" in types


def test_print_mode_allowed_tools(configured, project: Path) -> None:
    configured(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "new.txt", "content": "x"}}
                ]
            },
            {"content": "Wrote it."},
        ]
    )
    result = CliRunner().invoke(main_app, ["-p", "write", "--allowedTools", "Write"])
    assert result.exit_code == 0, result.output
    assert (project / "new.txt").read_text() == "x"
    assert os.environ["CMCODER_MODEL"] == "qwen3-27b"


def test_cli_module_does_not_import_langchain() -> None:
    """`cmcoder --version` and `--help` stay fast: the engine is imported lazily."""
    import subprocess
    import sys

    code = "import sys, cmcoder.cli.main; print(any(m.startswith(('langchain', 'langgraph')) for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_mcp_commands(project: Path, tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env["CMCODER_CONFIG_DIR"] = str(tmp_path / "config")
    demo = str(Path(__file__).parent / "mcp_servers" / "demo_server.py")

    def mcp(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "cmcoder", "mcp", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    r = mcp("add", "demo", "--", sys.executable, demo)
    assert r.returncode == 0, r.stderr
    r = mcp(
        "add", "web", "--url", "https://mcp.example/mcp", "-H", "Authorization: Bearer ${TOKEN}"
    )
    assert r.returncode == 0, r.stderr
    saved = json.loads((tmp_path / "config" / "settings.json").read_text())
    assert saved["mcpServers"]["web"]["headers"] == {"Authorization": "Bearer ${TOKEN}"}
    assert mcp("remove", "web").returncode == 0
    r = mcp("list", "--check")
    assert r.returncode == 0 and re.search(
        r"demo: connected · \d+ tools", " ".join(r.stdout.split())
    ), r.stdout + r.stderr
    assert mcp("add", "bad").returncode == 2  # neither --url nor a command
    # `approve` on your own server: nothing to approve, and it says so.
    r = mcp("approve", "demo")
    out = " ".join(r.stdout.split())
    assert r.returncode == 0 and "needs no approval" in out and "mcp list --check" in out

    # A project's server: listed only once the project is trusted, then approved.
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"proj": {"command": sys.executable, "args": [demo]}}})
    )

    def listed() -> str:
        return " ".join(mcp("list").stdout.split())

    assert "isn't trusted" in listed()
    assert mcp("approve", "proj").returncode == 1
    trusted = subprocess.run(
        [sys.executable, "-m", "cmcoder", "trust", "--yes"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert ".mcp.json: mcpServers (proj)" in " ".join(trusted.stdout.split())
    assert "not approved yet" in listed()
    assert mcp("approve", "proj").returncode == 0
    assert "this project; approved)" in listed()
