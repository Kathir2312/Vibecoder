"""Bash tool: runs commands in a persistent shell."""

from __future__ import annotations

import asyncio
import time

from pydantic import Field

from ..compat import SHELL_HELP, find_shell
from .base import Tool, ToolContext, ToolInput, ToolResult, truncate_middle
from .progress import LiveOutput, current_call, register_stop, unregister_stop
from .shell import PersistentShell

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600


class BashInput(ToolInput):
    command: str = Field(description="The shell command to run.")
    timeout_seconds: int | None = Field(
        None,
        ge=1,
        le=MAX_TIMEOUT,
        description=f"Timeout in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
    )
    description: str | None = Field(
        None, description="Short description of what the command does (5-10 words)."
    )
    dangerously_disable_sandbox: bool = Field(
        False,
        description="Run outside the sandbox. Only after a command failed because of the "
        "sandbox (read-only file system, blocked network) and it is really needed; the user "
        "is always asked.",
    )


class BashTool(Tool):
    name = "Bash"
    description = (
        "Run a program in a persistent bash shell (the working directory and environment carry "
        "over between calls): tests, builds, linters, git, package managers. stdin is closed, so "
        "interactive commands fail; pass non-interactive flags instead. Do NOT use it for file "
        "work: create files with Write (not cat/echo > file), look at them with Read (not "
        "cat/head/tail/sed -n), change them with Edit (not sed -i), search with Grep and Glob "
        "(not grep/find). Output is truncated if very long."
    )
    Input = BashInput

    def permission_target(self, args: BashInput, ctx: ToolContext) -> str:
        return args.command.strip()

    def describe(self, args: BashInput, ctx: ToolContext) -> str:
        cmd = args.command.strip().replace("\n", " ")
        label = f"Bash({cmd if len(cmd) <= 80 else cmd[:77] + '...'})"
        return f"{label} [outside the sandbox]" if outside_sandbox(args, ctx) else label

    async def run(self, args: BashInput, ctx: ToolContext) -> ToolResult:
        shell = find_shell()
        if shell is None and (ctx.shell is None or ctx.unsandboxed_shell is None):
            return ToolResult(f"No shell available. {SHELL_HELP}", is_error=True)
        if outside_sandbox(args, ctx):
            if ctx.unsandboxed_shell is None:
                assert shell is not None
                ctx.unsandboxed_shell = PersistentShell(ctx.cwd, shell)
            runner = ctx.unsandboxed_shell
        else:
            if ctx.shell is None:
                assert shell is not None
                ctx.shell = PersistentShell(ctx.cwd, shell, sandbox=ctx.sandbox)
            runner = ctx.shell
        # While it runs: its output goes to the chat live, and "Stop this command" can end it.
        live = LiveOutput.for_current_call()
        stop = asyncio.Event()
        call = current_call.get()
        if call is not None:
            register_stop(call[0], stop.set)
        started = time.monotonic()
        try:
            res = await runner.run(
                args.command,
                float(args.timeout_seconds or DEFAULT_TIMEOUT),
                on_output=live.feed if live else None,
                stop=stop,
            )
        finally:
            if call is not None:
                unregister_stop(call[0])
            if live is not None:
                live.flush()
        meta = {
            "exit_code": res.exit_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "total_lines": len(res.output.rstrip("\n").split("\n")) if res.output.strip() else 0,
            "stopped": res.stopped,
        }
        if res.stopped:
            note = "(The user stopped this command before it finished. Its output so far follows.)"
            shown = truncate_middle(res.output.rstrip("\n"), ctx.max_output_chars)
            return ToolResult(
                f"{note}\n{shown}" if shown else note, is_error=True, summary="stopped", meta=meta
            )
        out = truncate_middle(res.output.rstrip("\n"), ctx.max_output_chars)
        if ctx.sandbox is not None and runner is ctx.shell and _sandbox_blocked(res.output):
            out += "\n\n" + (
                "(This ran in the sandbox: it can write only inside the project and reach only "
                "allowed hosts. If the command really needs more, run it again with "
                "dangerously_disable_sandbox; the user will be asked.)"
                if ctx.sandbox.allow_unsandboxed
                else "(This ran in the sandbox, which can't be turned off here: tell the user "
                "what is blocked.)"
            )
        if res.exit_code not in (0, None):
            out = f"{out}\n\nExit code {res.exit_code}" if out else f"Exit code {res.exit_code}"
        hidden = hidden_pipe_failure(res.pipe_status) if res.exit_code == 0 else None
        if hidden:
            # e.g. `pip install x | tail -5`: tail's exit 0 hides pip's failure.
            out = (
                (f"{out}\n\n" if out else "")
                + f"Exit codes in the pipe: {hidden} (a command before the last one exited non-zero; its errors may be hidden)"
            )
        if not out:
            out = "(no output)"
        failed = res.exit_code != 0
        if res.timed_out:
            summary = "timed out"
        elif res.exit_code is None:
            summary = "shell exited"
        else:
            summary = f"exit {res.exit_code}" + (f" · pipe {hidden}" if hidden else "")
        return ToolResult(out, is_error=failed, summary=summary, meta=meta)


def outside_sandbox(args: BashInput, ctx: ToolContext) -> bool:
    """True when this call runs unsandboxed although a sandbox is active."""
    return ctx.sandbox is not None and args.dangerously_disable_sandbox


def _sandbox_blocked(output: str) -> bool:
    return any(
        s in output
        for s in ("Read-only file system", "cmcoder sandbox:", "Operation not permitted")
    )


SIGPIPE_EXIT = 141  # `yes | head -1`: the writer is stopped when the reader quits; not a failure


def hidden_pipe_failure(pipe_status: list[int] | None) -> str | None:
    """The pipe's exit codes as text when a command before the last one failed."""
    if not pipe_status or all(c in (0, SIGPIPE_EXIT) for c in pipe_status[:-1]):
        return None
    return " ".join(map(str, pipe_status))
