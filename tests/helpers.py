"""Helpers to build an agent against the scripted mock server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import AskFn, Session
from cmcoder.runtime.store import SessionStore


def mock_settings(base_url: str, **extra: Any) -> Settings:
    return Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b", **extra}
    )


def make_session(
    base_url: str,
    cwd: Path,
    *,
    ask: AskFn | None = None,
    store: SessionStore | None = None,
    settings: dict[str, Any] | None = None,
    **opts: Any,
) -> Session:
    agent = build_agent(
        mock_settings(base_url, **(settings or {})),
        AgentOptions(cwd=cwd, interactive=ask is not None, **opts),
        checkpointer=store.checkpointer if store is not None else None,
    )
    return Session(agent, ask=ask, store=store)


async def run_turn(session: Session, prompt: str) -> list[ev.Event]:
    return [e async for e in session.run(prompt)]


def of_type(events: list[ev.Event], kind: type) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]
