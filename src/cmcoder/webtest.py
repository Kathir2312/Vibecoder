"""/webtest: the agent starts the project's web app and tests it in the Browser tool.

    /webtest                         find how to start the app, start it, test its main flows
    /webtest add a todo and tick it  the same, with this as what to test
    /webtest http://localhost:3000   the app is already running there: just test it
    /webtest localhost:3000 login    both

cmcoder only prepares the request (this module): the model does the work with the tools
it already has (Bash to start the server, Browser to test, Read/Grep to find the cause).
The prompt is a LangChain `PromptTemplate`. A few likely start commands found in the
project (package.json scripts, manage.py) go in as hints, so a small model doesn't have
to search for them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_core.prompts import PromptTemplate

_ADDRESS = re.compile(r"^(https?://\S+|(?:localhost|127\.0\.0\.1)(?::\d+)\S*)$", re.I)
_SCRIPTS = ("dev", "start", "serve", "preview")

PROMPT = PromptTemplate.from_template(
    """\
Test the web app of this project in a real browser, and report what works and what \
doesn't. Do not change any project file unless I ask afterwards.

{start}

Then, with the Browser tool: open {where}, take a snapshot, and go through {what}. \
Click and type by element number, and take a new snapshot whenever the page changes. \
Read the "New in the browser" lines after each step: JavaScript errors, failed requests \
and 404s matter even when the page looks fine. Take a screenshot (action screenshot) \
once the page has loaded, and again whenever something looks wrong: I see them in the chat.

When you are done, close the browser{cleanup}.

Report: what you tested; what worked; and for each failure the element or page, what you \
did, what happened, what you expected, and where in the code the cause probably is \
(read the code to check before you say so)."""
)


def package_manager(root: Path) -> str:
    for lockfile, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun")):
        if (root / lockfile).exists():
            return name
    return "npm"


def start_hints(root: Path) -> list[str]:
    """Commands that probably start the app, found in the project (never run by cmcoder)."""
    hints: list[str] = []
    try:
        scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError, AttributeError):
        scripts = {}
    if isinstance(scripts, dict):
        manager = package_manager(root)
        run = "npm run" if manager == "npm" else manager
        hints += [f"{run} {name}  ({scripts[name]})" for name in _SCRIPTS if name in scripts]
    if (root / "manage.py").exists():
        hints.append("python manage.py runserver")
    if (root / "index.html").exists() and not hints:
        hints.append("index.html in the project folder: open it as a file:// address")
    return hints


def split_request(arg: str) -> tuple[str | None, str]:
    """`/webtest` arguments: (an address the app already runs at, what to test)."""
    first, _, rest = arg.strip().partition(" ")
    if first and _ADDRESS.match(first):
        return first, rest.strip()
    return None, arg.strip()


def webtest_prompt(root: Path, arg: str) -> str:
    address, goal = split_request(arg)
    if address:
        start = f"The app is already running at {address}; do not start it."
        where, cleanup = address, ""
    else:
        hints = start_hints(root)
        found = (
            "Likely start commands in this project: " + "; ".join(hints) + "."
            if hints
            else "Look in README, package.json, Makefile and the like for the start command."
        )
        start = (
            f"First start it. {found} Run it with Bash in the background, with its output sent "
            "to a file and its process id printed, e.g. "
            '`npm run dev > "${TMPDIR:-/tmp}/cmcoder-webtest.log" 2>&1 & echo $!`. Then wait '
            "until it answers (check the address with curl every few seconds, up to a minute; "
            "read the log if it doesn't) and find the address it listens on."
        )
        where, cleanup = "that address", " and stop the server you started (kill its process id)"
    what = (
        f"what I asked you to test: {goal}"
        if goal
        else "the main things a user can do here: each page you can reach, each form and button"
    )
    return PROMPT.format(start=start, where=where, what=what, cleanup=cleanup)
