"""Start the configured MCP servers and give the agent their tools.

LANGCHAIN CONCEPTS IN THIS FILE

* `langchain_mcp_adapters` turns MCP (Model Context Protocol) servers into
  LangChain `BaseTool`s, so the agent can't tell them from its own tools.
  `MultiServerMCPClient(connections)` knows how to reach each server
  (stdio, Streamable HTTP or SSE). `load_mcp_tools(session, ...)` lists a
  server's tools and wraps each one.
* Sessions: `client.get_tools()` is the quick way, but it opens a new session
  for *every tool call* (a stdio server is a new process each time). An agent
  that calls tools often should keep one session per server open:
  `async with client.session(name) as session` and `load_mcp_tools(session)`.
  We keep each session in a background task for the whole conversation, the
  way cmcoderagent did, because the MCP SDK's context managers must be closed
  by the task that opened them.
* `tool_interceptors`: middleware for MCP tool calls. We use one to put a
  timeout (`toolTimeout`) on every call.

cmcoder's rules on top of that: tools are named `mcp__<server>__<tool>`
(permission rules like `mcp__github` match a whole server); a project's own
servers (`.mcp.json`) need the user's approval before they first start,
because they run with the user's permissions; and managed settings can allow
or deny servers by name.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from langchain_core.tools import BaseTool
from pydantic import ConfigDict

from ..compat import IS_WINDOWS, find_program
from ..config.settings import (
    McpServerConfig,
    Settings,
    approve,
    config_fingerprint,
    is_approved,
)
from ..providers.transport import TransportOptions, build_ssl_context
from ..tools.base import Tool, ToolContext, ToolInput, ToolResult

MAX_DESCRIPTION_CHARS = 2000
NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

Origin = Literal["user", "project"]
Status = Literal["starting", "connected", "failed", "disabled", "not approved", "blocked"]
# Asked before a project's server first starts: (server) -> allowed?
ApproveFn = Callable[["McpServer"], Awaitable[bool]]


def tool_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>`, with characters model APIs reject replaced, max 64."""
    return f"mcp__{NAME_RE.sub('_', server)}__{NAME_RE.sub('_', tool)}"[:64]


def expand(value: str, environ: dict[str, str] | None = None) -> str:
    """`${VAR}` and `${VAR:-default}` from the environment. A missing variable
    without a default is an error (better than connecting with an empty token)."""
    env = os.environ if environ is None else environ

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ValueError(f"environment variable {name} is not set")

    return VAR_RE.sub(repl, value)


def resolve_command(command: str) -> str:
    """The server program's full path, never one planted in the project
    (Windows looks in the current folder first; see compat.find_program)."""
    if os.path.isabs(command) or "/" in command or "\\" in command:
        return command
    found = find_program(command)
    if found is None and IS_WINDOWS:
        for ext in (".cmd", ".bat", ".exe"):  # e.g. npx is npx.cmd
            if found := find_program(command + ext):
                break
    if found is None:
        raise FileNotFoundError(f"{command!r} was not found on PATH")
    return found


@dataclass
class McpServer:
    name: str
    config: McpServerConfig
    origin: Origin
    status: Status = "starting"
    error: str = ""
    tools: list[BaseTool] = field(default_factory=list)
    prompts: list[Any] = field(default_factory=list)  # MCP prompts, offered as /commands
    session: Any = None  # the open ClientSession, while connected
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def key(self) -> str:
        return f"mcp:{self.name}"

    def summary(self) -> str:
        detail = self.error or (f"{len(self.tools)} tools" if self.status == "connected" else "")
        origin = " (project)" if self.origin == "project" else ""
        return f"{self.name}{origin}: {self.status}" + (f" · {detail}" if detail else "")


class McpArgs(ToolInput):
    """Arguments are checked by the server against its own schema."""

    model_config = ConfigDict(extra="allow")


class McpPermissionTool(Tool):
    """How the permission rules see an MCP tool (core/permissions.py works on
    cmcoder `Tool`s). It never runs: the LangChain tool from the adapter does."""

    read_only: ClassVar[bool] = False
    Input = McpArgs

    def __init__(self, name: str, description: str) -> None:
        self.name = name  # type: ignore[misc]
        self.description = description  # type: ignore[misc]

    def describe(self, args: McpArgs, ctx: ToolContext) -> str:
        brief = json.dumps(args.model_dump(), ensure_ascii=False)
        return f"{self.name}({brief if len(brief) <= 60 else brief[:57] + '...'})"

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise RuntimeError("MCP tools run through langchain-mcp-adapters")


class McpManager:
    def __init__(self, settings: Settings, project_root: Path) -> None:
        self.root = project_root
        allowed = settings.allowed_mcp_servers
        denied = set(settings.denied_mcp_servers)
        self.servers: list[McpServer] = []
        for origin, configs in (
            ("user", settings.mcp_servers),
            ("project", settings.project_mcp_servers),
        ):
            for name, cfg in configs.items():
                if any(s.name == name for s in self.servers):
                    continue  # your own definition wins over a project's
                server = McpServer(name, cfg, origin)  # type: ignore[arg-type]
                if cfg.disabled:
                    server.status = "disabled"
                elif name in denied or (allowed is not None and name not in allowed):
                    server.status, server.error = "blocked", "not allowed by your organisation"
                self.servers.append(server)
        self.started = False

    def connected(self) -> list[McpServer]:
        return [s for s in self.servers if s.status == "connected"]

    def langchain_tools(self) -> list[BaseTool]:
        return [t for s in self.connected() for t in s.tools]

    def permission_tools(self) -> list[Tool]:
        return [McpPermissionTool(t.name, t.description) for t in self.langchain_tools()]

    async def start(self, approve_fn: ApproveFn | None) -> list[str]:
        """Start every server (once); returns a warning for each that can't start."""
        if self.started:
            return []
        self.started = True
        pending: list[McpServer] = []
        for s in self.servers:
            if s.status != "starting":
                continue
            if s.origin == "project":
                fp = config_fingerprint(s.config)
                if not is_approved(self.root, s.key, fp):
                    ok = approve_fn is not None and await approve_fn(s)
                    if not ok:
                        s.status = "not approved"
                        s.error = "this project's server was not approved"
                        continue
                    approve(self.root, s.key, fp)
            s._task = asyncio.create_task(self._run(s), name=f"mcp:{s.name}")
            pending.append(s)
        if pending:
            await asyncio.wait(
                [asyncio.create_task(s._ready.wait()) for s in pending],
                timeout=max(s.config.startup_timeout for s in pending) + 1,
            )
        warnings = []
        for s in pending:
            if s.status == "starting":
                s.status, s.error = "failed", "timed out while starting"
            if s.status == "failed":
                warnings.append(f"MCP server {s.name} could not start: {s.error}")
        return warnings

    async def _run(self, s: McpServer) -> None:
        """Owns one server's session for its whole life."""
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools

        try:
            client = MultiServerMCPClient({s.name: self._connection(s)})  # type: ignore[dict-item]
            async with client.session(s.name) as session:
                tools = await asyncio.wait_for(
                    load_mcp_tools(
                        session,
                        server_name=s.name,
                        tool_interceptors=[_timeout(s.config.tool_timeout)],
                    ),
                    s.config.startup_timeout,
                )
                for t in tools:
                    # Our naming, and say where the tool comes from.
                    t.name = tool_name(s.name, t.name)
                    text = (t.description or t.name).strip()
                    t.description = f"[MCP server {s.name}] {text}"[:MAX_DESCRIPTION_CHARS]
                s.tools = tools
                try:  # prompts are optional: servers without them answer with an error
                    s.prompts = list((await session.list_prompts()).prompts)
                except Exception:
                    s.prompts = []
                s.session = session
                s.status = "connected"
                s._ready.set()
                await s._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - anyio raises exception groups
            s.status, s.error = "failed", _describe(e)
        finally:
            s.session = None
            if s.status == "connected":
                s.status = "disabled"
            s._ready.set()

    def _connection(self, s: McpServer) -> dict[str, Any]:
        """The adapter's connection config (a TypedDict) for one server."""
        cfg = s.config
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError("an MCP server needs either `command` or `url`")
            env = {**os.environ, **{k: expand(v) for k, v in cfg.env.items()}}
            return {
                "transport": "stdio",
                "command": resolve_command(expand(cfg.command)),
                "args": [expand(a) for a in cfg.args],
                "env": env,
                "cwd": expand(cfg.cwd) if cfg.cwd else str(self.root),
            }
        if not cfg.url:
            raise ValueError("an MCP server needs either `command` or `url`")
        ssl_ctx = build_ssl_context(TransportOptions(ca_cert_path=cfg.ca_cert_path))

        def factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
            import httpx  # the company CA, as for the model gateway

            return httpx.AsyncClient(
                headers=headers, timeout=timeout, auth=auth, verify=ssl_ctx, trust_env=True
            )

        return {
            "transport": "sse" if cfg.transport == "sse" else "streamable_http",
            "url": expand(cfg.url),
            "headers": {k: expand(v) for k, v in cfg.headers.items()},
            "httpx_client_factory": factory,
        }

    async def close(self) -> None:
        for s in self.servers:
            s._stop.set()
        tasks = [s._task for s in self.servers if s._task is not None]
        if tasks:
            _, still = await asyncio.wait(tasks, timeout=5)
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)


def _timeout(seconds: float) -> Any:
    """A tool interceptor: wraps each MCP tool call, like middleware.

    A timeout or a broken server becomes an MCP *error result* (`isError`),
    which the adapter hands the model as an error ToolMessage; an exception
    would end the whole agent run instead.
    """
    from mcp.types import CallToolResult, TextContent

    def error(text: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)

    async def intercept(request: Any, handler: Any) -> Any:
        try:
            return await asyncio.wait_for(handler(request), seconds)
        except TimeoutError:
            return error(f"The MCP tool timed out after {seconds:.0f}s.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the server's problem, not the agent's
            return error(f"The MCP tool failed: {type(e).__name__}: {e}")

    return intercept


def _describe(e: BaseException) -> str:
    """The first real error inside (possibly nested) exception groups."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    text = str(e) or type(e).__name__
    return text.splitlines()[0][:300]


def status_lines(manager: McpManager | None) -> list[str]:
    """For `/mcp`: each server's state and tools."""
    if manager is None or not manager.servers:
        return ["No MCP servers. Add them under `mcpServers` in settings, or in .mcp.json."]
    lines = [s.summary() for s in manager.servers]
    for s in manager.connected():
        names = ", ".join(t.name for t in s.tools)
        lines.append(f"  {s.name} tools: {names}" if names else f"  {s.name}: no tools")
    return lines


async def prompt_text(server: McpServer, prompt: str, arguments: dict[str, str]) -> str:
    """An MCP prompt as one text: `load_mcp_prompt` returns LangChain messages."""
    from langchain_mcp_adapters.prompts import load_mcp_prompt

    if server.session is None:
        raise RuntimeError(f"MCP server {server.name} is not connected")
    messages = await load_mcp_prompt(server.session, prompt, arguments=arguments)
    parts = [m.text if m.type == "human" else f"[assistant]\n{m.text}" for m in messages]
    return "\n\n".join(p for p in parts if p.strip())
