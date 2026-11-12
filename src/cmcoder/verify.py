"""Verify: run the project's own checks (build, tests, lint) and report what failed.

What runs is the user's: `verify.commands` in their settings (a trusted project's too). cmcoder
suggests commands from the project's files (`detect_commands`, shown by `/verify`) but never
runs a guess. The loop that sends the agent back to fix a failure is middleware/verify.py; this
file is what both it and `/verify` use.

LANGCHAIN / LANGGRAPH: nothing here. Running a command is `tools/shell.PersistentShell` (the one
the Bash tool uses, so the sandbox applies), the rest is reading a few project files.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .compat import find_shell
from .sandbox import Sandbox
from .tools.base import truncate_middle
from .tools.shell import PersistentShell


@dataclass
class CheckResult:
    command: str
    ok: bool
    exit_code: int | None  # None: it timed out, or there is no shell to run it with
    output: str  # everything it printed (cut in the middle when very long)
    duration_ms: int
    timed_out: bool = False

    @property
    def summary(self) -> str:
        if self.timed_out:
            return "timed out"
        return (
            "ok"
            if self.ok
            else f"exit {self.exit_code}"
            if self.exit_code is not None
            else "failed"
        )

    @property
    def total_lines(self) -> int:
        return len(self.output.rstrip("\n").split("\n")) if self.output.strip() else 0


async def run_check(
    command: str,
    root: Path,
    *,
    sandbox: Sandbox | None,
    timeout: float,
    max_chars: int,
    on_output: Callable[[str], None] | None = None,
) -> CheckResult:
    """Run one command in the project folder, in a shell of its own (nothing a command `cd`s or
    exports reaches the agent's shell)."""
    started = time.monotonic()
    shell = find_shell()
    if shell is None:
        return CheckResult(command, False, None, "There is no bash to run it with.", 0)
    runner = PersistentShell(root, shell, sandbox=sandbox)
    try:
        # In a subshell: a command that runs `exit 3` ends that, not the shell, so its status is told.
        res = await runner.run(f"(\n{command}\n)", timeout, on_output=on_output)
    finally:
        await runner.close()
    output = truncate_middle(res.output.rstrip("\n"), max_chars)
    if res.timed_out:
        output = f"{output}\n\n(Stopped: it ran longer than {timeout:.0f}s.)".lstrip()
    return CheckResult(
        command,
        ok=res.exit_code == 0 and not res.timed_out,
        exit_code=res.exit_code,
        output=output,
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=res.timed_out,
    )


def failure_text(result: CheckResult) -> str:
    """One failed check as the agent reads it."""
    head = f"$ {result.command}\n{result.summary}"
    return f"{head}\n{result.output}" if result.output else head


def detect_commands(root: Path) -> list[str]:
    """Commands this project probably has for checking it, from its files: suggestions for
    `verify.commands`, never run by themselves. In the order a developer runs them."""
    found: list[str] = []
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        for name in ("lint", "build", "test"):
            if isinstance(scripts, dict) and name in scripts:
                found.append("npm test" if name == "test" else f"npm run {name}")
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    if "[tool.ruff" in text:
        found.append("ruff check .")
    if "[tool.pytest" in text or (root / "tests").is_dir() and text:
        found.append("pytest -q")
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        found += ["dotnet build --nologo", "dotnet test --nologo"]
    if (root / "go.mod").is_file():
        found.append("go test ./...")
    if (root / "Cargo.toml").is_file():
        found.append("cargo test")
    return found
