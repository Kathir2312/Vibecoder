"""What waits to go with the next message shows as removable chips: the engine says what is waiting
(an `attachments` event) and takes one off on `/pick drop <n>`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcoder.protocol import events as ev
from cmcoder.runtime.attachments import Attachment, changed
from cmcoder.runtime.commands import run_command

from .helpers import make_session
from .test_stdio import Agent


def waiting(*labels: str) -> list[Attachment]:
    return [Attachment("element", label, f"note of {label}") for label in labels]


def test_the_event_lists_what_waits_by_number() -> None:
    event = changed(waiting("#buy on /cart", "h1 on /"))
    assert event.type == "attachments"
    assert [(i.index, i.kind, i.label) for i in event.items] == [
        (0, "element", "#buy on /cart"),
        (1, "element", "h1 on /"),
    ]
    assert changed([]).items == []


async def test_every_pick_command_sends_the_chips_as_they_are_now(tmp_path: Path) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path)
    session.attachments += waiting("a", "b", "c")
    result = await run_command(session, "/pick drop 1")
    assert result.quiet is True and result.lines == ["Dropped b"]
    mistake = await run_command(session, "/pick drop 9")
    assert mistake.quiet is False, "a mistake is said, not swallowed"
    (event,) = result.events
    assert isinstance(event, ev.AttachmentsChanged) and [i.label for i in event.items] == ["a", "c"]
    assert [a.label for a in session.attachments] == ["a", "c"]
    cleared = await run_command(session, "/pick clear")
    assert cleared.quiet is False and cleared.events[0].items == []  # type: ignore[union-attr]
    assert session.attachments == []


@pytest.mark.parametrize("arg", ["drop", "drop x", "drop 9", "drop -1x", "drop 1.5"])
async def test_dropping_what_is_not_there_says_how(tmp_path: Path, arg: str) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path)
    session.attachments += waiting("a")
    result = await run_command(session, f"/pick {arg}")
    assert result.lines == ["Usage: /pick drop <number of a picked element>"]
    assert len(session.attachments) == 1, "nothing was dropped by a mistake"


async def test_an_address_that_starts_with_drop_is_still_an_address(tmp_path: Path) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path)
    result = await run_command(session, "/pick dropbox.com")
    assert not result.lines[0].startswith("Usage: /pick drop")
    assert result.quiet is False


async def test_over_stdio_the_chips_arrive_as_an_event_and_dropping_one_is_quiet(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(
            type="user_message", text="/pick"
        )  # no page is open: it says so, and the chips are none
        assert "No page is open yet" in (await agent.until("assistant_message"))["text"]
        assert (await agent.until("attachments"))["items"] == []
        assert (await agent.until("result"))["subtype"] == "success"
        await agent.send(
            type="user_message", text="/pick drop 0"
        )  # nothing is waiting: that is said in words
        assert "Usage: /pick drop" in (await agent.until("assistant_message"))["text"]
        await agent.until("result")
    finally:
        assert await agent.close() == 0
