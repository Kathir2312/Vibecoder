"""What /playwright needs to know about this PC and project: Node, a browser, what exists already.

Plain code: files and programs on the machine. Nothing is started and nothing is changed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..tools.browser import find_browser
from ..webtest import start_hints

ADDRESS = re.compile(r"^(https?://\S+|(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?\S*)$", re.I)
AGENT_GLOB = "playwright-test-*.md"  # what `npx playwright init-agents` writes


def normalize_address(raw: str) -> str | None:
    """`localhost:5080` -> `http://localhost:5080`; None when it isn't a web address."""
    raw = raw.strip().rstrip("/")
    if not raw or not ADDRESS.match(raw):
        return None
    return raw if "://" in raw else "http://" + raw


def split_setup(arg: str) -> tuple[str | None, str]:
    """`/playwright setup` arguments: (an address, text that is not one)."""
    first, _, rest = arg.strip().partition(" ")
    address = normalize_address(first) if first else None
    return (address, rest.strip()) if address else (None, arg.strip())


def node_version() -> str | None:
    """`v22.4.0`, or None when Node.js isn't installed (Playwright's test runner needs it)."""
    node = shutil.which("node")
    if node is None or shutil.which("npm") is None:
        return None
    try:
        out = subprocess.run(
            [node, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def channel_for(label: str, path: Path | None) -> tuple[str | None, str | None]:
    """How Playwright should start the browser found: (channel, executable path). A channel is
    the same on every PC ("chrome", "msedge"); a path is only for a browser that isn't either."""
    name = label.lower()
    if "edge" in name:
        return "msedge", None
    if "chrome" in name and "chromium" not in name:
        return "chrome", None
    return None, str(path) if path else None


@dataclass
class Probe:
    root: Path
    dir: str  # the Playwright project folder, relative to the root
    node: str | None = None
    browser_label: str = ""
    browser_path: Path | None = None
    channel: str | None = None
    executable: str | None = None
    has_package: bool = False
    has_config: bool = False
    has_seed: bool = False
    installed: bool = False  # @playwright/test is in the folder's node_modules
    start_hints: list[str] = field(default_factory=list)
    manual_agents: list[str] = field(default_factory=list)  # Playwright's own agent files

    @property
    def folder(self) -> Path:
        return self.root / self.dir

    @property
    def ready(self) -> bool:
        return self.has_package and self.has_config and self.installed


def manual_agent_files(root: Path) -> list[str]:
    """Playwright's generated agents already in the project (in `.claude/agents` or `.cmcoder/agents`)."""
    found: set[str] = set()
    for where in (".cmcoder/agents", ".claude/agents"):
        found.update(p.name for p in (root / where).glob(AGENT_GLOB))
    return sorted(found)


def probe(root: Path, directory: str, browser_path: str | None = None) -> Probe:
    root = root.resolve()
    path, label = find_browser(browser_path)
    channel, executable = channel_for(label, path) if path else (None, None)
    folder = root / directory
    return Probe(
        root=root,
        dir=directory,
        node=node_version(),
        browser_label=label if path else "",
        browser_path=path,
        channel=channel,
        executable=executable,
        has_package=(folder / "package.json").is_file(),
        has_config=any((folder / f"playwright.config.{x}").is_file() for x in ("ts", "js", "mjs")),
        has_seed=(folder / "tests" / "seed.spec.ts").is_file(),
        installed=(folder / "node_modules" / "@playwright" / "test").is_dir(),
        start_hints=start_hints(root),
        manual_agents=manual_agent_files(root),
    )
