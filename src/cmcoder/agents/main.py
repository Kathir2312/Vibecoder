"""Build cmcoder's main agent with LangChain's `create_agent`.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `create_agent(model, tools, system_prompt=..., middleware=[...], checkpointer=...)`
  returns a *compiled LangGraph graph*. Its basic loop is:

        START -> model -> (tool calls?) -> tools -> model -> ... -> END

  Each middleware adds hooks to that graph (before/after the model, around
  the model call, around each tool call). The agent is ordinary LangGraph, so
  everything LangGraph offers works on it: streaming, checkpoints,
  interrupts, `get_state`, time travel.

* Checkpointer: saves the graph state (the message list, the todo list)
  after every step, under a `thread_id` (our session id). This is what lets a
  run pause on an `interrupt()` for a permission prompt and continue later,
  and what `--resume` and `/rewind` read. The CLI passes an `AsyncSqliteSaver`
  (runtime/store.py); tests use the in-memory default.

* `context_schema=RunContext`: per-run data middleware can read
  (agents/context.py), such as the turn number for file snapshots.

* Middleware order (first in the list = outermost wrapper). Model-call
  wrappers, outer to inner (Compaction runs before them, as a before_model
  node; Vision only when the main model can't see images):

        TodoList -> Vision -> ContextEditing -> Rethink -> ModelProfile -> TextToolCalls -> model

  Tool-call wrappers, outer to inner (Hooks only when hooks are configured):

        PlainToolText -> LoopGuard -> BashFileWork -> Permission -> Hooks
            -> FileCheckpoint -> tool

  The node hooks run in list order for `before_*` and in *reverse* list order
  for `after_*` (the outermost middleware sees the reply last, as with
  wrappers).

  The guards answer before anyone is asked. Permission comes after them, so a
  call only reaches the user once the guards have let it through, and file
  snapshots are taken only for calls that will really run.

  Built-in LangChain middleware used: `ModelCallLimitMiddleware`,
  `TodoListMiddleware`, `SummarizationMiddleware` (subclassed in
  middleware/compaction.py) and `ContextEditingMiddleware`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.messages import AnyMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from ..config.settings import RethinkConfig, Settings, SettingsError, find_project_root
from ..core.hooks import ApproveHookFn, HookRunner
from ..core.permissions import ModeNotAllowed, PermissionPolicy
from ..core.prompt import build_subagent_prompt, build_system_prompt, load_memory_files
from ..core.skills import SkillTool, load_skills, skills_prompt
from ..ide_browser import IdeToolSet
from ..mcp import McpManager
from ..middleware.checkpoint import FileCheckpointMiddleware
from ..middleware.code_context import AutoContextMiddleware
from ..middleware.compaction import CompactionMiddleware
from ..middleware.critic import CriticMiddleware
from ..middleware.guards import (
    BashFileWorkMiddleware,
    LoopGuardMiddleware,
    PlainToolTextMiddleware,
    ToolCallRepairMiddleware,
)
from ..middleware.hooks import HooksMiddleware
from ..middleware.model import CHARS_PER_TOKEN, ModelProfileMiddleware, ReasoningMiddleware
from ..middleware.permission import PermissionMiddleware
from ..middleware.rethink import RethinkMiddleware
from ..middleware.tool_text import TextToolCallsMiddleware
from ..middleware.toolcall import ToolCallContextMiddleware
from ..middleware.vision import VisionMiddleware
from ..models.chat import ModelBundle, build_chat_model
from ..models.gateway import prepare_models
from ..notes import Memory
from ..rag.index import CodeIndex
from ..rag.session import open_code_index
from ..runtime.serde import checkpoint_serde
from ..sandbox import Sandbox, make_sandbox
from ..telemetry import Telemetry
from ..telemetry import from_settings as telemetry_from_settings
from ..tools.adapter import as_langchain_tools
from ..tools.base import Tool, ToolContext, output_budget_chars
from ..tools.browser import BrowserTool, browser_session
from ..tools.code_search import CodeSearchTool
from ..tools.registry import default_tools
from .context import RunContext
from .limits import recursion_limit_for
from .notes_tools import build_notes_tools
from .playwright_tests import build_playwright_tool
from .subagents import (
    CRITIC_NAME,
    SubagentRuntime,
    build_task_tool,
    listed,
    load_agents,
    model_ref,
)
from .team import build_team_tool

# What an agent is told when the whole conversation is that agent (not a subagent's one job).
PERSONA_TAIL = (
    '\n\nYou are talking with the user directly in this chat, as the "{name}" agent. '
    "If something is unclear or you need a decision, ask them. When you finish, say briefly "
    "what you did and where the results are."
)


@dataclass
class AgentOptions:
    """What the command line (or an IDE) chooses for this session."""

    cwd: Path
    model: str | None = None  # "provider:model"; default: settings.model
    permission_mode: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    interactive: bool = True  # False: -p mode, nobody answers permission prompts
    on_rule_saved: Callable[[str], None] | None = None
    # Asked before a project's hook first runs (it runs with the user's rights).
    approve_hook: ApproveHookFn | None = None
    critic: bool | None = None  # --critic; None: settings `critic.enabled`
    # --agent / `set_agent`: the whole conversation is this agent (a subagent definition from
    # .cmcoder/agents or ~/.cmcoder/agents): its instructions, its tools, its model.
    agent: str | None = None
    frontend: str = "cli"  # "cli", "print" (-p) or the IDE (--client): for telemetry


@dataclass
class Agent:
    """The compiled graph plus the session-scoped objects around it."""

    graph: CompiledStateGraph[Any, Any, Any, Any]
    bundle: ModelBundle
    small: ModelBundle  # the small/fast model (summaries, titles); the main one if unset
    ctx: ToolContext
    policy: PermissionPolicy
    tools: dict[str, Tool]
    max_turns: int
    # LangGraph's step budget for one turn, sized from the graph (`recursion_limit_for`).
    recursion_limit: int = 0
    auto_compact_threshold: float = 0.8
    has_small_model: bool = False  # smallFastModel is configured
    vision: ModelBundle | None = None  # visionModel: describes images for a main model that can't
    hooks: HookRunner | None = None  # the user's hook commands (core/hooks.py)
    subagents: SubagentRuntime | None = None  # the Task tool's runs (agents/subagents.py)
    critic: CriticMiddleware | None = None  # reviews answers; `.enabled` can be switched
    team: bool = False  # the Team tool is offered (`team.enabled`; agents/team.py)
    sandbox: Sandbox | None = None  # the Bash sandbox (sandbox/; None: unsandboxed)
    code_index: CodeIndex | None = None  # code search (rag/; None: off or not indexed)
    telemetry: Telemetry | None = None  # OpenTelemetry metrics (off by default)
    memory: Memory | None = None  # notes between sessions, on a LangGraph store (notes.py)
    frontend: str = "cli"
    # Shown once, with the first turn (e.g. a sandbox asked for that can't run here).
    startup_warnings: list[str] = field(default_factory=list)
    # The agent this conversation is (opts.agent), None: the default one.
    persona: str | None = None
    # The names of the IDE's own tools (VS Code's browser), when it has some.
    ide_tool_names: list[str] = field(default_factory=list)
    # How it was built, so `rebuild_agent` can build it again with changes.
    spec: dict[str, Any] = field(default_factory=dict)

    async def close(self) -> None:
        await self.ctx.close_shells()
        if self.subagents is not None:
            await self.subagents.close()
        if self.sandbox is not None:
            await self.sandbox.close()
        if self.code_index is not None:
            await self.code_index.close()
        if self.telemetry is not None:
            await self.telemetry.close()  # sends what's left
        if self.memory is not None:
            self.memory.close()


async def prepare_agent(settings: Settings, opts: AgentOptions) -> None:
    """Before `build_agent` (which is synchronous): ask the gateway about its
    models (limits, Open WebUI backends) and probe an unknown context window.
    Cached, quick, best effort (models/gateway.py)."""
    refs = [
        opts.model or settings.model,
        settings.small_fast_model,
        settings.subagent_model,
        settings.vision_model,
    ]
    try:
        await prepare_models(settings, refs)
    except Exception:  # never a reason not to start; the first request will tell
        pass


async def rebuild_agent(old: Agent, **changes: Any) -> Agent:
    """The same agent with changes (`model=`, `agent=`, `max_turns=`, `extra_tools=`, `mcp=`).

    `create_agent` compiles a fixed model and tool list into the graph, so a
    change means a new graph. Built on the *same checkpointer*, it continues
    the same threads: callers keep their thread id and the conversation goes
    on. The permission mode and the "always allow" answers carry over.
    """
    options = ("model", "agent", "max_turns")  # AgentOptions fields; the rest go to build_agent
    spec = {**old.spec, **{k: v for k, v in changes.items() if k not in options}}
    opts: AgentOptions = spec.pop("opts")
    settings: Settings = spec.pop("settings")
    if "model" in changes:
        opts = AgentOptions(**{**opts.__dict__, "model": changes["model"]})
        await prepare_agent(settings, opts)
    if "agent" in changes:  # None: back to the default agent
        opts = AgentOptions(**{**opts.__dict__, "agent": changes["agent"]})
    if "max_turns" in changes:  # the limit, its reminder at 80% and the step budget follow it
        opts = AgentOptions(**{**opts.__dict__, "max_turns": changes["max_turns"]})
    # The sandbox (its proxy, its temp folder) carries over too.
    spec.setdefault("code_index", old.code_index)
    new = build_agent(
        settings, opts, sandbox=old.sandbox, telemetry=old.telemetry, memory=old.memory, **spec
    )
    new.policy.mode = old.policy.mode
    new.policy.allow = old.policy.allow
    old.sandbox = old.telemetry = old.memory = None
    if old.code_index is new.code_index:
        old.code_index = None
    await old.close()
    return new


def build_policy(settings: Settings, opts: AgentOptions) -> PermissionPolicy:
    perms = settings.permissions
    locked = perms.allow_managed_permission_rules_only
    try:
        return PermissionPolicy(
            mode=opts.permission_mode or perms.default_mode,
            allow=[*perms.allow, *([] if locked else opts.allowed_tools)],
            deny=[*perms.deny, *opts.disallowed_tools],
            high_risk=perms.high_risk_commands,
            bypass_disabled=perms.disable_bypass_permissions_mode == "disable",
            auto_disabled=perms.disable_auto_mode == "disable",
            allow_rules_locked=locked,
        )
    except ModeNotAllowed as e:
        raise SettingsError(str(e)) from e


def fixed_prompt_tokens(system_prompt: str, tools: list[BaseTool]) -> int:
    """Tokens every request spends before any message: system prompt + tool schemas."""
    schemas = json.dumps([convert_to_openai_tool(t) for t in tools])
    return int((len(system_prompt) + len(schemas)) / CHARS_PER_TOKEN)


def loop_middleware(
    bundle: ModelBundle,
    policy: PermissionPolicy,
    ctx: ToolContext,
    tools: dict[str, Tool],
    opts: AgentOptions,
    hooks: HookRunner | None,
    *,
    on_window: Callable[[int], None] | None = None,
    summarise: Callable[[list[AnyMessage]], Awaitable[list[AnyMessage] | None]] | None = None,
    stop_event: Literal["Stop", "SubagentStop"] = "Stop",
    agent: str | None = None,
    rethink: RethinkConfig | None = None,
    max_turns: int | None = None,
    plan_todos: bool = False,
) -> list[AgentMiddleware[Any, Any]]:
    """The middleware every cmcoder agent loop has, the main one and subagents."""
    profile = bundle.profile
    rethink = rethink or RethinkConfig.model_validate({})
    return [
        # Stuck? Three failed calls in a row: step back and try something else; still
        # failing: report to the user (before the model-call wrappers: it can take the tools).
        *(
            [
                RethinkMiddleware(
                    after_failures=rethink.after_failures,
                    max_rethinks=rethink.max_rethinks,
                    budget_fraction=rethink.budget_warning,
                    max_turns=max_turns,
                )
            ]
            if rethink.enabled
            else []
        ),
        # Built-in: when the prompt passes ~60% of the window, replace old tool
        # outputs with a placeholder (cheap, before summarising is needed).
        ContextEditingMiddleware(
            edits=[ClearToolUsesEdit(trigger=int(profile.context_window * 0.6), keep=3)]
        ),
        ModelProfileMiddleware(bundle, on_window=on_window, summarise=summarise),
        TextToolCallsMiddleware(prompted=profile.tool_calling == "prompted"),
        ReasoningMiddleware(),
        ToolCallRepairMiddleware(),
        PlainToolTextMiddleware(),
        LoopGuardMiddleware(),
        BashFileWorkMiddleware(enabled=profile.steer_bash_file_work),
        PermissionMiddleware(
            policy,
            ctx,
            tools,
            interactive=opts.interactive,
            on_rule_saved=opts.on_rule_saved,
            hooks=hooks,
            approve_hook=opts.approve_hook,
            agent=agent,
            plan_todos=plan_todos,
        ),
        ToolCallContextMiddleware(),  # lets a tool find out which call it is (progress, stop)
        *([HooksMiddleware(hooks, opts.approve_hook, stop_event=stop_event)] if hooks else []),
        FileCheckpointMiddleware(ctx, tools),
    ]


def build_agent(
    settings: Settings,
    opts: AgentOptions,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    extra_middleware: list[AgentMiddleware[Any, Any]] | None = None,
    extra_tools: list[Tool] | None = None,
    mcp: McpManager | None = None,
    sandbox: Sandbox | None = None,
    telemetry: Telemetry | None = None,
    code_index: CodeIndex | None = None,
    memory: Memory | None = None,
    ide_browser: IdeToolSet | None = None,
) -> Agent:
    """`sandbox`, `telemetry`, `code_index`, `memory`: reuse these (a rebuild, or
    /index turning code search on); else made from settings. `ide_browser`: tools the IDE
    defines itself (VS Code's Integrated Browser), used instead of the Browser tool."""
    cwd = opts.cwd.resolve()
    if not settings.browser.use_ide_browser:
        ide_browser = None
    root = find_project_root(cwd)
    # A conversation that is one of the agents (a subagent definition): its tools, its model.
    persona = None
    if opts.agent:
        persona = load_agents(root, settings.project_trusted).get(opts.agent)
        if persona is None or persona.name == CRITIC_NAME:
            raise ValueError(
                f"No agent named {opts.agent!r}. Agents are definitions in .cmcoder/agents "
                "(of a trusted project) or ~/.cmcoder/agents; /agents lists them."
            )

    def keep(tool: str) -> bool:
        return persona is None or listed(tool, persona.tools)

    chosen = model_ref(settings, persona) if persona else None
    bundle = build_chat_model(settings, chosen or opts.model)
    profile = bundle.profile
    # The small/fast model writes summaries and titles (the main model if unset).
    small = (
        build_chat_model(settings, settings.small_fast_model)
        if settings.small_fast_model
        else bundle
    )

    # 1. Tools: cmcoder's tools, wrapped as LangChain BaseTools. The todo list
    #    comes from LangChain's TodoListMiddleware instead (below).
    # The Bash sandbox (bubblewrap / sandbox-exec), when settings ask for one.
    warnings: list[str] = []
    if sandbox is None:
        sandbox, warning = make_sandbox(settings.sandbox, root)
        warnings += [warning] if warning else []
    # Code search (Phase 5): the project's index, when it has one (rag/).
    if code_index is None:
        code_index, warning = open_code_index(settings, root)
        warnings += [warning] if warning else []
    ctx = ToolContext(cwd=cwd, project_root=root, sandbox=sandbox, code_index=code_index)
    ctx.max_output_chars = output_budget_chars(profile.context_window)
    tools = {t.name: t for t in default_tools() if t.name != "TodoWrite"}
    if code_index is not None:
        tools["CodeSearch"] = CodeSearchTool()
    # A real browser to test web apps (Playwright); started only when first used.
    ctx.browser = browser_session(settings, root)
    if ctx.browser is not None and not ide_browser:
        tools["Browser"] = BrowserTool()  # (the IDE's own browser, when it has one, replaces it)
    # Tools an IDE runs for the agent (getDiagnostics, openFile; core/ide.py).
    tools.update({t.name: t for t in extra_tools or []})
    # Skills (SKILL.md folders): only their descriptions go in the prompt; the
    # Skill tool loads one when the model decides it needs it.
    skills = load_skills(root)
    if skills:
        tools["Skill"] = SkillTool(skills)
    tools = {n: t for n, t in tools.items() if keep(n)}  # an agent's `tools:` list
    policy = build_policy(settings, opts)
    runner = HookRunner(settings, root)
    hooks = runner if runner.hooks else None

    # 2. System prompt: stable instructions first, then CMCODER.md memory, then
    #    the environment. Keeping the start identical between calls lets the
    #    server reuse its prompt cache (vLLM / llama.cpp prefix caching).
    notes = memory
    if notes is None and settings.memory.enabled:
        notes = Memory.for_project(root, settings.memory.auto)  # a LangGraph SqliteStore
    if persona is None:
        system_prompt = build_system_prompt(
            cwd,
            root,
            learned=notes.prompt_block() if notes else None,
            tier=profile.prompt_tier,
            model=bundle.name,
            memory=load_memory_files(cwd, root),
            skills=skills_prompt(skills),
            sandbox=sandbox.prompt_note() if sandbox else None,
            code_search=code_index is not None,
        )
    else:
        # The agent's own instructions (as for a subagent), the project's memory files and the
        # environment; plus a word that it is talking with the user itself.
        system_prompt = build_subagent_prompt(
            persona.prompt + PERSONA_TAIL.format(name=persona.name),
            cwd,
            root,
            model=bundle.name,
            memory=load_memory_files(cwd, root),
            skills=skills_prompt(skills) if keep("Skill") else None,
        )
        if sandbox is not None and keep("Bash"):
            system_prompt += "\n\n" + sandbox.prompt_note()

    # 3. Middleware: see the module docstring for the order.
    max_turns = opts.max_turns or settings.max_turns
    middleware: list[AgentMiddleware[Any, Any]] = [
        # Built-in: stop after N model calls per run.
        ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end"),
        # Built-in: the `write_todos` tool, with the list kept in graph state
        # (so it survives compaction and --resume). An agent has it only if it lists it.
        *([TodoListMiddleware()] if keep("write_todos") else []),
    ]
    if code_index is not None:
        # RAG before the model: the index's best matches for the user's message.
        middleware.append(
            AutoContextMiddleware(code_index, settings.rag.auto_context, profile.context_window)
        )
    # Images: for a main model that can't see them, `visionModel` describes
    # them (the session asks it; middleware/vision.py sends the descriptions).
    vision = build_chat_model(settings, settings.vision_model) if settings.vision_model else None
    if not profile.vision:
        middleware.append(
            VisionMiddleware(main_sees=False, describer_name=vision.name if vision else "")
        )
    lc_tools = as_langchain_tools(list(tools.values()), ctx)
    if mcp is not None:
        # MCP servers' tools are already LangChain tools (langchain-mcp-adapters);
        # the permission rules see them through small stand-ins.
        lc_tools += [t for t in mcp.langchain_tools() if keep(t.name)]
        tools.update({t.name: t for t in mcp.permission_tools() if keep(t.name)})
    ide_lc_tools: list[BaseTool] = []
    ide_names: set[str] = set()
    if ide_browser:
        # The IDE's own tools (already LangChain tools, like MCP's) and their stand-ins.
        ide_browser.max_chars = ctx.max_output_chars
        ide_lc_tools = [t for t in ide_browser.langchain_tools() if keep(t.name)]
        lc_tools += ide_lc_tools
        stand_ins = [t for t in ide_browser.permission_tools() if keep(t.name)]
        tools.update({t.name: t for t in stand_ins})
        ide_names = {t.name for t in stand_ins}
    compaction: CompactionMiddleware | None = None
    if settings.auto_compact:
        # Built-in (subclassed): summarise old turns with the small model.
        compaction = CompactionMiddleware(
            bundle.model,
            small.quick(),  # thinking off: a summary needs no reasoning
            threshold=settings.auto_compact_threshold,
            overhead_tokens=fixed_prompt_tokens(system_prompt, lc_tools),
            hooks=hooks,
            approve_hook=opts.approve_hook,
        )
        middleware.append(compaction)
    middleware += loop_middleware(
        bundle,
        policy,
        ctx,
        tools,
        opts,
        hooks,
        on_window=compaction.set_window if compaction else None,
        summarise=compaction.emergency if compaction else None,
        rethink=settings.rethink,
        max_turns=max_turns,
        plan_todos=keep("write_todos"),  # an approved plan becomes the todo list
    )
    middleware += extra_middleware or []
    # Subagents: the Task tool starts a fresh agent graph for a self-contained job.
    subagents = SubagentRuntime(
        settings=settings,
        opts=opts,
        policy=policy,
        hooks=hooks,
        main=bundle,
        cmcoder_tools={
            n: t for n, t in tools.items() if not n.startswith("mcp__") and n not in ide_names
        },
        langchain_tools=[
            *([t for t in mcp.langchain_tools() if keep(t.name)] if mcp is not None else []),
            *ide_lc_tools,
        ],
        permission_tools=tools,
        sandbox=sandbox,
        code_index=code_index,
    )
    if keep("Task"):
        lc_tools.append(build_task_tool(subagents))
    if notes is not None:
        lc_tools += [t for t in build_notes_tools(notes) if keep(t.name)]
    # The team: a StateGraph (PM → developer → tester ↺ → DevOps) behind one tool.
    if settings.team.enabled and keep("Team"):
        lc_tools.append(build_team_tool(subagents, settings.team.max_rounds))
    # `/playwright all`: map, plan, write, run and heal Playwright tests, area by area, as a
    # StateGraph behind one tool.
    if settings.playwright.enabled and keep("PlaywrightTests"):
        lc_tools.append(build_playwright_tool(subagents))
    # The critic (after_agent): reviews the final answer when switched on.
    critic = CriticMiddleware(
        subagents,
        enabled=settings.critic.enabled if opts.critic is None else opts.critic,
        max_rounds=settings.critic.max_rounds,
        window=profile.context_window,
    )
    middleware.append(critic)

    # 4. The agent itself: a compiled LangGraph graph. The saver it uses is kept in `spec`, made
    #    here when none was given, so that a rebuild (`rebuild_agent`) continues the same threads.
    saver = checkpointer or InMemorySaver(serde=checkpoint_serde())
    graph = create_agent(
        model=bundle.model,
        tools=lc_tools,
        system_prompt=system_prompt,
        middleware=middleware,
        context_schema=RunContext,
        checkpointer=saver,
        name="cmcoder",
    )
    return Agent(
        graph=graph,
        bundle=bundle,
        small=small,
        ctx=ctx,
        policy=policy,
        tools=tools,
        max_turns=max_turns,
        recursion_limit=recursion_limit_for(graph, max_turns),
        auto_compact_threshold=settings.auto_compact_threshold,
        has_small_model=small is not bundle,
        vision=vision,
        hooks=hooks,
        subagents=subagents,
        critic=critic,
        team=settings.team.enabled,
        sandbox=sandbox,
        code_index=code_index,
        memory=notes,
        telemetry=telemetry
        if telemetry is not None
        else telemetry_from_settings(settings.telemetry),
        frontend=opts.frontend,
        startup_warnings=warnings,
        persona=persona.name if persona else None,
        ide_tool_names=sorted(ide_names),
        spec={
            "settings": settings,
            "opts": opts,
            "checkpointer": saver,
            "extra_middleware": extra_middleware,
            "extra_tools": extra_tools,
            "mcp": mcp,
            "ide_browser": ide_browser,
        },
    )
