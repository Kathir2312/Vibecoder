"""The settings page's engine: which settings it edits, what they are now, and saving a change.

The page (`/settings` in the chat) edits a fixed, safe set of your own settings (the models, how much
the agent may do on its own, the checks after an edit, the audit log). Everything else (gateways,
hooks, sandbox rules, MCP servers) stays in `settings.json`, where a person reads what they change.
A change is written to `~/.cmcoder/settings.json`, which keeps every other key it has; the file is
checked as a whole before it is replaced, and replaced in one step, so a mistake never leaves it half
written. A setting the organisation fixes in its managed settings is shown, and cannot be changed
here. The API key is not a setting of this page and is never typed into the chat (it would pass
through the IDE and its logs): `cmcoder login` stores it.

LANGCHAIN / LANGGRAPH: nothing here; settings are cmcoder's own (pydantic) model.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..core.permissions import MODES
from ..protocol import events as ev
from . import settings as settings_module
from .settings import Settings, SettingsError, _read_json, config_dir

Kind = Literal["text", "choice", "bool", "number", "lines"]


@dataclass(frozen=True)
class FieldSpec:
    key: str  # the dotted path in settings.json
    label: str
    group: str
    kind: Kind
    help: str
    choices: tuple[str, ...] = ()
    low: int = 0
    high: int = 0


# Modes a person picks for everyday work; bypassPermissions is not offered on a page.
_MODE_CHOICES = tuple(m for m in MODES if m != "bypassPermissions")

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "model",
        "Main model",
        "Models",
        "text",
        "The model that does the work: provider:name, or a name on your gateway.",
    ),
    FieldSpec(
        "smallFastModel",
        "Small model",
        "Models",
        "text",
        "Writes summaries and titles; the main model when empty.",
    ),
    FieldSpec(
        "visionModel",
        "Vision model",
        "Models",
        "text",
        "Describes pasted images for a main model that cannot see them.",
    ),
    FieldSpec(
        "fallbackModel",
        "Fallback model",
        "Models",
        "text",
        "Finishes a request when the main model's gateway fails.",
    ),
    FieldSpec(
        "permissions.defaultMode",
        "Permission mode",
        "Behaviour",
        "choice",
        "How much cmcoder may do before it asks.",
        _MODE_CHOICES,
    ),
    FieldSpec(
        "maxTurns",
        "Model calls per request",
        "Behaviour",
        "number",
        "A request stops after this many model calls.",
        low=1,
        high=1000,
    ),
    FieldSpec(
        "autoCompact",
        "Summarise long conversations",
        "Behaviour",
        "bool",
        "Replace old messages by a summary before the window is full.",
    ),
    FieldSpec(
        "critic.enabled",
        "Critique every answer",
        "Behaviour",
        "bool",
        "A second agent reviews each answer before you see it.",
    ),
    FieldSpec(
        "verify.enabled",
        "Check after edits",
        "Checks",
        "bool",
        "Run the commands below after a request that changed code.",
    ),
    FieldSpec(
        "verify.commands",
        "Check commands",
        "Checks",
        "lines",
        "Build, test and lint commands, one per line, run in the project folder.",
    ),
    FieldSpec(
        "verify.maxRounds",
        "Attempts to fix a failing check",
        "Checks",
        "number",
        "How many times the agent is sent back to fix it.",
        low=1,
        high=5,
    ),
    FieldSpec(
        "audit.enabled",
        "Audit log",
        "Records",
        "bool",
        "Write what cmcoder ran and changed to a log file in your folder.",
    ),
    FieldSpec(
        "audit.includePrompts",
        "Audit log: prompts",
        "Records",
        "bool",
        "Also record the text of your prompts (keys hidden).",
    ),
)
BY_KEY = {f.key: f for f in FIELDS}


def user_settings_path() -> Path:
    return config_dir() / "settings.json"


# --- reading ----------------------------------------------------------------------------------


def value_of(settings: Settings, key: str) -> Any:
    """What `settings` has for one of the page's settings."""
    s = settings
    return {
        "model": s.model,
        "smallFastModel": s.small_fast_model,
        "visionModel": s.vision_model,
        "fallbackModel": s.fallback_model,
        "permissions.defaultMode": s.permissions.default_mode,
        "maxTurns": s.max_turns,
        "autoCompact": s.auto_compact,
        "critic.enabled": s.critic.enabled,
        "verify.enabled": s.verify.enabled,
        "verify.commands": list(s.verify.commands),
        "verify.maxRounds": s.verify.max_rounds,
        "audit.enabled": s.audit.enabled,
        "audit.includePrompts": s.audit.include_prompts,
    }[key]


def _has(data: dict[str, Any], dotted: str) -> bool:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def locked_keys() -> set[str]:
    """The settings the organisation fixes (managed settings): shown, not editable here."""
    try:
        managed = _read_json(settings_module.managed_settings_path())
    except SettingsError:
        return set()
    return {k for k in BY_KEY if _has(managed, k)}


def state(
    settings: Settings,
    *,
    notice: str = "",
    models: list[str] | None = None,
    keys: dict[str, bool] | None = None,
) -> ev.SettingsState:
    """The page: every setting with its value, the gateways and whether each has a key.
    `keys`: provider -> key found (looked up by the caller: it may ask the OS keychain)."""
    locked = locked_keys()
    return ev.SettingsState(
        fields=[
            ev.SettingsField(
                key=f.key,
                label=f.label,
                group=f.group,
                kind=f.kind,
                help=f.help,
                value=value_of(settings, f.key),
                choices=list(f.choices),
                locked=f.key in locked,
                note="set by your organisation" if f.key in locked else "",
            )
            for f in FIELDS
        ],
        gateways=[
            ev.SettingsGateway(
                name=name,
                base_url=cfg.base_url,
                key="not needed"
                if cfg.auth.type == "none"
                else ("set" if (keys or {}).get(name) else "missing"),
            )
            for name, cfg in settings.providers.items()
        ],
        file=str(user_settings_path()),
        notice=notice,
        models=models or [],
    )


def describe_lines(settings: Settings) -> list[str]:
    """The settings as text, for a terminal."""
    locked = locked_keys()
    lines = [f"Your settings file: {user_settings_path()}"]
    for f in FIELDS:
        value = value_of(settings, f.key)
        shown = ", ".join(value) if isinstance(value, list) else value
        mark = "  (set by your organisation)" if f.key in locked else ""
        lines.append(f"  {f.label:<34} {shown if shown not in (None, '') else '-'}{mark}")
    lines.append("Change one in that file; the chat's settings page edits them for you.")
    return lines


# --- writing ----------------------------------------------------------------------------------


class SettingsEditError(Exception):
    """A change that cannot be made; the message is for the user."""


def parse(spec: FieldSpec, text: str) -> Any:
    """The value `text` means for `spec`, or None to put the setting back to its default."""
    text = text.strip()
    if text in ("", "null", "default"):
        return None
    try:
        if spec.kind == "bool":
            if text not in ("true", "false"):
                raise ValueError
            return text == "true"
        if spec.kind == "number":
            number = int(text)
            if not spec.low <= number <= spec.high:
                raise SettingsEditError(f"{spec.label}: a number from {spec.low} to {spec.high}.")
            return number
        if spec.kind == "lines":
            value = json.loads(text)
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError
            return [x.strip() for x in value if x.strip()] or None
    except (ValueError, TypeError) as e:
        expect = {
            "bool": "true or false",
            "number": "a whole number",
            "lines": 'a list like ["npm test", "npm run build"]',
        }[spec.kind]
        raise SettingsEditError(f"{spec.label}: expected {expect}.") from e
    value = json.loads(text) if text.startswith('"') else text
    if spec.kind == "choice" and value not in spec.choices:
        raise SettingsEditError(f"{spec.label}: one of {', '.join(spec.choices)}.")
    if not isinstance(value, str):
        raise SettingsEditError(f"{spec.label}: text.")
    return value


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    *path, last = dotted.split(".")
    node = data
    for part in path:
        child = node.get(part)
        if not isinstance(child, dict):
            if value is None:
                return  # nothing to take out
            child = node[part] = {}
        node = child
    if value is None:
        node.pop(last, None)
        if not node and path:  # leave no empty section behind
            parent: Any = data
            for part in path[:-1]:
                parent = parent[part]
            parent.pop(path[-1], None)
    else:
        node[last] = value


def write_value(key: str, text: str, path: Path | None = None) -> tuple[Any, str]:
    """Save one setting in the user's settings file: (the value saved, a sentence for the user).
    Raises `SettingsEditError` when it cannot be: not a setting of this page, fixed by the
    organisation, a value that does not fit, a file that is not valid JSON."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise SettingsEditError(
            f"{key!r} is not a setting this page edits. Edit {user_settings_path()} for others."
        )
    if key in locked_keys():
        raise SettingsEditError(
            f"{spec.label} is set by your organisation and cannot be changed here."
        )
    value = parse(spec, text)
    target = path or user_settings_path()
    try:
        data = _read_json(target)
    except SettingsError as e:
        raise SettingsEditError(str(e)) from e
    _set(data, key, value)
    try:
        Settings.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        raise SettingsEditError(f"{spec.label}: {first.get('msg', 'not valid')}.") from e
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    shown = (
        "back to its default"
        if value is None
        else f"{', '.join(value) if isinstance(value, list) else value}"
    )
    return value, f"Saved {spec.label}: {shown}."
