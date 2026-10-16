"""Remember each model's real context window, as the server reported it.

Gateways often don't publish the window (no LiteLLM `/model/info`), so
cmcoder starts from the profile's default (32K for Qwen3). When the server
rejects a request with "maximum context length is N tokens", we now know N:
it is saved here, per gateway and model, and used from then on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config.settings import config_dir

SOURCE = "server limit (from its error)"

_WINDOW_PATTERNS = [
    re.compile(r"maximum context length is (\d+)"),
    re.compile(r"max_model_len\W{0,4}(\d+)"),
    re.compile(r"context (?:length|window|size) (?:is |of )?(?:only )?\(?(\d+) tokens"),
    re.compile(r"must have less than (\d+) (?:input )?tokens"),
]


def parse_context_window(text: str) -> int | None:
    """The context window a server states in an error message, if any."""
    low = text.lower().replace(",", "")
    for pattern in _WINDOW_PATTERNS:
        if m := pattern.search(low):
            n = int(m.group(1))
            if 512 <= n <= 10_000_000:  # ignore unrelated small numbers
                return n
    return None


def _file() -> Path:
    return config_dir() / "context-windows.json"


def _key(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')}|{model}"


def learned_window(base_url: str, model: str) -> tuple[int, str] | None:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        n = int(data[_key(base_url, model)])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return n, SOURCE


def save_window(base_url: str, model: str, window: int) -> None:
    path = _file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[_key(base_url, model)] = window
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # not being able to remember it is no reason to fail the turn
