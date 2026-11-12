"""Subagents: the `Task` tool hands a self-contained job to a fresh agent graph.

A subagent has its own conversation (so the main one stays small), its own
tools and possibly a smaller model, and returns one final report. It uses the
main agent's permission policy and hooks (SubagentStop instead of Stop), and
can't start subagents itself.

Built in: `general-purpose` (every tool, the main model) and `explore`
(read-only search: Read, Glob, Grep; `subagentModel`, else `smallFastModel`).
Your own are Markdown files in Claude Code's format, as in cmcoderagent:

    ~/.cmcoder/agents/reviewer.md     (yours, every project)
    .cmcoder/agents/reviewer.md       (this project's: only when it's trusted)

    ---
    name: reviewer
    description: Reviews a diff for bugs. Use after making changes.
    tools: Read, Grep, Glob, Bash
    model: small
    ---
    You are a careful code reviewer...

LANGGRAPH / LANGCHAIN CONCEPTS IN THIS FILE

* **A graph inside a tool.** The Task tool builds a second `create_agent`
  graph and runs it with `astream`. Because it runs *inside* the main graph's
  tool node, LangGraph treats it as a subgraph: it shares the main graph's
  checkpointer under its own namespace (compiled with `checkpointer=None`,
  it inherits the parent's). The payoff: when a subagent's permission
  middleware calls `interrupt()`, the interrupt travels up and pauses the
  *main* graph. The user answers in the same prompt as always, and on resume
  LangGraph re-runs the tool, and the subgraph continues where it stopped.
* **Parallel subagents for free.** Several Task calls in one reply are
  separate tool calls; `create_agent` sends each to the tools node as its own
  task (LangGraph's `Send`), so they run at the same time. A semaphore caps
  how many (`maxParallelSubagents`).
* **`ToolRuntime`.** A tool function with a `runtime: ToolRuntime` parameter
  gets it injected (the model never sees it): the tool call's id and a
  `stream_writer`. We use the writer to show the subagent's own tool calls and
  status in the UI, tagged with the Task call's id (`parent_tool_use_id`).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelCallLimitMiddleware,
    hook_config,
)
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from ..config.settings import Settings, config_dir, find_project_root
from ..core.commands import MAX_FILE_CHARS, parse_file, split_tools
from ..core.hooks import HookRunner
from ..core.permissions import PermissionPolicy
from ..core.prompt import build_subagent_prompt, load_memory_files
from ..middleware.permission import STOP_KEY
from ..models.chat import ModelBundle, build_chat_model
from ..protocol import events as ev
from ..sensitive import safe_project_file
from ..tools.adapter import as_langchain_tools
from ..tools.base import Tool, ToolContext, output_budget_chars
from ..tools.browser import browser_session
from .limits import recursion_limit_for

if TYPE_CHECKING:
    from .main import AgentOptions

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
NOT_FOR_SUBAGENTS = {
    "Task",
    "Jobs",
    "Team",
    "PlaywrightTests",
    "write_todos",
    "ExitPlanMode",
}  # no nesting; the todos and the plan the user approves are the main agent's
CRITIC_NAME = "critic"  # reviews answers (middleware/critic.py); not offered to the Task tool
MAX_REPORT_CHARS = 30_000

GENERAL_PURPOSE_PROMPT = """\
You are a subagent of cmcoder, a coding assistant. You were given one task by
the main agent. Do it completely with your tools, without asking questions:
nobody can answer them. Search broadly when you don't know where something is,
read the relevant files, and make changes only if the task asks for them.
Your model calls are limited: read what the task needs, not everything.

When you are done, reply with a concise report for the main agent: what you
found or did, with file paths (and line numbers when useful). That report is
the only thing the main agent sees, so include everything it needs."""

EXPLORE_PROMPT = """\
You are a read-only search subagent of cmcoder, a coding assistant. Find what
the main agent asked about, quickly: use Glob for file names, Grep for
contents, and Read for the relevant parts. You cannot change anything.

Reply with a concise report: the answer, the relevant file paths with line
numbers, and short excerpts where they help. That report is the only thing the
main agent sees."""


@dataclass
class AgentDefinition:
    name: str
    description: str
    prompt: str
    origin: Literal["built-in", "user", "project"]
    tools: list[str] | None = None  # None: every tool
    model: str | None = None  # None / "inherit": the main model
    path: Path | None = None


BUILT_IN_AGENTS = {
    "general-purpose": AgentDefinition(
        "general-purpose",
        "For multi-step tasks: research a question, find code across many files, or carry "
        "out a self-contained change, when doing it here would fill the conversation.",
        GENERAL_PURPOSE_PROMPT,
        "built-in",
    ),
    "explore": AgentDefinition(
        "explore",
        "Fast, read-only codebase search: find files, definitions and usages, and answer "
        "questions about how the code works. Say how thorough to be.",
        EXPLORE_PROMPT,
        "built-in",
        tools=["Read", "Glob", "Grep", "CodeSearch"],  # CodeSearch when the project is indexed
        model="subagent",
    ),
}


def _load_dir(
    folder: Path, origin: Literal["user", "project"], root: Path | None = None
) -> dict[str, AgentDefinition]:
    found: dict[str, AgentDefinition] = {}
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.md")):
        if root is not None and not safe_project_file(path, root):
            continue  # a project's file must really be inside it
        try:
            meta, body = parse_file(path.read_text(encoding="utf-8")[:MAX_FILE_CHARS])
        except (OSError, UnicodeDecodeError):
            continue
        name = meta.get("name") or path.stem
        if not NAME_RE.match(name) or not body or name in BUILT_IN_AGENTS:
            continue
        found[name] = AgentDefinition(
            name=name,
            description=meta.get("description", "") or f"Custom agent {name}",
            prompt=body,
            origin=origin,
            tools=split_tools(meta["tools"]) if meta.get("tools") else None,
            model=meta.get("model") or None,
            path=path,
        )
    return found


def load_agents(project_root: Path, project_trusted: bool) -> dict[str, AgentDefinition]:
    """Built-in agents, a trusted project's, then yours (yours win on a name clash)."""
    from .playwright_roles import ROLES  # not at the top: that module imports this one

    agents = dict(BUILT_IN_AGENTS)
    agents.update(ROLES)  # built in, but a file of the same name (yours, a trusted project's) wins
    if project_trusted:
        agents.update(_load_dir(project_root / ".cmcoder" / "agents", "project", project_root))
    agents.update(_load_dir(config_dir() / "agents", "user"))
    return agents


def untrusted_project_agents(project_root: Path) -> list[str]:
    """The project's own agent definitions, which are used only once it's trusted."""
    return sorted(_load_dir(project_root / ".cmcoder" / "agents", "project", project_root))


def model_ref(settings: Settings, definition: AgentDefinition) -> str | None:
    """The model an agent definition asks for ("provider:model"), or None: the main model
    (`model:` absent or `inherit`; `small` and `subagent` are the settings' own picks)."""
    ref = definition.model
    if ref in (None, "", "inherit"):
        return None
    if ref == "small":
        return settings.small_fast_model
    if ref == "subagent":
        return settings.subagent_model or settings.small_fast_model
    return ref


def listed(name: str, wanted: list[str] | None) -> bool:
    """Is tool `name` in an agent's `tools` list (None: every tool)? `mcp__server` means all
    the tools of that MCP server, and a name ending in `*` (`ide_*`) every tool that starts so."""
    if wanted is None:
        return True
    return name in wanted or any(
        (w.startswith("mcp__") and name.startswith(w + "__"))
        or (w.endswith("*") and name.startswith(w[:-1]))  # `ide_*`: all the IDE's browser tools
        for w in wanted
    )


def allowed(name: str, wanted: list[str] | None) -> bool:
    """`listed`, for a subagent: which never has the tools that start agents or the todo list."""
    return name not in NOT_FOR_SUBAGENTS and listed(name, wanted)


_EXAMPLES = re.compile(r"<example>.*?</example>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]{1,40}>")


def short_description(text: str, width: int = 140) -> str:
    """One line for a menu: the first sentence of an agent's description, without the
    `<example>` blocks that agent files (Playwright's, Claude Code's) put in it."""
    text = " ".join(_TAGS.sub(" ", _EXAMPLES.sub(" ", text)).split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= width else first[: width - 1].rstrip() + "…"


def agent_infos(root: Path, project_trusted: bool) -> list[ev.AgentInfo]:
    """The agents a conversation can be switched to, for the UI's list: the project's first (a
    project's own are listed greyed out until the project is trusted), then yours, then the
    built-in ones. The critic is an agent of its own and is left out."""
    found = load_agents(root, project_trusted)
    found.pop(CRITIC_NAME, None)
    from .playwright_roles import ROLE_NAMES

    infos = [
        ev.AgentInfo(
            name=d.name,
            description=short_description(d.description),
            origin=d.origin,
            group="Playwright" if d.name in ROLE_NAMES else "",
        )
        for d in found.values()
    ]
    if not project_trusted:
        infos += [
            ev.AgentInfo(
                name=name,
                description="",
                origin="project",
                usable=False,
                hint="This project's agents are used only once the project is trusted: "
                "run `cmcoder trust` in the project folder.",
            )
            for name in untrusted_project_agents(root)
            if name not in found
        ]
    order = {"project": 0, "user": 1, "built-in": 2}
    return sorted(infos, key=lambda i: (order[i.origin], i.name))


@dataclass
class SubagentRun:
    """One Task call's subagent, for the agent map (`subagent_status`) and `/agents`."""

    id: str  # the Task call's id
    number: int
    description: str
    agent_type: str
    model: str
    max_steps: int
    state: ev.SubagentState = "queued"
    steps: int = 0
    tool_uses: int = 0
    tokens: int = 0
    activity: str = ""
    report: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    stop_requested: bool = False  # the user pressed Stop on it (`stop_subagent`)

    def request_stop(self) -> bool:
        """Stop it: a queued one doesn't start; a running one writes its report
        at its next model call, without tools. False when it isn't active."""
        if self.state not in ("queued", "running", "waiting") or self.stop_requested:
            return False
        self.stop_requested = True
        if self.state != "queued":
            self.state = "stopping"
        return True

    def status(self) -> ev.SubagentStatus:
        end = self.ended_at or time.monotonic()
        return ev.SubagentStatus(
            id=self.id,
            number=self.number,
            description=self.description,
            agent_type=self.agent_type,
            model=self.model,
            state=self.state,
            steps=self.steps,
            max_steps=self.max_steps,
            tool_uses=self.tool_uses,
            tokens=self.tokens,
            elapsed_ms=int((end - self.started_at) * 1000) if self.started_at else 0,
            activity=self.activity,
        )


# What the main agent gets back from a subagent the user stopped (cmcoderagent's words).
STOPPED_SUBAGENT = "Not run: the user stopped this subagent."
STOPPED_SUBAGENT_REPORT = "The user stopped this subagent. Its report so far:"
STOP_REQUEST = (
    "<system-reminder>The user stopped you, so you can't use tools any more. Write "
    "your report for the main agent now from what you have found so far, with file "
    "paths, and say what you didn't get to.</system-reminder>"
)


class StopRequestMiddleware(AgentMiddleware):
    """Stop one subagent gracefully when the user asks (`stop_subagent`).

    Cancelling the subagent's task would lose what it found. Instead, its next
    model call is its last: `awrap_model_call` sends it without tools and with a
    "write your report now" reminder, and `aafter_model` then ends the run
    (`jump_to: "end"`) whatever the reply holds. A tool already running finishes
    first."""

    def __init__(self, run: SubagentRun) -> None:
        super().__init__()
        self.run = run
        self.wrapped_up = False

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.run.stop_requested:
            return await handler(request)
        self.wrapped_up = True
        last = request.override(tools=[], messages=[*request.messages, HumanMessage(STOP_REQUEST)])
        return await handler(last)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return {"jump_to": "end"} if self.wrapped_up else None


@dataclass
class SubagentRuntime:
    """What the Task tool needs from the main agent to build subagents."""

    settings: Settings
    opts: AgentOptions
    policy: PermissionPolicy
    hooks: HookRunner | None
    main: ModelBundle
    cmcoder_tools: dict[str, Tool]  # the main agent's runnable cmcoder tools
    langchain_tools: list[BaseTool]  # MCP tools (already LangChain tools)
    permission_tools: dict[str, Tool]  # what the permission rules know (incl. MCP stand-ins)
    sandbox: Any = None  # the main agent's Bash sandbox (shared; its own shell)
    code_index: Any = None  # the project's code index (rag/), for CodeSearch
    runs: dict[str, SubagentRun] = field(default_factory=dict)
    # A paused subagent's tool context (what it has read, its shell), by call id:
    # LangGraph re-runs the tool on resume and the subagent must continue with it.
    contexts: dict[str, ToolContext] = field(default_factory=dict)
    _slots: asyncio.Semaphore | None = None

    @property
    def slots(self) -> asyncio.Semaphore:
        if self._slots is None:  # created lazily: it must belong to the running loop
            self._slots = asyncio.Semaphore(self.settings.max_parallel_subagents)
        return self._slots

    @property
    def root(self) -> Path:
        return find_project_root(self.opts.cwd.resolve())

    def find(self, ref: str) -> SubagentRun | None:
        """A run by its id (the Task call's) or its number ("2", "#2")."""
        if ref in self.runs:
            return self.runs[ref]
        number = ref.lstrip("#")
        return next((r for r in self.runs.values() if str(r.number) == number), None)

    def stop(self, ref: str) -> SubagentRun | None:
        """Stop one subagent; the turn and the other subagents go on. None: no
        such run, or it isn't running."""
        run = self.find(ref)
        return run if run is not None and run.request_stop() else None

    @property
    def ctx_for_labels(self) -> ToolContext:
        return ToolContext(cwd=self.opts.cwd, project_root=self.root)

    async def close(self) -> None:
        """Close the shells of subagents left paused (never resumed)."""
        for ctx in self.contexts.values():
            await ctx.close_shells()
        self.contexts.clear()

    def definitions(self) -> dict[str, AgentDefinition]:
        found = load_agents(self.root, self.settings.project_trusted)
        found.pop(CRITIC_NAME, None)
        return found

    def model_for(self, definition: AgentDefinition) -> ModelBundle:
        ref = model_ref(self.settings, definition)
        return build_chat_model(self.settings, ref) if ref else self.main

    def build(
        self,
        definition: AgentDefinition,
        bundle: ModelBundle,
        ctx: ToolContext,
        *,
        response_format: Any = None,
        max_turns: int | None = None,
        run: SubagentRun | None = None,
    ) -> Any:
        """A subagent's graph: the same loop middleware as the main agent.

        `response_format`: LangChain structured output. The agent ends by
        filling that schema (here: the critic's verdict), and the result is in
        the state's `structured_response`."""
        from .main import loop_middleware

        cmcoder = [t for n, t in self.cmcoder_tools.items() if allowed(n, definition.tools)]
        tools = as_langchain_tools(cmcoder, ctx) + [
            t for t in self.langchain_tools if allowed(t.name, definition.tools)
        ]
        middleware = [
            # Outermost: when the user stops this subagent, its next model call
            # is the last one, with no tools (structured-output runs, the
            # critic's, can't drop their tool and run to the end).
            *([StopRequestMiddleware(run)] if run is not None and response_format is None else []),
            ModelCallLimitMiddleware(
                run_limit=max_turns or self.settings.subagent_max_turns, exit_behavior="end"
            ),
            *loop_middleware(
                bundle,
                self.policy,
                ctx,
                self.permission_tools,
                self.opts,
                self.hooks,
                stop_event="SubagentStop",
                agent=definition.name,
                rethink=self.settings.rethink,
                max_turns=max_turns or self.settings.subagent_max_turns,
            ),
        ]
        prompt = build_subagent_prompt(
            definition.prompt,
            ctx.cwd,
            ctx.project_root,
            model=bundle.name,
            memory=load_memory_files(ctx.cwd, ctx.project_root),
        )
        # No checkpointer: run inside the main graph, it uses the main one's
        # (so interrupts in the subagent pause the main graph).
        return create_agent(
            model=bundle.model,
            tools=tools,
            system_prompt=prompt,
            middleware=middleware,
            response_format=response_format,
            name=f"subagent-{definition.name}",
        )


class TaskInput(BaseModel):
    description: str = Field(description="A short (3-5 word) description of the task")
    prompt: str = Field(
        description="The task for the subagent: everything it needs to know, since it "
        "doesn't see this conversation, and what to put in its report"
    )
    subagent_type: str = Field(
        "general-purpose", description="Which agent to use (see the list in the description)"
    )


def task_description(rt: SubagentRuntime) -> str:
    from .playwright_roles import ROLE_NAMES

    # The Playwright roles are for /playwright and the Agent list, not for the model to pick here.
    agents = "\n".join(
        f"- {d.name}: {d.description}"
        for d in rt.definitions().values()
        if d.name not in ROLE_NAMES
    )
    text = (
        "Hand a self-contained task to a subagent with its own fresh conversation; it "
        "works on its own and returns one report. Use it for broad searches and research "
        "that would fill this conversation with file contents, or for independent parts "
        "of a larger task. Give it a complete prompt: it can't see this conversation. "
        "Its report isn't shown to the user: summarise what matters.\n\n"
        f"Agents (subagent_type):\n{agents}"
    )
    if rt.main.profile.parallel_tool_calls and rt.settings.max_parallel_subagents > 1:
        text += (
            "\n\nSeveral Task calls in one reply run at the same time (up to "
            f"{rt.settings.max_parallel_subagents}): use that for independent parts. "
            "Don't give two of them the same files to change."
        )
    return text


def build_task_tool(rt: SubagentRuntime) -> BaseTool:
    """The `Task` tool, as a LangChain tool with an injected `ToolRuntime`."""

    async def task(
        description: str,
        prompt: str,
        runtime: ToolRuntime,
        subagent_type: str = "general-purpose",
    ) -> str | ToolMessage:
        definitions = rt.definitions()
        definition = definitions.get(subagent_type)
        if definition is None:
            raise ToolException(
                f"Unknown subagent_type {subagent_type!r}. Available: {', '.join(definitions)}."
            )
        return await run_subagent(rt, definition, description, prompt, runtime)

    return StructuredTool.from_function(
        coroutine=task,
        name="Task",
        description=task_description(rt),
        args_schema=TaskInput,
        handle_tool_error=True,
    )


# Where a subagent's UI events go: the main graph's `custom` stream
# (`ToolRuntime.stream_writer` in a tool, `get_stream_writer()` in middleware).
Writer = Any


def _emit(writer: Writer, event: ev.Event) -> None:
    writer({"type": "event", "event": event.model_dump()})


@dataclass
class SubagentResult:
    report: str
    structured: Any = None  # with `response_format`: the parsed structured output
    stopped: bool = False  # the user denied one of its tool calls (no feedback)
    user_stopped: bool = False  # the user pressed Stop on this subagent (the turn goes on)


async def run_subagent(
    rt: SubagentRuntime,
    definition: AgentDefinition,
    description: str,
    prompt: str,
    runtime: ToolRuntime,
) -> str | ToolMessage:
    result = await run_agent(
        rt, definition, description, prompt, runtime.stream_writer, runtime.tool_call_id or ""
    )
    if result.stopped:
        # The user's "Deny" inside a subagent stops the main turn too
        # (middleware/permission.py ends the turn on this mark).
        return ToolMessage(
            "Stopped: the user denied a tool call of this subagent. "
            "Stop and wait for the user's instructions.",
            tool_call_id=runtime.tool_call_id or "",
            name="Task",
            status="error",
            additional_kwargs={STOP_KEY: True},
        )
    if result.user_stopped:
        # Unlike a denial, the main turn goes on: with what the subagent found.
        if not result.report:
            return STOPPED_SUBAGENT
        return f"{STOPPED_SUBAGENT_REPORT}\n\n{result.report[:MAX_REPORT_CHARS]}"
    return result.report[:MAX_REPORT_CHARS] or "The subagent finished without a report."


async def run_agent(
    rt: SubagentRuntime,
    definition: AgentDefinition,
    description: str,
    prompt: str,
    writer: Writer,
    call_id: str,
    *,
    response_format: Any = None,
    max_turns: int | None = None,
    context: Any = None,
) -> SubagentResult:
    """Run one subagent to its end, showing its progress through `writer`.

    `context`: the run context for its middleware. Left out, a nested graph
    gets its parent's (from the Task tool: the main graph's `RunContext`)."""
    bundle = rt.model_for(definition)
    # LangGraph re-runs a tool after a permission prompt inside the subagent:
    # keep the same run (its number and counts) for the same call.
    run = rt.runs.get(call_id) or SubagentRun(
        id=call_id,
        number=len(rt.runs) + 1,
        description=description,
        agent_type=definition.name,
        model=bundle.name,
        max_steps=max_turns or rt.settings.subagent_max_turns,
    )
    rt.runs[call_id] = run
    _emit(writer, run.status())
    async with rt.slots:
        run.state, run.started_at = "running", run.started_at or time.monotonic()
        _emit(writer, run.status())
        ctx = rt.contexts.get(call_id)
        if ctx is None:  # its own shell and read-before-write tracking
            ctx = ToolContext(
                cwd=rt.opts.cwd, project_root=rt.root, sandbox=rt.sandbox, code_index=rt.code_index
            )
            ctx.max_output_chars = output_budget_chars(bundle.profile.context_window)
            ctx.browser = browser_session(rt.settings, rt.root)  # its own, if it has the tool
            rt.contexts[call_id] = ctx
        if run.stop_requested:  # stopped while it waited for a slot: never starts
            await _release(rt, call_id)
            run.state, run.ended_at = "stopped", time.monotonic()
            _emit(writer, run.status())
            return SubagentResult("", user_stopped=True)
        graph = rt.build(
            definition, bundle, ctx, response_format=response_format, max_turns=max_turns, run=run
        )
        report = ""
        structured = None
        stopped = False
        try:
            extra = {"context": context} if context is not None else {}
            async for part in graph.astream(
                {"messages": [HumanMessage(prompt)]},
                {"recursion_limit": recursion_limit_for(graph, run.max_steps)},
                stream_mode=["updates"],
                version="v2",
                **extra,
            ):
                for update in part["data"].values():
                    if not isinstance(update, dict):
                        continue
                    if update.get("structured_response") is not None:
                        structured = update["structured_response"]
                    for msg in update.get("messages") or []:
                        stopped = stopped or (
                            isinstance(msg, ToolMessage)
                            and bool(msg.additional_kwargs.get(STOP_KEY))
                        )
                        report = _follow(writer, rt, run, msg, report)
        except GraphInterrupt:
            raise  # paused for a permission prompt: resumed later with the same ctx
        except asyncio.CancelledError:
            run.state, run.ended_at = "stopped", time.monotonic()
            _emit(writer, run.status())
            await _release(rt, call_id)
            raise
        except Exception:
            await _release(rt, call_id)
            raise
        await _release(rt, call_id)
    run.state = (
        "stopped"
        if stopped or run.stop_requested
        else "limit"
        if run.steps >= run.max_steps
        else "done"
    )
    run.report, run.activity, run.ended_at = report, "", time.monotonic()
    _emit(writer, run.status())
    return SubagentResult(report, structured, stopped, user_stopped=run.stop_requested)


async def _release(rt: SubagentRuntime, call_id: str) -> None:
    if (ctx := rt.contexts.pop(call_id, None)) is not None:
        await ctx.close_shells()


def _label(rt: SubagentRuntime, call: Any) -> str:
    tool = rt.permission_tools.get(call["name"])
    if tool is not None:
        try:
            return tool.describe(tool.Input.model_validate(call["args"]), rt.ctx_for_labels)
        except ValueError:
            pass
    return call["name"]


def _follow(writer: Writer, rt: SubagentRuntime, run: SubagentRun, msg: Any, report: str) -> str:
    """Show one of the subagent's messages in the UI; returns the report so far."""
    if isinstance(msg, AIMessage):
        if msg.usage_metadata:
            run.tokens += msg.usage_metadata["total_tokens"]
        if msg.tool_calls:
            run.steps += 1
            for c in msg.tool_calls:
                run.tool_uses += 1
                run.activity = _label(rt, c)
                _emit(
                    writer,
                    ev.ToolUse(
                        id=c["id"] or "",
                        name=c["name"],
                        input=c["args"],
                        label=run.activity,
                        parent_tool_use_id=run.id,
                    ),
                )
            _emit(writer, run.status())
        elif msg.text.strip():
            run.steps += 1
            return msg.text.strip()
    elif isinstance(msg, ToolMessage):
        _emit(
            writer,
            ev.ToolResult(
                id=msg.tool_call_id,
                name=msg.name or "",
                content=msg.text[:2000],
                is_error=msg.status == "error",
                parent_tool_use_id=run.id,
            ),
        )
    return report
