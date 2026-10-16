"""Turn exceptions from the model call into protocol `error` events with hints.

LangChain doesn't wrap provider errors: `ChatOpenAI` raises the OpenAI SDK's
exceptions (`openai.AuthenticationError`, `openai.APIConnectionError`, ...).
That's why error handling sits here, next to the model, rather than in the
agent loop.
"""

from __future__ import annotations

import ssl

import openai

from ..protocol import events as ev


def classify(exc: BaseException) -> ev.Error:
    msg = str(exc) or type(exc).__name__
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ev.Error(
            kind="auth",
            message=msg,
            hint="Check your API key: run `cmcoder login`, or set CMCODER_API_KEY.",
        )
    if isinstance(exc, openai.RateLimitError):
        budget = "budget" in msg.lower()
        return ev.Error(
            kind="budget" if budget else "rate_limit",
            message=msg,
            hint="Key budget exceeded: contact the gateway admin."
            if budget
            else "The gateway is rate limiting this key; wait a moment and retry.",
        )
    if isinstance(exc, openai.BadRequestError) and "context length" in msg.lower():
        return ev.Error(
            kind="context_length",
            message=msg,
            hint="The conversation is too long for the model. Start a new session or /clear.",
        )
    if isinstance(exc, openai.APIConnectionError):
        cause = exc.__cause__
        while cause is not None:
            if isinstance(cause, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(cause):
                return ev.Error(
                    kind="tls",
                    message=msg,
                    hint="The gateway's certificate isn't trusted. Set `caCertPath` to your "
                    "company root CA, then run `cmcoder doctor`.",
                )
            cause = cause.__cause__
        return ev.Error(
            kind="connection",
            message=msg,
            hint="Can't reach the gateway. Check the VPN, the URL and NO_PROXY; "
            "run `cmcoder doctor`.",
        )
    if isinstance(exc, openai.APIStatusError):
        return ev.Error(kind="server", message=msg)
    return ev.Error(kind="internal", message=f"{type(exc).__name__}: {msg}")
