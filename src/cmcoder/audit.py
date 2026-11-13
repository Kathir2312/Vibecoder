"""Audit log: a record, in the user's own folder, of what the agent ran and changed.

One JSON object per line, one file per day (`audit-YYYY-MM-DD.jsonl`, UTC), appended to and never
rewritten: tool calls and how they ended, every permission question and its answer, what a rule or a
mode refused, the files each request changed, model switches, errors and how each request ended.
Meant to be read without cmcoder (`jq`, a spreadsheet, a log collector). Off by default; managed
settings (the administrator's) can switch it on, and a repository's own settings can neither
switch it on or off nor move it.

What is left out on purpose: file contents (a Write or Edit is recorded as the path and sizes), the
model's replies, tool output, and, unless `audit.includePrompts` says so, the user's prompts (their
length only). Text that is recorded (commands, labels, errors) goes through `redact`, which hides
what looks like a key, a token or a password: best effort, so the log is still to be handled as
sensitive.

LANGCHAIN / LANGGRAPH: nothing here. The records come from the protocol events the session already
yields (`runtime/session.py`), the subagents' calls included, which is more than one callback handler
would see of the permission answers; the file is plain JSON lines because an audit trail should not
need LangChain to be read.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .protocol import events as ev

MAX_FIELD = 300  # characters of one recorded text
BODY_KEYS = frozenset(
    {"content", "new_string", "old_string", "text", "edits", "prompt", "new_text"}
)

# Things that look like a secret, hidden wherever the log records text.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[redacted]"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd|credentials?)"
            r"\w*\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"(?i)(--?(?:password|passwd|token|api-key|apikey|secret)(?:=|\s+))(?:\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+(@)"), r"\1[redacted]\2"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted]"),
]


def redact(text: str) -> str:
    """`text` with what looks like a key, a token or a password hidden."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _short(text: str) -> str:
    text = redact(" ".join(str(text).split()))
    return text if len(text) <= MAX_FIELD else text[: MAX_FIELD - 1] + "…"


def summarize_input(data: dict[str, Any]) -> dict[str, Any]:
    """A tool call's input as the log keeps it: short values as they are (redacted), the bodies of
    files and prompts as their length."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in BODY_KEYS:
            out[key] = f"<{len(str(value))} chars>"
        elif isinstance(value, str):
            out[key] = _short(value)
        elif isinstance(value, bool | int | float) or value is None:
            out[key] = value
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry, no environment: still log
        return "unknown"


class AuditLog:
    """Writes the records; `enabled` can be switched during a session (`/audit on|off`)."""

    def __init__(self, folder: Path, *, enabled: bool, include_prompts: bool = False) -> None:
        self.folder = folder
        self.enabled = enabled
        self.include_prompts = include_prompts
        self.user = _who()
        self.host = platform.node()
        self.failed: str | None = None  # why the last write failed, said once by `/audit`

    # --- files --------------------------------------------------------------------------
    def path_for(self, when: datetime | None = None) -> Path:
        day = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
        return self.folder / f"audit-{day}.jsonl"

    def record(self, session_id: str, what: str, **fields: Any) -> None:
        """Append one record. Never raises: a full disk must not stop the agent."""
        if not self.enabled:
            return
        now = datetime.now(UTC)
        line = {
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "session": session_id,
            "event": what,
            "user": self.user,
            "host": self.host,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path_for(now), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.failed = None
        except OSError as e:
            self.failed = f"{type(e).__name__}: {e}"

    def tail(self, session_id: str, count: int = 15) -> list[dict[str, Any]]:
        """The last `count` records of this session in today's file (and yesterday's, if needed)."""
        rows: list[dict[str, Any]] = []
        today = datetime.now(UTC)
        for path in (self.path_for(today), self.path_for(_yesterday(today))):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            mine = []
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("session") == session_id:
                    mine.append(row)
            rows = mine + rows
            if len(rows) >= count:
                break
        return rows[-count:]

    # --- what the session tells it ----------------------------------------------------------
    def session_started(
        self, session_id: str, *, cwd: str, model: str, mode: str, project: str
    ) -> None:
        self.record(
            session_id,
            "session_start",
            cwd=cwd,
            project=project,
            model=model,
            permission_mode=mode,
            version=__version__,
        )

    def prompt(self, session_id: str, turn: int, text: str) -> None:
        self.record(
            session_id,
            "prompt",
            turn=turn,
            chars=len(text),
            text=_short(text) if self.include_prompts else None,
        )

    def permission(
        self, session_id: str, turn: int, request: ev.PermissionRequest, answer: dict[str, Any]
    ) -> None:
        allowed = bool(answer.get("allow"))
        remember = (
            "project"
            if answer.get("remember")
            else "session"
            if answer.get("remember_session")
            else None
        )
        self.record(
            session_id,
            "permission",
            turn=turn,
            tool=request.name,
            label=_short(request.label),
            decision="allow" if allowed else "deny",
            remember=remember if allowed else None,
            high_risk=True if request.high_risk else None,
            path=request.change.path if request.change else None,
            agent=request.agent,
        )

    def observe(self, event: ev.Event, session_id: str, turn: int) -> None:
        """The records the events of a request make."""
        if not self.enabled:
            return
        if isinstance(event, ev.ToolUse):
            self.record(
                session_id,
                "tool_call",
                turn=turn,
                tool=event.name,
                label=_short(event.label),
                input=summarize_input(event.input),
                id=event.id,
                parent=event.parent_tool_use_id,
            )
        elif isinstance(event, ev.ToolResult):
            self.record(
                session_id,
                "tool_result",
                turn=turn,
                tool=event.name,
                id=event.id,
                ok=not event.is_error,
                summary=_short(event.summary or "") or None,
                exit_code=event.exit_code,
                duration_ms=event.duration_ms,
                stopped=True if event.stopped else None,
            )
        elif isinstance(event, ev.PermissionDenied):
            self.record(
                session_id,
                "denied",
                turn=turn,
                tool=event.name,
                reason=_short(event.reason),
            )
        elif isinstance(event, ev.TurnChanges):
            self.record(
                session_id,
                "files_changed",
                turn=event.turn,
                files=[
                    {"path": f.path, "status": f.status, "added": f.added, "removed": f.removed}
                    for f in event.files
                ],
            )
        elif isinstance(event, ev.ModelChanged):
            self.record(
                session_id, "model", turn=turn, model=event.model, reason=event.reason or None
            )
        elif isinstance(event, ev.Error):
            self.record(
                session_id, "error", turn=turn, kind=event.kind, message=_short(event.message)
            )
        elif isinstance(event, ev.Result):
            self.record(
                session_id,
                "turn_end",
                turn=turn,
                result=event.subtype,
                duration_ms=event.duration_ms,
                model_calls=event.num_turns,
                prompt_tokens=event.usage.get("prompt_tokens"),
                completion_tokens=event.usage.get("completion_tokens"),
            )

    def reviewed(self, session_id: str, turn: int, action: str, lines: list[str]) -> None:
        """The user's decision on a request's changes in the review: `keep` or `reject`."""
        self.record(
            session_id,
            "review",
            turn=turn,
            action=action,
            files=[_short(line) for line in lines[:20]],
        )

    def rewound(self, session_id: str, turn: int, actions: list[ev.RewindAction]) -> None:
        self.record(
            session_id,
            "rewind",
            turn=turn,
            files=[{"path": a.path, "action": a.action} for a in actions],
        )


def _yesterday(day: datetime) -> datetime:
    return datetime.fromtimestamp(day.timestamp() - 86400, UTC)


def line_for_humans(row: dict[str, Any]) -> str:
    """One record as `/audit` shows it: time, what, and the one thing that says most."""
    when = str(row.get("ts", ""))[11:19]
    kind = str(row.get("event", ""))
    detail = {
        "tool_call": row.get("label"),
        "tool_result": f"{row.get('tool')} {'ok' if row.get('ok') else 'failed'}"
        + (f" ({row['summary']})" if row.get("summary") else ""),
        "permission": f"{row.get('decision')} {row.get('label')}"
        + (f" (always, {row['remember']})" if row.get("remember") else ""),
        "denied": f"{row.get('tool')}: {row.get('reason')}",
        "files_changed": ", ".join(f["path"] for f in row.get("files", [])[:4])
        + (" …" if len(row.get("files", [])) > 4 else ""),
        "model": f"{row.get('model')}" + (f" ({row['reason']})" if row.get("reason") else ""),
        "error": f"{row.get('kind')}: {row.get('message')}",
        "turn_end": f"{row.get('result')} in {(row.get('duration_ms') or 0) / 1000:.1f}s",
        "prompt": f"{row.get('chars')} characters"
        + (f": {row['text']}" if row.get("text") else ""),
        "session_start": f"{row.get('model')} in {row.get('cwd')}",
        "rewind": f"{len(row.get('files', []))} file(s) put back",
        "review": f"{row.get('action')}: " + "; ".join(row.get("files", [])[:3]),
    }.get(kind, "")
    return f"{when}  {kind:<13} {detail}".rstrip()
