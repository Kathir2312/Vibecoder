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

* Middleware order (first = outermost). Model-call wrappers, outer to inner:

        Compaction (before_model) -> ContextEditing -> ModelProfile -> TextToolCalls -> model

  Tool-call wrappers, outer to inner:

        LoopGuard -> BashFileWork -> Permission -> FileCheckpoint -> tool

  The guards answer before anyone is asked. Permission comes after them, so a
  call only reaches the user once the guards have let it through, and file
  snapshots are taken only for calls that will really run.

  Built-in LangChain middleware used: `ModelCallLimitMiddleware`,
  `TodoListMiddleware`, `SummarizationMiddleware` (subclassed in
  middleware/compaction.py) and `ContextEditingMiddleware`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from ..config.settings import Settings, SettingsError, find_project_root
from ..core.permissions import ModeNotAllowed, PermissionPolicy
from ..core.prompt import build_system_prompt, load_memory_files
from ..middleware.checkpoint import FileCheckpointMiddleware
from ..middleware.compaction import CompactionMiddleware
from ..middleware.guards import (
    BashFileWorkMiddleware,
    LoopGuardMiddleware,
    ToolCallRepairMiddleware,
)
from ..middleware.model import CHARS_PER_TOKEN, ModelProfileMiddleware, ReasoningMiddleware
from ..middleware.permission import PermissionMiddleware
from ..middleware.tool_text import TextToolCallsMiddleware
from ..models.chat import ModelBundle, build_chat_model
from ..tools.adapter import as_langchain_tools
from ..tools.base import Tool, ToolContext, output_budget_chars
from ..tools.registry import default_tools
from .context import RunContext


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


@dataclass
class Agent:
    """The compiled graph plus the session-scoped objects around it."""

    graph: CompiledStateGraph[Any, Any, Any, Any]
    bundle: ModelBundle
    small: ModelBundle  # the small/fast model (summaries, titles)
    ctx: ToolContext
    policy: PermissionPolicy
    tools: dict[str, Tool]
    max_turns: int
    auto_compact_threshold: float = 0.8

    async def close(self) -> None:
        await self.ctx.close_shells()


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


def build_agent(
    settings: Settings,
    opts: AgentOptions,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    extra_middleware: list[AgentMiddleware[Any, Any]] | None = None,
) -> Agent:
    cwd = opts.cwd.resolve()
    root = find_project_root(cwd)
    bundle = build_chat_model(settings, opts.model)
    profile = bundle.profile
    # The small/fast model writes summaries and titles (the main model if unset).
    small = (
        build_chat_model(settings, settings.small_fast_model)
        if settings.small_fast_model
        else bundle
    )

    # 1. Tools: cmcoder's tools, wrapped as LangChain BaseTools. The todo list
    #    comes from LangChain's TodoListMiddleware instead (below).
    ctx = ToolContext(cwd=cwd, project_root=root)
    ctx.max_output_chars = output_budget_chars(profile.context_window)
    tools = {t.name: t for t in default_tools() if t.name != "TodoWrite"}
    policy = build_policy(settings, opts)

    # 2. System prompt: stable instructions first, then CMCODER.md memory, then
    #    the environment. Keeping the start identical between calls lets the
    #    server reuse its prompt cache (vLLM / llama.cpp prefix caching).
    system_prompt = build_system_prompt(
        cwd,
        root,
        tier=profile.prompt_tier,
        model=bundle.name,
        memory=load_memory_files(cwd, root),
    )

    # 3. Middleware: see the module docstring for the order.
    max_turns = opts.max_turns or settings.max_turns
    middleware: list[AgentMiddleware[Any, Any]] = [
        # Built-in: stop after N model calls per run.
        ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end"),
        # Built-in: the `write_todos` tool, with the list kept in graph state
        # (so it survives compaction and --resume).
        TodoListMiddleware(),
    ]
    lc_tools = as_langchain_tools(list(tools.values()), ctx)
    compaction: CompactionMiddleware | None = None
    if settings.auto_compact:
        # Built-in (subclassed): summarise old turns with the small model.
        compaction = CompactionMiddleware(
            bundle.model,
            small.model,
            threshold=settings.auto_compact_threshold,
            overhead_tokens=fixed_prompt_tokens(system_prompt, lc_tools),
        )
        middleware.append(compaction)
    middleware += [
        # Built-in: when the prompt passes ~60% of the window, replace old tool
        # outputs with a placeholder (cheap, before summarising is needed).
        ContextEditingMiddleware(
            edits=[ClearToolUsesEdit(trigger=int(profile.context_window * 0.6), keep=3)]
        ),
        ModelProfileMiddleware(bundle, on_window=compaction.set_window if compaction else None),
        TextToolCallsMiddleware(prompted=profile.tool_calling == "prompted"),
        ReasoningMiddleware(),
        ToolCallRepairMiddleware(),
        LoopGuardMiddleware(),
        BashFileWorkMiddleware(enabled=profile.steer_bash_file_work),
        PermissionMiddleware(
            policy,
            ctx,
            tools,
            interactive=opts.interactive,
            on_rule_saved=opts.on_rule_saved,
        ),
        FileCheckpointMiddleware(ctx, tools),
        *(extra_middleware or []),
    ]

    # 4. The agent itself: a compiled LangGraph graph.
    graph = create_agent(
        model=bundle.model,
        tools=lc_tools,
        system_prompt=system_prompt,
        middleware=middleware,
        context_schema=RunContext,
        checkpointer=checkpointer or InMemorySaver(),
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
        auto_compact_threshold=settings.auto_compact_threshold,
    )
