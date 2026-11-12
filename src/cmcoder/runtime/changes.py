"""What a request changed, for review: per file and per hunk, with a safe way to take a part back.

At the end of a request that wrote or edited files, `ChangeReviews.build` turns the file snapshots
`/rewind` already keeps (core/checkpoints.py: each file as it was before the request first touched
it) into a `turn_changes` event: the files, and for each one its hunks (the changed lines with a
little around them). The chat shows them; `Revert` on a file or a hunk comes back as `/revert`
with numbers, never a path, so the panel can only ask for what is in the list.

A revert never overwrites what the user did meanwhile. The review remembers what it expects to find
on disk (what the agent wrote, then what each revert wrote); if the file is anything else, someone
edited it since and the revert is refused with that explanation. Changes made by shell commands are
not tracked (the same limit as `/rewind`): only Write and Edit go through the snapshots.

Plain Python: difflib for the hunks; LangChain and LangGraph have nothing for reviewing file diffs.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.checkpoints import Checkpoints
from ..protocol import events as ev

MAX_FILES = 30
MAX_DIFF_LINES = 2000  # per file: more is a rewrite, shown as a count
MAX_BYTES = 1_000_000  # per file side: bigger files are listed but not diffed
CONTEXT = 3


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


@dataclass
class _File:
    path: Path
    shown: str  # as the chat shows it: relative to the project when it is inside
    before: bytes | None  # None: the agent created the file
    after: bytes
    reverted_blocks: set[int] = field(default_factory=set)
    on_disk: bytes = b""  # what this review expects to find now
    deleted: bool = False  # a file the agent created, taken back by deleting it
    note: str = ""  # why the last revert was refused, if it was

    def __post_init__(self) -> None:
        self.on_disk = self.after

    @property
    def added(self) -> bool:
        return self.before is None


class _Diff:
    """The opcodes of a file's change, and the hunks the chat draws from them."""

    def __init__(self, before: list[str], after: list[str]) -> None:
        self.before, self.after = before, after
        matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
        # A copy: get_grouped_opcodes() below trims the cached list it is given, and the whole
        # file (the untouched lines at both ends too) is needed to put a file back together.
        self.opcodes = list(matcher.get_opcodes())
        # A change block is an opcode that is not "equal", numbered in order; a hunk is the group of
        # change blocks that lie close enough to share their surrounding lines.
        self.blocks = {
            (i1, i2, j1, j2): n
            for n, (i1, i2, j1, j2) in enumerate(
                (op[1], op[2], op[3], op[4]) for op in self.opcodes if op[0] != "equal"
            )
        }
        self.hunks: list[list[tuple[str, int, int, int, int]]] = [
            list(g) for g in matcher.get_grouped_opcodes(CONTEXT)
        ]

    def block_ids_of(self, hunk: int) -> set[int]:
        """The change blocks inside hunk `hunk`."""
        return {
            self.blocks[(i1, i2, j1, j2)]
            for tag, i1, i2, j1, j2 in self.hunks[hunk]
            if tag != "equal"
        }

    def merged(self, reverted: set[int]) -> list[str]:
        """The file with the given change blocks taken back and the others kept."""
        out: list[str] = []
        block = 0
        for tag, i1, i2, j1, j2 in self.opcodes:
            if tag == "equal":
                out += self.after[j1:j2]
                continue
            out += self.before[i1:i2] if block in reverted else self.after[j1:j2]
            block += 1
        return out


class ChangeReviews:
    """The reviews of this session's requests, by turn: what each changed and what was taken back."""

    def __init__(self) -> None:
        self._turns: dict[int, list[_File]] = {}
        self._diffs: dict[tuple[int, int], _Diff | None] = {}

    # -- building ----------------------------------------------------------------

    def build(self, turn: int, checkpoints: Checkpoints, root: Path) -> ev.TurnChanges | None:
        """The files this request changed, or None when it changed none."""
        entries = checkpoints.entries_in(turn)
        files: list[_File] = []
        for entry in entries[:MAX_FILES]:
            path = Path(entry.path)
            if entry.skipped:
                continue
            before = None if entry.blob is None else checkpoints.read(entry.blob)
            try:
                after = path.read_bytes() if path.is_file() else None
            except OSError:
                after = None
            if after is None or before == after:
                continue  # gone again, or changed back to what it was
            try:
                shown = path.relative_to(root).as_posix()
            except ValueError:
                shown = path.as_posix()
            files.append(_File(path, shown, before, after))
        if not files:
            self._turns.pop(turn, None)
            return None
        self._turns[turn] = files
        for n in range(len(files)):
            self._diffs.pop((turn, n), None)
        return self.event(turn)

    # -- the event ---------------------------------------------------------------

    def _diff(self, turn: int, n: int) -> _Diff | None:
        key = (turn, n)
        if key not in self._diffs:
            f = self._turns[turn][n]
            before = b"" if f.before is None else f.before
            small = len(before) <= MAX_BYTES and len(f.after) <= MAX_BYTES
            a, b = _text(before), _text(f.after)
            self._diffs[key] = (
                _Diff(_lines(a), _lines(b)) if small and a is not None and b is not None else None
            )
        return self._diffs[key]

    def event(self, turn: int) -> ev.TurnChanges:
        out: list[ev.ChangedFile] = []
        for n, f in enumerate(self._turns[turn]):
            diff = self._diff(turn, n)
            hunks: list[ev.ChangedHunk] = []
            added = removed = 0
            if diff is not None:
                for tag, i1, i2, j1, j2 in diff.opcodes:
                    if tag != "equal":
                        added += j2 - j1
                        removed += i2 - i1
                total = 0
                for h, group in enumerate(diff.hunks):
                    lines: list[ev.DiffLine] = []
                    for tag, i1, i2, j1, j2 in group:
                        if tag == "equal":
                            lines += [
                                ev.DiffLine(kind="ctx", text=t.rstrip("\r\n"))
                                for t in diff.before[i1:i2]
                            ]
                            continue
                        lines += [
                            ev.DiffLine(kind="del", text=t.rstrip("\r\n"))
                            for t in diff.before[i1:i2]
                        ]
                        lines += [
                            ev.DiffLine(kind="add", text=t.rstrip("\r\n"))
                            for t in diff.after[j1:j2]
                        ]
                    total += len(lines)
                    if total > MAX_DIFF_LINES:
                        hunks = []  # a rewrite: counted, not drawn
                        break
                    blocks = diff.block_ids_of(h)
                    hunks.append(
                        ev.ChangedHunk(
                            id=h,
                            old_start=group[0][1] + 1,
                            new_start=group[0][3] + 1,
                            lines=lines,
                            reverted=bool(blocks) and blocks <= f.reverted_blocks,
                        )
                    )
            if diff is None:
                back = f.deleted or (not f.added and f.on_disk == f.before)
            else:
                back = f.deleted or (
                    bool(diff.blocks) and len(f.reverted_blocks) == len(diff.blocks)
                )
            out.append(
                ev.ChangedFile(
                    index=n,
                    path=f.shown,
                    status="added" if f.added else "modified",
                    added=added,
                    removed=removed,
                    hunks=hunks,
                    binary=diff is None,
                    truncated=diff is not None and bool(diff.blocks) and not hunks,
                    reverted=back,
                    note=f.note,
                )
            )
        return ev.TurnChanges(
            turn=turn,
            files=out,
            added=sum(f.added for f in out),
            removed=sum(f.removed for f in out),
        )

    # -- taking changes back ------------------------------------------------------

    def revert(
        self, turn: int, file: int | None, hunk: int | None
    ) -> tuple[list[str], ev.TurnChanges | None]:
        """Take back a hunk, a file (`hunk` None) or the whole request (both None). Lines to say what
        happened, and the review as it is now."""
        files = self._turns.get(turn)
        if files is None:
            return [f"There is nothing to review for request {turn} any more."], None
        targets = range(len(files)) if file is None else [file]
        said: list[str] = []
        for n in targets:
            if not 0 <= n < len(files):
                return [f"Request {turn} has no file number {n}."], self.event(turn)
            said.append(self._revert_file(turn, n, hunk if file is not None else None))
        return said, self.event(turn)

    def _revert_file(self, turn: int, n: int, hunk: int | None) -> str:
        f = self._turns[turn][n]
        f.note = ""
        if f.deleted:
            return f"{f.shown}: already deleted"
        try:
            current = f.path.read_bytes() if f.path.is_file() else None
        except OSError as e:
            f.note = f"could not read it: {e}"
            return f"{f.shown}: {f.note}"
        if current != f.on_disk:
            f.note = "changed since cmcoder wrote it, so it is left alone (edit it yourself, or use /rewind)"
            return f"{f.shown}: {f.note}"
        diff = self._diff(turn, n)
        try:
            if f.added:  # created by the agent: one change, taken back by deleting the file
                f.path.unlink(missing_ok=True)
                f.on_disk = b""
                f.deleted = True
                return f"{f.shown}: deleted (cmcoder had created it)"
            if diff is None:  # not text, or too big to diff: the whole file or nothing
                if hunk is not None:
                    f.note = "can only be taken back as a whole file"
                    return f"{f.shown}: {f.note}"
                assert f.before is not None
                f.path.write_bytes(f.before)
                f.on_disk = f.before
                return f"{f.shown}: restored"
            if hunk is None:
                blocks = set(range(len(diff.blocks)))
            elif 0 <= hunk < len(diff.hunks):
                blocks = diff.block_ids_of(hunk)
            else:
                f.note = f"has no hunk number {hunk}"
                return f"{f.shown}: {f.note}"
            f.reverted_blocks |= blocks
            merged = "".join(diff.merged(f.reverted_blocks)).encode("utf-8")
            f.path.write_bytes(merged)
            f.on_disk = merged
            return f"{f.shown}: {'restored' if hunk is None else f'hunk {hunk + 1} taken back'}"
        except OSError as e:
            f.note = f"could not write it: {e}"
            return f"{f.shown}: {f.note}"
