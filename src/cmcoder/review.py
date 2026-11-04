"""/review: the model reviews a change for bugs before you commit or merge it.

cmcoder collects the change itself (plain `git`, run by cmcoder, not by the
model) and hands the model the diff in its prompt, so the review always starts
from the same text and doesn't depend on the model asking for it. Files that
`sensitive.py` keeps from the model (keys, `.env`) are left out of the diff.
The model then reads the surrounding code with its normal read-only tools and
reports findings; it never changes a file.

    /review               uncommitted changes (staged and unstaged) and new files
    /review staged        only what is staged
    /review main          this branch's changes since it left `main` (any branch, tag or commit)
    /review src/app.py    uncommitted changes in one file or folder
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .sensitive import is_secret

MAX_DIFF_CHARS = 60_000
MAX_LISTED = 40
_TIMEOUT = 30.0


class ReviewError(Exception):
    """Nothing to review (or not possible); the message is shown to the user."""


@dataclass
class Change:
    title: str  # what is being reviewed, in words
    diff: str  # the diff text (secrets removed, capped)
    left_out: list[str]  # files whose diff didn't fit
    hidden: list[str]  # files kept from the model on purpose
    untracked: list[str]  # new files git doesn't know yet (not in the diff)
    log: str = ""  # commit subjects, for a branch review


async def _git(root: Path, *args: str) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.quotepath=off",
            *args,
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), _TIMEOUT)
    except FileNotFoundError as e:
        raise ReviewError("git isn't installed or isn't on PATH.") from e
    except TimeoutError as e:
        raise ReviewError("git took too long.") from e
    return proc.returncode or 0, out.decode("utf-8", "replace")


def split_files(diff: str) -> list[tuple[str, str]]:
    """A `git diff` as (path, text) pieces, one per file."""
    pieces = re.split(r"(?m)^(?=diff --git )", diff)
    files = []
    for piece in pieces:
        if not piece.strip():
            continue
        m = re.match(r"diff --git a/(.*?) b/(.*)", piece)
        files.append((m.group(2) if m else "?", piece))
    return files


def fit(diff: str, root: Path, limit: int = MAX_DIFF_CHARS) -> tuple[str, list[str], list[str]]:
    """Drop secret files, then keep whole files up to `limit` characters."""
    kept: list[str] = []
    hidden: list[str] = []
    left_out: list[str] = []
    size = 0
    for path, text in split_files(diff):
        if is_secret(root / path, root):
            hidden.append(path)
        elif size + len(text) > limit and kept:
            left_out.append(path)
        elif len(text) > limit:  # one huge file: its start, cut at a line
            cut = text[:limit].rsplit("\n", 1)[0]
            kept.append(cut + "\n[... the rest of this file's diff is left out ...]\n")
            left_out.append(path + " (rest)")
            size += limit
        else:
            kept.append(text)
            size += len(text)
    return "".join(kept), left_out, hidden


async def _verify(root: Path, ref: str) -> bool:
    code, _ = await _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return code == 0


def _existing(root: Path, top: Path, target: str) -> str | None:
    """`target` as a path relative to the repository, if it names a file or folder here."""
    for base in (root, top):
        candidate = (base / target).resolve()
        if candidate.exists() and (candidate == top or candidate.is_relative_to(top)):
            return os.path.relpath(candidate, top).replace(os.sep, "/")
    return None


async def collect(root: Path, target: str = "") -> Change:
    """The change to review. Raises `ReviewError` when there is none."""
    target = target.strip()
    code, out = await _git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        raise ReviewError("This isn't a git repository, so there is no change to review.")
    top = Path(out.strip())
    if target.startswith("-"):
        raise ReviewError(f"Not a branch, commit or file: {target!r}.")
    head = ["HEAD"] if await _verify(top, "HEAD") else []  # a new repository has no HEAD yet
    log = ""
    scope = ""  # a path the review is limited to
    new_files = True  # whether untracked files belong to this review
    if target == "staged":
        title, args = "the staged changes", ["diff", "--cached", "--no-color"]
    elif target and await _verify(top, target):
        title = f"this branch's changes since it left {target}"
        args = ["diff", "--no-color", f"{target}...HEAD"]
        _, log = await _git(top, "log", "--oneline", "--no-color", f"{target}..HEAD")
        new_files = False
    elif ".." in target:
        title, args, new_files = f"the changes {target}", ["diff", "--no-color", target], False
    elif target and (scope := _existing(root, top, target) or ""):
        title = f"the uncommitted changes in {target}"
        args = ["diff", "--no-color", *(head or ["--cached"]), "--", scope]
    elif target:
        raise ReviewError(f"{target!r} isn't a branch, tag, commit or file here.")
    else:
        title = "the uncommitted changes (staged and unstaged)"
        args = ["diff", "--no-color", *(head or ["--cached"])]
    code, diff = await _git(top, *args)
    if code != 0:
        raise ReviewError(f"git could not make the diff: {diff.strip()[:200]}")
    untracked: list[str] = []
    if new_files:
        _, listed = await _git(top, "ls-files", "--others", "--exclude-standard")
        untracked = [
            f
            for f in listed.splitlines()
            if f
            and (not scope or f == scope or f.startswith(scope.rstrip("/") + "/"))
            and not is_secret(top / f, top)
        ][:MAX_LISTED]
    if not diff.strip() and not untracked:
        raise ReviewError(
            f"No changes to review for {target!r}."
            if target
            else "No uncommitted changes to review. Try /review <branch> for a branch."
        )
    text, left_out, hidden = fit(diff, top)
    return Change(title, text, left_out, hidden, untracked, log)


PROMPT = """\
Review {title} for bugs, as a careful senior engineer would before a merge. You may \
read any file in the project to understand the change; do not modify anything.

Focus on what can actually go wrong: logic errors and wrong edge cases, crashes and \
unhandled errors, security problems (injection, secrets, unsafe input, permissions), \
broken or missing tests for the new behaviour, and changes that break their callers. \
Check a suspicion by reading the surrounding code and the callers before you report it. \
Skip style and naming unless they hide a bug.

Report each finding as:
  [high|medium|low] file:line - what is wrong, and a concrete input or situation that \
makes it fail, then the fix in one sentence.
Order them most serious first. If you find nothing real, say so plainly; do not invent \
findings. End with one line: what you reviewed and how confident you are.
{extras}
Here is the change:

{diff}"""


def review_prompt(change: Change) -> str:
    extras = ""
    if change.log.strip():
        extras += f"\nCommits in this branch:\n{change.log.strip()}\n"
    if change.untracked:
        extras += "\nNew files not in the diff (Read them): " + ", ".join(change.untracked) + "\n"
    if change.left_out:
        extras += (
            "\nThe diff below is cut for size. Not shown: "
            + ", ".join(change.left_out)
            + ". Read those files, or run `git diff -- <file>`.\n"
        )
    if change.hidden:
        extras += "\nLeft out on purpose (secret files): " + ", ".join(change.hidden) + "\n"
    return PROMPT.format(
        title=change.title, extras=extras, diff=change.diff or "(no tracked changes)"
    )
