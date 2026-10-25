"""What the rest of code search shares, without LangChain: errors, the
index's private folders, a Chroma server's key name. Light, so the command
line (`cmcoder rag ...`) loads without importing the engine."""

from __future__ import annotations

from pathlib import Path

from ..config.settings import config_dir


class StoreError(Exception):
    """The store can't be used (unreachable, refused): say why."""


class EmbeddingError(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def private_folder(path: Path) -> None:
    """Create `path` readable by the user only, like the folders above it up to
    ~/.cmcoder/index (an index holds copies of the code)."""
    path.mkdir(parents=True, exist_ok=True)
    top = config_dir() / "index"
    for folder in [path, *path.parents]:
        try:
            folder.chmod(0o700)
        except OSError:
            pass
        if folder == top or top not in folder.parents:
            break


def chroma_key_name(url: str) -> str:
    """Where `cmcoder rag setup` keeps a Chroma server's API key (keychain account)."""
    return "chroma:" + url.rstrip("/")


def chroma_needs_url() -> str:
    """Chroma is always used through its URL; what to set instead."""
    return (
        'Chroma needs its URL: "store": {"type": "chroma", "url": "http://localhost:8000"} '
        "for a Chroma running on this PC, or your Chroma server's address. "
        'Or use the built-in index ("store": {"type": "local"}). `cmcoder rag setup` sets either.'
    )
