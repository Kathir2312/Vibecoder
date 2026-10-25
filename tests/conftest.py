"""Shared pytest fixtures.

Every test runs with an isolated config folder and no real API keys, so the
suite never touches a developer's ~/.cmcoder or keychain.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cmcoder.testing.mock_server import MockServer, MockState
from cmcoder.tools.base import ToolContext

API_KEY = "sk-test-key"


@pytest.fixture(autouse=True)
def _isolate_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_CONFIG_DIR", str(tmp_path_factory.mktemp("cmcoder-config")))
    for var in list(os.environ):
        if var.startswith(("CMCODER_", "OPENAI_", "LANGSMITH_", "LANGCHAIN_")) and (
            var != "CMCODER_CONFIG_DIR"
        ):
            monkeypatch.delenv(var)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("CMCODER_SANDBOX", "off")
    missing = tmp_path_factory.mktemp("no-managed") / "managed-settings.json"
    monkeypatch.setattr("cmcoder.config.settings.managed_settings_path", lambda: missing)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    return root


@pytest.fixture
def ctx(project: Path) -> Iterator[ToolContext]:
    yield ToolContext(cwd=project, project_root=project)


def start_mock(script: list[dict[str, Any]], **kw: Any) -> MockServer:
    """A scripted OpenAI-compatible server, started (stop it with __exit__)."""
    server = MockServer(MockState(script, api_key=API_KEY, **kw))
    server.__enter__()
    return server


@pytest.fixture
def mock_server() -> Iterator[Any]:
    """Start scripted OpenAI-compatible servers; all are stopped after the test."""
    servers: list[MockServer] = []

    def make(script: list[dict[str, Any]], **kw: Any) -> MockServer:
        server = start_mock(script, **kw)
        servers.append(server)
        return server

    yield make
    for s in servers:
        s.__exit__(None, None, None)
