"""Remember each model's real context window, as the server reported it.

Gateways often don't publish the window (no LiteLLM `/model/info`), so
cmcoder starts from the profile's default (32K for Qwen3). When the server
rejects a request as too long and says what the limit is ("maximum context
length is N tokens", "prompt is too long: ... > N maximum"), we now know N:
it is saved here, per gateway and model, and used from then on.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..config.settings import config_dir

SOURCE = "server limit (from its error)"
PROBE_SOURCE = "server limit (probe)"
LEARNED_TTL = 30 * 24 * 3600  # a known window: a month (servers get reconfigured)
UNKNOWN_TTL = 24 * 3600  # a probe without an answer: try again tomorrow

_WINDOW_PATTERNS = [
    re.compile(r"maximum context length is (\d+)"),
    re.compile(r"max_model_len\W{0,4}(\d+)"),
    re.compile(r"context (?:length|window|size) (?:is |of )?(?:only )?\(?(\d+) tokens"),
    re.compile(r"must have less than (\d+) (?:input )?tokens"),
    re.compile(
        r"\d+ tokens? > (\d+)"
    ),  # Anthropic: "prompt is too long: 205482 tokens > 200000 maximum"
    re.compile(r"maximum number of tokens allowed \((\d+)\)"),  # Gemini
    re.compile(r"maximum (?:input )?length (?:is|of) (\d+)"),
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


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def learned_entry(base_url: str, model: str) -> dict[str, Any] | None:
    """What was learned, if still fresh: {"tokens": N or None, "source", "at"}.

    `tokens` None: a probe got no answer (asked again after a day)."""
    entry = _read().get(_key(base_url, model))
    if isinstance(entry, int):  # the first format: just the number
        return {"tokens": entry, "source": SOURCE, "at": time.time()}
    if not isinstance(entry, dict):
        return None
    ttl = LEARNED_TTL if entry.get("tokens") else UNKNOWN_TTL
    if time.time() - float(entry.get("at", 0)) > ttl:
        return None
    return entry


def learned_window(base_url: str, model: str) -> tuple[int, str] | None:
    entry = learned_entry(base_url, model)
    if entry is None or not entry.get("tokens"):
        return None
    return int(entry["tokens"]), str(entry.get("source") or SOURCE)


def save_window(base_url: str, model: str, window: int | None, source: str = SOURCE) -> None:
    data = _read()
    data[_key(base_url, model)] = {"tokens": window, "source": source, "at": time.time()}
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # not being able to remember it is no reason to fail the turn
