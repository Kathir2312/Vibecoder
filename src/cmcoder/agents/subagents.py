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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel, Field

from ..config.settings import Settings, config_dir, find_project_root
from ..core.commands import MAX_FILE_CHARS, parse_file, split_tools
from ..core.hooks import HookRunner
from ..core.permissions import PermissionPolicy
from ..core.prompt import build_subagent_prompt, load_memory_files
from ..models.chat import ModelBundle, build_chat_model
from ..protocol import events as ev
from ..sensitive import safe_project_file
from ..tools.adapter import as_langchain_tools
from ..tools.base import Tool, ToolContext, output_budget_chars

if TYPE_CHECKING:
    from .main import AgentOptions

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
NOT_FOR_SUBAGENTS = {"Task", "Team", "write_todos"}  # no nesting; the todos are the main agent's
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
    agents = dict(BUILT_IN_AGENTS)
    if project_trusted:
        agents.update(_load_dir(project_root / ".cmcoder" / "agents", "project", project_root))
    agents.update(_load_dir(config_dir() / "agents", "user"))
    return agents


def untrusted_project_agents(project_root: Path) -> list[str]:
    """The project's own agent definitions, which are used only once it's trusted."""
    return sorted(_load_dir(project_root / ".cmcoder" / "agents", "project", project_root))


def allowed(name: str, wanted: list[str] | None) -> bool:
    """Is tool `name` in an agent's `tools` list? `mcp__server` means all its tools."""
    if name in NOT_FOR_SUBAGENTS:
        return False
    if wanted is None:
        return True
    return name in wanted or any(
        w.startswith("mcp__") and name.startswith(w + "__") for w in wanted
    )


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
        ref = definition.model
        s = self.settings
        if ref in (None, "", "inherit"):
            return self.main
        if ref == "small":
            ref = s.small_fast_model
        elif ref == "subagent":
            ref = s.subagent_model or s.small_fast_model
        return build_chat_model(s, ref) if ref else self.main

    def build(
        self,
        definition: AgentDefinition,
        bundle: ModelBundle,
        ctx: ToolContext,
        *,
        response_format: Any = None,
        max_turns: int | None = None,
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
    agents = "\n".join(f"- {d.name}: {d.description}" for d in rt.definitions().values())
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
    ) -> str:
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


async def run_subagent(
    rt: SubagentRuntime,
    definition: AgentDefinition,
    description: str,
    prompt: str,
    runtime: ToolRuntime,
) -> str:
    result = await run_agent(
        rt, definition, description, prompt, runtime.stream_writer, runtime.tool_call_id or ""
    )
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
            rt.contexts[call_id] = ctx
        graph = rt.build(
            definition, bundle, ctx, response_format=response_format, max_turns=max_turns
        )
        report = ""
        structured = None
        try:
            extra = {"context": context} if context is not None else {}
            async for part in graph.astream(
                {"messages": [HumanMessage(prompt)]},
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
    run.state = "limit" if run.steps >= run.max_steps else "done"
    run.report, run.activity, run.ended_at = report, "", time.monotonic()
    _emit(writer, run.status())
    return SubagentResult(report, structured)


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
