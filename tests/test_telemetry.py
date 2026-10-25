"""OpenTelemetry metrics (Phase 4 item 4), against a stand-in OTLP/HTTP collector."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cmcoder.config.settings import TelemetryConfig, load_settings, set_project_trust
from cmcoder.telemetry import Telemetry, from_settings, tool_label

from .conftest import API_KEY
from .helpers import make_session

SECRET_PROMPT = "please refactor the zebra-unicorn-4711 module"


class Collector:
    def __init__(self) -> None:
        self.posts: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.status = 200
        collector = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                collector.posts.append((dict(self.headers), json.loads(raw)))
                self.send_response(collector.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def metrics(self) -> dict[str, list[dict[str, Any]]]:
        """The last export: metric name -> data points (attributes as a dict)."""
        _, body = self.posts[-1]
        out: dict[str, list[dict[str, Any]]] = {}
        for rm in body["resourceMetrics"]:
            for sm in rm["scopeMetrics"]:
                for m in sm["metrics"]:
                    for p in m["sum"]["dataPoints"]:
                        attrs = {a["key"]: a["value"]["stringValue"] for a in p["attributes"]}
                        value = float(p.get("asDouble", p.get("asInt", 0)))
                        out.setdefault(m["name"], []).append({**attrs, "value": value})
        return out


@pytest.fixture
def collector() -> Iterator[Collector]:
    c = Collector()
    yield c
    c.httpd.shutdown()
    c.httpd.server_close()


def cfg(**kw: Any) -> TelemetryConfig:
    return TelemetryConfig.model_validate(kw)


async def test_a_session_is_counted_without_its_content(
    mock_server: Any, project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_TOKEN", "t0ken")
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    (project / "a.txt").write_text("hello\n")
    server = mock_server(
        [
            {"tool_calls": [{"name": "Read", "arguments": {"file_path": "a.txt"}}]},
            # A subagent: its model call and its tool are counted too (callbacks
            # reach nested runs).
            {"tool_calls": [{"name": "Task", "arguments": {"description": "d", "prompt": "p"}}]},
            {"tool_calls": [{"name": "Glob", "arguments": {"pattern": "*.txt"}}]},
            {"content": "report"},
            {"content": "Refactored the zebra-unicorn-4711 module.", "cost": 0.25},
        ]
    )
    telemetry = {
        "enabled": True,
        "endpoint": collector.url,
        "headers": {"Authorization": "Bearer ${OTEL_TOKEN}"},
        "resourceAttributes": {"team": "payments"},
    }
    session = make_session(
        server.base_url,
        project,
        settings={"telemetry": telemetry, "permissions": {"allow": ["Glob"]}},
    )
    try:
        events = [e async for e in session.run(SECRET_PROMPT)]
    finally:
        await session.agent.close()  # exports what's left
    assert events[-1].result.startswith("Refactored")

    headers, body = collector.posts[-1]
    assert headers["Authorization"] == "Bearer t0ken"
    assert "zebra" not in json.dumps(body) and "a.txt" not in json.dumps(body)
    resource = {
        a["key"]: a["value"]["stringValue"]
        for a in body["resourceMetrics"][0]["resource"]["attributes"]
    }
    assert resource["service.name"] == "cmcoder" and resource["team"] == "payments"
    assert len(resource["cmcoder.installation.id"]) == 36

    m = collector.metrics()
    assert m["cmcoder.session.count"][0]["frontend"] == "cli"
    tokens = {p["type"]: p["value"] for p in m["cmcoder.token.usage"]}
    assert tokens["input"] > 0 and tokens["output"] > 0
    assert m["cmcoder.cost.usage"][0]["value"] == 0.25
    calls = {(p["tool"], p["result"]): p["value"] for p in m["cmcoder.tool.calls"]}
    assert calls == {("Read", "ok"): 1, ("Task", "ok"): 1, ("Glob", "ok"): 1}
    assert m["cmcoder.turn.count"][0]["result"] == "success"
    assert m["cmcoder.turn.duration"][0]["value"] > 0
    assert {p["session.id"] for p in m["cmcoder.turn.count"]} == {session.session_id}
    assert session.cost == 0.25


async def test_denied_calls_and_errors(
    mock_server: Any, project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server(
        [
            {"tool_calls": [{"name": "Bash", "arguments": {"command": "rm -rf build"}}]},
            {"error": {"status": 401, "message": "bad key"}},
        ]
    )
    session = make_session(
        server.base_url,
        project,
        settings={"telemetry": {"enabled": True, "endpoint": collector.url}},
    )  # nobody to ask: a command that needs approval is denied
    try:
        [e async for e in session.run("clean up")]
    finally:
        await session.agent.close()
    m = collector.metrics()
    assert {(p["tool"], p["result"]) for p in m["cmcoder.tool.calls"]} == {("Bash", "denied")}
    assert [p["kind"] for p in m["cmcoder.api.errors"]] == ["auth"]
    assert m["cmcoder.turn.count"][0]["result"] == "error"


def test_off_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert from_settings(cfg(), environ={}) is None
    assert from_settings(cfg(enabled=True), environ={}) is None  # no endpoint
    env = {
        "CMCODER_TELEMETRY": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel:4318/",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=abc%3D,other=1",
        "OTEL_RESOURCE_ATTRIBUTES": "department=it",
        "OTEL_METRIC_EXPORT_INTERVAL": "30000",
    }
    t = from_settings(cfg(), environ=env)
    assert t is not None
    assert t.url == "http://otel:4318/v1/metrics" and t.headers["x-api-key"] == "abc="
    assert t.resource["department"] == "it" and t.interval == 30


async def test_a_collector_that_fails_never_disturbs(collector: Collector) -> None:
    collector.status = 500
    t = from_settings(cfg(enabled=True, endpoint=collector.url), environ={})
    assert t is not None
    t.add("cmcoder.turn.count", 1, {"model": "m"})
    assert await t.export() is False and t.last_error and "500" in t.last_error
    await t.client.aclose()
    t2 = from_settings(cfg(enabled=True, endpoint="http://127.0.0.1:1"), environ={})
    assert t2 is not None
    t2.add("cmcoder.turn.count", 1, {"model": "m"})
    assert await t2.export() is False
    await t2.close()  # still fine


def test_mcp_tools_are_grouped_by_server() -> None:
    assert tool_label("mcp__github__create_issue") == "mcp__github"
    assert tool_label("Bash") == "Bash"


def test_a_repository_cannot_send_your_metrics_elsewhere(project: Path) -> None:
    (project / ".cmcoder").mkdir()
    (project / ".cmcoder" / "settings.json").write_text(
        '{"telemetry": {"enabled": true, "endpoint": "https://collector.attacker.example"}}'
    )
    s = load_settings(project, environ={})
    assert s.telemetry.endpoint is None
    assert any("telemetry" in x for x in s.ignored_project_settings)
    set_project_trust(project, True)
    assert load_settings(project, environ={}).telemetry.endpoint is not None


def test_turn_time_is_measured_with_a_fine_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows' time.monotonic() ticks every ~15 ms, so a quick turn measured
    # 0 ms (seen on Windows CI); the turn time needs perf_counter.
    import time

    import httpx

    from cmcoder.protocol import events as ev

    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)  # a clock that hasn't ticked
    telemetry = Telemetry("http://collector.invalid", {}, {}, 60.0, httpx.AsyncClient())
    telemetry.turn_started("s")
    sum(range(10_000))  # a quick turn
    telemetry.observe(
        ev.Result(
            subtype="success",
            is_error=False,
            result="",
            num_turns=1,
            duration_ms=1,
            usage={},
            session_id="s",
        ),
        "m",
        "s",
    )
    [ms] = [v for (name, _), v in telemetry.sums.items() if name == "cmcoder.turn.duration"]
    assert ms > 0


@pytest.mark.parametrize(
    ("args", "label"),
    [
        ((), "vscode"),
        (("--client", "netbeans"), "netbeans"),
        (("--client", "jetbrains"), "jetbrains"),  # every IntelliJ-based IDE
    ],
)
async def test_an_ide_session_is_counted_under_its_ide(
    collector: Collector,
    mock_server: Any,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    label: str,
) -> None:
    from .test_stdio import Agent as Panel

    monkeypatch.setenv("CMCODER_TELEMETRY", "1")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    panel = await Panel.start(project, mock_server([]), *args)
    try:
        assert (await panel.next())["type"] == "system_init"
    finally:
        assert await panel.close() == 0  # the session's counts are exported as it ends
    assert collector.metrics()["cmcoder.session.count"][0]["frontend"] == label


@pytest.mark.parametrize(
    "args", [("--client", "netbeans"), ("--protocol", "stdio", "--client", "notepad")]
)
def test_client_goes_with_the_protocol_and_a_known_ide(
    args: tuple[str, ...], project: Path, mock_server: Any
) -> None:
    import os
    import subprocess
    import sys

    env = {**os.environ, "CMCODER_BASE_URL": mock_server([]).base_url, "CMCODER_MODEL": "m"}
    result = subprocess.run(
        [sys.executable, "-m", "cmcoder", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 2
    assert "--client goes with --protocol stdio" in result.stderr
