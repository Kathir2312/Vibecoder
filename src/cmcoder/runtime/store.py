"""Saved conversations: LangGraph's SQLite checkpointer, one database per project.

LANGGRAPH CONCEPTS IN THIS FILE

* A *checkpointer* saves the graph state after every step, keyed by
  `thread_id`. `InMemorySaver` keeps it in memory (gone when the program
  exits); `AsyncSqliteSaver` writes it to a SQLite file, so a conversation can
  be continued tomorrow with `--continue` / `--resume`. The agent code doesn't
  change at all: a checkpointer is just a constructor argument of
  `create_agent`.
* Every step is kept, not only the latest: that history is what `/rewind`
  uses (runtime/session.py) through `graph.aget_state_history(config)`.
* `adelete_thread(thread_id)` removes a conversation and all of its checkpoints.

What LangGraph doesn't store is our own index of sessions (title, last use,
working folder), so `/resume` can list them without loading every thread. That
lives in a small extra table, `cmcoder_sessions`, in the same file.

Privacy: the checkpoints contain the conversation, so code. The folder is
created owner-only (0700) and the database owner-only (0600), as
cmcoderagent's JSON Lines sessions were.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ..config.settings import config_dir
from .serde import checkpoint_serde

DB_NAME = "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cmcoder_sessions (
    thread_id TEXT PRIMARY KEY,
    title     TEXT NOT NULL DEFAULT '',
    cwd       TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    turns     INTEGER NOT NULL DEFAULT 0
)
"""


def project_key(project_root: Path) -> str:
    """Folder name for a project: readable name + hash of its normalised path.

    `os.path.normcase` lower-cases Windows paths, so C:\\Repo and c:\\repo are
    the same project there.
    """
    norm = os.path.normcase(os.path.abspath(str(project_root)))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_root.name)[:40]
    return f"{name or 'project'}-{digest}"


def sessions_dir(project_root: Path) -> Path:
    return config_dir() / "projects" / project_key(project_root)


@dataclass
class SessionInfo:
    thread_id: str
    title: str
    cwd: str
    model: str
    created: float
    updated: float
    turns: int


def _owner_only(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # Windows: per-user profile folders are already private
        pass


class SessionStore:
    """Opens the project's checkpoint database and keeps the session index.

    Use as an async context manager:

        async with SessionStore.open(project_root) as store:
            agent = build_agent(settings, opts, checkpointer=store.checkpointer)
    """

    def __init__(self, checkpointer: BaseCheckpointSaver[Any], conn: aiosqlite.Connection | None):
        self.checkpointer = checkpointer
        self._conn = conn

    @classmethod
    def open(cls, project_root: Path, *, persist: bool = True) -> _StoreContext:
        return _StoreContext(project_root, persist)

    @property
    def persistent(self) -> bool:
        return self._conn is not None

    # --- the session index ------------------------------------------------------
    async def touch(
        self, thread_id: str, *, cwd: str, model: str, title: str | None = None
    ) -> None:
        """Record that a session ran a turn (creates the row on first use)."""
        if self._conn is None:
            return
        now = time.time()
        await self._conn.execute(
            "INSERT INTO cmcoder_sessions (thread_id, title, cwd, model, created, updated, turns) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(thread_id) DO UPDATE SET updated = excluded.updated, "
            "model = excluded.model, turns = turns + 1",
            (thread_id, title or "", cwd, model, now, now),
        )
        await self._conn.commit()

    async def set_title(self, thread_id: str, title: str) -> None:
        if self._conn is None:
            return
        await self._conn.execute(
            "UPDATE cmcoder_sessions SET title = ? WHERE thread_id = ?", (title, thread_id)
        )
        await self._conn.commit()

    async def list(self) -> list[SessionInfo]:
        """Most recently used first."""
        if self._conn is None:
            return []
        async with self._conn.execute(
            "SELECT thread_id, title, cwd, model, created, updated, turns "
            "FROM cmcoder_sessions ORDER BY updated DESC"
        ) as cur:
            return [SessionInfo(*row) async for row in cur]

    async def latest(self) -> SessionInfo | None:
        sessions = await self.list()
        return sessions[0] if sessions else None

    async def find(self, ref: str) -> SessionInfo | None:
        """A session by full id or a unique prefix of it."""
        matches = [s for s in await self.list() if s.thread_id.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    async def delete(self, thread_id: str) -> None:
        await self.checkpointer.adelete_thread(thread_id)
        if self._conn is not None:
            await self._conn.execute(
                "DELETE FROM cmcoder_sessions WHERE thread_id = ?", (thread_id,)
            )
            await self._conn.commit()

    async def cleanup(self, days: int) -> int:
        """Delete sessions not used for `days` days (settings `cleanupPeriodDays`)."""
        cutoff = time.time() - days * 86400
        old = [s for s in await self.list() if s.updated < cutoff]
        for s in old:
            await self.delete(s.thread_id)
        return len(old)


class _StoreContext:
    """`async with SessionStore.open(...)`: opens and closes the database."""

    def __init__(self, project_root: Path, persist: bool) -> None:
        self.project_root = project_root
        self.persist = persist
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SessionStore:
        if not self.persist:  # `persistSessions: false`: nothing touches the disk
            return SessionStore(InMemorySaver(serde=checkpoint_serde()), None)
        folder = sessions_dir(self.project_root)
        folder.mkdir(parents=True, exist_ok=True)
        _owner_only(folder, stat.S_IRWXU)
        db = folder / DB_NAME
        db.touch(exist_ok=True)
        _owner_only(db, stat.S_IRUSR | stat.S_IWUSR)
        self._conn = await aiosqlite.connect(db)
        saver = AsyncSqliteSaver(self._conn, serde=checkpoint_serde())
        await saver.setup()  # LangGraph's own tables (checkpoints, writes)
        await self._conn.execute(_SCHEMA)
        await self._conn.commit()
        return SessionStore(saver, self._conn)

    async def __aexit__(self, *exc: object) -> None:
        if self._conn is not None:
            await self._conn.close()
