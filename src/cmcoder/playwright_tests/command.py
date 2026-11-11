"""`/playwright ...`: from the command line to what is shown or sent to the model.

Returns a `Stage`: lines to show (a status, or why a step can't start yet), or the request for
the model with the permission rules for that request. runtime/commands.py turns it into a
`CommandResult`; the stdio server and the REPL do what they do for `/webtest`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import prompts, status
from .probe import Probe, probe, split_setup

if TYPE_CHECKING:
    from ..config.settings import Settings

STEPS = "setup | map | plan | generate | run | heal"
BASE_URL = re.compile(r"baseURL:\s*\"([^\"]+)\"")
WEB_SERVER = re.compile(r"^\s*webServer\s*:", re.M)

# Builds a step's Stage: (the step, its request, the address to allow the browser to open).
Go = Callable[..., "Stage"]


@dataclass
class Stage:
    lines: list[str] = field(default_factory=list)  # shown to the user, no model turn
    prompt: str | None = None  # sent to the model
    allow: list[str] = field(default_factory=list)  # permission rules for that request


def config_text(p: Probe) -> str:
    for ext in ("ts", "js", "mjs"):
        f = p.folder / f"playwright.config.{ext}"
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
    return ""


def app_url(p: Probe, settings: Settings) -> str | None:
    """The address of the app under test: the one in the config written by `setup`, else the setting."""
    found = BASE_URL.search(config_text(p))
    return found.group(1) if found else settings.playwright.app_url


def command(
    arg: str,
    *,
    root: Path,
    settings: Settings,
    ide_tools: list[str] | None = None,
    trusted: bool = False,
) -> Stage:
    cfg = settings.playwright
    if not cfg.enabled:
        return Stage(["The /playwright commands are off here (playwright.enabled in settings)."])
    step, _, rest = arg.strip().partition(" ")
    step, rest = step.lower(), rest.strip()
    p = probe(root, cfg.dir, settings.browser.executable_path)
    s = status.state(p)
    url = app_url(p, settings)

    def go(stage: str, text: str, address: str | None = url) -> Stage:
        return Stage(prompt=text, allow=prompts.allow_rules(stage, p, address, trusted=trusted))

    if step in ("", "status"):
        return Stage(status.lines(s, url))
    if step == "setup":
        return _setup(p, settings, rest, trusted)
    if step not in ("map", "plan", "generate", "run", "heal"):
        return Stage(
            [f'Unknown step "{step}". Steps: {STEPS}. Plain /playwright shows where you are.']
        )
    if not p.ready:
        why = "Node.js isn't installed" if p.node is None else "it isn't set up yet"
        return Stage([f"Not yet: {why}.", "Next: " + status.next_step(s, url)])
    if url is None:
        return Stage(
            [
                "Tell me the address the app answers at: /playwright setup <address>, e.g. "
                "/playwright setup http://localhost:5080 (it keeps the rest of the set-up)."
            ]
        )
    starts_app = bool(WEB_SERVER.search(config_text(p)))

    if step == "map":
        return go("map", prompts.map_prompt(p, url, ide_tools))
    if step == "plan":
        return _plan(p, s, url, rest, go, ide_tools)
    if step == "generate":
        return _generate(p, s, rest, go, ide_tools)
    area = _area_for_run(p, s, rest)
    if isinstance(area, Stage):
        return area
    if step == "run":
        return go("run", prompts.run_prompt(p, url, area, starts_app, ide_tools))
    return go("heal", prompts.heal_prompt(p, area, cfg.max_heal_rounds, ide_tools))


def _setup(p: Probe, settings: Settings, rest: str, trusted: bool) -> Stage:
    if p.node is None:
        return Stage(
            [
                "Node.js isn't installed, and Playwright's test runner needs it (this PC only: "
                "the people who use the app don't).",
                "Install Node.js (LTS) from your company's software portal or nodejs.org, open a "
                "new terminal, and run /playwright setup again.",
            ]
        )
    address, extra = split_setup(rest)
    start: str | None = None
    if extra:
        if not extra.startswith("--") or not extra[2:].strip():
            return Stage(
                [
                    "To have Playwright start the app for the tests, give the command after "
                    "the address: /playwright setup http://localhost:5080 -- npm run dev"
                ]
            )
        start = extra[2:].strip()
    url = address or app_url(p, settings)
    if p.ready and not rest:
        return Stage(
            [
                f"Already set up in {p.dir}/ (config, @playwright/test installed).",
                "Next: /playwright map",
            ]
        )
    if url is None:
        return Stage(
            [
                "Tell me the address the app answers at, e.g. /playwright setup http://localhost:5080",
                "(If Playwright should also start the app, add its command: "
                "/playwright setup http://localhost:5080 -- npm run dev)",
            ]
        )
    if p.has_config and rest:
        return Stage(
            [
                f"{p.dir}/playwright.config.ts exists: cmcoder doesn't overwrite it.",
                "Edit its baseURL (and webServer) yourself, or delete it and run setup again.",
            ]
        )
    stage = "setup"
    return Stage(
        prompt=prompts.setup_prompt(p, url, start),
        allow=prompts.allow_rules(stage, p, url, trusted=trusted),
    )


def _plan(
    p: Probe,
    s: status.State,
    url: str,
    rest: str,
    go: Go,
    ide_tools: list[str] | None,
) -> Stage:
    if rest:
        area = s.find(rest)
        if area is None:
            return Stage(_unknown_area(s, rest))
        return go("plan", prompts.plan_prompt(p, url, area.name, area.pages, ide_tools))
    if not s.has_map:
        return go("plan", prompts.plan_prompt(p, url, None, [], ide_tools))
    area = s.next_to_plan()
    if area is None:
        return Stage(
            [
                "Every area of the site map has a plan. Plan one again with /playwright plan <area>:",
                "  " + ", ".join(a.name for a in s.areas),
                "Next: /playwright generate",
            ]
        )
    return go("plan", prompts.plan_prompt(p, url, area.name, area.pages, ide_tools))


def _generate(p: Probe, s: status.State, rest: str, go: Go, ide_tools: list[str] | None) -> Stage:
    if not s.plans:
        return Stage(["There is no test plan yet.", "Next: /playwright plan"])
    if rest:
        area = s.find(rest)
        if area is None and prompts.slug(rest) not in s.plans:
            return Stage(_unknown_area(s, rest))
        name = area.name if area else None
        if area is not None and not area.planned:
            return Stage([f'"{area.name}" has no plan yet.', f"Next: /playwright plan {area.name}"])
        return go("generate", prompts.generate_prompt(p, name, ide_tools))
    area = s.next_to_generate()
    if area is not None:
        return go("generate", prompts.generate_prompt(p, area.name, ide_tools))
    if not s.areas and "site" in s.plans and not s.tests:
        return go("generate", prompts.generate_prompt(p, None, ide_tools))
    return Stage(
        [
            "Every plan already has tests. Write them again with /playwright generate <area>.",
            "Next: /playwright run",
        ]
    )


def _area_for_run(p: Probe, s: status.State, rest: str) -> str | Stage | None:
    """The folder of tests a run or heal covers: the area named, or all (None)."""
    if not s.tests:
        return Stage(["There are no tests yet.", "Next: /playwright generate"])
    if not rest:
        return None
    key = prompts.slug(rest)
    if (p.folder / "tests" / key).is_dir():
        return key
    return Stage(_unknown_area(s, rest))


def _unknown_area(s: status.State, text: str) -> list[str]:
    names = [a.name for a in s.areas] or ["(no site map yet: /playwright map)"]
    return [f'No area "{text}". The areas: ' + ", ".join(names)]
