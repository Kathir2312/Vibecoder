"""The critic: an after_agent review with structured output (a Verdict)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def verdict(v: str, summary: str, issues: list | None = None) -> dict:
    args = {"verdict": v, "summary": summary, "issues": issues or []}
    return {"tool_calls": [{"name": "Verdict", "arguments": args}]}


async def test_a_failed_review_sends_the_agent_back(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {"content": "Draft: done."},  # the main agent's first answer
            verdict(
                "fail", "Nothing was checked.", [{"problem": "run the tests", "severity": "high"}]
            ),
            {"content": "Final: done and checked."},  # after the fix request
            verdict("pass", "Looks right."),
        ]
    )
    session = make_session(server.base_url, project, critic=True)
    try:
        events = await run_turn(session, "do it")
    finally:
        await session.agent.close()
    reviews = of_type(events, ev.ReviewResult)
    assert [(r.verdict, r.final) for r in reviews] == [("fail", False), ("pass", True)]
    # Only the reviewed answer is shown, never the draft.
    shown = [m.text for m in of_type(events, ev.AssistantMessage)]
    assert shown == ["Final: done and checked."]
    assert "Draft" not in "".join(d.text for d in of_type(events, ev.AssistantDelta))
    assert events[-1].review == {"verdict": "pass", "rounds": 2, "issues": []}
    # The fix request carried the critic's findings to the main agent.
    assert "run the tests" in str(server.requests[2]["messages"])
    # The critic is read-only and fills the Verdict schema.
    critic_tools = {t["function"]["name"] for t in server.requests[1]["tools"]}
    assert critic_tools == {"Read", "Glob", "Grep", "Verdict"}


async def test_out_of_rounds_shows_the_answer_not_validated(mock_server, project: Path) -> None:
    server = mock_server(
        [
            {"content": "Answer one."},
            verdict("fail", "Wrong."),
            {"content": "Answer two."},
            verdict("fail", "Still wrong."),
        ]
    )
    session = make_session(
        server.base_url, project, critic=True, settings={"critic": {"maxRounds": 2}}
    )
    try:
        events = await run_turn(session, "do it")
    finally:
        await session.agent.close()
    assert of_type(events, ev.ReviewResult)[-1].final
    assert events[-1].result == "Answer two." and events[-1].review["verdict"] == "fail"


async def test_critic_can_be_switched(mock_server, project: Path) -> None:
    server = mock_server([{"content": "plain answer"}])
    session = make_session(server.base_url, project)
    try:
        assert "off" in (await run_command(session, "/critic")).lines[0]
        events = await run_turn(session, "hi")  # off: no review
        assert not of_type(events, ev.ReviewResult) and events[-1].review is None
        assert "on" in (await run_command(session, "/critic on")).lines[0]
        assert session.agent.critic is not None and session.agent.critic.enabled
    finally:
        await session.agent.close()
