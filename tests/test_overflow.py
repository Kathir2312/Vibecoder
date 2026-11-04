"""The conversation outgrows the model's window: cmcoder recovers by itself.

The gateway's real window is 10,000 tokens (cmcoder's own system prompt and tool
list take over half of them) while cmcoder still assumes the
profile's 32K (the gateway doesn't publish it), so the first request that is
too big is refused. Whatever the server's wording, the turn must still end
normally: old tool outputs are cleared, older messages summarised, long tool
outputs shortened, and the request retried, without the user retrying by hand.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import openai
import pytest

from cmcoder.models.errors import classify
from cmcoder.models.overflow import is_context_overflow
from cmcoder.models.windows import parse_context_window
from cmcoder.protocol import events as ev

from .conftest import API_KEY
from .helpers import make_session, of_type, run_turn

ANTHROPIC = "prompt is too long: {requested} tokens > {window} maximum"
BEDROCK = "Input is too long for requested model."  # no number in it


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def small_server(mock_server, script: list[dict], message: str | None):
    return mock_server(
        script, enforce_context=True, context_windows={"qwen3-27b": 10000}, overflow_message=message
    )


@pytest.mark.parametrize("message", [None, ANTHROPIC, BEDROCK])
async def test_a_big_tool_output_is_shortened_not_an_error(
    mock_server, project: Path, message: str | None
) -> None:
    (project / "big.txt").write_text(
        "\n".join(f"line {i}: text to fill the window" for i in range(400))
    )
    server = small_server(
        mock_server,
        [
            {"tool_calls": [{"name": "Read", "arguments": {"file_path": "big.txt"}}]},
            {"content": "It is a big file."},
        ],
        message,
    )
    session = make_session(server.base_url, project)
    try:
        events = await run_turn(session, "read big.txt")
    finally:
        await session.agent.close()
    assert not of_type(events, ev.Error), of_type(events, ev.Error)
    assert events[-1].subtype == "success" and events[-1].result == "It is a big file."
    # The user is told what happened, not left wondering about the delay.
    assert any("too long for the model" in w.message for w in of_type(events, ev.Warning))


@pytest.mark.parametrize("message", [None, ANTHROPIC, BEDROCK])
async def test_a_long_history_is_summarised_not_an_error(
    mock_server, project: Path, message: str | None
) -> None:
    server = small_server(mock_server, [{"content": f"answer {i}"} for i in range(8)], message)
    session = make_session(server.base_url, project)
    results = []
    try:
        for i in range(8):
            events = await run_turn(session, f"question {i}: " + "context " * 500)
            assert not of_type(events, ev.Error), (i, of_type(events, ev.Error))
            results.append(events[-1].result)
    finally:
        await session.agent.close()
    assert results == [f"answer {i}" for i in range(8)]
    assert server.state.summary_requests  # it summarised the older part to fit
    # The conversation itself was shortened too, not only each request.
    kept = await session.messages()
    assert len(kept) < 16


def refused(status: int, message: str, code: str | None = None) -> openai.APIStatusError:
    """The error `ChatOpenAI` raises for a server's answer."""
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
        json={"error": {"message": message, "code": code}},
    )
    body = {"message": message, "code": code}
    cls = openai.BadRequestError if status == 400 else openai.APIStatusError
    return cls(message, response=response, body=body)


# What servers and gateways say, and the window each one states (None: not stated).
TOO_LONG = [
    ("This model's maximum context length is 8192 tokens. However, you requested 9000.", 8192),
    ("The decoder prompt (length 40000) is longer than the model's context length (32768)", None),
    ("prompt is too long: 205482 tokens > 200000 maximum", 200000),
    ("Input is too long for requested model.", None),
    (
        "The input token count (1050000) exceeds the maximum number of tokens allowed (1048576).",
        1048576,
    ),
    ("litellm.ContextWindowExceededError: AnthropicException - prompt is too long", None),
    ("Please reduce the length of the messages or completion.", None),
]


@pytest.mark.parametrize(("message", "window"), TOO_LONG)
def test_every_wording_of_too_long_is_recognised(message: str, window: int | None) -> None:
    error = refused(400, message)
    assert is_context_overflow(error)
    assert parse_context_window(str(error)) == window
    assert classify(error).kind == "context_length"


def test_the_error_code_alone_is_enough() -> None:
    assert is_context_overflow(refused(400, "Error", code="context_length_exceeded"))
    assert is_context_overflow(refused(413, "Request too large for model"))


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "Invalid parameter: temperature must be between 0 and 2"),
        (400, "Unknown model qwen3-99b"),
        (429, "Rate limit reached: too many requests"),  # "too many" is not "too many tokens"
        (500, "context length handling crashed"),  # a fault of the server, not of the request
    ],
)
def test_other_errors_are_not_taken_for_too_long(status: int, message: str) -> None:
    assert not is_context_overflow(refused(status, message))


def test_langchains_own_overflow_error_is_recognised() -> None:
    from langchain_core.exceptions import ContextOverflowError

    assert is_context_overflow(ContextOverflowError("too long"))


def test_old_tool_outputs_are_cleared_with_langchains_edit() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from cmcoder.middleware.model import CLEARED, clear_old_tool_outputs

    messages = [HumanMessage("go")]
    for i in range(5):
        messages += [
            AIMessage("", tool_calls=[{"name": "Read", "args": {}, "id": f"c{i}"}]),
            ToolMessage(f"output {i}", tool_call_id=f"c{i}", name="Read"),
        ]
    out = clear_old_tool_outputs(messages, keep=2)
    tool_texts = [m.content for m in out if isinstance(m, ToolMessage)]
    assert tool_texts == [CLEARED] * 3 + ["output 3", "output 4"]
    assert [m.content for m in messages if isinstance(m, ToolMessage)][0] == "output 0"  # a copy
