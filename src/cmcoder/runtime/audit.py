"""`/audit`: what the audit log has recorded of this conversation, and a switch for it.

`/audit`        the log's state and the last records of this conversation
`/audit <n>`    the last n records (up to 100)
`/audit on|off` switch recording for this conversation (managed settings may have it on already)

LANGCHAIN / LANGGRAPH: nothing here; see audit.py.
"""

from __future__ import annotations

from ..audit import line_for_humans
from .commands import CommandResult
from .session import Session

DEFAULT_ROWS = 15
MAX_ROWS = 100


def audit_lines(session: Session, arg: str) -> CommandResult:
    audit = session.agent.audit
    if audit is None:
        return CommandResult(["The audit log is not available here."])
    count = DEFAULT_ROWS
    if arg in ("on", "off"):
        was = audit.enabled
        audit.enabled = arg == "on"
        if audit.enabled and not was:
            session.audit_session_started()  # so that the log says which conversation this is
    elif arg.isdigit():
        count = min(max(int(arg), 1), MAX_ROWS)
    elif arg:
        return CommandResult(["Usage: /audit [on|off|<number of records>]"])
    state = "on" if audit.enabled else "off"
    lines = [
        f"The audit log is {state}"
        + (f": {audit.path_for()}" if audit.enabled else " (settings: audit.enabled, or /audit on)")
    ]
    if audit.failed:
        lines.append(f"The last write failed: {audit.failed}")
    if audit.enabled:
        rows = audit.tail(session.session_id, count)
        lines += [line_for_humans(r) for r in rows] or [
            "Nothing recorded for this conversation yet."
        ]
    return CommandResult(lines)
