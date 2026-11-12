"""`cmcoder --protocol stdio`: the Agent Protocol over stdin/stdout, for IDEs.

A long-lived process for one conversation, used by the VS Code, Visual
Studio, Eclipse, NetBeans and JetBrains plugins. The client writes one JSON
message per line (protocol/messages.py); the agent writes one JSON event per
line (protocol/events.py). Nothing else ever goes to stdout: stray prints go
to stderr. The protocol is the same one cmcoderagent speaks, so the IDE
plugins work unchanged on the LangGraph engine.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* **Driving a compiled graph from a long-lived server**: each user message
  is one `astream` run on the same `thread_id`; the checkpointer carries the
  conversation from run to run.
* **Interrupts across processes' time**: a run that hit `interrupt()` is
  resumed by a *later* client message (`Command(resume=...)`), not by a
  function call returning.
* **When not to use an interrupt**: quick IDE tool calls just await the
  client's answer inside the tool.
* **Rebuilding a graph on the same checkpointer**: `create_agent` compiles a
  fixed model and tool list; a new graph on the same thread continues the
  conversation.

HOW THE PIECES MEET

* A turn runs as an asyncio task (`Session.run`, a LangGraph stream), while
  this server keeps reading client messages. That is what lets an
  `interrupt` or a `permission_response` arrive in the middle of a turn.
* Permission prompts are LangGraph interrupts (middleware/permission.py).
  `Session.run` yields a `permission_request` event, which we send to the
  client, and then awaits `ask()`. `ask()` waits for the client's
  `permission_response` with the same `request_id` (the interrupt's id), and
  the session resumes the graph with `Command(resume=...)`.
* IDE tools (`getDiagnostics`, `openFile`) are not interrupts: they are quick
  calls the editor answers right away, so the tool simply awaits the
  client's `ide_tool_result`. An interrupt is for an answer that may take
  long or must survive a restart: a person's decision.
* Switching the model or adding IDE tools builds a new graph (`create_agent`
  compiles a fixed set of tools and one model) on the *same* checkpointer
  and thread, so the conversation carries on.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import ValidationError

from ..agents.main import Agent, AgentOptions, build_agent, prepare_agent, rebuild_agent
from ..agents.subagents import agent_infos
from ..config.settings import Settings, SettingsError, add_local_allow_rule, find_project_root
from ..core.hooks import Hook
from ..core.ide import IDE_TOOLS, format_ide_context
from ..core.permissions import MODES, Decision, ModeNotAllowed
from ..ide_browser import IdeToolSet
from ..images import Image, ImageError, from_base64
from ..mcp import McpManager
from ..mcp.manager import McpServer
from ..middleware.compaction import CompactionBlocked
from ..protocol import events as ev
from ..protocol import messages as msg
from ..runtime.commands import BUILT_IN, run_command
from ..runtime.session import Session
from ..runtime.store import SessionStore
from ..tools.base import Tool, ToolResult
from ..tools.files import ReadInput, ReadTool

IDE_TOOL_TIMEOUT = 30.0  # seconds
MAX_IDE_IMAGES = 3  # pictures kept from one IDE tool result
MAX_IDE_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_DATA_URL = re.compile(
    r"data:image/(?P<kind>png|jpeg|gif|webp);base64,(?P<data>[A-Za-z0-9+/]+={0,2})"
)
# Built-in commands that work in an IDE panel; the others (/clear, /resume,
# /mode) are the panel's own buttons.
PANEL_COMMANDS = {
    "help", "compact", "rewind", "model", "turns", "cost", "todos", "context", "mcp", "agents",
    "critic", "index", "memory", "pick", "retry", "revert", "verify", "audit",
}  # fmt: skip
# Commands that normally send a prompt to the model; when they only have a message
# (their usage, "it is off", "no changes"), the panel just shows it.
PROMPT_COMMANDS = ("team", "init", "review", "commit", "pr", "webtest", "playwright")
_READ = ReadTool()


class StdioServer:
    def __init__(
        self,
        settings: Settings,
        opts: AgentOptions,
        out: BinaryIO,
        *,
        continue_session: bool = False,
        resume: str | None = None,
    ) -> None:
        self.settings = settings
        self.side_tasks: set[asyncio.Task[Any]] = set()  # indexing, beside the turn
        self.opts = opts
        self.out = out
        self.continue_session = continue_session
        self.resume = resume
        self.session: Session | None = None
        self.store: SessionStore | None = None
        self.turn: asyncio.Task[None] | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.ide_pending: dict[str, asyncio.Future[ToolResult]] = {}
        self.ide_tools: list[Tool] = []
        self.ide_browser: IdeToolSet | None = None  # the IDE's own tools (VS Code's browser)
        self.ide_shots: Path | None = None  # their screenshots, until this process ends
        self.ide_dirty = False  # the IDE's tools changed during a turn: rebuild before the next
        self.mcp: McpManager | None = None
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()

    def emit(self, event: ev.Event) -> None:
        self.out.write(event.model_dump_json().encode() + b"\n")
        self.out.flush()

    def error(self, kind: str, message: str, hint: str | None = None) -> None:
        self.emit(ev.Error(kind=kind, message=message, hint=hint))

    @property
    def agent(self) -> Agent:
        assert self.session is not None
        return self.session.agent

    # --- input -------------------------------------------------------------------
    def _start_reader(self, stdin_fd: int) -> None:
        """Read stdin in a thread: portable (Windows pipes don't work with
        asyncio's stdin readers) and a daemon, so a blocked read never holds
        up exit. It reads the raw file descriptor with `os.read`: a daemon
        thread blocked in a *buffered* read holds the buffer's lock, and Python
        aborts if it is still held at exit."""
        loop = asyncio.get_running_loop()

        def put(line: bytes) -> None:
            with suppress(RuntimeError):  # the loop already closed: we're exiting
                loop.call_soon_threadsafe(self.inbox.put_nowait, line)

        def read() -> None:
            buffer = b""
            while True:
                try:
                    chunk = os.read(stdin_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    put(line)
            if buffer:
                put(buffer)
            put(b"")  # EOF

        threading.Thread(target=read, name="cmcoder-stdin", daemon=True).start()

    # --- main loop -----------------------------------------------------------------
    async def serve(self, stdin_fd: int) -> int:
        root = find_project_root(self.opts.cwd.resolve())
        self.opts.interactive = True

        def save_rule(rule: str) -> None:  # "always allow" answers go to settings.local.json
            add_local_allow_rule(root, rule)

        self.opts.on_rule_saved = save_rule
        self.opts.approve_hook = self._approve_hook
        async with SessionStore.open(root, persist=self.settings.persist_sessions) as store:
            self.store = store
            try:
                self.mcp = McpManager(self.settings, root)
                await prepare_agent(self.settings, self.opts)
                agent = build_agent(
                    self.settings, self.opts, checkpointer=store.checkpointer, mcp=self.mcp
                )
            except (SettingsError, ValueError) as e:
                hint = "In a terminal, run:  cmcoder setup" if "cmcoder setup" in str(e) else None
                self.error("startup", str(e), hint)
                return 2
            self.session = Session(agent, ask=self.ask, store=store)
            if self.resume or self.continue_session:
                found = await (store.find(self.resume) if self.resume else store.latest())
                if found is not None:
                    await self.session.resume(found.thread_id)
            if sys.platform == "win32":
                # On Windows, loading numpy's C extension while the reader thread
                # is blocked reading the stdin pipe hangs for good (DLL loading
                # waits on the pipe). Code search needs numpy, so load it first.
                import numpy  # noqa: F401  # pyright: ignore[reportUnusedImport]
            self._start_reader(stdin_fd)
            init = self.session.init_event()
            init.critique = bool(agent.critic and agent.critic.enabled)
            self.emit(init)
            if self.session.turn:  # a resumed conversation: show it again
                self.emit(ev.History(messages=await self.session.history()))
                if todos := await self.session.todos():
                    self.emit(ev.TodoUpdate(todos=todos))
            self.emit(
                self._agent_list()
            )  # (after those: clients read init, history, todos in order)
            try:
                while True:
                    line = await self.inbox.get()
                    if not line:
                        break  # EOF: the client went away
                    if not line.strip():
                        continue
                    try:
                        message = msg.parse_message(line)
                    except ValidationError as e:
                        self.error("protocol", f"Invalid message: {_first_error(e)}")
                        if _is_user_message(line):  # the client waits for this turn's result
                            self.emit(
                                self._result("error", "Invalid message.", True, time.monotonic())
                            )
                        continue
                    if isinstance(message, msg.Shutdown):
                        break
                    await self.handle(message)
            finally:
                await self._stop_turn()
                for task in list(self.side_tasks):  # indexing: what's done is saved
                    task.cancel()
                await asyncio.gather(*self.side_tasks, return_exceptions=True)
                await self.session.close()
                await self.agent.close()
                await self.mcp.close()
                if self.ide_shots is not None:
                    shutil.rmtree(self.ide_shots, ignore_errors=True)
        return 0

    async def handle(self, message: msg.ClientMessage) -> None:
        session = self.session
        assert session is not None
        if isinstance(message, msg.UserMessage):
            if self.busy:
                self.error("busy", "A turn is already running; interrupt it first.")
                return
            note = (
                format_ide_context(message.context, self.agent.ctx.project_root, self._withheld)
                if message.context
                else None
            )
            try:
                images = [
                    await asyncio.to_thread(from_base64, a.data, a.name or "")
                    for a in message.images
                ]
            except ImageError as e:
                self.error("images", str(e))
                self.emit(self._result("error", str(e), True, time.monotonic()))
                return
            self.turn = asyncio.create_task(self._run_turn(message.text, note, images))
        elif isinstance(message, msg.Interrupt):
            await self._stop_turn()
        elif isinstance(message, msg.PermissionResponse):
            future = self.pending.pop(message.request_id, None)
            if future is None or future.done():
                self.error("protocol", f"No pending permission request {message.request_id!r}.")
                return
            future.set_result(
                {
                    "allow": message.allow,
                    "remember": message.remember,
                    "remember_session": message.remember_session,
                    "feedback": message.feedback,
                }
            )
        elif isinstance(message, msg.StopTool):
            from ..tools.progress import stop as stop_tool

            if not stop_tool(message.tool_use_id):  # finished already, or not stoppable
                self.emit(ev.Warning(message="That command is no longer running."))
        elif isinstance(message, msg.SetMode):
            if message.mode not in MODES:
                modes = ", ".join(self.agent.policy.available_modes())
                self.error(
                    "invalid_mode", f"Unknown mode {message.mode!r}. Choose one of: {modes}."
                )
                return
            try:
                self.agent.policy.mode = message.mode
            except ModeNotAllowed as e:
                self.error("invalid_mode", str(e))
                return
            self.emit(ev.ModeChanged(mode=self.agent.policy.mode))
        elif isinstance(message, msg.SetAgent):
            await self._set_agent(message.agent)
        elif isinstance(message, msg.SetModel):
            if self.busy:
                self.error("busy", "Can't switch models during a turn.")
                return
            try:
                await self._rebuild(model=message.model)
            except (SettingsError, ValueError) as e:
                self.error("invalid_model", str(e))
                return
            b = self.agent.bundle
            self.emit(ev.ModelChanged(model=b.name, context_window=b.profile.context_window))
        elif isinstance(message, msg.IdeCapabilities):
            self.ide_tools = [IDE_TOOLS[n](self.ide_call) for n in message.tools if n in IDE_TOOLS]
            self.ide_browser = IdeToolSet(message.tool_defs, self.ide_call) or None
            if self.busy:
                self.ide_dirty = True  # a new graph in the middle of a turn: not now
            else:
                await self._rebuild()
        elif isinstance(message, msg.IdeToolResult):
            pending = self.ide_pending.pop(message.request_id, None)
            if pending is None or pending.done():
                self.error("protocol", f"No pending IDE tool request {message.request_id!r}.")
                return
            pending.set_result(
                ToolResult(
                    message.content, is_error=message.is_error, images=self._save_images(message)
                )
            )
        elif isinstance(message, msg.ListSessions):
            assert self.store is not None
            sessions = [
                ev.SessionSummary(
                    id=s.thread_id, title=s.title, updated=s.updated, messages=s.turns
                )
                for s in await self.store.list()
                if s.thread_id != session.session_id
            ]
            self.emit(ev.SessionList(sessions=sessions))
        elif isinstance(message, msg.Rewind):
            await self._rewind(message)
        elif isinstance(message, msg.ListCommands):
            self.emit(ev.CommandList(commands=command_list()))
        elif isinstance(message, msg.Index):
            self._side(self._index(message.action))
        elif isinstance(message, msg.RagCandidates):
            self._side(
                self._rag_candidates(),
                lambda e: ev.RagCandidatesList(likely=[], other=[], errors={"cmcoder": e}),
            )
        elif isinstance(message, msg.RagSetup):
            self._side(self._rag_setup(message), lambda e: ev.RagSetupResult(ok=False, message=e))
        elif isinstance(message, msg.SetCritique):
            if self.agent.critic is not None:
                self.agent.critic.enabled = message.enabled
                self.opts.critic = message.enabled  # kept when the graph is rebuilt
            self.emit(
                ev.CritiqueChanged(enabled=bool(self.agent.critic and self.agent.critic.enabled))
            )
        elif isinstance(message, msg.StopSubagent):
            # The turn and the other subagents go on; this one writes its report
            # at its next model call (agents/subagents.py StopRequestMiddleware).
            rt = self.agent.subagents
            run = rt.stop(message.id) if rt is not None else None
            if run is None:
                self.error("not_running", f"No running subagent {message.id!r}.")
            else:
                self.emit(run.status())  # "stopping" (or "queued" until it's dropped)

    def _agent_list(self) -> ev.AgentList:
        """The agents the UI can offer, and the one in use."""
        return ev.AgentList(
            agents=agent_infos(self.agent.ctx.project_root, self.settings.project_trusted),
            current=self.agent.persona,
        )

    async def _set_agent(self, name: str | None) -> None:
        """Talk to this agent from now on (None: the default one): a new graph with its
        instructions, tools and model, on the same conversation."""
        if self.busy:
            self.error("busy", "Can't switch agents during a turn.")
            return
        if name is not None:
            infos = {i.name: i for i in self._agent_list().agents}
            info = infos.get(name)
            if info is None:
                self.error(
                    "invalid_agent",
                    f"No agent named {name!r}. Choose one of: {', '.join(infos) or 'none'}.",
                )
                return
            if not info.usable:
                self.error("invalid_agent", f"{name}: {info.hint}")
                return
        try:
            await self._rebuild(agent=name)
        except (SettingsError, ValueError) as e:
            self.error("invalid_agent", str(e))
            return
        b = self.agent.bundle
        self.emit(ev.AgentChanged(agent=self.agent.persona))
        self.emit(ev.ModelChanged(model=b.name, context_window=b.profile.context_window))
        self.emit(self._agent_list())

    async def _switch_for_step(self, name: str) -> bool:
        """A command's request runs as this agent (`/playwright plan` as the planner): the list in
        the chat shows it. False if it can't (then the request runs as the agent in use)."""
        try:
            await self._rebuild(agent=name)
        except (SettingsError, ValueError):
            return False
        self.emit(ev.AgentChanged(agent=self.agent.persona, reason="step"))
        self.emit(self._agent_list())
        return True

    async def _switch_back(self, name: str | None) -> None:
        """The agent from before the step is back (quietly: the chat only moves its list)."""
        try:
            await self._rebuild(agent=name)
        except (SettingsError, ValueError):
            await self._rebuild(agent=None)
        self.emit(ev.AgentChanged(agent=self.agent.persona, reason="restore"))
        self.emit(self._agent_list())

    async def _rebuild(self, model: str | None = None, **more: Any) -> None:
        """A new graph (another model, IDE tools, MCP tools, a code index) on the
        same checkpointer and thread."""
        assert self.session is not None
        changes: dict[str, Any] = {
            "extra_tools": self.ide_tools,
            "mcp": self.mcp,
            "ide_browser": self.ide_browser,
            **more,
        }
        if model:
            changes["model"] = model
            self.opts.model = model
        if "agent" in more:
            self.opts.agent = more["agent"]
        self.session.agent = await rebuild_agent(self.agent, **changes)

    async def _start_mcp(self) -> None:
        """MCP servers start with the first message, as in cmcoderagent; a
        project's own servers ask first (through the permission prompt)."""
        if self.ide_dirty:
            self.ide_dirty = False
            await self._rebuild()
        if self.mcp is None or self.mcp.started:
            return
        for warning in await self.mcp.start(self._approve_server):
            self.emit(ev.Warning(message=warning))
        if self.mcp.connected():
            await self._rebuild()

    async def _approve_hook(self, hook: Hook) -> bool:
        request = ev.PermissionRequest(
            request_id=f"hook-{hook.event}-{uuid.uuid4().hex[:6]}",
            tool_use_id=f"hook-{hook.event}",
            name="Hook",
            label=f"Run this project's {hook.event} hook",
            input={
                "event": hook.event,
                "matcher": hook.matcher or "*",
                "command": hook.command.command,
            },
            suggested_rule="",
            reason=f"This project's settings want to run a command on every {hook.event} "
            "event; it runs with your permissions. Allowing is remembered until the command changes.",
            can_remember=False,
        )
        self.emit(request)
        return bool((await self.ask(request)).get("allow"))

    async def _approve_server(self, server: McpServer) -> bool:
        request = ev.PermissionRequest(
            request_id=f"mcp-{server.name}-{uuid.uuid4().hex[:6]}",
            tool_use_id=f"mcp-{server.name}",
            name="McpServer",
            label=f"Start MCP server {server.name}",
            input={"server": server.name, **server.config.approval_details()},
            suggested_rule="",
            reason="This project's settings (.mcp.json) want to start an MCP server; it runs "
            "with your permissions. Allowing is remembered until its settings change.",
            can_remember=False,
        )
        self.emit(request)
        return bool((await self.ask(request)).get("allow"))

    def _withheld(self, path: str) -> bool:
        """Files whose contents Read would refuse (secrets, deny rules)."""
        try:
            args = ReadInput.model_validate({"file_path": path})
            return self.agent.policy.check(_READ, args, self.agent.ctx).decision == Decision.DENY
        except (ValueError, OSError):
            return True

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    def _result(self, subtype: str, text: str, is_error: bool, started: float) -> ev.Result:
        assert self.session is not None
        return ev.Result(
            subtype=subtype,  # type: ignore[arg-type]
            is_error=is_error,
            result=text,
            num_turns=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            usage={},
            session_id=self.session.session_id,
        )

    async def _run_turn(self, text: str, note: str | None, images: list[Image]) -> None:
        session = self.session
        assert session is not None
        started = time.monotonic()
        try:
            name = text[1:].split(" ", 1)[0] if text.startswith("/") else ""
            if name == "compact":
                arg = text.partition(" ")[2].strip()
                try:
                    if compacted := await session.compact(arg or None):
                        self.emit(compacted)
                except CompactionBlocked as e:
                    self.emit(
                        ev.Warning(message=f"Not compacted: a PreCompact hook declined ({e}).")
                    )
                self.emit(self._result("success", "", False, started))
            elif name == "retry":  # the failed step again, from the graph's checkpoint
                async for event in session.retry():
                    self.emit(event)
            elif name in PANEL_COMMANDS:  # answered here, not sent to the model
                if name == "index":  # with progress, and the status bar after it
                    from ..rag.session import index_command

                    arg = text.partition(" ")[2].strip()
                    lines = await index_command(session, arg, self._progress)
                    body = "\n".join(lines)
                    self.emit(ev.AssistantMessage(text=f"**Code index**\n\n```text\n{body}\n```"))
                    self._side(self._index("status"))
                elif name == "rewind" and not text.partition(" ")[2].strip():
                    points = await session.rewind_points()
                    self.emit(ev.RewindPoints(points=points[-20:]))
                else:
                    result = await run_command(
                        session,
                        text,
                        switch_model=self._switch_model,
                        set_max_turns=self._set_max_turns,
                    )
                    body = "\n".join(result.lines)
                    if not (result.quiet and result.events):  # a quiet one is shown by its events
                        self.emit(ev.AssistantMessage(text=f"```text\n{body}\n```"))
                    for event in result.events:
                        self.emit(event)
                self.emit(self._result("success", "", False, started))
            else:
                await self._start_mcp()
                allow: list[str] = []
                step_agent: str | None = None
                if name:  # maybe a custom command or an MCP prompt; else a plain prompt
                    result = await run_command(session, text)
                    if result.send is not None:
                        text, allow, step_agent = result.send, result.allow, result.agent
                    elif name in PROMPT_COMMANDS:  # its usage or "it is off": no model turn
                        self.emit(ev.AssistantMessage(text="\n".join(result.lines)))
                        self.emit(self._result("success", "", False, started))
                        return
                before = self.agent.persona
                switched = False
                if step_agent is not None and step_agent != before:
                    switched = await self._switch_for_step(step_agent)
                held: ev.Result | None = None
                try:
                    async for event in session.run(text, note=note, images=images, allow=allow):
                        if isinstance(event, ev.Result) and switched:
                            held = event  # after the agent is back: the chat is free again then
                        else:
                            self.emit(event)
                finally:
                    if switched:
                        await asyncio.shield(self._switch_back(before))
                if held is not None:
                    self.emit(held)
        except asyncio.CancelledError:
            self.emit(self._result("interrupted", "Interrupted by the user.", False, started))
            raise
        except Exception as e:  # a bug: report it instead of dying silently
            self.error("internal", f"{type(e).__name__}: {e}")
            self.emit(self._result("error", str(e), True, started))

    # --- code search (VS Code's status bar and setup) ------------------------------

    def _side(self, job: Any, reply: Callable[[str], ev.Event] | None = None) -> None:
        """Run a job beside the conversation (indexing can take a while).

        A job that fails unexpectedly still answers (`reply`, else a warning):
        the client waits for an answer to its request."""

        async def run() -> None:
            try:
                await job
            except Exception as e:  # never take the session down, never leave it waiting
                text = f"{type(e).__name__}: {e}"
                self.emit(reply(text) if reply else ev.Warning(message=text))

        task = asyncio.get_running_loop().create_task(run())
        self.side_tasks.add(task)
        task.add_done_callback(self.side_tasks.discard)

    def _progress(self, p: Any) -> None:
        self.emit(ev.IndexProgress(done=p.done, total=p.total, chunks=p.chunks))

    async def _index_status(self) -> ev.IndexStatus:
        from ..rag.common import EmbeddingError, StoreError
        from ..rag.index import open_index
        from ..rag.setup import status_lines

        cfg = self.settings.rag
        if cfg.enabled is False or not cfg.embedding_model:
            return ev.IndexStatus(set_up=False, active=False)
        index = self.agent.code_index
        attached = index is not None
        if index is None:
            try:
                index = open_index(self.settings, self.agent.ctx.project_root)
            except (SettingsError, StoreError) as e:
                return ev.IndexStatus(set_up=True, active=False, error=str(e))
            if index is None:
                return ev.IndexStatus(set_up=False, active=False)
        try:
            st = await index.status()
            lines = await status_lines(index)
        except (StoreError, EmbeddingError) as e:
            return ev.IndexStatus(set_up=True, active=attached, error=str(e))
        finally:
            if not attached:
                await index.close()
        return ev.IndexStatus(
            set_up=True,
            active=attached,
            model=st.model,
            store=st.store,
            files=st.files,
            chunks=st.chunks,
            updated=st.updated,
            read_only=st.read_only,
            updating=index.updating,
            error=index.last_error,
            lines=lines,
        )

    async def _index(self, action: str) -> None:
        from ..rag.session import index_command

        try:
            if action != "status":
                assert self.session is not None
                lines = await index_command(self.session, action, self._progress)
                text = "\n".join(lines)
                if text.startswith(("Indexing failed", "Code search", "This index")):
                    self.emit(ev.Warning(message=text))
            self.emit(await self._index_status())
        except Exception as e:  # never take the session down
            self.emit(ev.Warning(message=f"Code index: {type(e).__name__}: {e}"))

    async def _rag_candidates(self) -> None:
        from ..rag.setup import embedding_candidates

        likely, other, errors = await embedding_candidates(self.settings)
        self.emit(ev.RagCandidatesList(likely=likely, other=other, errors=errors))

    async def _rag_setup(self, m: msg.RagSetup) -> None:
        """Set up code search as `cmcoder rag setup` does, and use it now."""
        from ..config.settings import RagConfig
        from ..rag.common import EmbeddingError, StoreError
        from ..rag.session import index_command
        from ..rag.setup import (
            SetupChoice,
            apply_setup,
            check_embedding_model,
            check_store,
            rag_block,
        )

        choice = SetupChoice(m.embedding_model, m.store, m.url, m.api_key, m.scope, m.read_only)
        try:
            ref, dim = await check_embedding_model(self.settings, m.embedding_model)
            choice.embedding_model = ref
            where = await check_store(choice)
        except (EmbeddingError, SettingsError, StoreError) as e:
            self.emit(ev.RagSetupResult(ok=False, message=str(e)))
            return
        path = apply_setup(choice, self.agent.ctx.project_root)
        # The session uses what was just chosen (a project's store would wait for trust).
        self.settings.rag = RagConfig.model_validate(
            {**self.settings.rag.model_dump(by_alias=True), **rag_block(choice)}
        )
        if self.agent.code_index is not None:  # another model or store: start again
            await self._rebuild(code_index=None)
        message = f"Code search uses {ref} ({dim} dimensions), kept in {where}; saved in {path}."
        if m.index_now and not m.read_only:
            assert self.session is not None
            lines = await index_command(self.session, "update", self._progress)
            message += "\n" + "\n".join(lines)
        elif m.read_only:
            await self._rebuild(code_index=None)  # opens the shared index, if it has one
        self.emit(ev.RagSetupResult(ok=True, message=message, settings_file=str(path)))
        self.emit(await self._index_status())

    async def _switch_model(self, ref: str) -> None:
        await self._rebuild(model=ref)
        b = self.agent.bundle
        self.emit(ev.ModelChanged(model=b.name, context_window=b.profile.context_window))

    async def _set_max_turns(self, n: int) -> None:
        self.opts.max_turns = n  # a later rebuild (an agent, MCP tools) keeps it
        await self._rebuild(max_turns=n)

    async def _stop_turn(self) -> None:
        for future in [*self.pending.values(), *self.ide_pending.values()]:
            future.cancel()
        self.pending.clear()
        self.ide_pending.clear()
        if self.busy:
            assert self.turn is not None
            self.turn.cancel()
            with suppress(asyncio.CancelledError):
                await self.turn

    async def ask(self, req: ev.PermissionRequest) -> dict[str, Any]:
        """The session's permission callback: wait for the client's answer.

        The session has already emitted `req`; its request_id is the
        LangGraph interrupt's id."""
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[req.request_id] = future
        try:
            answer = await future
        finally:
            self.pending.pop(req.request_id, None)
        if (answer.get("remember") or answer.get("remember_session")) and not req.can_remember:
            answer["remember"] = answer["remember_session"] = False
        return answer

    def _save_images(self, message: msg.IdeToolResult) -> list[Path]:
        """The pictures an IDE tool returned (`data:image/...;base64,...`), as files the chat
        can show: only real image types, a few, none of them huge."""
        paths: list[Path] = []
        for url in message.images[:MAX_IDE_IMAGES]:
            m = IMAGE_DATA_URL.fullmatch(url)
            if m is None:
                continue
            try:
                data = base64.b64decode(m["data"], validate=True)
            except ValueError:
                continue
            if not data or len(data) > MAX_IDE_IMAGE_BYTES:
                continue
            if self.ide_shots is None:
                self.ide_shots = Path(tempfile.mkdtemp(prefix="cmcoder-ide-"))
            path = (
                self.ide_shots / f"shot-{uuid.uuid4().hex[:8]}.{m['kind'].replace('jpeg', 'jpg')}"
            )
            path.write_bytes(data)
            paths.append(path)
        return paths

    async def ide_call(self, name: str, data: dict[str, Any]) -> ToolResult:
        """Run an IDE tool in the client and wait for its answer."""
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self.ide_pending[request_id] = future
        self.emit(ev.IdeToolRequest(request_id=request_id, name=name, input=data))
        try:
            return await asyncio.wait_for(future, IDE_TOOL_TIMEOUT)
        except TimeoutError:
            return ToolResult(f"The editor did not answer {name} in time.", is_error=True)
        finally:
            self.ide_pending.pop(request_id, None)

    async def _rewind(self, m: msg.Rewind) -> None:
        session = self.session
        assert session is not None
        if self.busy:
            self.error("busy", "Can't rewind during a turn; stop it first.")
            return
        if all(p.turn != m.turn for p in await session.rewind_points()):
            self.error("protocol", f"No message {m.turn} to rewind to.")
            return
        self.emit(
            await session.rewind(
                m.turn, code=m.code, conversation=m.conversation, outside=m.outside
            )
        )
        if m.conversation:
            self.emit(ev.History(messages=await session.history()))
            self.emit(ev.TodoUpdate(todos=await session.todos()))


def command_list() -> list[ev.CommandInfo]:
    """The built-in commands an IDE's chat offers when "/" is typed: the ones answered here and the
    ones that send a request to the model (/webtest, /review, /init, /team, /playwright)."""
    return [
        ev.CommandInfo(name=n, description=d, argument_hint=h, origin="built-in")
        for n, h, d in BUILT_IN
        if n in PANEL_COMMANDS or n in PROMPT_COMMANDS
    ]


def _is_user_message(line: str | bytes) -> bool:
    try:
        data = json.loads(line)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") == "user_message"


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(x) for x in err.get("loc", ()))
    return f"{where}: {err['msg']}" if where else err["msg"]


async def run_stdio(
    settings: Settings,
    opts: AgentOptions,
    *,
    continue_session: bool = False,
    resume: str | None = None,
) -> int:
    out = sys.stdout.buffer
    # Only protocol events may reach stdout; anything else printed goes to stderr.
    sys.stdout = sys.stderr
    server = StdioServer(settings, opts, out, continue_session=continue_session, resume=resume)
    return await server.serve(sys.stdin.fileno())
