"""Is this error "the conversation doesn't fit the model's context window"?

Servers say it in their own words, and a gateway such as LiteLLM passes the
wording of whatever sits behind it. cmcoder recovers from this error by
itself (middleware/model.py: clear old tool outputs, summarise, shorten), so
it has to recognise it however it is phrased. A message that isn't recognised
reaches the user as a plain error and a manual retry.

    OpenAI, Azure, vLLM    "This model's maximum context length is 8192 tokens ..."
                           code "context_length_exceeded"
    vLLM                   "... is longer than the model's context length (32768 tokens)"
    Anthropic              "prompt is too long: 205482 tokens > 200000 maximum"
    Bedrock                "Input is too long for requested model"
    Gemini                 "The input token count (1050000) exceeds the maximum number of
                           tokens allowed (1048576)"
    LiteLLM                "ContextWindowExceededError: ..."
"""

from __future__ import annotations

import re

import openai
from langchain_core.exceptions import ContextOverflowError

_STATUS = (400, 413, 422)  # a refused request; never a server fault or a rate limit

_PHRASES = [
    re.compile(p)
    for p in (
        r"context[_ ]length",
        r"context window",
        r"contextwindowexceeded",
        r"prompt is too long",
        r"input is too long",
        r"input too long",
        r"too many (?:input )?tokens",
        r"reduce the length",
        r"exceeds? the maximum number of tokens",
        r"input token count .{0,40}exceeds",
        r"\d+ tokens? > \d+",
        r"maximum input length",
        r"max_model_len",
        r"longer than the model's",
        r"request too large for model",
    )
]


def is_context_overflow(exc: BaseException) -> bool:
    """True when `exc` is a model server refusing a request as too long."""
    if isinstance(exc, ContextOverflowError):  # LangChain's own signal (langchain-openai raises it)
        return True
    if not isinstance(exc, openai.APIStatusError) or exc.status_code not in _STATUS:
        return False
    if getattr(exc, "code", None) == "context_length_exceeded":
        return True
    text = str(exc).lower()
    return any(p.search(text) for p in _PHRASES)
