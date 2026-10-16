"""The command line: `cmcoder -p` against the mock server."""

from __future__ import annotations

import json
import os
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
