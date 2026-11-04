"""/review: cmcoder collects the change with git and the model reviews it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cmcoder.review import MAX_DIFF_CHARS, ReviewError, collect, fit, review_prompt
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, run_turn


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "util.py").write_text("X = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    return root


async def prompt_for(root: Path, target: str = "") -> str:
    return review_prompt(await collect(root, target))


async def test_uncommitted_changes_and_new_files(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "new.py").write_text("print('hi')\n")
    text = await prompt_for(repo)
    assert (
        "uncommitted changes" in text
        and "-    return a + b" in text
        and "+    return a - b" in text
    )
    assert "new.py" in text  # listed, to be read


async def test_staged_only(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a * b\n")
    git(repo, "add", "app.py")
    (repo / "util.py").write_text("X = 2\n")  # not staged
    text = await prompt_for(repo, "staged")
    assert "a * b" in text and "X = 2" not in text


async def test_a_branch_since_it_left_main(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "util.py").write_text("X = 99\n")
    git(repo, "commit", "-qam", "change X")
    text = await prompt_for(repo, "main")
    assert "since it left main" in text and "change X" in text and "+X = 99" in text


async def test_one_path(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return 0\n")
    (repo / "util.py").write_text("X = 5\n")
    text = await prompt_for(repo, "app.py")
    assert "return 0" in text and "X = 5" not in text


async def test_secret_files_are_left_out(repo: Path) -> None:
    (repo / ".env").write_text("KEY=one\n")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-qm", "env")
    (repo / ".env").write_text("KEY=hunter2-very-secret\n")
    (repo / "app.py").write_text("def add(a, b):\n    return 1\n")
    text = await prompt_for(repo)
    assert "hunter2" not in text and ".env" in text  # named as left out, never shown
    assert "return 1" in text


def test_a_big_diff_keeps_whole_files_and_names_the_rest(tmp_path: Path) -> None:
    def block(name: str, n: int) -> str:
        return f"diff --git a/{name} b/{name}\n" + "".join(f"+line {i}\n" for i in range(n))

    diff = block("a.py", 100) + block("b.py", 20_000) + block("c.py", 10)
    text, left_out, hidden = fit(diff, tmp_path, limit=MAX_DIFF_CHARS)
    assert "a.py" in text and "b.py" not in text
    assert "b.py" in left_out and not hidden
    assert len(text) <= MAX_DIFF_CHARS


async def test_nothing_to_review_and_bad_targets(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="No uncommitted changes"):
        await collect(repo)
    with pytest.raises(ReviewError, match="isn't a branch"):
        await collect(repo, "nope")
    with pytest.raises(ReviewError, match="Not a branch"):
        await collect(repo, "--output=/tmp/x")
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(ReviewError, match="isn't a git repository"):
        await collect(outside)
    git(repo, "checkout", "-q", "-b", "same")
    with pytest.raises(ReviewError, match="No changes to review"):
        await collect(repo, "main")


async def test_review_command_sends_the_diff_to_the_model(mock_server, repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    server = mock_server([{"content": "[high] app.py:2 - add subtracts."}])
    session = make_session(server.base_url, repo)
    try:
        result = await run_command(session, "/review")
        assert result.send and "return a - b" in result.send
        events = await run_turn(session, result.send)
        clean = await run_command(session, "/review nope")
        assert clean.send is None and "isn't a branch" in clean.lines[0]
    finally:
        await session.agent.close()
    assert events[-1].result.startswith("[high]")
    assert "return a - b" in str(server.requests[0]["messages"][-1])
