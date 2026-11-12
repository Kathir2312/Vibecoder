"""Turn exceptions from the model call into protocol `error` events with hints.

`ChatOpenAI` raises the OpenAI SDK's exceptions (`openai.AuthenticationError`,
`openai.APIConnectionError`, ...); langchain-openai 1.x raises subclasses of
them that are also LangChain's own (`ModelAuthenticationError`), so matching
on the SDK classes catches both. Turning them into a message and a hint for
the user is cmcoder's job, which is why it sits here, next to the model,
rather than in the agent loop.

Each error also says, in plain words, what happened (`title`) and which of the IDE's buttons make
sense for it (`actions`): retrying a gateway that was down is worth offering, retrying a refused
key is not.
"""

from __future__ import annotations

import ssl

import openai

from ..protocol import events as ev
from .overflow import is_context_overflow

# Retry, or another model, helps when the gateway or the network is at fault.
_TRY_AGAIN: list[ev.ErrorAction] = ["retry", "switch_model", "open_log"]


def classify(exc: BaseException) -> ev.Error:
    msg = str(exc) or type(exc).__name__
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ev.Error(
            kind="auth",
            message=msg,
            title="The model key was refused",
            hint="Check your API key: run `cmcoder login`, or set CMCODER_API_KEY.",
            actions=["open_log"],
        )
    if isinstance(exc, openai.RateLimitError):
        budget = "budget" in msg.lower()
        return ev.Error(
            kind="budget" if budget else "rate_limit",
            message=msg,
            title="The key's budget is used up"
            if budget
            else "The gateway is rate limiting this key",
            hint="Key budget exceeded: contact the gateway admin."
            if budget
            else "The gateway is rate limiting this key; wait a moment and retry.",
            actions=["open_log"] if budget else ["retry", "open_log"],
        )
    if is_context_overflow(exc):
        return ev.Error(
            kind="context_length",
            message=msg,
            title="The conversation is too long for the model",
            hint="The conversation is too long for the model, even after cmcoder "
            "summarised it. Run /compact, or start a new session with /clear.",
            actions=["compact", "new_conversation"],
        )
    if isinstance(exc, openai.APITimeoutError):
        return ev.Error(
            kind="timeout",
            message=msg,
            title="The model took too long to answer",
            hint="The gateway didn't answer in time. Retry, or switch to a faster model.",
            actions=list(_TRY_AGAIN),
        )
    if isinstance(exc, openai.APIConnectionError):
        # The TLS error is somewhere down the chain (httpx, httpcore, ssl), linked
        # by `raise ... from` or, on macOS (truststore), only as the context.
        cause: BaseException | None = exc.__cause__ or exc.__context__
        while cause is not None:
            if isinstance(cause, ssl.SSLError) or "certificate verify failed" in str(cause).lower():
                return ev.Error(
                    kind="tls",
                    message=msg,
                    title="The gateway's certificate isn't trusted",
                    hint="The gateway's certificate isn't trusted. Set `caCertPath` to your "
                    "company root CA, then run `cmcoder doctor`.",
                    actions=["open_log"],
                )
            cause = cause.__cause__ or cause.__context__
        return ev.Error(
            kind="connection",
            message=msg,
            title="Can't reach the model gateway",
            hint="Can't reach the gateway. Check the VPN, the URL and NO_PROXY; "
            "run `cmcoder doctor`.",
            actions=list(_TRY_AGAIN),
        )
    if isinstance(exc, openai.NotFoundError):
        return ev.Error(
            kind="model_not_found",
            message=msg,
            title="The gateway doesn't know this model",
            hint="Check the model name (/model shows it) and the gateway's URL; run `cmcoder doctor`.",
            actions=["switch_model", "open_log"],
        )
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status in (502, 503, 504):
            return ev.Error(
                kind="gateway",
                message=msg,
                title=f"The model gateway is not answering ({status})",
                hint="The gateway is down or overloaded: that is on its side, not in your project. "
                "Retry in a moment, or switch to another model.",
                actions=list(_TRY_AGAIN),
            )
        if status >= 500:
            return ev.Error(
                kind="server",
                message=msg,
                title=f"The model server hit an error ({status})",
                hint="The server failed on this request. Retry, or switch to another model.",
                actions=list(_TRY_AGAIN),
            )
        return ev.Error(
            kind="server", message=msg, title=f"The model refused the request ({status})"
        )
    return ev.Error(
        kind="internal",
        message=f"{type(exc).__name__}: {msg}",
        title="cmcoder hit an unexpected error",
        actions=["retry", "open_log"],
    )
