"""Phase 4 hardening: checkpoints load only our types; LangSmith stays off."""

from __future__ import annotations

from pydantic import BaseModel

from cmcoder.agents.team import Stories, TestReport
from cmcoder.middleware.critic import Verdict
from cmcoder.runtime.serde import checkpoint_serde


class NotOurs(BaseModel):
    x: int = 1


def roundtrip(value: object) -> object:
    serde = checkpoint_serde()
    return serde.loads_typed(serde.dumps_typed(value))


def test_our_structured_outputs_come_back() -> None:
    for value in (
        Verdict(verdict="pass", summary="ok"),
        Stories(file="a.md", summary="s", criteria=["c"], needs_devops=False),
        TestReport(verdict="fail", summary="s"),
    ):
        assert roundtrip(value) == value


def test_another_class_is_not_built() -> None:
    back = roundtrip(NotOurs())
    assert not isinstance(back, NotOurs)  # blocked: its data, not the object


def test_langsmith_tracing_stays_off_unless_asked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No telemetry by default: a stray LANGSMITH_TRACING in the environment
    must not send conversations to LangSmith (cli/main.py run())."""
    import os
    import sys

    import pytest

    from cmcoder.cli import main

    for opt_in, kept in ((None, False), ("1", True)):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
        if opt_in:
            monkeypatch.setenv("CMCODER_ENABLE_LANGSMITH", opt_in)
        monkeypatch.setattr(sys, "argv", ["cmcoder", "--version"])
        with pytest.raises(SystemExit):
            main.run()
        assert ("LANGSMITH_TRACING" in os.environ) is kept
        assert ("LANGCHAIN_TRACING_V2" in os.environ) is kept
