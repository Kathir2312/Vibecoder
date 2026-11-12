"""/commit and /pr: cmcoder collects the facts with git (secret files left out), the model writes the
message and runs the git commands. Real repositories in temp folders; the model is scripted."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cmcoder.gitflow import commit_prompt, compare_url, pr_prompt, sandbox_note, strip_credentials
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import BUILT_IN, run_command

from .conftest import API_KEY
from .helpers import make_session
from .test_stdio import Agent


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "app.py").write_text("def greet():\n    return 'hi'\n")
    git(root, "add", "app.py")
    git(root, "commit", "-q", "-m", "Add the greeting")
    (root / "README.md").write_text("# app\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "Add a README")
    return root


async def allow(request: ev.PermissionRequest) -> dict[str, Any]:
    return {"allow": True}


# --- /commit ----------------------------------------------------------------------------------


def test_the_commands_are_built_in() -> None:
    assert {"commit", "pr"} <= {name for name, _, _ in BUILT_IN}


async def test_commit_in_a_folder_that_is_not_a_repository_or_has_nothing_to_commit_says_so(
    repo: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    session = make_session("http://127.0.0.1:9", plain)
    try:
        assert (await run_command(session, "/commit")).lines == ["This isn't a git repository."]
    finally:
        await session.agent.close()
    session = make_session("http://127.0.0.1:9", repo)
    try:
        result = await run_command(session, "/commit")
        assert result.lines == ["Nothing to commit: the working tree is clean."]
        assert result.send is None
    finally:
        await session.agent.close()


async def test_the_commit_request_carries_the_diff_the_style_and_the_files_never_to_stage(
    repo: Path,
) -> None:
    (repo / "app.py").write_text("def greet(name):\n    return f'hi {name}'\n")
    (repo / "new_module.py").write_text("VALUE = 1\n")
    (repo / ".env").write_text("API_TOKEN=abc123secret\n")
    session = make_session("http://127.0.0.1:9", repo)
    try:
        result = await run_command(session, "/commit make greet take a name")
    finally:
        await session.agent.close()
    text = result.send or ""
    assert "+    return f'hi {name}'" in text and "-    return 'hi'" in text
    assert "new_module.py" in text, "new files are listed"
    assert "Branch: main" in text
    assert "- Add a README" in text and "- Add the greeting" in text, "the commit style"
    assert "make greet take a name" in text
    assert "Never stage these, they hold secrets: .env." in text
    assert "abc123secret" not in text, "a secret file's contents never reach the model"
    assert "Never run `git add -A`" in text and "Do not push, amend, rebase, reset" in text


async def test_a_secret_that_is_only_a_change_is_also_kept_out_of_the_diff(repo: Path) -> None:
    (repo / ".env").write_text("A=1\n")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "Oops")
    (repo / ".env").write_text("A=2 SECRET_VALUE\n")
    (repo / "app.py").write_text("x = 1\n")
    session = make_session("http://127.0.0.1:9", repo)
    try:
        text = (await run_command(session, "/commit")).send or ""
    finally:
        await session.agent.close()
    assert "SECRET_VALUE" not in text and "x = 1" in text
    assert "Never stage these, they hold secrets: .env." in text


async def test_a_commit_the_model_makes_is_what_the_user_approved(
    mock_server: Any, repo: Path
) -> None:
    (repo / "app.py").write_text("def greet(name):\n    return f'hi {name}'\n")
    (repo / ".env").write_text("API_TOKEN=abc123secret\n")
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {
                            "command": 'git add app.py && git commit -m "Let greet take a name" '
                            '-m "The greeting was the same for everyone."',
                            # the sandbox keeps .git read-only: this is how a commit is made
                            "dangerously_disable_sandbox": True,
                        },
                    }
                ]
            },
            {"content": "Committed: greet takes a name."},
        ]
    )
    asked: list[ev.PermissionRequest] = []

    async def record(request: ev.PermissionRequest) -> dict[str, Any]:
        asked.append(request)
        return {"allow": True}

    session = make_session(server.base_url, repo, ask=record)
    try:
        result = await run_command(session, "/commit")
        assert result.send is not None
        sandboxed = session.agent.sandbox is not None
        assert ("dangerously_disable_sandbox" in result.send) == sandboxed
        events = [e async for e in session.run(result.send, allow=result.allow)]
    finally:
        await session.agent.close()
    if sandboxed:  # a commit outside the sandbox is always the user's to allow
        assert [r.name for r in asked] == ["Bash"] and "git commit" in asked[0].label
    assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
    assert git(repo, "log", "-1", "--pretty=%s") == "Let greet take a name"
    assert git(repo, "log", "-1", "--pretty=%b") == "The greeting was the same for everyone."
    assert git(repo, "show", "--name-only", "--pretty=format:", "HEAD") == "app.py"
    assert git(repo, "status", "--porcelain") == "?? .env", "the secret file was left alone"
    sent = str(server.requests[0]["messages"])
    assert "abc123secret" not in sent


# --- /pr --------------------------------------------------------------------------------------


async def branch_with_work(
    repo: Path, remote: str | None = "https://github.com/acme/app.git"
) -> None:
    if remote:
        git(repo, "remote", "add", "origin", remote)
    git(repo, "switch", "-q", "-c", "feature/names")
    (repo / "app.py").write_text("def greet(name):\n    return f'hi {name}'\n")
    git(repo, "commit", "-q", "-am", "Let greet take a name")


async def test_the_pr_request_has_the_commits_the_change_and_the_page_that_opens_it(
    repo: Path,
) -> None:
    await branch_with_work(repo)
    session = make_session("http://127.0.0.1:9", repo)
    try:
        result = await run_command(session, "/pr")
    finally:
        await session.agent.close()
    text = result.send or ""
    assert "branch `feature/names` against `main`" in text
    assert "Let greet take a name" in text and "+    return f'hi {name}'" in text
    assert "git push -u origin feature/names" in text and "Never force-push" in text
    assert "https://github.com/acme/app/compare/main...feature%2Fnames?expand=1" in text
    assert "gh pr create --base main --head feature/names" in text
    assert "Uncommitted changes are not part" not in text


async def test_uncommitted_work_is_said_not_to_be_part_of_the_pr_and_a_base_can_be_named(
    repo: Path,
) -> None:
    git(repo, "branch", "develop")
    await branch_with_work(repo, remote=None)
    (repo / "scratch.py").write_text("x = 1\n")
    session = make_session("http://127.0.0.1:9", repo)
    try:
        text = (await run_command(session, "/pr develop")).send or ""
    finally:
        await session.agent.close()
    assert "against `develop`" in text and "Uncommitted changes are not part" in text
    assert "compare/" not in text and "pull request page of the repository's host" in text


async def test_pr_says_why_it_cannot_when_there_is_nothing_to_open(repo: Path) -> None:
    session = make_session("http://127.0.0.1:9", repo)
    try:
        on_main = (await run_command(session, "/pr")).lines
        assert on_main and "You are on main" in on_main[0]
        git(repo, "switch", "-q", "-c", "empty")
        none = (await run_command(session, "/pr")).lines
        assert none and "No commits on empty since it left main" in none[0]
        unknown = (await run_command(session, "/pr nosuchbranch")).lines
        assert unknown and "nosuchbranch" in unknown[0]
        flag = (await run_command(session, "/pr --force")).lines
        assert flag and "Not a branch name" in flag[0]
    finally:
        await session.agent.close()


def test_the_page_that_opens_a_pull_request_is_found_for_the_hosts_we_know() -> None:
    assert (
        compare_url("git@github.com:acme/app.git", "main", "fix")
        == "https://github.com/acme/app/compare/main...fix?expand=1"
    )
    assert compare_url("https://gitlab.com/acme/app", "main", "fix") == (
        "https://gitlab.com/acme/app/-/merge_requests/new"
        "?merge_request%5Bsource_branch%5D=fix&merge_request%5Btarget_branch%5D=main"
    )
    azure = "https://dev.azure.com/acme/shop/_git/web"
    assert compare_url(azure, "main", "fix") == (
        "https://dev.azure.com/acme/shop/_git/web/pullrequestcreate?sourceRef=fix&targetRef=main"
    )
    assert compare_url("git@ssh.dev.azure.com:v3/acme/shop/web", "main", "fix") == (
        "https://dev.azure.com/acme/shop/_git/web/pullrequestcreate?sourceRef=fix&targetRef=main"
    )
    assert compare_url("https://git.corp.example/acme/app.git", "main", "fix") is None
    assert compare_url("not a url", "main", "fix") is None


def test_a_password_in_a_remote_address_never_reaches_the_prompt() -> None:
    assert strip_credentials("https://user:s3cret@github.com/acme/app.git") == (
        "https://github.com/acme/app.git"
    )
    url = compare_url("https://user:s3cret@github.com/acme/app.git", "main", "fix")
    assert url is not None and "s3cret" not in url and "user" not in url


# --- git inside the sandbox -----------------------------------------------------------------


def test_the_sandbox_keeps_git_read_only_so_the_model_is_told_how_a_commit_is_made() -> None:
    assert sandbox_note(None) == ""
    allowed = sandbox_note(SimpleNamespace(allow_unsandboxed=True))  # type: ignore[arg-type]
    assert "dangerously_disable_sandbox set to true" in allowed and "asked before each" in allowed
    locked = sandbox_note(SimpleNamespace(allow_unsandboxed=False))  # type: ignore[arg-type]
    assert "cannot be turned off here" in locked and "dangerously" not in locked
    assert "terminal" in locked


async def test_both_requests_carry_that_note(repo: Path) -> None:
    (repo / "app.py").write_text("x = 1\n")
    sandbox = SimpleNamespace(allow_unsandboxed=True)
    commit = await commit_prompt(repo, "", sandbox)  # type: ignore[arg-type]
    assert "dangerously_disable_sandbox set to true" in commit
    await branch_with_work(repo, remote=None)
    pr = await pr_prompt(repo, "", sandbox)  # type: ignore[arg-type]
    assert "dangerously_disable_sandbox set to true" in pr
    assert "dangerously" not in await pr_prompt(repo, "", None)


# --- over the protocol ------------------------------------------------------------------------


async def test_over_stdio_the_commands_are_listed_and_a_clean_tree_needs_no_model_turn(
    mock_server: Any, repo: Path
) -> None:
    server = mock_server([{"content": "should never be asked"}])
    agent = await Agent.start(repo, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")
        names = {c["name"] for c in (await agent.until("command_list"))["commands"]}
        assert {"commit", "pr"} <= names
        await agent.send(type="user_message", text="/commit")
        message = await agent.until("assistant_message")
        assert "Nothing to commit" in message["text"]
        assert (await agent.until("result"))["subtype"] == "success"
        assert server.requests == [], "nothing went to the model"
    finally:
        assert await agent.close() == 0
