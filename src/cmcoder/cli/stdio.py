"""`cmcoder --protocol stdio`: the Agent Protocol over stdin/stdout, for IDEs.

A long-lived process for one conversation, used by the VS Code, Visual
Studio, Eclipse, NetBeans and JetBrains plugins. The client writes one JSON
message per line (protocol/messages.py); the agent writes one JSON event per
line (protocol/events.py). Nothing else ever goes to stdout: stray prints go
to stderr. The protocol is the same one cmcoderagent speaks, so the IDE
plugins work unchanged on the LangGraph engine.

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
import os
import sys
import threading
import time
import uuid
from contextlib import suppress
from typing import Any, BinaryIO

from pydantic import ValidationError

from ..agents.main import Agent, AgentOptions, build_agent
from ..config.settings import Settings, SettingsError, add_local_allow_rule, find_project_root
from ..core.ide import IDE_TOOLS, format_ide_context
from ..core.permissions import MODES, Decision, ModeNotAllowed
from ..images import Image, ImageError, from_base64
from ..protocol import events as ev
from ..protocol import messages as msg
from ..runtime.commands import BUILT_IN, run_command
from ..runtime.session import Session
from ..runtime.store import SessionStore
from ..tools.base import Tool, ToolResult
from ..tools.files import ReadInput, ReadTool

IDE_TOOL_TIMEOUT = 30.0  # seconds
# Built-in commands that work in an IDE panel; the others (/clear, /resume,
# /mode) are the panel's own buttons.
PANEL_COMMANDS = {"help", "compact", "rewind", "model", "cost", "todos", "context"}
# Features of later phases that IDE panels may ask for: answered with the
# protocol's own "not set up / not available" events, so panels show a state.
CODE_SEARCH_LATER = "Code search arrives in Phase 5 of the LangGraph engine."
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
        async with SessionStore.open(root, persist=self.settings.persist_sessions) as store:
            self.store = store
            try:
                agent = build_agent(self.settings, self.opts, checkpointer=store.checkpointer)
            except (SettingsError, ValueError) as e:
                hint = "In a terminal, run:  cmcoder setup" if "cmcoder setup" in str(e) else None
                self.error("startup", str(e), hint)
                return 2
            self.session = Session(agent, ask=self.ask, store=store)
            if self.resume or self.continue_session:
                found = await (store.find(self.resume) if self.resume else store.latest())
                if found is not None:
                    await self.session.resume(found.thread_id)
            self._start_reader(stdin_fd)
            self.emit(self.session.init_event())
            if self.session.turn:  # a resumed conversation: show it again
                self.emit(ev.History(messages=await self.session.history()))
                if todos := await self.session.todos():
                    self.emit(ev.TodoUpdate(todos=todos))
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
                        continue
                    if isinstance(message, msg.Shutdown):
                        break
                    await self.handle(message)
            finally:
                await self._stop_turn()
                await self.session.close()
                await self.agent.close()
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
                {"allow": message.allow, "remember": message.remember, "feedback": message.feedback}
            )
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
            await self._rebuild()
        elif isinstance(message, msg.IdeToolResult):
            pending = self.ide_pending.pop(message.request_id, None)
            if pending is None or pending.done():
                self.error("protocol", f"No pending IDE tool request {message.request_id!r}.")
                return
            pending.set_result(ToolResult(message.content, is_error=message.is_error))
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
            self.emit(ev.IndexStatus(set_up=False, active=False, lines=[CODE_SEARCH_LATER]))
        elif isinstance(message, msg.RagCandidates):
            self.emit(ev.RagCandidatesList(likely=[], other=[], errors={}))
        elif isinstance(message, msg.RagSetup):
            self.emit(ev.RagSetupResult(ok=False, message=CODE_SEARCH_LATER))
        elif isinstance(message, msg.SetCritique):
            self.emit(ev.CritiqueChanged(enabled=False))
            if message.enabled:
                self.emit(ev.Warning(message="The critic arrives in Phase 3."))
        elif isinstance(message, msg.StopSubagent):
            self.error("not_running", f"No running subagent {message.id!r} (subagents: Phase 3).")

    async def _rebuild(self, model: str | None = None) -> None:
        """A new graph (another model, or IDE tools) on the same checkpointer and thread."""
        assert self.session is not None and self.store is not None
        opts = AgentOptions(**{**self.opts.__dict__, "model": model or self.agent.bundle.ref})
        new = build_agent(
            self.settings, opts, checkpointer=self.store.checkpointer, extra_tools=self.ide_tools
        )
        new.policy.mode = self.agent.policy.mode
        new.policy.allow = self.agent.policy.allow  # "always allow" answers so far
        await self.agent.close()
        self.session.agent = new
        if model:
            self.opts.model = model

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
                if compacted := await session.compact(arg or None):
                    self.emit(compacted)
                self.emit(self._result("success", "", False, started))
            elif name in PANEL_COMMANDS:  # answered here, not sent to the model
                if name == "rewind" and not text.partition(" ")[2].strip():
                    points = await session.rewind_points()
                    self.emit(ev.RewindPoints(points=points[-20:]))
                else:
                    result = await run_command(session, text, switch_model=self._switch_model)
                    body = "\n".join(result.lines)
                    self.emit(ev.AssistantMessage(text=f"```text\n{body}\n```"))
                self.emit(self._result("success", "", False, started))
            else:
                async for event in session.run(text, note=note, images=images):
                    self.emit(event)
        except asyncio.CancelledError:
            self.emit(self._result("interrupted", "Interrupted by the user.", False, started))
            raise
        except Exception as e:  # a bug: report it instead of dying silently
            self.error("internal", f"{type(e).__name__}: {e}")
            self.emit(self._result("error", str(e), True, started))

    async def _switch_model(self, ref: str) -> None:
        await self._rebuild(model=ref)
        b = self.agent.bundle
        self.emit(ev.ModelChanged(model=b.name, context_window=b.profile.context_window))

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
        if answer.get("remember") and not req.can_remember:
            answer["remember"] = False
        return answer

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
    return [
        ev.CommandInfo(name=n, description=d, argument_hint=h, origin="built-in")
        for n, h, d in BUILT_IN
        if n in PANEL_COMMANDS
    ]


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
