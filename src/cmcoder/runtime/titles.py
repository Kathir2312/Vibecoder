"""Session titles for `/resume`, written by the small/fast model.

After the first turn the small model is asked for a short title in the
background (`asyncio.create_task`): nothing waits for it, and if it fails the
title falls back to the start of the first message.

LANGCHAIN CONCEPTS IN THIS FILE

* **Calling a chat model directly**, outside any agent:
  `model.ainvoke([SystemMessage(...), HumanMessage(...)])` returns an
  `AIMessage`; `.text` is its text.
* **Per-call options**: extra keyword arguments to `ainvoke` (`max_tokens=40`)
  are passed through to the request, so one call can use a smaller output
  limit without a second model object (compare `bind`, which makes a
  reusable runnable with those options).
* **The small/fast model**: a second chat model for cheap jobs (titles,
  summaries), chosen in settings (`smallFastModel`).
"""

from __future__ import annotations

import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..models.reasoning import split_think

# Recognised by the mock server in tests; harmless to real models.
TITLE_TAG = "<cmcoder-title>"
SYSTEM_PROMPT = f"""{TITLE_TAG}
Write a short title (3 to 7 words) for a coding conversation that starts
with the user's message below. Reply with the title only: no quotes, no
punctuation at the end, no explanation."""
MAX_PROMPT_CHARS = 2000
MAX_TITLE_CHARS = 60


def clean_title(text: str) -> str | None:
    """First non-empty line, without quotes, labels or trailing punctuation."""
    for line in text.splitlines():
        line = line.strip().strip("*#`\"'“”‘’ ").strip()
        line = re.sub(r"^(title|session title)\s*:\s*", "", line, flags=re.I).strip("\"'“” ")
        line = line.rstrip(".:;!,")
        if line:
            return line[:MAX_TITLE_CHARS].rstrip()
    return None


def fallback_title(first_message: str) -> str:
    return " ".join(first_message.split())[:MAX_TITLE_CHARS] or "(empty)"


async def make_title(model: BaseChatModel, first_message: str) -> str:
    try:
        reply = await model.ainvoke(
            [SystemMessage(SYSTEM_PROMPT), HumanMessage(first_message[:MAX_PROMPT_CHARS])],
            max_tokens=40,
        )
        visible, _ = split_think(reply.text)
        return clean_title(visible) or fallback_title(first_message)
    except Exception:  # a title is never worth an error
        return fallback_title(first_message)
