"""/commit and /pr: the model writes the message and does the git work, from facts cmcoder collected.

    /commit                   commit the uncommitted changes, with a message from the diff
    /commit fix the login     the same, with this as what the change is for
    /pr                       prepare a pull request for this branch against the default branch
    /pr develop               the same, against `develop`

cmcoder collects the facts itself with plain `git` (the diff with secret files left out, the branch,
the recent commit subjects, which files must never be staged, the remote and a link that opens the
pull request page) and puts them in the prompt, so a small model does not have to find them. The
model then runs the git commands with its Bash tool, and each of `git add`, `git commit`, `git push`
and `gh pr create` asks the user first, like any command that changes something: nothing here
approves them. The prompts forbid `git add -A`, force pushes, amending and resetting; a force push is
also one of the high-risk commands the permission card always asks about.

LANGCHAIN / LANGGRAPH: the prompts are LangChain `PromptTemplate`s (as webtest.py and review.py);
the rest is reading git's output.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from langchain_core.prompts import PromptTemplate

from .review import MAX_LISTED, ReviewError, _git, collect
from .sandbox import Sandbox
from .sensitive import is_secret

# How many recent subjects show a commit style.
STYLE_COMMITS = 6


async def _top(root: Path) -> Path:
    code, out = await _git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        raise ReviewError("This isn't a git repository.")
    return Path(out.strip())


async def _branch(top: Path) -> str:
    """The branch HEAD is on, or "" when it is detached."""
    code, out = await _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out.strip() if code == 0 else ""


async def _subjects(top: Path, rev: str = "HEAD") -> list[str]:
    code, out = await _git(top, "log", f"-{STYLE_COMMITS}", "--no-color", "--pretty=%s", rev)
    return [line for line in out.splitlines() if line.strip()] if code == 0 else []


def sandbox_note(sandbox: Sandbox | None) -> str:
    """What the model must know about git inside the Bash sandbox, which keeps `.git` read-only
    (a hook or the config there would run outside it): git cannot write there, so `git add`,
    `git commit` and `git push` run with the sandbox switched off, which always asks the user."""
    if sandbox is None:
        return ""
    if sandbox.allow_unsandboxed:
        return (
            "\nThe Bash sandbox keeps .git read-only, so run `git add`, `git commit` and "
            "`git push` with the Bash tool's dangerously_disable_sandbox set to true: I am "
            "asked before each of them runs. Run `git status`, `git diff` and `git log` "
            "inside the sandbox as usual.\n"
        )
    return (
        "\nThe Bash sandbox keeps .git read-only and cannot be turned off here, so git cannot "
        "write from inside cmcoder. Do not try: give me the exact git commands to run in a "
        "terminal instead.\n"
    )


def strip_credentials(url: str) -> str:
    """A remote's address without the user:password in it."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", url.strip())


def compare_url(remote: str, base: str, branch: str) -> str | None:
    """The page that opens a pull request for `branch`, for the hosts we know; None otherwise."""
    url = strip_credentials(remote)
    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
    http = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", url)
    found = ssh or http
    if not found:
        return None
    host, path = found.group(1), found.group(2)
    b, s = quote(base, safe=""), quote(branch, safe="")
    if host == "github.com":
        return f"https://github.com/{path}/compare/{b}...{s}?expand=1"
    if host == "gitlab.com":
        return (
            f"https://gitlab.com/{path}/-/merge_requests/new"
            f"?merge_request%5Bsource_branch%5D={s}&merge_request%5Btarget_branch%5D={b}"
        )
    if host.endswith("dev.azure.com") or host.endswith("visualstudio.com"):
        # https://dev.azure.com/org/project/_git/repo, or ssh: git@ssh.dev.azure.com:v3/org/project/repo
        azure = re.match(r"^(?:v3/)?([^/]+)/([^/]+)/(?:_git/)?([^/]+)$", path)
        if not azure:
            return None
        org, project, repo = azure.groups()
        base_url = (
            f"https://dev.azure.com/{org}" if host.endswith("dev.azure.com") else f"https://{host}"
        )
        return f"{base_url}/{project}/_git/{repo}/pullrequestcreate?sourceRef={s}&targetRef={b}"
    return None


async def _protected(top: Path) -> list[str]:
    """Changed or new files that must never be committed by the model (keys, `.env`)."""
    _, changed = await _git(top, "diff", "--name-only", "HEAD")
    _, new = await _git(top, "ls-files", "--others", "--exclude-standard")
    names = {*changed.splitlines(), *new.splitlines()}
    return sorted(n for n in names if n and is_secret(top / n, top))[:MAX_LISTED]


COMMIT_PROMPT = PromptTemplate.from_template(
    """\
Commit the uncommitted changes of this project to git. Work through these steps in order.

1. Read the change below. If it is really two or more unrelated changes, make one commit \
for each (steps 2-4 for each).
2. Stage only the files of the commit, by name: `git add <file> <file>`. Never run \
`git add -A`, `git add .` or `git commit -a`.{never}
3. Write the message: a subject of at most 72 characters in the imperative mood ("Add \
...", "Fix ..."), then, if the reason is not obvious, a blank line and a short body that says \
why, not what. {style}{hint}
4. Run `git commit -m "<subject>" -m "<body>"` (leave out the second -m when there is no body).
5. Run `git log -1 --stat` and tell me in a sentence what was committed.

Do not push, amend, rebase, reset or change the git configuration. If a commit fails (a hook, \
a missing identity), say what git printed instead of working around it.

Branch: {branch}
{sandbox}{extras}
Here is the change:

{diff}"""
)

PR_PROMPT = PromptTemplate.from_template(
    """\
Prepare a pull request for the branch `{branch}` against `{base}`. Work through these steps \
in order.

1. Read the commits and the change below. Write a title of at most 70 characters, and a body \
with a short summary (what changed and why, as bullets) and a test plan that lists what the \
change actually adds or touches in tests; do not claim you ran anything you did not run.
2. Push the branch: `git push -u origin {branch}`. Never force-push.
3. {open}

Do not commit, amend, rebase or reset anything.
{sandbox}
{extras}
Here is the change, {base} to {branch}:

{diff}"""
)


async def commit_prompt(root: Path, hint: str = "", sandbox: Sandbox | None = None) -> str:
    """The request to commit what is uncommitted. Raises `ReviewError` with what to tell the user."""
    top = await _top(root)
    code, status = await _git(top, "status", "--porcelain")
    if code != 0:
        raise ReviewError(f"git could not read the status: {status.strip()[:200]}")
    if not status.strip():
        raise ReviewError("Nothing to commit: the working tree is clean.")
    change = await collect(top, "")
    protected = await _protected(top)
    subjects = await _subjects(top)
    extras = ""
    if change.untracked:
        extras += "\nNew files, not in the diff (Read one if you need to): " + ", ".join(
            change.untracked
        )
        extras += "\n"
    if change.left_out:
        extras += (
            "\nThe diff below is cut for size. Not shown: " + ", ".join(change.left_out) + "\n"
        )
    return COMMIT_PROMPT.format(
        never=(
            "\n   Never stage these, they hold secrets: " + ", ".join(protected) + "."
            if protected
            else ""
        ),
        style=(
            "Match the style of the recent commits:\n"
            + "\n".join(f"   - {s}" for s in subjects)
            + "\n"
            if subjects
            else "There are no earlier commits to match.\n"
        ),
        hint=(f"\n   The change is for this, in the user's words: {hint}" if hint else ""),
        branch=await _branch(top) or "(detached HEAD)",
        sandbox=sandbox_note(sandbox),
        extras=extras,
        diff=change.diff or "(no tracked changes: only the new files above)",
    )


async def _default_base(top: Path) -> str | None:
    code, out = await _git(top, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip()  # "origin/main"
    for name in ("origin/main", "origin/master", "main", "master"):
        check, _ = await _git(top, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if check == 0:
            return name
    return None


async def pr_prompt(root: Path, base_arg: str = "", sandbox: Sandbox | None = None) -> str:
    """The request to prepare a pull request. Raises `ReviewError` with what to tell the user."""
    top = await _top(root)
    branch = await _branch(top)
    if not branch:
        raise ReviewError("HEAD is detached: switch to the branch (or make one) first.")
    base = base_arg.strip() or await _default_base(top)
    if not base:
        raise ReviewError(
            "Could not tell which branch to open the pull request against: /pr <base>."
        )
    if base.startswith("-"):
        raise ReviewError(f"Not a branch name: {base!r}.")
    short = base.removeprefix("origin/")
    if branch == short:
        raise ReviewError(
            f"You are on {branch}, the branch a pull request goes into. Make a branch for the "
            "work first (ask cmcoder to, or `git switch -c <name>`)."
        )
    try:
        change = await collect(top, base)
    except ReviewError as e:
        raise ReviewError(
            f"No commits on {branch} since it left {base}: nothing to open a pull request for."
            if "No changes" in str(e)
            else str(e)
        ) from e
    if not change.log.strip():
        raise ReviewError(
            f"No commits on {branch} since it left {base}: nothing to put in a pull request."
        )
    code, remote = await _git(top, "remote", "get-url", "origin")
    link = compare_url(remote, short, branch) if code == 0 else None
    _, dirty = await _git(top, "status", "--porcelain")
    extras = f"Commits:\n{change.log.strip()}\n"
    if dirty.strip():
        extras += "\nUncommitted changes are not part of the pull request (/commit puts them in a commit).\n"
    if change.left_out:
        extras += (
            "\nThe diff below is cut for size. Not shown: " + ", ".join(change.left_out) + "\n"
        )
    if change.hidden:
        extras += "\nLeft out on purpose (secret files): " + ", ".join(change.hidden) + "\n"
    opening = (
        "Open it: if `gh --version` works, run `gh pr create --base "
        f'{short} --head {branch} --title "<title>" --body "<body>"`; otherwise give me this '
        f"link to open the page ({link or 'the address of the repository'}) and the title and body in "
        "a code block to paste into it."
        if link
        else "Open it: if `gh --version` works, run `gh pr create --base "
        f'{short} --head {branch} --title "<title>" --body "<body>"`; otherwise give me the title '
        "and body in a code block to paste into the pull request page of the repository's host."
    )
    return PR_PROMPT.format(
        branch=branch,
        base=short,
        open=opening,
        sandbox=sandbox_note(sandbox),
        extras=extras,
        diff=change.diff or "(no tracked changes)",
    )
