"""Errors an IDE can act on: classified in plain words with the buttons that make sense, and `/retry`
runs the step that failed again from the graph's checkpoint, without sending the message twice."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cmcoder.models.errors import classify
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session
from .test_stdio import Agent

REQUEST = httpx.Request("POST", "http://gateway/v1/chat/completions")


def status_error(
    cls: type[openai.APIStatusError], status: int, message: str = "boom"
) -> openai.APIStatusError:
    response = httpx.Response(status, request=REQUEST, json={"error": {"message": message}})
    return cls(message, response=response, body={"error": {"message": message}})


def test_each_kind_of_failure_says_what_happened_and_offers_what_helps() -> None:
    gateway = classify(status_error(openai.InternalServerError, 502, "Bad Gateway"))
    assert (gateway.kind, gateway.title) == ("gateway", "The model gateway is not answering (502)")
    assert gateway.actions == ["retry", "switch_model", "open_log"]
    assert classify(status_error(openai.InternalServerError, 503)).kind == "gateway"
    assert classify(status_error(openai.InternalServerError, 504)).kind == "gateway"
    server = classify(status_error(openai.InternalServerError, 500))
    assert (server.kind, server.title) == ("server", "The model server hit an error (500)")

    timeout = classify(openai.APITimeoutError(request=REQUEST))
    assert (timeout.kind, timeout.title) == ("timeout", "The model took too long to answer")
    assert timeout.actions == ["retry", "switch_model", "open_log"]

    connection = classify(openai.APIConnectionError(request=REQUEST))
    assert (connection.kind, connection.actions) == (
        "connection",
        ["retry", "switch_model", "open_log"],
    )

    cause = ssl.SSLError("certificate verify failed")
    tls = openai.APIConnectionError(request=REQUEST)
    tls.__cause__ = cause
    assert (classify(tls).kind, classify(tls).actions) == ("tls", ["open_log"])

    missing = classify(status_error(openai.NotFoundError, 404, "model not found"))
    assert (missing.kind, missing.actions) == ("model_not_found", ["switch_model", "open_log"])

    overflow = classify(
        status_error(
            openai.BadRequestError, 400, "This model's maximum context length is 4096 tokens"
        )
    )
    assert (overflow.kind, overflow.actions) == ("context_length", ["compact", "new_conversation"])

    limited = classify(status_error(openai.RateLimitError, 429, "slow down"))
    assert (limited.kind, limited.actions) == ("rate_limit", ["retry", "open_log"])
    budget = classify(status_error(openai.RateLimitError, 429, "Budget has been exceeded"))
    assert (budget.kind, budget.actions) == ("budget", ["open_log"])

    refused = classify(status_error(openai.BadRequestError, 400, "bad field"))
    assert (refused.kind, refused.title, refused.actions) == (
        "server",
        "The model refused the request (400)",
        [],
    )

    bug = classify(ValueError("oops"))
    assert (bug.kind, bug.actions) == ("internal", ["retry", "open_log"])
    assert bug.message == "ValueError: oops"


def test_a_refused_key_is_never_offered_a_retry() -> None:
    for exc in (
        status_error(openai.AuthenticationError, 401),
        status_error(openai.PermissionDeniedError, 403),
    ):
        error = classify(exc)
        assert error.kind == "auth" and error.title == "The model key was refused"
        assert "retry" not in error.actions and error.hint and "cmcoder login" in error.hint


def test_the_old_fields_are_unchanged() -> None:
    # The SDK words the message as "Error code: 502 - {...}": that, and the hint, are what an IDE
    # without cards still shows.
    error = classify(status_error(openai.InternalServerError, 502, "Error code: 502 - Bad Gateway"))
    assert error.message == "Error code: 502 - Bad Gateway" and error.hint


async def test_slash_retry_asks_the_front_end_to_run_the_step_again(tmp_path: Path) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path)
    result = await run_command(session, "/retry")
    assert result.retry is True and result.send is None and result.lines == []


# --- the real thing: a gateway that fails, then recovers ------------------------------------------

BAD = {"error": {"message": "Bad Gateway", "status": 502}}


def failing_then(server_script: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The model SDK tries a call three times before it gives up: three failures are one failed step."""
    return [BAD, BAD, BAD, *server_script]


async def test_retry_runs_the_failed_step_again_and_the_message_is_not_sent_twice(
    mock_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server(failing_then([{"content": "recovered"}]))
    session = make_session(server.base_url, tmp_path)
    try:
        first = [e async for e in session.run("hello")]
        error = next(e for e in first if isinstance(e, ev.Error))
        assert error.kind == "gateway" and "retry" in error.actions
        assert isinstance(first[-1], ev.Result) and first[-1].is_error
        snapshot = await session.agent.graph.aget_state(session.config)
        assert snapshot.next, "the failed step is waiting in the checkpoint"

        second = [e async for e in session.retry()]
        assert [e.text for e in second if isinstance(e, ev.AssistantMessage)] == ["recovered"]
        assert isinstance(second[-1], ev.Result) and second[-1].subtype == "success"

        messages = await session.messages()
        assert [type(m) for m in messages] == [HumanMessage, AIMessage], "one question, one answer"
        assert messages[0].content == "hello"
        assert len(server.requests) == 4
        last = server.requests[-1]["messages"]
        assert sum(1 for m in last if m["role"] == "user" and "hello" in str(m["content"])) == 1
    finally:
        await session.agent.close()


async def test_retry_after_a_finished_request_says_there_is_nothing_to_retry(
    mock_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server([{"content": "done"}])
    session = make_session(server.base_url, tmp_path)
    try:
        fresh = [e async for e in session.retry()]
        assert [type(e) for e in fresh] == [ev.Error, ev.Result]
        assert fresh[0].kind == "nothing_to_retry" and fresh[0].actions == []  # type: ignore[union-attr]
        [e async for e in session.run("hi")]
        again = [e async for e in session.retry()]
        assert isinstance(again[0], ev.Error) and again[0].kind == "nothing_to_retry"
        assert len(server.requests) == 1, "nothing was sent to the model"
    finally:
        await session.agent.close()


async def test_over_stdio_the_error_carries_its_buttons_and_slash_retry_recovers(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(failing_then([{"content": "recovered"}]))
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")
        assert "retry" in {c["name"] for c in (await agent.until("command_list"))["commands"]}
        system = next(e for e in agent.events if e["type"] == "system_init")
        assert "retry" in system["features"]

        await agent.send(type="user_message", text="hello")
        error = await agent.until("error")
        assert error["kind"] == "gateway" and "502" in error["title"]
        assert error["actions"] == ["retry", "switch_model", "open_log"]
        assert (await agent.until("result"))["subtype"] == "error"

        await agent.send(type="user_message", text="/retry")
        assert (await agent.until("assistant_message"))["text"] == "recovered"
        assert (await agent.until("result"))["subtype"] == "success"
        users = [m for m in server.requests[-1]["messages"] if m["role"] == "user"]
        assert len(users) == 1 and "hello" in str(users[0]["content"])

        await agent.send(type="user_message", text="/retry")  # nothing is waiting any more
        assert (await agent.until("error"))["kind"] == "nothing_to_retry"
        assert (await agent.until("result"))["is_error"] is True
    finally:
        assert await agent.close() == 0
