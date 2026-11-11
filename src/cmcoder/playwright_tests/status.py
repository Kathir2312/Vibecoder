"""Where the Playwright tests of this project stand, from the files in its folder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .probe import Probe
from .prompts import slug

AREA = re.compile(r"^##\s*Area:\s*(.+?)\s*$", re.M)
PAGE = re.compile(r"^-\s+(.+)$", re.M)
FIXME = re.compile(r"\btest\.fixme\(")
NOT_PLANS = {"site-map.md", "readme.md"}


@dataclass
class Area:
    name: str
    slug: str
    pages: list[str]
    planned: bool
    tests: int


@dataclass
class State:
    probe: Probe
    areas: list[Area]
    has_map: bool
    plans: list[str]  # slugs of the plans written
    tests: int
    fixme: int

    def find(self, text: str) -> Area | None:
        """An area by name or file-name form."""
        wanted = slug(text)
        return next(
            (a for a in self.areas if a.slug == wanted or a.name.lower() == text.lower()), None
        )

    def next_to_plan(self) -> Area | None:
        return next((a for a in self.areas if not a.planned), None)

    def next_to_generate(self) -> Area | None:
        return next((a for a in self.areas if a.planned and not a.tests), None)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_map(site_map: Path) -> list[tuple[str, list[str]]]:
    """(area name, its page lines) from site-map.md."""
    text = _read(site_map)
    parts = AREA.split(text)  # [before, name1, body1, name2, body2, ...]
    return [(parts[i], PAGE.findall(parts[i + 1])) for i in range(1, len(parts) - 1, 2)]


def state(p: Probe) -> State:
    specs, tests = p.folder / "specs", p.folder / "tests"
    plans = sorted(f.stem for f in specs.glob("*.md") if f.name.lower() not in NOT_PLANS)
    site_map = specs / "site-map.md"
    areas = []
    for name, pages in read_map(site_map):
        key = slug(name)
        areas.append(
            Area(name, key, pages, key in plans, len(list((tests / key).glob("*.spec.ts"))))
        )
    spec_files = [f for f in tests.rglob("*.spec.ts") if f.name != "seed.spec.ts"]
    return State(
        probe=p,
        areas=areas,
        has_map=site_map.is_file() and bool(areas),
        plans=plans,
        tests=len(spec_files),
        fixme=sum(len(FIXME.findall(_read(f))) for f in spec_files),
    )


def lines(s: State, url: str | None) -> list[str]:
    """What `/playwright` alone says: the state, and the one command to type next."""
    p = s.probe
    out = ["Playwright tests for this project's web app:"]
    out.append(
        f"  Node.js     {p.node}"
        if p.node
        else "  Node.js     missing (Playwright's runner needs it)"
    )
    out.append(
        f"  Browser     {p.browser_label}" if p.browser_label else "  Browser     none found"
    )
    out.append(f"  App address {url}" if url else "  App address not set")
    out.append(f"  Set up      {'yes (' + p.dir + '/)' if p.ready else 'not yet'}")
    if s.has_map:
        planned = sum(a.planned for a in s.areas)
        generated = sum(bool(a.tests) for a in s.areas)
        pages = sum(len(a.pages) for a in s.areas)
        out.append(
            f"  Site map    {len(s.areas)} areas, {pages} pages; plans {planned}/{len(s.areas)}, "
            f"tests written for {generated}/{len(s.areas)}"
        )
    elif p.ready:
        out.append("  Site map    not yet")
    if s.tests:
        found = f", {s.fixme} marked as problems of the app (test.fixme)" if s.fixme else ""
        out.append(f"  Tests       {s.tests}{found}")
    if p.manual_agents:
        out.append(
            "  Agents      Playwright's own agents are in this project ("
            + ", ".join(p.manual_agents)
            + ")"
        )
    out.append("")
    out.append("Next: " + next_step(s, url))
    out.append("All steps: /playwright setup | map | plan | generate | run | heal")
    if p.ready:
        out.append("Or all of it in one go: /playwright all   (then /playwright run to check)")
    return out


def next_step(s: State, url: str | None) -> str:
    p = s.probe
    if p.node is None:
        return "install Node.js (LTS), then /playwright setup"
    if not p.ready:
        return f"/playwright setup {url or '<the app address, e.g. http://localhost:5080>'}"
    if not s.has_map:
        return "/playwright map"
    # One area at a time: tests for a plan before the next plan, so a problem shows up early.
    if (area := s.next_to_generate()) is not None:
        return f"/playwright generate   (next area: {area.name})"
    if (area := s.next_to_plan()) is not None:
        return f"/playwright plan   (next area: {area.name})"
    if s.fixme:
        return "/playwright run, then fix the problems of the app that it lists"
    return "/playwright run, then /playwright heal if tests fail"
