"""Build cmcoder's main agent with LangChain's `create_agent`.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `create_agent(model, tools, system_prompt=..., middleware=[...], checkpointer=...)`
  returns a *compiled LangGraph graph*. Its basic loop is:

        START -> model -> (tool calls?) -> tools -> model -> ... -> END

  Each middleware adds hooks to that graph (before/after the model, around
  the model call, around each tool call). The agent is ordinary LangGraph, so
  everything LangGraph offers works on it: streaming, checkpoints,
  interrupts, `get_state`, time travel.

* Checkpointer: saves the graph state (the message list) after every step,
  under a `thread_id` (our session id). This is what lets a run pause on an
  `interrupt()` for a permission prompt and continue later. Phase 0 keeps
  checkpoints in memory (`InMemorySaver`); Phase 1 switches to
  `AsyncSqliteSaver` for `--resume`.

* Middleware order (first = outermost). For tool calls, the request passes
  through the wrappers in list order:

        LoopGuard -> BashFileWork -> Permission -> tool

  LoopGuard and BashFileWork answer before anyone is asked. Permission is
  innermost, so a call only reaches the user once the guards have let it
  through.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from ..config.settings import Settings, SettingsError, find_project_root
from ..core.permissions import ModeNotAllowed, PermissionPolicy
from ..core.prompt import build_system_prompt, load_memory_files
from ..middleware.guards import (
    BashFileWorkMiddleware,
    LoopGuardMiddleware,
    ToolCallRepairMiddleware,
)
from ..middleware.model import ModelProfileMiddleware, ReasoningMiddleware
from ..middleware.permission import PermissionMiddleware
from ..models.chat import ModelBundle, build_chat_model
from ..tools.adapter import as_langchain_tools
from ..tools.base import Tool, ToolContext, output_budget_chars
from ..tools.registry import default_tools


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
    ctx: ToolContext
    policy: PermissionPolicy
    tools: dict[str, Tool]
    max_turns: int

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

    # 1. Tools: cmcoder's tools, wrapped as LangChain BaseTools.
    ctx = ToolContext(cwd=cwd, project_root=root)
    ctx.max_output_chars = output_budget_chars(profile.context_window)
    tools = {t.name: t for t in default_tools()}
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
        # Built-in LangChain middleware: stop after N model calls per run.
        ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end"),
        ModelProfileMiddleware(profile),
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
        *(extra_middleware or []),
    ]

    # 4. The agent itself: a compiled LangGraph graph.
    graph = create_agent(
        model=bundle.model,
        tools=as_langchain_tools(list(tools.values()), ctx),
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=checkpointer or InMemorySaver(),
        name="cmcoder",
    )
    return Agent(
        graph=graph, bundle=bundle, ctx=ctx, policy=policy, tools=tools, max_turns=max_turns
    )
