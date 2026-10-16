"""Non-interactive mode: `cmcoder -p "prompt"` for scripts and CI.

Nobody can answer permission prompts here, so the agent is built with
`interactive=False`: calls that would ask are denied with a hint instead of
pausing the graph on an interrupt.
"""

from __future__ import annotations

import sys
from typing import Literal

from ..agents.main import AgentOptions, build_agent
from ..config.settings import Settings, find_project_root
from ..mcp import McpManager
from ..protocol import events as ev
from ..runtime.commands import run_command
from ..runtime.session import Session
from ..runtime.store import SessionStore

OutputFormat = Literal["text", "json", "stream-json"]


def _emit(event: ev.Event) -> None:
    sys.stdout.write(event.model_dump_json() + "\n")
    sys.stdout.flush()


async def run_headless(
    settings: Settings,
    opts: AgentOptions,
    prompt: str,
    output_format: OutputFormat,
    verbose: bool,
    *,
    continue_session: bool = False,
    resume: str | None = None,
) -> int:
    opts.interactive = False
    root = find_project_root(opts.cwd.resolve())
    async with SessionStore.open(root, persist=settings.persist_sessions) as store:
        # No one to approve a project's MCP servers here: only approved ones start.
        mcp = McpManager(settings, root)
        for warning in await mcp.start(None):
            print(f"warning: {warning}", file=sys.stderr)
        agent = build_agent(settings, opts, checkpointer=store.checkpointer, mcp=mcp)
        session = Session(agent, ask=None, store=store)
        if resume or continue_session:
            found = await (store.find(resume) if resume else store.latest())
            if found is None:
                print("error: no saved conversation to continue", file=sys.stderr)
                await agent.close()
                await mcp.close()
                return 1
            await session.resume(found.thread_id)
        try:
            return await _run(session, prompt, output_format, verbose)
        finally:
            await mcp.close()


async def _run(session: Session, prompt: str, output_format: OutputFormat, verbose: bool) -> int:
    final: ev.Result | None = None
    try:
        if output_format == "stream-json":
            _emit(session.init_event())
        allow: list[str] = []
        if prompt.startswith("/"):  # a custom command or an MCP prompt
            result = await run_command(session, prompt)
            if result.send is not None:
                prompt, allow = result.send, result.allow
        async for event in session.run(prompt, allow=allow):
            if output_format == "stream-json":
                _emit(event)
            if isinstance(event, ev.Result):
                final = event
            elif isinstance(event, ev.Error):
                hint = f"\n  hint: {event.hint}" if event.hint else ""
                print(f"error: {event.message}{hint}", file=sys.stderr)
            elif verbose and isinstance(event, ev.ToolUse):
                print(f"> {event.label}", file=sys.stderr)
            elif verbose and isinstance(event, ev.ToolResult):
                print(f"  {'error' if event.is_error else 'done'}", file=sys.stderr)
    finally:
        await session.close()
        await session.agent.close()
    if final is None:
        return 1
    if output_format == "json":
        _emit(final)
    elif output_format == "text" and final.result:
        print(final.result)
    return 1 if final.is_error else 0
