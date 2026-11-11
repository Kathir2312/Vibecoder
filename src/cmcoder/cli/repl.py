"""Interactive terminal session: a line-based REPL (prompt_toolkit + rich).

What it shows about LangGraph: when the graph pauses on a permission
interrupt, `ask()` below prompts the user, and the session resumes the graph
with the answer. Sessions are saved through the SQLite checkpointer, so
`--continue` / `--resume` and `/rewind` work across runs.

Keys: Enter sends, Shift+Tab cycles the permission mode, Ctrl+C interrupts
the running turn, Ctrl+D exits. `/help` lists the commands; a line starting
with `#` is added to the project's CMCODER.md.
"""

from __future__ import annotations

import asyncio
import difflib

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel

from ..agents.main import AgentOptions, build_agent, prepare_agent, rebuild_agent
from ..compat import InterruptHandler
from ..config.settings import Settings, add_local_allow_rule, find_project_root
from ..core.hooks import Hook
from ..mcp import McpManager
from ..mcp.manager import McpServer
from ..protocol import events as ev
from ..runtime.commands import add_memory, run_command
from ..runtime.session import Session
from ..runtime.store import SessionStore

PREVIEW_LINES = 30  # long previews would push the options off screen


def _preview(req: ev.PermissionRequest) -> str:
    if req.change is not None:
        diff = difflib.unified_diff(
            (req.change.before or "").splitlines(),
            req.change.after.splitlines(),
            "before",
            "after",
            lineterm="",
        )
        lines = list(diff)
    else:
        lines = str(req.input.get("command") or req.input).splitlines()
    if len(lines) > PREVIEW_LINES:
        half = PREVIEW_LINES // 2
        lines = [*lines[:half], f"... {len(lines) - PREVIEW_LINES} more lines ...", *lines[-half:]]
    return "\n".join(lines)


async def run_repl(
    settings: Settings,
    opts: AgentOptions,
    first_prompt: str | None,
    *,
    continue_session: bool = False,
    resume: str | None = None,
) -> int:
    console = Console()
    opts.interactive = True
    root = find_project_root(opts.cwd.resolve())

    def save_rule(rule: str) -> None:  # "always allow" answers go to settings.local.json
        add_local_allow_rule(root, rule)

    opts.on_rule_saved = save_rule
    async with SessionStore.open(root, persist=settings.persist_sessions) as store:
        await store.cleanup(settings.cleanup_period_days)
        bindings = KeyBindings()
        prompt_session: PromptSession[str] = PromptSession(key_bindings=bindings)

        async def approve_server(server: McpServer) -> bool:
            details = "\n".join(f"{k}: {v}" for k, v in server.config.approval_details().items())
            console.print(
                Panel(
                    details,
                    title=f"Start this project's MCP server {server.name}?",
                    subtitle="It runs with your permissions; the answer is remembered.",
                )
            )
            answer = (await prompt_session.prompt_async("y/N: ")).strip().lower()
            return answer in ("y", "yes")

        async def approve_hook(hook: Hook) -> bool:
            console.print(
                Panel(
                    hook.command.command,
                    title=f"Run this project's {hook.event} hook?",
                    subtitle="It runs with your permissions; the answer is remembered.",
                )
            )
            return (await prompt_session.prompt_async("y/N: ")).strip().lower() in ("y", "yes")

        opts.approve_hook = approve_hook
        # MCP servers start now, so their tools are in the agent from the start.
        mcp = McpManager(settings, root)
        for warning in await mcp.start(approve_server):
            console.print(f"[yellow]{warning}[/yellow]")
        await prepare_agent(settings, opts)
        agent = build_agent(settings, opts, checkpointer=store.checkpointer, mcp=mcp)

        @bindings.add("s-tab")
        def _cycle_mode(event: KeyPressEvent) -> None:
            modes = session.agent.policy.available_modes()
            policy = session.agent.policy
            policy.mode = modes[(modes.index(policy.mode) + 1) % len(modes)]
            event.app.invalidate()

        async def ask(req: ev.PermissionRequest) -> dict[str, object]:
            console.print(
                Panel(_preview(req), title=f"cmcoder wants to run {req.label}", subtitle=req.reason)
            )
            # The options are drawn after the preview and repeated in the input line.
            options = "1 yes · 2 always · 3 no" if req.can_remember else "1 yes · 3 no"
            while True:
                answer = (await prompt_session.prompt_async(f"{options}: ")).strip()
                if answer in ("1", "y", "yes"):
                    return {"allow": True}
                if answer == "2" and req.can_remember:
                    return {"allow": True, "remember": True}
                if answer in ("3", "n", "no"):
                    feedback = await prompt_session.prompt_async(
                        "Tell cmcoder what to do instead: "
                    )
                    return {"allow": False, "feedback": feedback}

        session = Session(agent, ask=ask, store=store)

        async def switch_model(ref: str) -> None:
            # A new graph with another model, on the same checkpointer: the
            # conversation (thread) carries on unchanged.
            session.agent = await rebuild_agent(session.agent, model=ref)

        async def set_max_turns(n: int) -> None:
            session.agent = await rebuild_agent(session.agent, max_turns=n)

        if resume or continue_session:
            found = await (store.find(resume) if resume else store.latest())
            if found is None:
                console.print(
                    f"[red]No saved conversation {'matches ' + resume if resume else 'here'}.[/red]"
                )
                return 1
            await session.resume(found.thread_id)
            console.print(
                f"[dim]Resumed: {found.title or found.thread_id[:8]} ({found.turns} turns)[/dim]"
            )

        init = session.init_event()
        console.print(
            f"[bold]cmcoder[/bold] · {init.provider}:{init.model} · mode {init.permission_mode}"
        )
        console.print(
            "[dim]/help for commands · Shift+Tab: mode · Ctrl+C: interrupt · Ctrl+D: exit[/dim]"
        )
        prompt = first_prompt
        default = ""
        interrupt = InterruptHandler(asyncio.get_running_loop())
        try:
            while True:
                if prompt is None:
                    with patch_stdout():
                        try:
                            prompt = (
                                await prompt_session.prompt_async(
                                    lambda: f"[{session.agent.policy.mode}] > ", default=default
                                )
                            ).strip()
                        except EOFError:
                            return 0
                        except KeyboardInterrupt:
                            prompt = None
                            continue
                default = ""
                if not prompt:
                    prompt = None
                    continue
                if prompt.startswith("#"):
                    path = add_memory(root, prompt.lstrip("#"))
                    session.agent = await rebuild_agent(session.agent)  # a new system prompt
                    console.print(f"[dim]Added to {path}.[/dim]")
                    prompt = None
                    continue
                allow: list[str] = []
                if prompt.startswith("/"):
                    result = await run_command(
                        session, prompt, switch_model=switch_model, set_max_turns=set_max_turns
                    )
                    for line in result.lines:
                        console.print(line, markup=False, highlight=False)
                    if result.exit:
                        return 0
                    if result.send is None:
                        default = result.prompt or ""
                        prompt = None
                        continue
                    prompt, allow = result.send, result.allow  # a custom command
                task = asyncio.create_task(_show_turn(console, session, prompt, allow))
                interrupt.arm(task.cancel)
                try:
                    await task
                except asyncio.CancelledError:
                    console.print("[yellow]interrupted[/yellow]")
                finally:
                    interrupt.disarm()
                prompt = None
        finally:
            await session.close()
            await session.agent.close()
            await mcp.close()


async def _show_turn(
    console: Console, session: Session, prompt: str, allow: list[str] | None = None
) -> None:
    streamed = False
    async for event in session.run(prompt, allow=allow):
        if isinstance(event, ev.AssistantDelta):
            console.print(event.text, end="", markup=False, highlight=False)
            streamed = True
        elif isinstance(event, ev.AssistantMessage):
            if streamed:
                console.print()
            streamed = False
        elif isinstance(event, ev.ToolUse):
            console.print(f"[cyan]●[/cyan] {event.label}")
        elif isinstance(event, ev.ToolResult):
            first = event.content.strip().splitlines()[:1]
            style = "red" if event.is_error else "dim"
            console.print(f"  [{style}]⎿ {(first[0] if first else 'done')[:120]}[/{style}]")
        elif isinstance(event, ev.TodoUpdate):
            marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
            for t in event.todos:
                console.print(
                    f"  {marks.get(t.get('status', ''), '[ ]')} {t.get('content', '')}",
                    markup=False,
                )
        elif isinstance(event, ev.Compacted):
            console.print(
                f"[dim]Context nearly full: summarised {event.summarized_messages} earlier "
                f"messages with {event.model}.[/dim]"
            )
        elif isinstance(event, ev.Error):
            console.print(f"[red]error:[/red] {event.message}")
            if event.hint:
                console.print(f"[dim]hint: {event.hint}[/dim]")
    console.print()
