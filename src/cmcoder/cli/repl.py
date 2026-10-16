"""Interactive terminal session (Phase 0: a simple line-based REPL).

The full-screen Textual UI and slash commands come in Phase 1. This REPL
already shows the whole human-in-the-loop flow: when the graph pauses on a
permission interrupt, `ask()` below prompts the user, and the session resumes
the graph with the answer.
"""

from __future__ import annotations

import asyncio
import difflib

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel

from ..agents.main import AgentOptions, build_agent
from ..config.settings import Settings, add_local_allow_rule, find_project_root
from ..protocol import events as ev
from ..runtime.session import Session

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


async def run_repl(settings: Settings, opts: AgentOptions, first_prompt: str | None) -> int:
    console = Console()
    prompt_session: PromptSession[str] = PromptSession()
    opts.interactive = True
    root = find_project_root(opts.cwd.resolve())

    def save_rule(rule: str) -> None:  # "always allow" answers go to settings.local.json
        add_local_allow_rule(root, rule)

    opts.on_rule_saved = save_rule
    agent = build_agent(settings, opts)

    async def ask(req: ev.PermissionRequest) -> dict[str, object]:
        console.print(
            Panel(_preview(req), title=f"cmcoder wants to run {req.label}", subtitle=req.reason)
        )
        options = "1 yes · 2 always · 3 no" if req.can_remember else "1 yes · 3 no"
        while True:
            answer = (await prompt_session.prompt_async(f"{options}: ")).strip()
            if answer in ("1", "y", "yes"):
                return {"allow": True}
            if answer == "2" and req.can_remember:
                return {"allow": True, "remember": True}
            if answer in ("3", "n", "no"):
                feedback = await prompt_session.prompt_async("Tell cmcoder what to do instead: ")
                return {"allow": False, "feedback": feedback}

    session = Session(agent, ask=ask)
    init = session.init_event()
    console.print(
        f"[bold]cmcoder[/bold] · {init.provider}:{init.model} · mode {init.permission_mode}"
    )
    console.print("[dim]Ctrl+C interrupts, Ctrl+D exits.[/dim]")
    prompt = first_prompt
    try:
        while True:
            if prompt is None:
                with patch_stdout():
                    try:
                        prompt = (await prompt_session.prompt_async("> ")).strip()
                    except EOFError:
                        return 0
                    except KeyboardInterrupt:
                        prompt = None
                        continue
            if not prompt:
                prompt = None
                continue
            if prompt in ("/exit", "/quit"):
                return 0
            task = asyncio.create_task(_show_turn(console, session, prompt))
            try:
                await task
            except KeyboardInterrupt:
                task.cancel()
                console.print("[yellow]interrupted[/yellow]")
            prompt = None
    finally:
        await agent.close()


async def _show_turn(console: Console, session: Session, prompt: str) -> None:
    streamed = False
    async for event in session.run(prompt):
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
        elif isinstance(event, ev.Error):
            console.print(f"[red]error:[/red] {event.message}")
            if event.hint:
                console.print(f"[dim]hint: {event.hint}[/dim]")
    console.print()
