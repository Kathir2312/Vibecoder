"""The code index in a session: opened at the start, `/index` to build it.

`open_code_index` is what `build_agent` calls: the project's index when code
search is on and the project has one (or a read-only shared one), else None
and, when worth saying, why. It also starts bringing the index up to date
(changed files only) in the background when the session has an event loop.

`index_command` is `/index [status|rebuild|clear]` in the terminal and the
IDEs. Built for the first time mid-session, the index is attached with a
rebuild of the agent (`rebuild_agent(..., code_index=...)`): `create_agent`
compiles a fixed tool list, so adding the CodeSearch tool means a new graph
on the same checkpointer, and the conversation goes on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.settings import Settings, SettingsError
from .embed import EmbeddingError
from .index import CodeIndex, Progress, open_index
from .setup import status_lines
from .stores import StoreError

if TYPE_CHECKING:
    from ..runtime.session import Session

NOT_SET_UP = (
    "Code search isn't set up: run `cmcoder rag setup` in a terminal "
    '(or "Set up code search" in VS Code).'
)
NO_INDEX_YET = "Code search is on, but this project has no index yet: run `cmcoder index`."


def open_code_index(settings: Settings, root: Path) -> tuple[CodeIndex | None, str | None]:
    try:
        index = open_index(settings, root)
    except (SettingsError, StoreError) as e:
        return None, f"Code search is off for this session: {e}"
    if index is None:
        return None, None
    # Known without asking the store: our own manifest, or a shared index.
    if not (index.has_local_index() or index.read_only):
        _close_soon(index)
        return None, NO_INDEX_YET if settings.rag.enabled is True else None
    if settings.rag.auto_update and not index.read_only:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop (a unit test building an agent): no background update
        else:
            index.start_background_update()
    return index, None


def _close_soon(index: CodeIndex) -> None:
    """Close an index we won't use (its store holds a file or an HTTP client)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(index.close())
        return
    task = loop.create_task(index.close())
    _closing.add(task)
    task.add_done_callback(_closing.discard)


_closing: set[asyncio.Task[Any]] = set()


async def index_command(
    session: Session, arg: str, progress: Callable[[Progress], None] | None = None
) -> list[str]:
    """`/index [status|rebuild|clear]`: lines to show."""
    from ..agents.main import rebuild_agent

    agent = session.agent
    settings: Settings = agent.spec["settings"]
    index = agent.code_index
    attached = index is not None
    if index is None:
        try:
            index = open_index(settings, agent.ctx.project_root)
        except (SettingsError, StoreError) as e:
            return [f"Code search is off: {e}"]
        if index is None:
            return [NOT_SET_UP]
    try:
        if arg == "status":
            if not attached and not await index.available():
                return ["No index for this project yet: /index builds it."]
            return await status_lines(index)
        if arg not in ("", "update", "rebuild", "clear"):
            return ["Usage: /index (build or update the code index) or /index status"]
        if index.read_only:
            return ["This index is read-only: it's kept up to date elsewhere."]
        if arg in ("rebuild", "clear"):
            await index.clear()
            if arg == "clear":
                return ["Deleted this project's index."]
        result = await index.update(progress)
        lines = [
            f"Indexed {result.indexed} files ({result.chunks} pieces) in {result.seconds:.1f}s; "
            f"{result.unchanged} unchanged, {result.removed} removed."
        ]
        if not attached and await index.available():
            session.agent = await rebuild_agent(agent, code_index=index)
            attached = True
            lines.append("Code search is on for the rest of this session.")
        return lines
    except (StoreError, EmbeddingError) as e:
        hint = getattr(e, "hint", None)
        return [f"Indexing failed: {e}" + (f"\n{hint}" if hint else "")]
    finally:
        if not attached:
            await index.close()
