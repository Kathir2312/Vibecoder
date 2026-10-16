"""Images for a main model that can't see them (settings `visionModel`).

The user's images always go into the conversation as LangChain's standard
multimodal content blocks (`{"type": "image_url", ...}`). For a main model
that sees images that's all. For one that doesn't (Qwen3 27B), a vision
model (a Qwen-VL on the same gateway) writes a description of each image,
once, and the main model gets the description instead of the image.

When: the session (runtime/session.py) has `describe()` write the
descriptions before the turn starts. The user sees "<model> is reading the
image…", and an image that can't be read stops the turn before anything is
sent to the main model. The descriptions are saved on the user's message
(`additional_kwargs`), so they are written once, even across `--resume`.

LANGCHAIN CONCEPTS IN THIS FILE

* **`awrap_model_call` changes only this request**: image blocks become their
  descriptions in `request.messages`, while the *state* keeps the images (a
  later switch to a model that sees images still has them).
* **Multimodal messages**: `HumanMessage(content=[{"type": "text", ...},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}])`,
  sent to the vision model like any other message.
* `model.astream(...)` with an idle timeout: a vision model that keeps
  answering is waited for; one that goes silent is given up on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

DESCRIPTIONS_KEY = "cmcoder_image_descriptions"  # on the HumanMessage: one per image
MAX_DESCRIPTION_TOKENS = 2000
# How long a vision model may stay silent (no reply yet, or a reply that
# stalls) before cmcoder gives up. One that is still loading, or one the
# gateway lists but doesn't really serve, would otherwise keep the chat waiting.
IDLE_SECONDS = 120.0

SYSTEM_PROMPT = """\
You describe an image for a coding assistant that cannot see it. Your \
description replaces the image, so leave nothing out that the assistant may need.

- Transcribe every piece of visible text exactly as written: code, error \
messages, stack traces, logs, terminal output, file and folder names, UI labels, \
values in tables. Put code, logs and terminal output in fenced code blocks.
- Then describe what the image shows: the application or kind of screen, the \
layout, which element is selected, highlighted or marked in red, dialogs, \
diagrams and charts (their parts, arrows and values).
- Be factual and complete. Don't guess what the user wants and don't answer \
their question: only describe the image."""


def image_blocks(message: AnyMessage) -> list[dict[str, Any]]:
    if not isinstance(message, HumanMessage) or isinstance(message.content, str):
        return []
    return [b for b in message.content if isinstance(b, dict) and b.get("type") == "image_url"]


def _text_of(message: HumanMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        str(b.get("text", ""))
        for b in message.content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def as_text(description: str | None, number: int, by: str) -> str:
    if description:
        return f'<image n="{number}" described_by="{by}">\n{description}\n</image>'
    return (
        f'<image n="{number}">(The user attached image #{number}, but this model can\'t see '
        "images and no description of it was made.)</image>"
    )


class VisionError(Exception):
    """The vision model didn't describe the image."""


async def ask(model: BaseChatModel, name: str, messages: list[AnyMessage], max_tokens: int) -> str:
    """One streamed request with images; VisionError when the model stays
    silent for IDLE_SECONDS (also used by `cmcoder doctor`)."""
    text = ""
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(IDLE_SECONDS) as limit:
            async for chunk in model.astream(messages, max_tokens=max_tokens):
                limit.reschedule(loop.time() + IDLE_SECONDS)  # it's answering
                text += chunk.text
    except TimeoutError:
        raise VisionError(
            f"no answer from {name} within {IDLE_SECONDS:.0f} s. It may still be loading, "
            "or your gateway may not serve it: `cmcoder doctor` checks it."
        ) from None
    return text.strip()


async def describe(model: BaseChatModel, name: str, image: dict[str, Any], user_text: str) -> str:
    """The vision model's description of one image block; `user_text` is the
    user's message, so the description can be precise where they are looking."""
    note = f"The user's message that came with the image:\n{user_text}" if user_text.strip() else ""
    content: list[str | dict[str, Any]] = [
        {"type": "text", "text": note or "Describe the image."},
        image,
    ]
    text = await ask(
        model, name, [SystemMessage(SYSTEM_PROMPT), HumanMessage(content)], MAX_DESCRIPTION_TOKENS
    )
    if not text:
        raise VisionError(f"{name} returned no description")
    return text


class VisionMiddleware(AgentMiddleware):
    """For a main model that can't see images: their descriptions instead."""

    def __init__(self, *, main_sees: bool, describer_name: str) -> None:
        super().__init__()
        self.main_sees = main_sees
        self.describer_name = describer_name

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if self.main_sees or not any(image_blocks(m) for m in request.messages):
            return await handler(request)
        return await handler(
            request.override(messages=images_as_text(request.messages, self.describer_name))
        )


def images_as_text(messages: list[AnyMessage], by: str = "") -> list[AnyMessage]:
    """The messages with each image block replaced by its saved description
    (for a model that can't see images, and for summaries, which are text)."""
    out: list[AnyMessage] = []
    number = 0
    for message in messages:
        if not image_blocks(message):
            out.append(message)
            continue
        descriptions = message.additional_kwargs.get(DESCRIPTIONS_KEY) or []
        content: list[str | dict[str, Any]] = []
        index = 0
        for block in message.content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                number += 1
                text = descriptions[index] if index < len(descriptions) else ""
                content.append({"type": "text", "text": as_text(text, number, by or "visionModel")})
                index += 1
            else:
                content.append(block)
        # All text now: one string, as for any text message.
        texts = [str(b["text"]) for b in content if isinstance(b, dict) and b.get("text")]
        out.append(message.model_copy(update={"content": "\n\n".join(texts)}))
    return out
