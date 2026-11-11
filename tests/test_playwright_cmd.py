"""/playwright: set up, plan, write, run and heal Playwright tests for a web app (any framework)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcoder.config.settings import Settings
from cmcoder.playwright_tests import command as cmd
from cmcoder.playwright_tests import probe as probe_mod
from cmcoder.playwright_tests import prompts, status, templates
from cmcoder.playwright_tests.probe import (
    channel_for,
    manual_agent_files,
    normalize_address,
    probe,
    split_setup,
)

DIR = "playwright-tests"
URL = "http://localhost:5080"


def settings(**playwright: Any) -> Settings:
    return Settings.model_validate({"playwright": playwright})


@pytest.fixture(autouse=True)
def _node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node.js is on every PC these tests describe, whatever this one has."""
    monkeypatch.setattr(probe_mod, "node_version", lambda: "v22.4.0")


def project(root: Path, *, installed: bool = True, config: bool = True) -> Path:
    """A project with a set-up Playwright folder."""
    folder = root / DIR
    (folder / "specs").mkdir(parents=True)
    (folder / "tests").mkdir()
    (folder / "package.json").write_text("{}")
    if config:
        (folder / "playwright.config.ts").write_text(
            templates.config_ts(URL, channel="msedge", executable=None, start=None)
        )
    if installed:
        (folder / "node_modules" / "@playwright" / "test").mkdir(parents=True)
    return root


SITE_MAP = """\
# Site map of http://localhost:5080
## Area: Orders
- Orders | /orders | list and filter
- New order | /orders/new | a form
## Area: Reports & Exports
- Reports | /reports | pick a report [needs login]
"""


def add_map(root: Path) -> None:
    (root / DIR / "specs" / "site-map.md").write_text(SITE_MAP)


def add_plan(root: Path, name: str) -> None:
    (root / DIR / "specs" / f"{name}.md").write_text("# plan\n")


def add_tests(root: Path, area: str, names: tuple[str, ...] = ("a",), body: str = "") -> None:
    folder = root / DIR / "tests" / area
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / f"{n}.spec.ts").write_text(f"test('{n}', async () => {{}});\n{body}")


def run(root: Path, arg: str, **kw: Any) -> cmd.Stage:
    return cmd.command(arg, root=root, settings=kw.pop("settings", settings()), **kw)


# --- addresses, browser, what exists ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:5080", "http://localhost:5080"),
        ("localhost:5080", "http://localhost:5080"),
        ("https://app.corp.example/", "https://app.corp.example"),
        ("127.0.0.1:3000/app", "http://127.0.0.1:3000/app"),
        ("npm run dev", None),
        ("", None),
        ("javascript:alert(1)", None),
        ("ftp://host", None),
    ],
)
def test_normalize_address(raw: str, expected: str | None) -> None:
    assert normalize_address(raw) == expected


def test_split_setup() -> None:
    assert split_setup("") == (None, "")
    assert split_setup("http://localhost:5080") == (URL, "")
    assert split_setup("localhost:5080 -- npm run dev") == (URL, "-- npm run dev")
    assert split_setup("-- npm run dev") == (None, "-- npm run dev")


def test_the_browser_is_started_by_channel_when_it_is_chrome_or_edge(tmp_path: Path) -> None:
    exe = tmp_path / "x"
    assert channel_for("Edge", exe) == ("msedge", None)
    assert channel_for("microsoft-edge", exe) == ("msedge", None)
    assert channel_for("Chrome", exe) == ("chrome", None)
    assert channel_for("google-chrome-stable", exe) == ("chrome", None)
    assert channel_for("Playwright Chromium", exe) == (None, str(exe))  # not "chrome"
    assert channel_for("chromium", exe) == (None, str(exe))


def test_probe_sees_what_is_there(tmp_path: Path) -> None:
    empty = probe(tmp_path, DIR)
    assert not empty.has_package and not empty.has_config and not empty.ready
    assert empty.node == "v22.4.0"
    project(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    found = probe(tmp_path, DIR)
    assert found.has_package and found.has_config and found.installed and found.ready
    assert found.start_hints == ["npm run dev  (vite)"]
    assert not probe(tmp_path, "other").ready  # another folder name: another project


def test_playwrights_own_agents_are_noticed(tmp_path: Path) -> None:
    assert manual_agent_files(tmp_path) == []
    for where in (".claude/agents", ".cmcoder/agents"):
        (tmp_path / where).mkdir(parents=True)
        (tmp_path / where / "playwright-test-planner.md").write_text("x")
    (tmp_path / ".claude/agents" / "playwright-test-healer.md").write_text("x")
    (tmp_path / ".claude/agents" / "other.md").write_text("x")
    assert manual_agent_files(tmp_path) == [
        "playwright-test-healer.md",
        "playwright-test-planner.md",
    ]


# --- the files setup has written ----------------------------------------------------------------


def test_package_json_runs_the_tests_by_script() -> None:
    package = json.loads(templates.package_json(DIR))
    assert package["scripts"]["test"] == "playwright test"
    assert package["scripts"]["list"] == "playwright test --list"
    assert package["devDependencies"] == {"@playwright/test": templates.PLAYWRIGHT_VERSION}


def test_config_uses_the_browser_found_and_starts_the_app_only_when_told() -> None:
    plain = templates.config_ts(URL, channel="msedge", executable=None, start=None)
    assert 'baseURL: "http://localhost:5080"' in plain and 'channel: "msedge"' in plain
    assert "ignoreHTTPSErrors: true" in plain and "webServer: {" not in plain
    assert "must already be running" in plain

    exe = templates.config_ts(URL, channel=None, executable="/opt/chromium/chrome", start=None)
    assert 'launchOptions: { executablePath: "/opt/chromium/chrome" }' in exe

    none = templates.config_ts(URL, channel=None, executable=None, start=None)
    assert "playwright install chromium" in none

    starting = templates.config_ts(
        URL, channel="chrome", executable=None, start='node "my app.js" --port 5080'
    )
    assert "webServer: {" in starting and "reuseExistingServer: true" in starting
    # Quotes in the command are escaped for TypeScript, not left to break the file.
    assert 'command: "node \\"my app.js\\" --port 5080",' in starting
    assert 'url: "http://localhost:5080",' in starting
    assert (
        "cwd: '..'," in starting
    )  # the command runs in the project folder, not in the tests folder


def test_files_are_named_inside_the_folder() -> None:
    made = templates.files(DIR, URL, channel="chrome", executable=None, start=None)
    assert set(made) == {
        f"{DIR}/package.json",
        f"{DIR}/playwright.config.ts",
        f"{DIR}/tests/seed.spec.ts",
        f"{DIR}/specs/README.md",
        f"{DIR}/.gitignore",
    }
    assert "node_modules/" in made[f"{DIR}/.gitignore"]


# --- the requests -------------------------------------------------------------------------------


def test_slug_and_scope_words() -> None:
    assert prompts.slug("Orders & Invoices") == "orders-invoices"
    assert prompts.slug("  ") == "site"
    assert prompts.target_words("orders") == ("for the area orders", "tests/orders ")
    assert prompts.target_words(None) == ("(all of them)", "")
    assert "Browser tool" in prompts.browser_words(None)
    assert "ide_open_browser_page" in prompts.browser_words(["ide_open_browser_page"])


def test_setup_prompt_gives_each_file_and_skips_what_exists(tmp_path: Path) -> None:
    p = probe(tmp_path, DIR)
    text = prompts.setup_prompt(p, URL, None)
    for name in ("package.json", "playwright.config.ts", "tests/seed.spec.ts", ".gitignore"):
        assert f"File {DIR}/{name}:" in text
    assert f"npm --prefix {DIR} install" in text and f"npm --prefix {DIR} run list" in text
    assert "has to be running at http://localhost:5080" in text and "-- <the command" in text
    started = prompts.setup_prompt(p, URL, "npm run dev")
    assert "Playwright will start the app with `npm run dev`" in started
    project(tmp_path)
    again = prompts.setup_prompt(probe(tmp_path, DIR), URL, None)
    assert "must NOT be overwritten" in again and f"File {DIR}/playwright.config.ts:" not in again
    assert f"File {DIR}/specs/README.md:" in again  # what is missing is still written


def test_every_step_names_its_folder_and_keeps_to_the_tests(tmp_path: Path) -> None:
    p = probe(tmp_path, DIR)
    plan = prompts.plan_prompt(p, URL, "Orders", ["Orders | /orders | list"], None)
    assert f"{DIR}/specs/orders.md" in plan and "- Orders | /orders | list" in plan
    assert "Change no file except" in plan
    whole = prompts.plan_prompt(p, URL, None, [], None)
    assert f"{DIR}/specs/site.md" in whole and "no site map yet" in whole
    gen = prompts.generate_prompt(p, "Orders", None)
    assert f"{DIR}/tests/orders/" in gen and "getByRole" in gen and "waitForTimeout" in gen
    assert "import { test, expect } from '@playwright/test';" in gen  # braces survive formatting
    run_text = prompts.run_prompt(p, URL, "orders", False, None)
    assert f"npm --prefix {DIR} test -- tests/orders --reporter=line" in run_text
    assert "Do not change any file" in run_text and "http://localhost:5080" in run_text
    assert "by itself" in prompts.run_prompt(p, URL, None, True, None)
    heal = prompts.heal_prompt(p, None, 3, None)
    assert "at most 3 times" in heal and "test.fixme(" in heal and "Never change the app" in heal
    assert f"npm --prefix {DIR} test -- --reporter=line" in heal


def test_permission_rules_are_as_narrow_as_each_step(tmp_path: Path) -> None:
    p = probe(tmp_path, DIR)
    trusted = {
        s: prompts.allow_rules(s, p, URL, trusted=True)
        for s in ("setup", "map", "plan", "generate", "run", "heal")
    }
    assert trusted["setup"] == [f"Write({DIR}/**)", f"Bash(npm --prefix {DIR} run list:*)"]
    assert trusted["map"] == ["Browser(localhost)", f"Write({DIR}/specs/site-map.md)"]
    assert trusted["plan"] == ["Browser(localhost)", f"Write({DIR}/specs/**)"]
    assert (
        f"Edit({DIR}/tests/**)" in trusted["generate"]
        and f"Write({DIR}/tests/**)" in trusted["heal"]
    )
    assert trusted["run"] == ["Browser(localhost)", f"Bash(npm --prefix {DIR} test:*)"]
    assert f"Bash(npm --prefix {DIR} test:*)" in trusted["heal"]
    # Never a rule for the app's own files, a whole tool, or a broad command.
    for rules in trusted.values():
        assert all(r.split("(")[0] in ("Write", "Edit", "Bash", "Browser") for r in rules)
        assert "Browser" not in rules and not any(r.startswith("Bash(npm test") for r in rules)
    # In a project that isn't trusted nothing that runs the folder's scripts is allowed unasked.
    for stage in ("setup", "generate", "run", "heal"):
        assert not any(
            r.startswith("Bash") for r in prompts.allow_rules(stage, p, URL, trusted=False)
        )
    assert "Browser(app.corp.example)" in prompts.allow_rules(
        "map", p, "https://app.corp.example:8443/x", trusted=True
    )
    assert prompts.allow_rules("map", p, None, trusted=True) == [f"Write({DIR}/specs/site-map.md)"]


# --- where things stand -------------------------------------------------------------------------


def test_the_site_map_gives_the_areas_and_what_is_done(tmp_path: Path) -> None:
    project(tmp_path)
    s = status.state(probe(tmp_path, DIR))
    assert not s.has_map and s.areas == [] and s.tests == 0
    add_map(tmp_path)
    s = status.state(probe(tmp_path, DIR))
    assert [a.name for a in s.areas] == ["Orders", "Reports & Exports"]
    assert s.areas[0].pages == [
        "Orders | /orders | list and filter",
        "New order | /orders/new | a form",
    ]
    assert s.find("reports-exports") is s.areas[1] and s.find("orders") is s.areas[0]
    assert s.find("REPORTS & EXPORTS") is s.areas[1] and s.find("nope") is None
    assert s.next_to_plan() is s.areas[0] and s.next_to_generate() is None
    add_plan(tmp_path, "orders")
    add_tests(tmp_path, "orders", ("a", "b"), body="test.fixme('x', async () => {});\n")
    (tmp_path / DIR / "tests" / "seed.spec.ts").write_text("test('seed', async () => {});\n")
    s = status.state(probe(tmp_path, DIR))
    assert s.areas[0].planned and s.areas[0].tests == 2 and s.next_to_plan() is s.areas[1]
    assert s.tests == 2 and s.fixme == 2  # the seed isn't counted
    assert s.plans == ["orders"]  # the site map and the README aren't plans


def test_the_next_step_follows_the_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def next_for() -> str:
        return status.next_step(status.state(probe(tmp_path, DIR)), URL)

    monkeypatch.setattr(probe_mod, "node_version", lambda: None)
    assert "install Node.js" in next_for()
    monkeypatch.setattr(probe_mod, "node_version", lambda: "v22.4.0")
    assert next_for() == f"/playwright setup {URL}"
    project(tmp_path)
    assert next_for() == "/playwright map"
    add_map(tmp_path)
    assert next_for() == "/playwright plan   (next area: Orders)"
    add_plan(tmp_path, "orders")
    assert next_for() == "/playwright generate   (next area: Orders)"
    add_tests(tmp_path, "orders")
    assert next_for() == "/playwright plan   (next area: Reports & Exports)"
    add_plan(tmp_path, "reports-exports")
    add_tests(tmp_path, "reports-exports")
    assert next_for() == "/playwright run, then /playwright heal if tests fail"
    add_tests(tmp_path, "orders", ("c",), body="test.fixme('c', async () => {});\n")
    assert "fix the problems of the app" in next_for()


def test_the_status_says_what_it_knows(tmp_path: Path) -> None:
    out = "\n".join(run(tmp_path, "").lines)
    assert "Node.js     v22.4.0" in out and "Set up      not yet" in out
    assert "Next: /playwright setup <the app address" in out
    project(tmp_path)
    add_map(tmp_path)
    add_plan(tmp_path, "orders")
    add_tests(tmp_path, "orders", body="test.fixme('x', async () => {});\n")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "playwright-test-healer.md").write_text("x")
    out = "\n".join(run(tmp_path, "status").lines)
    assert f"Set up      yes ({DIR}/)" in out and f"App address {URL}" in out
    assert "2 areas, 3 pages; plans 1/2, tests written for 1/2" in out
    assert "1 marked as problems of the app (test.fixme)" in out
    assert "Playwright's own agents are in this project (playwright-test-healer.md)" in out
    assert "Next: /playwright plan   (next area: Reports & Exports)" in out


# --- the command --------------------------------------------------------------------------------


def test_off_in_settings_and_unknown_steps(tmp_path: Path) -> None:
    assert "off here" in run(tmp_path, "map", settings=settings(enabled=False)).lines[0]
    unknown = run(tmp_path, "fix")
    assert unknown.prompt is None and 'Unknown step "fix"' in unknown.lines[0]
    assert "setup | map | plan | generate | run | heal" in unknown.lines[0]


def test_setup_needs_node_and_an_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod, "node_version", lambda: None)
    out = run(tmp_path, f"setup {URL}")
    assert out.prompt is None and "Node.js isn't installed" in out.lines[0]
    monkeypatch.setattr(probe_mod, "node_version", lambda: "v22.4.0")
    asked = run(tmp_path, "setup")
    assert asked.prompt is None and "/playwright setup http://localhost:5080" in asked.lines[0]
    bad = run(tmp_path, f"setup {URL} npm run dev")
    assert bad.prompt is None and "-- npm run dev" in bad.lines[0]
    empty = run(tmp_path, f"setup {URL} --")
    assert empty.prompt is None


def test_setup_sends_the_request_with_its_rules(tmp_path: Path) -> None:
    out = run(tmp_path, f"setup {URL} -- npm run dev", trusted=True)
    assert out.prompt and "Playwright will start the app with `npm run dev`" in out.prompt
    assert f"Write({DIR}/**)" in out.allow and f"Bash(npm --prefix {DIR} run list:*)" in out.allow
    from_setting = run(tmp_path, "setup", settings=settings(appUrl=URL))
    assert from_setting.prompt and "http://localhost:5080" in from_setting.prompt


def test_setup_is_not_repeated_and_never_overwrites_a_config(tmp_path: Path) -> None:
    project(tmp_path)
    done = run(tmp_path, "setup")
    assert done.prompt is None and "Already set up" in done.lines[0]
    again = run(tmp_path, "setup http://localhost:9999")
    assert again.prompt is None and "doesn't overwrite" in again.lines[0]
    # Set up but not installed yet (the install failed): setup goes on and doesn't touch the config.
    other = tmp_path / "other"
    project(other, installed=False)
    resume = run(other, f"setup {URL}")
    assert resume.prompt is None and "doesn't overwrite" in resume.lines[0]


def test_steps_before_setup_say_what_to_do(tmp_path: Path) -> None:
    for step in ("map", "plan", "generate", "run", "heal"):
        out = run(tmp_path, step)
        assert out.prompt is None and "Not yet: it isn't set up yet" in out.lines[0], step
        assert out.lines[1].startswith("Next: /playwright setup")


def test_steps_need_the_address(tmp_path: Path) -> None:
    project(tmp_path, config=False)
    (tmp_path / DIR / "playwright.config.ts").write_text("export default {};\n")  # no baseURL
    out = run(tmp_path, "map")
    assert out.prompt is None and "Tell me the address" in out.lines[0]
    assert run(tmp_path, "map", settings=settings(appUrl=URL)).prompt  # the setting is enough


def test_plan_goes_through_the_areas_in_order(tmp_path: Path) -> None:
    project(tmp_path)
    whole = run(tmp_path, "plan")  # no site map yet: the whole app
    assert whole.prompt and f"{DIR}/specs/site.md" in whole.prompt
    add_map(tmp_path)
    first = run(tmp_path, "plan")
    assert first.prompt and f"{DIR}/specs/orders.md" in first.prompt
    assert "- New order | /orders/new | a form" in first.prompt
    add_plan(tmp_path, "orders")
    second = run(tmp_path, "plan")
    assert second.prompt and f"{DIR}/specs/reports-exports.md" in second.prompt
    named = run(tmp_path, "plan Orders")  # an area again, by name
    assert named.prompt and f"{DIR}/specs/orders.md" in named.prompt
    add_plan(tmp_path, "reports-exports")
    done = run(tmp_path, "plan")
    assert done.prompt is None and "Every area of the site map has a plan" in done.lines[0]
    unknown = run(tmp_path, "plan Billing")
    assert unknown.prompt is None and 'No area "Billing"' in unknown.lines[0]


def test_generate_needs_a_plan_and_takes_the_next_planned_area(tmp_path: Path) -> None:
    project(tmp_path)
    add_map(tmp_path)
    nothing = run(tmp_path, "generate")
    assert nothing.prompt is None and "no test plan yet" in nothing.lines[0]
    add_plan(tmp_path, "reports-exports")
    out = run(tmp_path, "generate")
    assert out.prompt and f"{DIR}/specs/reports-exports.md" in out.prompt
    unplanned = run(tmp_path, "generate Orders")
    assert unplanned.prompt is None and "has no plan yet" in unplanned.lines[0]
    add_tests(tmp_path, "reports-exports")
    again = run(tmp_path, "generate")
    assert again.prompt is None and "already has tests" in again.lines[0]
    assert run(tmp_path, "generate Reports & Exports").prompt  # asked for by name: written again


def test_a_plan_of_the_whole_app_can_be_generated(tmp_path: Path) -> None:
    project(tmp_path)
    add_plan(tmp_path, "site")
    out = run(tmp_path, "generate")
    assert (
        out.prompt and f"{DIR}/specs/site.md" in out.prompt and f"{DIR}/tests/site/" in out.prompt
    )


def test_run_and_heal_cover_everything_or_one_area(tmp_path: Path) -> None:
    project(tmp_path)
    none = run(tmp_path, "run")
    assert none.prompt is None and "no tests yet" in none.lines[0]
    add_map(tmp_path)
    add_plan(tmp_path, "orders")
    add_tests(tmp_path, "orders")
    all_tests = run(tmp_path, "run", trusted=True)
    assert all_tests.prompt and f"npm --prefix {DIR} test -- --reporter=line" in all_tests.prompt
    assert "open it with the Browser tool" in all_tests.prompt  # no webServer: the app must run
    one = run(tmp_path, "heal Orders", trusted=True)
    assert one.prompt and "tests/orders --reporter=line" in one.prompt
    assert "at most 3 times" in one.prompt and f"Bash(npm --prefix {DIR} test:*)" in one.allow
    assert "at most 5 times" in run(tmp_path, "heal", settings=settings(maxHealRounds=5)).prompt
    missing = run(tmp_path, "run billing")
    assert missing.prompt is None and 'No area "billing"' in missing.lines[0]
    unasked = run(tmp_path, "run", trusted=False)
    assert not any(r.startswith("Bash") for r in unasked.allow)


def test_run_does_not_ask_for_the_app_when_playwright_starts_it(tmp_path: Path) -> None:
    project(tmp_path, config=False)
    (tmp_path / DIR / "playwright.config.ts").write_text(
        templates.config_ts(URL, channel=None, executable=None, start="npm run dev")
    )
    add_tests(tmp_path, "orders")
    out = run(tmp_path, "run")
    assert out.prompt and "Playwright starts the app by itself" in out.prompt
