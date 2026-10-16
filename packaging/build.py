"""Build the standalone `cmcoder` (no Python or uv needed) with PyInstaller.

    uv run --with pyinstaller python packaging/build.py

Output: dist/cmcoder/ (a folder holding the `cmcoder` executable and its
_internal files). A folder rather than one file starts faster and isn't
unpacked to a temp folder on every run, which antivirus software dislikes.

LangChain note: LangChain and LangGraph import some modules lazily (by name,
at run time), which PyInstaller's import scan can't see. `--collect-submodules`
bundles them all.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_id() -> str:
    """The commit and UTC date, e.g. "aa3dc83 2026-10-07" ("+changes" if the tree isn't clean)."""

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    commit = git("rev-parse", "--short=7", "HEAD") or "unknown"
    if git("status", "--porcelain", "--untracked-files=no"):
        commit += "+changes"
    return f"{commit} {datetime.datetime.now(datetime.UTC):%Y-%m-%d}"


def main() -> None:
    try:
        import PyInstaller.__main__
    except ImportError:
        sys.exit("PyInstaller is needed: uv run --with pyinstaller python packaging/build.py")

    build_file = ROOT / "build" / "BUILD"
    build_file.parent.mkdir(exist_ok=True)
    build_file.write_text(build_id(), encoding="utf-8")
    out = ROOT / "dist"
    shutil.rmtree(out / "cmcoder", ignore_errors=True)
    sep = ";" if sys.platform == "win32" else ":"
    PyInstaller.__main__.run(
        [
            str(ROOT / "packaging" / "cmcoder_entry.py"),
            "--name=cmcoder",
            "--onedir",
            "--console",
            "--noconfirm",
            "--clean",
            f"--distpath={out}",
            f"--workpath={ROOT / 'build' / 'pyinstaller'}",
            f"--specpath={ROOT / 'build'}",
            f"--add-data={build_file}{sep}cmcoder",
            # Imported lazily or found through entry points / package data:
            "--collect-submodules=cmcoder",
            "--collect-submodules=langchain",
            "--collect-submodules=langchain_core",
            "--collect-submodules=langchain_openai",
            "--collect-submodules=langgraph",
            "--collect-data=tiktoken",
            "--collect-submodules=keyring",
            "--copy-metadata=keyring",
            "--collect-submodules=truststore",
            # langchain-core reads its own version from package metadata.
            "--copy-metadata=langchain-core",
            "--copy-metadata=langchain",
            "--copy-metadata=langgraph",
            "--copy-metadata=langchain-openai",
        ]
    )
    exe = out / "cmcoder" / ("cmcoder.exe" if sys.platform == "win32" else "cmcoder")
    print(f"built {exe}")


if __name__ == "__main__":
    main()
