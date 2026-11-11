"""`/playwright agents`: use Playwright's own planner, generator and healer in cmcoder.

`npx playwright init-agents --loop=claude` writes them into `.claude/agents/` (Claude Code's
format). cmcoder reads agents from `.cmcoder/agents/` and `model: sonnet` there would be sent to
the gateway as a model name, so this copies each file over with `model: inherit` (your own
model) and without `color:`. The user's own command writes the files, like `#` memory adds a
line to CMCODER.md; nothing is run and nothing existing is overwritten.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..sensitive import safe_project_file
from .probe import AGENT_GLOB

MODEL = re.compile(r"(?m)^model:.*$")
COLOR = re.compile(r"(?m)^color:.*\r?\n")
MAX_CHARS = 60_000


def convert(text: str) -> str:
    """Playwright's agent file as cmcoder wants it: your model, no colour."""
    return COLOR.sub("", MODEL.sub("model: inherit", text, count=1))


def copy_agents(root: Path) -> list[str]:
    """Copy `.claude/agents/playwright-test-*.md` into `.cmcoder/agents/`; the lines to show."""
    source = root / ".claude" / "agents"
    found = sorted(p for p in source.glob(AGENT_GLOB) if safe_project_file(p, root))
    if not found:
        return [
            "No Playwright agents found in .claude/agents.",
            "Make them first (in the folder with your Playwright project):",
            "  npx playwright init-agents --loop=claude",
            "then run /playwright agents again.",
            "(You don't need them: /playwright plan, generate and heal do the same.)",
        ]
    target = root / ".cmcoder" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    copied = 0
    for path in found:
        dest = target / path.name
        if dest.exists():
            lines.append(f"  kept    {dest.relative_to(root).as_posix()} (it exists already)")
            continue
        try:
            text = path.read_text(encoding="utf-8")[:MAX_CHARS]
        except (OSError, UnicodeDecodeError):
            lines.append(f"  skipped {path.name} (can't be read)")
            continue
        dest.write_text(convert(text), encoding="utf-8")
        lines.append(f"  copied  {dest.relative_to(root).as_posix()}")
        copied += 1
    lines.append("")
    lines.append(f"{copied} agent file(s) copied; model: inherit means your own model.")
    lines.append(
        "A project's agents are used only once it is trusted (`cmcoder trust`). Then pick one in "
        "the chat's Agent list. Their browser tools come from Playwright's MCP server: "
        "`cmcoder mcp approve playwright-test` (it is in .mcp.json)."
    )
    return lines
