"""`/verify`: run the project's checks now, or switch the automatic check after edits.

`/verify`        run `verify.commands` now (what the agent's check after an edit would run)
`/verify on|off` switch the automatic check for this conversation
`/verify setup`  suggest commands for this project's settings (never run by themselves)

LANGCHAIN / LANGGRAPH: nothing here; the loop is middleware/verify.py, the running is verify.py.
"""

from __future__ import annotations

import json
import time

from ..protocol import events as ev
from ..verify import detect_commands, run_check
from .commands import CommandResult
from .session import Session

SHOWN_LINES = 12  # of a failed command's output in the text; the run card has all of it


def _tail(text: str, n: int = SHOWN_LINES) -> list[str]:
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    return [f"    {line}" for line in lines[-n:]]


def _setup(session: Session) -> list[str]:
    found = detect_commands(session.agent.ctx.project_root)
    lines = ["No verify.commands are set, so nothing runs after an edit."]
    if found:
        example = json.dumps({"verify": {"commands": found}}, indent=2)
        lines += [
            "This project looks like it has:",
            *(f"  {c}" for c in found),
            "Put the ones you want in your settings.json (a project's own settings count only when "
            "the project is trusted: `cmcoder trust`):",
            *example.split("\n"),
        ]
    else:
        lines.append(
            'Add "verify": {"commands": ["<your build or test command>"]} to settings.json.'
        )
    return lines


async def verify_lines(session: Session, arg: str) -> CommandResult:
    verify = session.agent.verify
    if verify is None:
        return CommandResult(["Verify is not available here."])
    if arg in ("on", "off"):
        verify.enabled = arg == "on"
        state = "on" if verify.enabled else "off"
        lines = [
            f"The check after edits is {state} "
            f"({len(verify.commands)} command(s); up to {verify.max_rounds} attempts to fix)."
        ]
        return CommandResult(lines + ([] if verify.commands else _setup(session)))
    if arg in ("setup", "help", "?") or not verify.commands:
        return CommandResult(_setup(session))
    if arg:
        return CommandResult(["Usage: /verify [on|off|setup]"])
    lines: list[str] = []
    events: list[ev.Event] = []
    failed = False
    for number, command in enumerate(verify.commands, 1):
        call_id = f"verify-now-{int(time.time() * 1000)}-{number}"
        result = await run_check(
            command,
            verify.root,
            sandbox=verify.sandbox,
            timeout=verify.timeout,
            max_chars=verify.max_output_chars,
        )
        events += [
            ev.ToolUse(
                id=call_id, name="Verify", input={"command": command}, label=f"Verify({command})"
            ),
            ev.ToolResult(
                id=call_id,
                name="Verify",
                content=result.output or "(no output)",
                is_error=not result.ok,
                summary=result.summary,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                total_lines=result.total_lines,
            ),
        ]
        lines.append(
            f"{'✓' if result.ok else '✗'} {command}  ({result.summary}, {result.duration_ms / 1000:.1f}s)"
        )
        if not result.ok:
            failed = True
            lines += _tail(result.output)
            break  # the first failure stops it, as after an edit
    if not failed:
        lines.append("All checks pass.")
    return CommandResult(lines, events=events, quiet=True)
