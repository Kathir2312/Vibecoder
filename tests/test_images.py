"""Images: checks and sizes, and the vision model that describes them for a
main model that can't see images (middleware/vision.py, runtime/session.py)."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as Pil

from cmcoder.images import (
    MAX_SIDE,
    Image,
    ImageError,
    from_base64,
    prepare,
    sniff,
)
from cmcoder.middleware.vision import DESCRIPTIONS_KEY
from cmcoder.protocol import events as ev
from cmcoder.providers.profiles import looks_like_vision, resolve_profile

from .conftest import API_KEY
from .helpers import make_session, of_type


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def png(width: int = 40, height: int = 20, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Pil.new(mode, (width, height), (200, 30, 30, 255)[: len(mode)]).save(out, "PNG")
    return out.getvalue()


def small_image() -> Image:
    return prepare(png(), "shot.png")


# --- checks and sizes ------------------------------------------------------------


def test_the_type_comes_from_the_bytes_not_the_name() -> None:
    assert sniff(png()) == "image/png"
    jpeg = io.BytesIO()
    Pil.new("RGB", (8, 8)).save(jpeg, "JPEG")
    assert sniff(jpeg.getvalue()) == "image/jpeg"
    assert sniff(b"GIF89a....") == "image/gif"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    with pytest.raises(ImageError, match="isn't a PNG, JPEG, GIF or WebP"):
        prepare(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "evil.png")
    with pytest.raises(ImageError, match="isn't a PNG"):
        prepare(b"MZ\x90\x00 an exe", "photo.jpg")


def test_a_small_image_is_sent_as_it_is() -> None:
    raw = png()
    img = prepare(raw, "shot.png")
    assert (img.media_type, img.width, img.height, img.name) == ("image/png", 40, 20, "shot.png")
    assert base64.b64decode(img.data) == raw
    assert img.data_url.startswith("data:image/png;base64,")


def test_a_large_screenshot_is_made_smaller() -> None:
    img = prepare(png(4000, 2000), "big.png")
    assert max(img.width, img.height) == MAX_SIDE
    assert img.media_type == "image/jpeg"  # no transparency: JPEG, much smaller
    with Pil.open(io.BytesIO(base64.b64decode(img.data))) as im:
        assert im.size == (MAX_SIDE, MAX_SIDE // 2)
    transparent = prepare(png(3000, 100, "RGBA"))
    assert transparent.media_type == "image/png"  # transparency kept


def test_broken_and_huge_images_are_refused() -> None:
    with pytest.raises(ImageError, match="couldn't be read"):
        prepare(png()[:40] + b"\x00" * 100)
    # A "decompression bomb": tiny file, enormous picture.
    header = io.BytesIO()
    Pil.new("1", (10_000, 10_000)).save(header, "PNG")
    with pytest.raises(ImageError, match="too large"):
        prepare(header.getvalue())
    with pytest.raises(ImageError, match="valid base64"):
        from_base64("not base64!!")


def test_base64_and_data_urls() -> None:
    raw = png()
    assert from_base64(base64.b64encode(raw).decode()).width == 40
    assert from_base64("data:image/png;base64," + base64.b64encode(raw).decode()).height == 20


# --- what the gateway gets -------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "sees"),
    [
        ("qwen2.5-vl-7b", True),
        ("qwen3-vl-32b-instruct", True),
        ("llava:13b", True),
        ("gemma3:27b", True),
        ("corp-vision-model", True),
        ("qwen3-coder-30b-a3b", False),
        ("qwen3-27b", False),
        ("devl-coder", False),
    ],
)
def test_vision_from_the_name(model: str, sees: bool) -> None:
    assert looks_like_vision(model) is sees
    assert resolve_profile(model).vision is sees


def test_settings_beat_the_gateway_which_beats_the_name() -> None:
    assert resolve_profile("qwen2.5-vl-7b", server_info={"supports_vision": False}).vision is False
    assert resolve_profile("qwen3-27b", server_info={"supports_vision": True}).vision is True
    override = [{"match": "qwen3*", "vision": False}]
    assert (
        resolve_profile("qwen3-x", override, server_info={"supports_vision": True}).vision is False
    )


# --- the vision model, in a session --------------------------------------------------------

VISION = {"visionModel": "mock:qwen2.5-vl-7b"}


def has_image(request: dict[str, Any]) -> bool:
    return "image_url" in str(request["messages"])


async def run(session: Any, text: str, images: list[Image]) -> list[Any]:
    try:
        return [e async for e in session.run(text, images=images)]
    finally:
        await session.agent.close()


async def test_a_text_only_model_gets_the_vision_models_description(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(
        [{"content": "A traceback: KeyError: 'x'"}, {"content": "It's a KeyError."}]
    )
    session = make_session(server.base_url, project, settings=VISION)
    events = await run(session, "what's this error?", [small_image()])
    kinds = [type(e) for e in events]
    # The chat says it's reading the image first (it can take a while).
    assert kinds.index(ev.ImagesDescribing) < kinds.index(ev.ImagesDescribed)
    describing = events[kinds.index(ev.ImagesDescribing)]
    assert describing.summary() == "qwen2.5-vl-7b is reading the image…"
    described = of_type(events, ev.ImagesDescribed)[0]
    assert described.model == "qwen2.5-vl-7b" and described.count == 1
    # The vision model saw the image, with the user's message for context.
    seen = server.requests[0]
    assert seen["model"] == "qwen2.5-vl-7b" and has_image(seen)
    assert "what's this error?" in str(seen["messages"])
    # The main model got the description, not the image.
    main = server.requests[1]
    assert main["model"] == "qwen3-27b" and not has_image(main)
    assert str(main["messages"]).count("KeyError") == 1
    assert 'described_by=\\"qwen2.5-vl-7b\\"' in str(main["messages"]) or (
        'described_by="qwen2.5-vl-7b"' in str(main["messages"])
    )
    assert events[-1].subtype == "success"
    # Saved with the conversation: the image, and its description.
    message = (await session.messages())[0]
    assert "image_url" in str(message.content)
    assert message.additional_kwargs[DESCRIPTIONS_KEY] == ["A traceback: KeyError: 'x'"]


async def test_a_silent_vision_model_is_given_up_on(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cmcoder.middleware.vision.IDLE_SECONDS", 0.3)
    server = mock_server([{"content": "A trace back that stalls", "delay": 3}])
    session = make_session(server.base_url, project, settings=VISION)
    events = await run(session, "what's this?", [small_image()])
    errors = of_type(events, ev.Error)
    assert errors and errors[0].kind == "images"
    assert "couldn't describe the image: no answer from qwen2.5-vl-7b within" in errors[0].message
    assert "cmcoder doctor" in errors[0].message
    assert events[-1].is_error
    assert len(server.requests) == 1  # the turn didn't start: the main model wasn't asked
    assert await session.messages() == [] and session.turn == 0


async def test_a_slow_but_steady_vision_model_is_waited_for(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cmcoder.middleware.vision.IDLE_SECONDS", 0.5)
    words = "A traceback: KeyError in the handler of the login page, line twelve."
    server = mock_server([{"content": words, "delay": 0.1}, {"content": "It's a KeyError."}])
    session = make_session(server.base_url, project, settings=VISION)
    events = await run(session, "what's this?", [small_image()])
    assert of_type(events, ev.ImagesDescribed) and events[-1].subtype == "success"


async def test_a_model_that_sees_images_gets_them_directly(mock_server: Any, project: Path) -> None:
    server = mock_server([{"content": "A red box."}])
    session = make_session(server.base_url, project, model="mock:qwen2.5-vl-72b", settings=VISION)
    events = await run(session, "what is it?", [small_image()])
    assert not of_type(events, ev.ImagesDescribing)
    assert len(server.requests) == 1 and has_image(server.requests[0])


async def test_without_a_vision_model_the_turn_says_why_and_doesnt_start(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([])
    session = make_session(server.base_url, project)
    events = await run(session, "what's this?", [small_image()])
    errors = of_type(events, ev.Error)
    assert errors and "visionModel" in errors[0].message and "can't see images" in errors[0].message
    assert events[-1].is_error and not server.requests
    assert await session.messages() == [] and session.turn == 0  # the message wasn't kept


async def test_summaries_get_the_description_not_the_image(mock_server: Any, project: Path) -> None:
    server = mock_server(
        [{"content": "A KeyError traceback."}, {"content": "KeyError."}, {"content": "Fixed."}]
    )
    session = make_session(server.base_url, project, settings=VISION)
    try:
        [e async for e in session.run("what's this?", images=[small_image()])]
        [e async for e in session.run("fix it")]
        await session.compact(None)
    finally:
        await session.agent.close()
    summary = server.state.summary_requests[0]
    assert not has_image(summary) and "A KeyError traceback." in str(summary["messages"])
