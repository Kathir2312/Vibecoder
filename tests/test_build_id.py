"""The build is named after its git commit; where there is no git (or no commit, or no wish
for one) CMCODER_BUILD_ID names it instead, in packaging/build.py and in the package scripts."""

from __future__ import annotations

import datetime
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


@pytest.fixture(scope="module")
def build() -> ModuleType:
    sys.path.insert(0, str(PACKAGING))  # build.py imports brand from there
    spec = importlib.util.spec_from_file_location("cmcoder_build_script", PACKAGING / "build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TODAY = r"\d{4}-\d\d-\d\d"


def test_a_named_build_uses_no_git(build: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*a: object, **k: object) -> None:
        raise AssertionError("git was started")

    monkeypatch.setenv("CMCODER_BUILD_ID", "nogit")
    monkeypatch.setattr(subprocess, "run", no_git)
    name, date = build.build_id().split(" ")
    assert name == "nogit"
    assert date == f"{datetime.datetime.now(datetime.UTC):%Y-%m-%d}"


@pytest.mark.parametrize("bad", ["two words", "a/b", "x" * 25, "semi;colon", "ä"])
def test_a_build_name_with_odd_characters_is_refused(
    build: ModuleType, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("CMCODER_BUILD_ID", bad)
    with pytest.raises(SystemExit, match="CMCODER_BUILD_ID"):
        build.build_id()


def test_without_git_and_without_a_name_the_build_is_unknown(
    build: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*a: object, **k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.delenv("CMCODER_BUILD_ID", raising=False)
    monkeypatch.setattr(subprocess, "run", no_git)
    assert re.fullmatch(rf"unknown {TODAY}", build.build_id())


def test_with_git_the_build_is_named_by_its_commit(
    build: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CMCODER_BUILD_ID", raising=False)
    assert re.fullmatch(rf"(?:[0-9a-f]{{7}}(?:\+changes)?|unknown) {TODAY}", build.build_id())


@pytest.mark.skipif(sys.platform == "win32", reason="runs the .sh scripts' check with bash")
def test_the_shell_check_accepts_a_name_where_there_is_no_git(tmp_path: Path) -> None:
    """No git on the PATH (only grep, which the check uses): only the name lets it through."""
    bash, grep = shutil.which("bash"), shutil.which("grep")
    if not bash or not grep:
        pytest.skip("needs bash and grep")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "grep").symlink_to(grep)
    script = f"""
        source '{PACKAGING / "package-common.sh"}'
        root='{tmp_path}'; skip_tests=0
        check_git
        echo passed
    """

    def run(**extra: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": str(bin_dir), "HOME": str(tmp_path), **extra}
        return subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True)

    refused = run()
    assert refused.returncode != 0 and "git is needed" in refused.stderr
    assert "CMCODER_BUILD_ID" in refused.stderr  # it says what to do
    named = run(CMCODER_BUILD_ID="nogit")
    assert named.returncode == 0 and "passed" in named.stdout
    assert run(CMCODER_BUILD_ID="bad name").returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="runs the .sh scripts' check with bash")
def test_the_shell_check_names_the_way_out_of_a_repository_without_commits(tmp_path: Path) -> None:
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("no git")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    script = f"""
        source '{PACKAGING / "package-common.sh"}'
        root='{tmp_path}'; skip_tests=0
        check_git
        echo passed
    """
    env = {**os.environ}
    env.pop("CMCODER_BUILD_ID", None)
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    for hint in ("Needed a single revision", "git commit -m build", "CMCODER_BUILD_ID=nogit"):
        assert hint in r.stderr, r.stderr
    env["CMCODER_BUILD_ID"] = "nogit"
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "passed" in r.stdout
