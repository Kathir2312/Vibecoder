"""OpenTelemetry metrics (off by default): usage, cost and errors, sent to a
company collector over OTLP/HTTP (JSON encoding, `<endpoint>/v1/metrics`).

    "telemetry": {
      "enabled": true,
      "endpoint": "https://otel.corp.example:4318",
      "headers": {"Authorization": "Bearer ${OTEL_TOKEN}"},
      "resourceAttributes": {"team": "payments", "user.name": "${USERNAME}"}
    }

The standard `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_RESOURCE_ATTRIBUTES` and `OTEL_METRIC_EXPORT_INTERVAL` (ms) work too;
`CMCODER_TELEMETRY=1` switches it on.

Only counts and timings leave the machine: never prompts, replies, code, file
paths or command text. Metrics (cumulative counters, as OTLP "sums"):

    cmcoder.session.count       {frontend}
    cmcoder.token.usage         {model, type: input|output}       tokens
    cmcoder.cost.usage          {model}                          USD (when the gateway says)
    cmcoder.tool.calls          {tool, result: ok|error|denied}
    cmcoder.api.errors          {model, kind}
    cmcoder.compaction.count    {trigger}
    cmcoder.turn.count          {model, result}
    cmcoder.turn.duration       {model}                          ms (sum; with turn.count, an average)

Each point also carries `session.id`. The resource has `service.name`
("cmcoder"), `service.version`, `os.type`, `host.arch`, a random
`cmcoder.installation.id` (stored in ~/.cmcoder), and resourceAttributes.
Exported every 60 s (exportIntervalSeconds) and when a session ends; a
collector that can't be reached never disturbs the session.

Where the numbers come from:

* Model calls and tool runs: `TelemetryCallback`, a LangChain callback
  handler passed in the run's config. Callbacks reach nested runs too, so a
  subagent's (or the team's) model calls and tools are counted as well.
* Turns, their duration, compactions and denied tool calls: the session
  (runtime/session.py), from the events it sends to the UI.

LANGCHAIN CONCEPTS IN THIS FILE

* **Callbacks** (`AsyncCallbackHandler`): LangChain calls `on_chat_model_start`,
  `on_llm_end`, `on_llm_error`, `on_tool_start`, `on_tool_end`,
  `on_tool_error` around every model call and tool run. Pass a handler with
  `graph.astream(..., config={"callbacks": [handler]})` and it sees the whole
  run, nested graphs included. This is how LangSmith tracing works too; ours
  only adds numbers to counters.
* `run_id` / `parent_run_id`: each call has an id, and its start and end
  callbacks carry it; that's how the model name seen at the start is matched
  with the token counts at the end.
* `LLMResult.generations[0][0].message.usage_metadata`: the token counts a
  chat model reports (`input_tokens`, `output_tokens`).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import time
import uuid
from contextlib import suppress
from typing import Any
from uuid import UUID

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult

from . import __version__
from .config.settings import TelemetryConfig, config_dir
from .protocol import events as ev

MCP_PREFIX = "mcp__"


def _expand(value: str) -> str:
    from .mcp.manager import expand  # ${VAR} and ${VAR:-default}, as for MCP servers

    try:
        return expand(value)
    except ValueError:
        return ""


def _pairs(raw: str) -> dict[str, str]:
    """`k=v,k2=v2` (OTEL_* variables; values may be URL-encoded)."""
    from urllib.parse import unquote

    out: dict[str, str] = {}
    for part in raw.split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            out[key.strip()] = unquote(value.strip())
    return out


def installation_id() -> str:
    path = config_dir() / "installation-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return value


def tool_label(name: str) -> str:
    """MCP tools by server only (`mcp__github`): tool names can be numerous."""
    if name.startswith(MCP_PREFIX):
        return MCP_PREFIX + name[len(MCP_PREFIX) :].split("__", 1)[0]
    return name


class Telemetry:
    def __init__(
        self,
        endpoint: str,
        headers: dict[str, str],
        resource: dict[str, str],
        interval: float,
        client: httpx.AsyncClient,
    ) -> None:
        self.url = endpoint.rstrip("/") + "/v1/metrics"
        self.headers = {"Content-Type": "application/json", **headers}
        self.resource = resource
        self.interval = interval
        self.client = client
        self.start_ns = time.time_ns()
        # (metric, frozen attributes) -> value
        self.sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.last_error: str | None = None
        self.exports = 0
        self._task: asyncio.Task[None] | None = None
        self._turn_started: dict[str, float] = {}

    # -- recording ----------------------------------------------------------------

    def add(self, metric: str, value: float, attrs: dict[str, str]) -> None:
        key = (metric, tuple(sorted(attrs.items())))
        self.sums[key] = self.sums.get(key, 0) + value
        self._ensure_running()

    def session_started(self, frontend: str, session_id: str) -> None:
        self.add("cmcoder.session.count", 1, {"frontend": frontend, "session.id": session_id})

    def turn_started(self, session_id: str) -> None:
        self._turn_started[session_id] = time.perf_counter()  # fine on Windows too

    def observe(self, event: ev.Event, model: str, session_id: str) -> None:
        """What the session sees and LangChain doesn't: turns, compactions,
        tool calls a permission rule stopped (the tool never ran)."""

        def attrs(**kw: str) -> dict[str, str]:
            return {**kw, "session.id": session_id}

        if isinstance(event, ev.PermissionDenied):
            self.add("cmcoder.tool.calls", 1, attrs(tool=tool_label(event.name), result="denied"))
        elif isinstance(event, ev.Compacted):
            self.add("cmcoder.compaction.count", 1, attrs(trigger=event.trigger))
        elif isinstance(event, ev.Result):
            self.add("cmcoder.turn.count", 1, attrs(model=model, result=event.subtype))
            started = self._turn_started.pop(session_id, None)
            if started is not None:
                ms = (time.perf_counter() - started) * 1000
                self.add("cmcoder.turn.duration", ms, attrs(model=model))

    def callback(self, session_id: str) -> TelemetryCallback:
        return TelemetryCallback(self, session_id)

    # -- export -------------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        now = time.time_ns()
        metrics: dict[str, list[dict[str, Any]]] = {}
        for (name, attrs), value in self.sums.items():
            point: dict[str, Any] = {
                "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs],
                "startTimeUnixNano": str(self.start_ns),
                "timeUnixNano": str(now),
            }
            if name in ("cmcoder.cost.usage", "cmcoder.turn.duration"):
                point["asDouble"] = float(value)
            else:
                point["asInt"] = str(int(value))
            metrics.setdefault(name, []).append(point)
        units = {
            "cmcoder.token.usage": "tokens",
            "cmcoder.cost.usage": "USD",
            "cmcoder.turn.duration": "ms",
        }
        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": k, "value": {"stringValue": v}}
                            for k, v in self.resource.items()
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "cmcoder", "version": __version__},
                            "metrics": [
                                {
                                    "name": name,
                                    "unit": units.get(name, "1"),
                                    "sum": {
                                        "dataPoints": points,
                                        "aggregationTemporality": 2,  # cumulative
                                        "isMonotonic": True,
                                    },
                                }
                                for name, points in sorted(metrics.items())
                            ],
                        }
                    ],
                }
            ]
        }

    async def export(self, timeout: float = 10.0) -> bool:
        """Send everything counted so far (cumulative). Never raises."""
        if not self.sums:
            return True
        try:
            resp = await self.client.post(
                self.url, content=json.dumps(self.payload()), headers=self.headers, timeout=timeout
            )
        except httpx.HTTPError as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        if resp.status_code >= 300:
            self.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return False
        self.last_error = None
        self.exports += 1
        return True

    async def check(self) -> str | None:
        """Send an empty export (no metrics): None if the collector accepted it."""
        try:
            resp = await self.client.post(
                self.url, content=b'{"resourceMetrics": []}', headers=self.headers, timeout=10
            )
        except httpx.HTTPError as e:
            return f"{type(e).__name__}: {e}"
        return None if resp.status_code < 300 else f"HTTP {resp.status_code}: {resp.text[:200]}"

    def _ensure_running(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # nothing to schedule on; close() exports the rest
        self._task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.export()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self.export(timeout=3.0)
        await self.client.aclose()


def from_settings(cfg: TelemetryConfig, environ: dict[str, str] | None = None) -> Telemetry | None:
    """The session's telemetry, or None when it's off or has no endpoint."""
    from .providers.transport import TransportOptions, build_client

    env = os.environ if environ is None else environ
    enabled = cfg.enabled or env.get("CMCODER_TELEMETRY", "").lower() in ("1", "true", "on")
    endpoint = cfg.endpoint or env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not enabled or not endpoint:
        return None
    headers = _pairs(env.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
    headers.update({k: _expand(v) for k, v in cfg.headers.items()})
    resource = {
        "service.name": "cmcoder",
        "service.version": __version__,
        "os.type": platform.system().lower(),
        "host.arch": platform.machine().lower(),
        "cmcoder.installation.id": installation_id(),
    }
    resource.update(_pairs(env.get("OTEL_RESOURCE_ATTRIBUTES", "")))
    resource.update({k: _expand(v) for k, v in cfg.resource_attributes.items()})
    interval = cfg.export_interval_seconds
    if env.get("OTEL_METRIC_EXPORT_INTERVAL", "").isdigit():
        interval = int(env["OTEL_METRIC_EXPORT_INTERVAL"]) / 1000
    client = build_client(TransportOptions(ca_cert_path=cfg.ca_cert_path, read_timeout=15))
    return Telemetry(endpoint, headers, resource, max(interval, 5.0), client)


class TelemetryCallback(AsyncCallbackHandler):
    """Counts model calls and tool runs as LangChain reports them."""

    def __init__(self, telemetry: Telemetry, session_id: str) -> None:
        self.telemetry = telemetry
        self.session_id = session_id
        self._models: dict[UUID, str] = {}  # run id -> model name, from the start
        self._tools: dict[UUID, str] = {}

    def _attrs(self, **kw: str) -> dict[str, str]:
        return {**kw, "session.id": self.session_id}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # LangChain puts the provider's model name in the metadata (`ls_model_name`).
        self._models[run_id] = str((metadata or {}).get("ls_model_name") or "unknown")

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        model = self._models.pop(run_id, "unknown")
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                cost = (getattr(message, "response_metadata", None) or {}).get("cost")
                if cost:  # LiteLLM's cost header (models/chat.py)
                    self.telemetry.add("cmcoder.cost.usage", cost, self._attrs(model=model))
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                add = self.telemetry.add
                add(
                    "cmcoder.token.usage",
                    usage["input_tokens"],
                    self._attrs(model=model, type="input"),
                )
                add(
                    "cmcoder.token.usage",
                    usage["output_tokens"],
                    self._attrs(model=model, type="output"),
                )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        from .models.errors import classify

        model = self._models.pop(run_id, "unknown")
        kind = classify(error).kind if isinstance(error, Exception) else "cancelled"
        self.telemetry.add("cmcoder.api.errors", 1, self._attrs(model=model, kind=kind))

    async def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._tools[run_id] = str((serialized or {}).get("name") or kwargs.get("name") or "?")

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        name = self._tools.pop(run_id, "?")
        error = isinstance(output, ToolMessage) and output.status == "error"
        self._tool(name, "error" if error else "ok")

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._tool(self._tools.pop(run_id, "?"), "error")

    def _tool(self, name: str, result: str) -> None:
        self.telemetry.add(
            "cmcoder.tool.calls", 1, self._attrs(tool=tool_label(name), result=result)
        )
