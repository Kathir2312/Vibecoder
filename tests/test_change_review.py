"""Change review: what a request changed, per file and per hunk, and taking parts of it back safely."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcoder.core.checkpoints import Checkpoints
from cmcoder.protocol import events as ev
from cmcoder.runtime import changes
from cmcoder.runtime.changes import ChangeReviews
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, run_turn
from .test_stdio import Agent

ORIGINAL = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"


def edited(text: str, **replace: str) -> str:
    """`ORIGINAL`-style text with some lines replaced: edited(ORIGINAL, **{"line 3": "three"})."""
    for old, new in replace.items():
        text = text.replace(old + "\n", new + "\n")
    return text


def review_of(
    tmp: Path, name: str, before: bytes | None, after: bytes, turn: int = 1
) -> tuple[ChangeReviews, ev.TurnChanges, Path, Checkpoints]:
    path = tmp / name
    if before is not None:
        path.write_bytes(before)
    cp = Checkpoints(None)
    cp.capture(turn, path)  # as the agent's first touch does
    path.write_bytes(after)
    reviews = ChangeReviews()
    event = reviews.build(turn, cp, tmp)
    assert event is not None
    return reviews, event, path, cp


def test_a_changed_file_is_listed_with_its_hunks_and_counts(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 3": "three", "line 30": "thirty", "line 31": "thirty-one"})
    _, event, _, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    (f,) = event.files
    assert (f.path, f.status, f.index, f.reverted, f.binary, f.truncated) == (
        "a.txt",
        "modified",
        0,
        False,
        False,
        False,
    )
    assert (f.added, f.removed) == (3, 3) and (event.added, event.removed, event.turn) == (3, 3, 1)
    assert len(f.hunks) == 2, "two changes far apart are two hunks"
    first = f.hunks[0]
    assert (first.id, first.old_start, first.new_start) == (0, 1, 1)
    kinds = [(line.kind, line.text) for line in first.lines]
    assert kinds[:2] == [("ctx", "line 1"), ("ctx", "line 2")]
    assert ("del", "line 3") in kinds and ("add", "three") in kinds
    assert [line.kind for line in first.lines].count("ctx") == 2 + 3, (
        "two lines before the change, three after"
    )


def test_changes_close_together_share_one_hunk(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 10": "ten", "line 12": "twelve"})
    _, event, _, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    assert len(event.files[0].hunks) == 1


def test_a_file_the_agent_created_is_added_and_taking_it_back_deletes_it(tmp_path: Path) -> None:
    reviews, event, path, _ = review_of(tmp_path, "new.py", None, b"a = 1\nb = 2\n")
    (f,) = event.files
    assert (f.status, f.added, f.removed, len(f.hunks)) == ("added", 2, 0, 1)
    lines, after = reviews.revert(1, 0, None)
    assert lines == ["new.py: deleted (cmcoder had created it)"] and not path.exists()
    assert after is not None and after.files[0].reverted is True
    assert reviews.revert(1, 0, None)[0] == ["new.py: already deleted"]


def test_one_hunk_at_a_time_goes_back_and_the_rest_stays(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 3": "three", "line 30": "thirty"})
    reviews, _, path, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    lines, event = reviews.revert(1, 0, 0)
    assert lines == ["a.txt: hunk 1 taken back"]
    assert path.read_text() == edited(ORIGINAL, **{"line 30": "thirty"})
    assert event is not None and [h.reverted for h in event.files[0].hunks] == [True, False]
    assert event.files[0].reverted is False
    reviews.revert(1, 0, 1)
    assert path.read_bytes() == ORIGINAL.encode(), "both hunks back: the file is as it was"
    assert reviews.event(1).files[0].reverted is True


def test_a_whole_file_and_a_whole_request_go_back(tmp_path: Path) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("one\n"), b.write_text("two\n")
    cp = Checkpoints(None)
    cp.capture(1, a), cp.capture(1, b)
    a.write_text("ONE\n"), b.write_text("TWO\nmore\n")
    reviews = ChangeReviews()
    reviews.build(1, cp, tmp_path)
    lines, event = reviews.revert(1, None, None)
    assert lines == ["a.txt: restored", "b.txt: restored"]
    assert (a.read_text(), b.read_text()) == ("one\n", "two\n")
    assert event is not None and all(f.reverted for f in event.files)


def test_what_the_user_changed_since_is_never_overwritten(tmp_path: Path) -> None:
    reviews, _, path, _ = review_of(
        tmp_path, "a.txt", ORIGINAL.encode(), edited(ORIGINAL, **{"line 3": "three"}).encode()
    )
    path.write_text("the user rewrote this\n")
    lines, event = reviews.revert(1, 0, 0)
    assert "changed since cmcoder wrote it" in lines[0] and "/rewind" in lines[0]
    assert path.read_text() == "the user rewrote this\n"
    assert (
        event is not None
        and "changed since" in event.files[0].note
        and event.files[0].reverted is False
    )


def test_after_a_revert_the_next_one_still_expects_what_the_last_wrote(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 3": "three", "line 30": "thirty"})
    reviews, _, path, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    reviews.revert(1, 0, 0)
    path.write_text(path.read_text() + "tacked on by the user\n")
    assert "changed since" in reviews.revert(1, 0, 1)[0][0]


def test_line_endings_survive_a_revert(tmp_path: Path) -> None:
    before = b"one\r\ntwo\r\nthree\r\n"
    reviews, event, path, _ = review_of(tmp_path, "w.txt", before, b"one\r\nTWO\r\nthree\r\n")
    assert [(line.kind, line.text) for line in event.files[0].hunks[0].lines] == [
        ("ctx", "one"),
        ("del", "two"),
        ("add", "TWO"),
        ("ctx", "three"),
    ], "drawn without the \\r"
    reviews.revert(1, 0, 0)
    assert path.read_bytes() == before


def test_a_file_that_is_not_text_can_only_come_back_whole(tmp_path: Path) -> None:
    before, after = bytes(range(256)), bytes(range(255, -1, -1))
    reviews, event, path, _ = review_of(tmp_path, "x.bin", before, after)
    f = event.files[0]
    assert f.binary is True and f.hunks == []
    assert "only be taken back as a whole file" in reviews.revert(1, 0, 0)[0][0]
    assert path.read_bytes() == after
    assert reviews.revert(1, 0, None)[0] == ["x.bin: restored"] and path.read_bytes() == before
    assert reviews.event(1).files[0].reverted is True


def test_a_rewrite_too_big_to_draw_is_counted_and_comes_back_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(changes, "MAX_DIFF_LINES", 10)
    reviews, event, path, _ = review_of(
        tmp_path, "big.txt", ORIGINAL.encode(), ORIGINAL.replace("line", "LINE").encode()
    )
    f = event.files[0]
    assert f.truncated is True and f.hunks == [] and (f.added, f.removed) == (40, 40)
    reviews.revert(1, 0, None)
    assert path.read_bytes() == ORIGINAL.encode()


def test_files_that_did_not_really_change_are_not_listed(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("same\n")
    cp = Checkpoints(None)
    cp.capture(1, path)
    path.write_text("changed\n")
    path.write_text("same\n")  # the agent changed it back
    gone = tmp_path / "gone.txt"
    gone.write_text("x\n")
    cp.capture(1, gone)
    gone.unlink()
    assert ChangeReviews().build(1, cp, tmp_path) is None
    assert ChangeReviews().build(2, cp, tmp_path) is None, "nothing was captured in request 2"


def test_asking_for_what_is_not_there_says_so(tmp_path: Path) -> None:
    reviews, _, path, _ = review_of(tmp_path, "a.txt", b"x\n", b"y\n")
    assert "nothing to review for request 9" in reviews.revert(9, 0, None)[0][0]
    assert reviews.revert(1, 5, None)[0] == ["Request 1 has no file number 5."]
    assert "no hunk number 7" in reviews.revert(1, 0, 7)[0][0]
    assert path.read_text() == "y\n"


async def test_the_command_takes_numbers_and_never_a_path(tmp_path: Path) -> None:
    session = make_session("http://127.0.0.1:9", tmp_path)
    for arg in ("", "x", "1", "1 all 2", "1 2 3 4", "../../etc/passwd all"):
        assert (
            (await run_command(session, f"/revert {arg}")).lines[0].startswith("Usage: /revert")
        ), arg
    assert "nothing to review" in (await run_command(session, "/revert 1 all")).lines[0]


# --- a real request, through the session and over stdio -------------------------------------------

EDIT = {
    "name": "Edit",
    "arguments": {
        "file_path": "app.py",
        "old_string": "return a - b",
        "new_string": "return a + b",
    },
}


def read_then_edit() -> list[dict[str, Any]]:
    return [
        {"tool_calls": [{"name": "Read", "arguments": {"file_path": "app.py"}}]},
        {"tool_calls": [EDIT]},
        {"content": "Fixed."},
    ]


async def test_a_request_that_edits_a_file_ends_with_its_review_and_it_can_be_taken_back(
    mock_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(read_then_edit())
    session = make_session(server.base_url, tmp_path, permission_mode="bypassPermissions")
    try:
        events = await run_turn(session, "fix add")
        kinds = [type(e).__name__ for e in events]
        assert kinds[-2:] == ["TurnChanges", "Result"], "the review comes just before the result"
        review = next(e for e in events if isinstance(e, ev.TurnChanges))
        assert (review.turn, review.added, review.removed) == (1, 1, 1)
        assert [(line.kind, line.text) for line in review.files[0].hunks[0].lines] == [
            ("ctx", "def add(a, b):"),
            ("del", "    return a - b"),
            ("add", "    return a + b"),
        ]
        assert "return a + b" in (tmp_path / "app.py").read_text()

        result = await run_command(session, "/revert 1 0 0")
        assert (tmp_path / "app.py").read_text() == "def add(a, b):\n    return a - b\n"
        assert isinstance(result.events[0], ev.TurnChanges) and result.events[0].files[0].reverted

        await session.clear()  # a new conversation: the old requests are no longer there to review
        assert "nothing to review" in (await run_command(session, "/revert 1 all")).lines[0]
    finally:
        await session.agent.close()


async def test_a_request_that_changes_nothing_has_no_review(
    mock_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server([{"content": "Hello."}])
    session = make_session(server.base_url, tmp_path)
    try:
        events = await run_turn(session, "hi")
        assert not [e for e in events if isinstance(e, ev.TurnChanges)]
    finally:
        await session.agent.close()


async def test_over_stdio_the_review_arrives_before_the_result_and_revert_updates_it(
    mock_server: Any, project: Path
) -> None:
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server(read_then_edit())
    agent = await Agent.start(project, server, "--permission-mode", "bypassPermissions")
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")
        assert "revert" in {c["name"] for c in (await agent.until("command_list"))["commands"]}
        await agent.send(type="user_message", text="fix add")
        review = await agent.until("turn_changes")
        assert review["files"][0]["path"] == "app.py" and review["files"][0]["hunks"][0]["id"] == 0
        assert (await agent.until("result"))["subtype"] == "success"
        mark = len(agent.events)
        await agent.send(type="user_message", text="/revert 1 0 0")
        again = await agent.until("turn_changes")
        assert (
            again["files"][0]["reverted"] is True
            and again["files"][0]["hunks"][0]["reverted"] is True
        )
        assert (await agent.until("result"))["subtype"] == "success"
        assert (project / "app.py").read_text() == "def add(a, b):\n    return a - b\n"
        said = [e for e in agent.events[mark:] if e["type"] == "assistant_message"]
        assert said == [], (
            "taking a hunk back says nothing in the conversation: the review shows it"
        )
        await agent.send(type="user_message", text="/revert 1")
        assert "Usage: /revert" in (await agent.until("assistant_message"))["text"]
    finally:
        assert await agent.close() == 0


# --- Keep: approving what a request changed -----------------------------------------------------


def test_keeping_a_file_marks_it_and_changes_nothing_on_disk(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 3": "three"})
    reviews, event, path, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    assert event.files[0].kept is False
    said, now = reviews.keep(1, 0)
    assert said == ["a.txt: kept"] and now is not None and now.files[0].kept is True
    assert path.read_text() == after, "approving writes nothing"
    # the review is drawn again from the engine: kept stays marked
    assert reviews.event(1).files[0].kept is True


def test_keep_all_marks_every_file_that_is_still_there_and_skips_what_was_taken_back(
    tmp_path: Path,
) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a\n")
    cp = Checkpoints(None)
    cp.capture(1, a)
    cp.capture(1, b)  # b is created by the agent
    a.write_text("a2\n")
    b.write_text("b\n")
    reviews = ChangeReviews()
    assert reviews.build(1, cp, tmp_path) is not None
    reviews.revert(1, 1, None)  # b (created) is taken back: deleted
    said, now = reviews.keep(1, None)
    assert said == ["a.txt: kept"] and now is not None
    assert [(f.path, f.kept, f.reverted) for f in now.files] == [
        ("a.txt", True, False),
        ("b.txt", False, True),
    ]
    one, after = reviews.keep(1, 1)
    assert one == ["b.txt: was taken back, so there is nothing to keep"]
    assert after is not None and after.files[1].kept is False


def test_taking_a_kept_file_back_is_a_new_decision_and_clears_the_mark(tmp_path: Path) -> None:
    after = edited(ORIGINAL, **{"line 3": "three"})
    reviews, _, path, _ = review_of(tmp_path, "a.txt", ORIGINAL.encode(), after.encode())
    reviews.keep(1, 0)
    _, now = reviews.revert(1, 0, None)
    assert now is not None and (now.files[0].kept, now.files[0].reverted) == (False, True)
    assert path.read_text() == ORIGINAL


def test_keeping_something_that_is_not_there_says_so(tmp_path: Path) -> None:
    reviews, _, _, _ = review_of(tmp_path, "a.txt", b"x\n", b"y\n")
    assert reviews.keep(9, 0)[0] == ["There is nothing to review for request 9 any more."]
    said, _ = reviews.keep(1, 5)
    assert said == ["Request 1 has no file number 5."]


async def test_slash_keep_is_a_quiet_command_that_resends_the_review_and_is_audited(
    mock_server: Any, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "new.py", "content": "x = 1\n"}}
                ]
            },
            {"content": "done"},
        ]
    )

    async def allow(request: ev.PermissionRequest) -> dict[str, Any]:
        return {"allow": True}

    audit_dir = tmp_path / "audit"
    session = make_session(
        server.base_url,
        project,
        ask=allow,
        settings={"audit": {"enabled": True, "dir": str(audit_dir)}},
    )
    try:
        events = await run_turn(session, "make new.py")
        shown = [e for e in events if isinstance(e, ev.TurnChanges)]
        assert shown and shown[0].files[0].kept is False
        result = await run_command(session, "/keep 1 all")
        assert result.quiet and result.lines == ["new.py: kept"]
        (resent,) = [e for e in result.events if isinstance(e, ev.TurnChanges)]
        assert resent.files[0].kept is True
        assert (await run_command(session, "/keep nonsense")).lines == [
            "Usage: /keep <request> all | <file>  (the numbers are in the changes list)"
        ]
        await run_command(session, "/revert 1 0")
    finally:
        await session.agent.close()
    rows = [
        _json.loads(line)
        for p in audit_dir.glob("audit-*.jsonl")
        for line in p.read_text().splitlines()
    ]
    reviews = [(r["action"], r["turn"]) for r in rows if r["event"] == "review"]
    assert reviews == [("keep", 1), ("reject", 1)], "the user's decisions are in the audit log"


async def test_over_stdio_keep_works_while_a_later_request_is_still_running(
    mock_server: Any, project: Path
) -> None:
    from .test_stdio import write

    server = mock_server(
        [
            write("notes.md", "# notes\n"),
            {"content": "Wrote the notes."},
            {
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {"command": "sleep 60", "run_in_background": True},
                    }
                ]
            },
            # waits up to 30 s for output that never comes: the second request is busy meanwhile
            {
                "tool_calls": [
                    {
                        "name": "Jobs",
                        "arguments": {"action": "output", "job_id": 1, "wait_seconds": 30},
                    }
                ]
            },
            {"content": "Stopped."},
        ]
    )
    agent = await Agent.start(project, server, "--permission-mode", "bypassPermissions")
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="write notes")
        first = await agent.until("turn_changes")
        assert first["files"][0]["kept"] is False
        await agent.until("result")
        await agent.send(type="user_message", text="start a server and watch it")
        await agent.until("jobs_changed")
        await agent.until("tool_use")  # the second request is now inside its wait
        await agent.send(type="user_message", text="/keep 1 all")  # the chat's Keep button
        kept = await agent.until("turn_changes")
        assert kept["files"][0]["kept"] is True and kept["turn"] == 1
        await agent.send(type="user_message", text="/jobs stop 1")
        assert (await agent.until("result"))["subtype"] == "success"
        assert not [e for e in agent.events if e["type"] == "error"], "never refused as busy"
    finally:
        assert await agent.close() == 0
