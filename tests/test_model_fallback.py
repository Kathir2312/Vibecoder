"""A request goes on with `fallbackModel` when the main model's gateway fails, from the graph's last
checkpoint, once; the next request tries the main model first. A refused key, a refused request and a
conversation that is too long are not the gateway's fault, so there the request stops with the error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cmcoder.agents.main import AgentOptions, build_agent, rebuild_agent
from cmcoder.models.errors import worth_fallback
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import make_session, mock_settings, of_type
from .test_error_cards import BAD, status_error
from .test_stdio import Agent

FALLBACK = {"fallbackModel": "mock:qwen3-7b"}
DOWN = [BAD, BAD, BAD]  # the SDK tries a call three times: three failures are one failed step


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def models_asked(server: Any) -> list[str]:
    return [r["model"] for r in server.requests]


def test_only_the_gateways_failures_are_worth_another_model() -> None:
    import openai

    from cmcoder.models.errors import classify

    for exc, worth in (
        (status_error(openai.InternalServerError, 502), True),
        (status_error(openai.InternalServerError, 500), True),
        (status_error(openai.RateLimitError, 429, "slow down"), True),
        (status_error(openai.NotFoundError, 404, "no such model"), True),
        (status_error(openai.AuthenticationError, 401), False),
        (status_error(openai.RateLimitError, 429, "Budget has been exceeded"), False),
        (status_error(openai.BadRequestError, 400, "bad field"), False),
        (status_error(openai.BadRequestError, 400, "maximum context length is 4096 tokens"), False),
        (ValueError("a bug in cmcoder"), False),
    ):
        assert worth_fallback(classify(exc)) is worth, exc


async def test_a_failed_gateway_finishes_the_request_on_the_fallback_model(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([*DOWN, {"content": "from the fallback"}])
    session = make_session(server.base_url, tmp_path, settings=FALLBACK)
    try:
        events = [e async for e in session.run("hello")]
        assert not of_type(events, ev.Error), "the user is told, not shown an error"
        warning = of_type(events, ev.Warning)[0].message
        assert "qwen3-27b" in warning and "qwen3-7b" in warning and "gateway" in warning
        changed = of_type(events, ev.ModelChanged)
        assert [(c.model, c.reason) for c in changed] == [("qwen3-7b", "fallback")]
        assert [e.text for e in of_type(events, ev.AssistantMessage)] == ["from the fallback"]
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
        assert models_asked(server) == ["qwen3-27b"] * 3 + ["qwen3-7b"]

        messages = await session.messages()
        assert [type(m) for m in messages] == [HumanMessage, AIMessage], "one question, one answer"
        last = server.requests[-1]["messages"]
        assert sum(1 for m in last if m["role"] == "user" and "hello" in str(m["content"])) == 1
        assert session.agent.bundle.name == "qwen3-7b", (
            "it stays on the fallback until the next one"
        )
    finally:
        await session.agent.close()


async def test_the_next_request_tries_the_main_model_first_and_the_fallback_is_given_up_when_it_works(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([*DOWN, {"content": "one"}, {"content": "two"}])
    session = make_session(server.base_url, tmp_path, settings=FALLBACK)
    try:
        [e async for e in session.run("first")]
        events = [e async for e in session.run("second")]
        changed = of_type(events, ev.ModelChanged)
        assert [(c.model, c.reason) for c in changed] == [("qwen3-27b", "restore")]
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
        assert models_asked(server)[-1] == "qwen3-27b"
        assert session.agent.bundle.name == "qwen3-27b"
        assert [e.text for e in of_type(events, ev.AssistantMessage)] == ["two"]
    finally:
        await session.agent.close()


async def test_when_the_fallback_fails_too_the_request_stops_with_the_error_and_retry_starts_over(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([*DOWN, *DOWN, {"content": "back"}])
    session = make_session(server.base_url, tmp_path, settings=FALLBACK)
    try:
        events = [e async for e in session.run("hello")]
        errors = of_type(events, ev.Error)
        assert len(errors) == 1 and "the fallback model too" in errors[0].title
        assert errors[0].kind == "gateway" and "retry" in errors[0].actions
        assert isinstance(events[-1], ev.Result) and events[-1].is_error
        assert models_asked(server) == ["qwen3-27b"] * 3 + ["qwen3-7b"] * 3, "no second fallback"

        again = [e async for e in session.retry()]
        assert [(c.model, c.reason) for c in of_type(again, ev.ModelChanged)] == [
            ("qwen3-27b", "restore")
        ]
        assert [e.text for e in of_type(again, ev.AssistantMessage)] == ["back"]
        assert models_asked(server)[-1] == "qwen3-27b"
        assert [type(m) for m in await session.messages()] == [HumanMessage, AIMessage]
    finally:
        await session.agent.close()


async def test_a_refused_key_stops_the_request_there_is_no_other_model_to_ask(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([{"error": {"message": "bad key", "status": 401}}, {"content": "no"}])
    session = make_session(server.base_url, tmp_path, settings=FALLBACK)
    try:
        events = [e async for e in session.run("hello")]
        assert of_type(events, ev.Error)[0].kind == "auth"
        assert not of_type(events, ev.ModelChanged)
        assert models_asked(server) == ["qwen3-27b"]
    finally:
        await session.agent.close()


async def test_without_a_fallback_model_nothing_changes(mock_server: Any, tmp_path: Path) -> None:
    server = mock_server(DOWN)
    session = make_session(server.base_url, tmp_path)
    try:
        events = [e async for e in session.run("hello")]
        assert of_type(events, ev.Error)[0].title == "The model gateway is not answering (502)"
        assert not of_type(events, ev.ModelChanged)
        assert models_asked(server) == ["qwen3-27b"] * 3
    finally:
        await session.agent.close()


async def test_a_fallback_the_gateway_does_not_know_is_just_the_same_error(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([*DOWN, {"error": {"message": "no such model", "status": 404}}])
    session = make_session(server.base_url, tmp_path, settings={"fallbackModel": "mock:ghost"})
    try:
        events = [e async for e in session.run("hello")]
        errors = of_type(events, ev.Error)
        assert len(errors) == 1 and errors[0].kind == "model_not_found"
        assert "the fallback model too" in errors[0].title
    finally:
        await session.agent.close()


async def test_a_model_the_user_picks_meanwhile_stays(mock_server: Any, tmp_path: Path) -> None:
    server = mock_server([*DOWN, {"content": "one"}, {"content": "two"}])
    session = make_session(server.base_url, tmp_path, settings=FALLBACK)
    try:
        [e async for e in session.run("first")]
        session.agent = await rebuild_agent(session.agent, model="mock:qwen3-27b")  # /model
        events = [e async for e in session.run("second")]
        assert not of_type(events, ev.ModelChanged), "nothing to restore: the user chose it"
        assert models_asked(server)[-1] == "qwen3-27b"
    finally:
        await session.agent.close()


async def test_the_rules_a_turn_allows_survive_the_switch(tmp_path: Path) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path, settings=FALLBACK)
    try:
        session.agent.policy.set_turn_allow(["Bash(echo hi)"])
        session.agent = await session._rebuilt(model="mock:qwen3-7b")
        assert [str(r) for r in session.agent.policy.turn_allow] == ["Bash(echo hi)"]
    finally:
        await session.agent.close()


async def test_an_agent_that_names_its_own_model_keeps_it(mock_server: Any, tmp_path: Path) -> None:
    agents = tmp_path / ".cmcoder" / "agents"
    agents.mkdir(parents=True)
    (agents / "pinned.md").write_text(
        "---\nname: pinned\ndescription: Uses one model\nmodel: mock:qwen3-27b\n---\nBe brief.\n"
    )
    server = mock_server(DOWN)
    settings = mock_settings(server.base_url, **FALLBACK)
    settings.project_trusted = True  # a project's agents are usable only when it is trusted
    session = Session(build_agent(settings, AgentOptions(cwd=tmp_path, agent="pinned")))
    try:
        events = [e async for e in session.run("hello")]
        assert of_type(events, ev.Error) and not of_type(events, ev.ModelChanged)
        assert models_asked(server) == ["qwen3-27b"] * 3
    finally:
        await session.agent.close()


async def test_over_stdio_the_panel_is_told_which_model_finishes_the_request(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_FALLBACK_MODEL", "qwen3-7b")
    server = mock_server([*DOWN, {"content": "from the fallback"}, {"content": "again"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="hello")
        warning = await agent.until("warning")
        assert "qwen3-7b" in warning["message"]
        changed = await agent.until("model_changed")
        assert (changed["model"], changed["reason"]) == ("qwen3-7b", "fallback")
        assert (await agent.until("assistant_message"))["text"] == "from the fallback"
        assert (await agent.until("result"))["subtype"] == "success"

        await agent.send(type="user_message", text="again")
        back = await agent.until("model_changed")
        assert (back["model"], back["reason"]) == ("qwen3-27b", "restore")
        assert (await agent.until("result"))["subtype"] == "success"
    finally:
        assert await agent.close() == 0
