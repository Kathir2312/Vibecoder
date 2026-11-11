"""/playwright all (the PlaywrightTests graph) and /playwright agents (Playwright's own agents)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cmcoder.agents import playwright_tests as flow
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.agents.subagents import NOT_FOR_SUBAGENTS, SubagentResult
from cmcoder.config.settings import Settings
from cmcoder.playwright_tests import probe as probe_mod
from cmcoder.playwright_tests.agents_import import convert, copy_agents
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import run_command

from .conftest import API_KEY
from .helpers import make_session, of_type
from .test_playwright_cmd import DIR, add_map, add_plan, add_tests, run
from .test_playwright_cmd import project as set_up

# --- /playwright agents -------------------------------------------------------------------------

PLANNER = """\
---
name: playwright-test-planner
description: Use this agent when you need to create comprehensive test plan
tools: Glob, Grep, Read, LS, mcp__playwright-test__browser_click
model: sonnet
color: green
---

You are an expert web test planner. Say "model: sonnet" in prose and keep it.
"""


def test_a_playwright_agent_file_is_converted() -> None:
    out = convert(PLANNER)
    assert "model: inherit" in out and "model: sonnet\n" not in out.split("---")[1]
    assert "color:" not in out
    assert "tools: Glob, Grep, Read, LS, mcp__playwright-test__browser_click" in out
    assert 'Say "model: sonnet" in prose and keep it.' in out  # only the front matter's line


def test_agents_are_copied_and_never_overwritten(tmp_path: Path) -> None:
    lines = copy_agents(tmp_path)
    assert "No Playwright agents found" in lines[0] and "init-agents --loop=claude" in lines[2]
    source = tmp_path / ".claude" / "agents"
    source.mkdir(parents=True)
    (source / "playwright-test-planner.md").write_text(PLANNER)
    (source / "playwright-test-healer.md").write_text(PLANNER.replace("planner", "healer"))
    (source / "other.md").write_text("not Playwright's")
    lines = copy_agents(tmp_path)
    target = tmp_path / ".cmcoder" / "agents"
    assert sorted(f.name for f in target.iterdir()) == [
        "playwright-test-healer.md",
        "playwright-test-planner.md",
    ]
    assert "model: inherit" in (target / "playwright-test-planner.md").read_text()
    assert "2 agent file(s) copied" in "\n".join(lines) and "cmcoder trust" in "\n".join(lines)
    (target / "playwright-test-planner.md").write_text("mine")
    again = "\n".join(copy_agents(tmp_path))
    assert (
        "kept    .cmcoder/agents/playwright-test-planner.md" in again and "0 agent file(s)" in again
    )
    assert (target / "playwright-test-planner.md").read_text() == "mine"


def test_the_agents_step_works_before_setup(tmp_path: Path) -> None:
    out = run(tmp_path, "agents")
    assert out.prompt is None and "No Playwright agents found" in out.lines[0]


# --- /playwright all: the request ---------------------------------------------------------------


def test_all_asks_for_the_tool_once_with_every_rule_it_needs(tmp_path: Path) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    everything = run(tmp_path, "all", trusted=True)
    assert everything.prompt and "PlaywrightTests tool once" in everything.prompt
    assert "with area" not in everything.prompt
    for rule in (
        "PlaywrightTests",
        "Browser(localhost)",
        f"Write({DIR}/specs/site-map.md)",
        f"Write({DIR}/specs/**)",
        f"Write({DIR}/tests/**)",
        f"Edit({DIR}/tests/**)",
        f"Bash(npm --prefix {DIR} run list:*)",
        f"Bash(npm --prefix {DIR} test:*)",
    ):
        assert rule in everything.allow, rule
    assert len(everything.allow) == len(set(everything.allow))
    one = run(tmp_path, "all reports-exports")
    assert one.prompt and 'with area "Reports & Exports"' in one.prompt
    assert not any(r.startswith("Bash") for r in one.allow)  # not trusted
    unknown = run(tmp_path, "all Billing")
    assert unknown.prompt is None and 'No area "Billing"' in unknown.lines[0]
    assert run(tmp_path, "all").prompt  # with no site map yet: the tool makes one


def test_all_needs_the_set_up_first(tmp_path: Path) -> None:
    out = run(tmp_path, "all")
    assert out.prompt is None and "Not yet" in out.lines[0]


# --- the graph, with the model's work replaced by a script --------------------------------------


class Script:
    """Stands in for the roles: records every stage and does what each would leave on disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stages: list[str] = []
        self.runs: list[flow.RunReport] = []  # what the next runs report, in order
        self.heals: list[flow.HealReport] = []
        self.skip_plan: set[str] = set()
        self.skip_tests: set[str] = set()
        self.map_text: str | None = None

    async def __call__(
        self, ctx: Any, role: str, step: str, description: str, prompt: str, schema: Any = None
    ) -> SubagentResult:
        self.stages.append(step)
        folder = self.root / DIR
        slug = (
            step.split("-", 1)[1].rsplit("-", 1)[0]
            if step.startswith(("run-", "heal-"))
            else step.split("-", 1)[-1]
        )
        if step == "map":
            if self.map_text is not None:
                (folder / "specs" / "site-map.md").write_text(self.map_text)
            return SubagentResult("", flow.StageReport(summary="mapped"))
        if step.startswith("plan-"):
            if slug not in self.skip_plan:
                add_plan(self.root, slug)
            return SubagentResult("", flow.StageReport(summary="planned"))
        if step.startswith("generate-"):
            if slug not in self.skip_tests:
                add_tests(self.root, slug)
            return SubagentResult("", flow.StageReport(summary="written"))
        if step.startswith("run-"):
            report = (
                self.runs.pop(0) if self.runs else flow.RunReport(passed=3, failed=0, summary="ok")
            )
            return SubagentResult("", report)
        report = self.heals.pop(0) if self.heals else flow.HealReport(summary="fixed")
        return SubagentResult("", report)


def runtime(root: Path, **playwright: Any) -> Any:
    return SimpleNamespace(
        root=root,
        settings=Settings.model_validate({"playwright": playwright}),
        langchain_tools=[],
        definitions=lambda: {},
    )


async def go(
    root: Path,
    script: Script,
    only: str = "",
    monkeypatch: pytest.MonkeyPatch | None = None,
    **cfg: Any,
) -> dict[str, Any]:
    assert monkeypatch is not None
    monkeypatch.setattr(flow, "run_stage", script)
    ctx = flow.FlowContext(runtime(root, **cfg), lambda e: None, "t", None)
    return await flow.FLOW_GRAPH.ainvoke({"only": only}, context=ctx)


def problem(test: str, kind: str, what: str = "x") -> flow.Problem:
    return flow.Problem(test=test, kind=kind, what=what)  # type: ignore[arg-type]


async def test_a_project_that_is_not_set_up_is_told_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script(tmp_path)
    final = await go(tmp_path, script, monkeypatch=monkeypatch)
    assert script.stages == []
    text = flow.flow_summary(final)
    assert "Nothing was run" in text and "/playwright setup <address>" in text


async def test_every_area_is_planned_written_and_run_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    final = await go(tmp_path, script, monkeypatch=monkeypatch)
    assert script.stages == [
        "plan-orders", "generate-orders", "run-orders-0",
        "plan-reports-exports", "generate-reports-exports", "run-reports-exports-0",
    ]  # fmt: skip
    assert final["done"] == ["Orders: 3 passed, 0 failed", "Reports & Exports: 3 passed, 0 failed"]
    text = flow.flow_summary(final)
    assert "No problems of the app were found." in text and "- Orders: 3 passed" in text


async def test_tests_that_are_wrong_are_healed_and_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    script.runs = [
        flow.RunReport(
            passed=1, failed=2, summary="", problems=[problem("a", "TEST"), problem("b", "TEST")]
        ),
        flow.RunReport(passed=3, failed=0, summary="all pass now"),
    ]
    final = await go(tmp_path, script, only="Orders", monkeypatch=monkeypatch)
    assert script.stages == [
        "plan-orders", "generate-orders", "run-orders-0", "heal-orders-1", "run-orders-1",
    ]  # fmt: skip
    assert final["done"] == ["Orders: 3 passed, 0 failed"]


async def test_healing_stops_after_its_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    still = flow.RunReport(passed=0, failed=1, summary="", problems=[problem("a", "TEST")])
    script.runs = [still] * 10
    final = await go(tmp_path, script, only="Orders", monkeypatch=monkeypatch, maxHealRounds=2)
    assert [s for s in script.stages if s.startswith("heal-")] == ["heal-orders-1", "heal-orders-2"]
    assert script.stages.count("run-orders-0") == 1 and script.stages[-1] == "run-orders-2"
    assert "1 test(s) still wrong after 2 rounds" in final["done"][0]


async def test_the_apps_own_problems_are_collected_and_never_healed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    broken = problem("saving shows an error page", "APP", "HTTP 500 after Save; expected 'Saved'")
    script.runs = [
        flow.RunReport(passed=2, failed=1, summary="", problems=[broken]),
        flow.RunReport(passed=2, failed=0, summary=""),
    ]
    final = await go(tmp_path, script, only="Orders", monkeypatch=monkeypatch)
    assert not any(s.startswith("heal-") for s in script.stages)  # nothing the tests did wrong
    assert [x.test for x in final["app"]] == ["saving shows an error page"]
    text = flow.flow_summary(final)
    assert "Problems of the app that the tests found" in text and "HTTP 500 after Save" in text
    assert "No problems of the app" not in text


async def test_problems_the_healer_marked_are_added_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    app = problem("broken link", "APP", "404")
    script.runs = [
        flow.RunReport(passed=0, failed=2, summary="", problems=[problem("a", "TEST"), app]),
        flow.RunReport(passed=2, failed=0, summary=""),
    ]
    script.heals = [flow.HealReport(summary="fixed a, marked the link", app_problems=[app])]
    final = await go(tmp_path, script, only="Orders", monkeypatch=monkeypatch)
    assert flow.flow_summary(final).count("- broken link: 404") == 1


async def test_running_it_again_carries_on_where_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    add_plan(tmp_path, "orders")
    add_tests(tmp_path, "orders")
    add_plan(tmp_path, "reports-exports")
    script = Script(tmp_path)
    await go(tmp_path, script, monkeypatch=monkeypatch)
    assert script.stages == [
        "run-orders-0",  # plan and tests exist: straight to the run
        "generate-reports-exports", "run-reports-exports-0",  # its plan exists
    ]  # fmt: skip


async def test_an_area_whose_plan_or_tests_did_not_come_out_is_left_and_the_rest_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    script.skip_plan = {"orders"}
    script.skip_tests = {"reports-exports"}
    final = await go(tmp_path, script, monkeypatch=monkeypatch)
    assert script.stages == ["plan-orders", "plan-reports-exports", "generate-reports-exports"]
    assert [x.split(":")[0] for x in final["done"]] == ["Orders", "Reports & Exports"]
    assert all("stopped" in x for x in final["done"])
    assert "Orders: no plan was written" in "; ".join(final["notes"])
    assert "Reports & Exports: no tests were written" in "; ".join(final["notes"])


async def test_without_a_site_map_it_maps_first_or_takes_the_whole_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(tmp_path)
    script = Script(tmp_path)
    script.map_text = "# Site map\n## Area: Home\n- Home | / | the front page\n"
    final = await go(tmp_path, script, monkeypatch=monkeypatch)
    assert script.stages[:2] == ["map", "plan-home"] and final["done"] == [
        "Home: 3 passed, 0 failed"
    ]
    other = tmp_path / "second"
    set_up(other)
    nothing = Script(other)  # the mapper writes nothing
    final = await go(other, nothing, monkeypatch=monkeypatch)
    assert nothing.stages[:3] == ["map", "plan-site", "generate-site"]
    assert "the whole app is one area" in "; ".join(final["notes"])
    assert final["done"] == ["the whole app: 3 passed, 0 failed"]


async def test_an_unknown_area_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_up(tmp_path)
    add_map(tmp_path)
    script = Script(tmp_path)
    final = await go(tmp_path, script, only="Billing", monkeypatch=monkeypatch)
    assert script.stages == []
    assert 'No area "Billing": the areas are Orders, Reports & Exports' in flow.flow_summary(final)


# --- the tool ------------------------------------------------------------------------------------


def tool_names(agent: Any) -> set[str]:
    return {t.name for t in agent.graph.nodes["tools"].bound.tools_by_name.values()}


def test_the_tool_is_offered_unless_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", "k")
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": "http://x"}}, "model": "mock:m"}
    )
    on = build_agent(settings, AgentOptions(cwd=tmp_path))
    assert "PlaywrightTests" in tool_names(on)
    off = build_agent(
        Settings.model_validate(
            {
                "providers": {"mock": {"baseUrl": "http://x"}},
                "model": "mock:m",
                "playwright": {"enabled": False},
            }
        ),
        AgentOptions(cwd=tmp_path),
    )
    assert "PlaywrightTests" not in tool_names(off)
    assert "PlaywrightTests" in NOT_FOR_SUBAGENTS  # a role can't start the whole job again


async def test_the_model_calls_the_tool_and_gets_the_report(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(project)
    add_map(project)
    script = Script(project)
    monkeypatch.setattr(flow, "run_stage", script)
    monkeypatch.setattr(probe_mod, "node_version", lambda: "v22.4.0")
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    server = mock_server(
        [
            {"tool_calls": [{"name": "PlaywrightTests", "arguments": {"area": "Orders"}}]},
            {"content": "Orders: 3 passed."},
        ]
    )
    session = make_session(server.base_url, project)
    try:
        stage = await run_command(session, "/playwright all Orders")
        assert stage.send and "PlaywrightTests tool once" in stage.send
        assert not any(r.startswith("Bash") for r in stage.allow)  # the project isn't trusted
        events = [e async for e in session.run(stage.send, allow=stage.allow)]
    finally:
        await session.agent.close()
    assert script.stages[:3] == ["plan-orders", "generate-orders", "run-orders-0"]
    sent = str(server.requests[1]["messages"])
    assert "Playwright tests, area by area" in sent and "Orders: 3 passed, 0 failed" in sent
    assert of_type(events, ev.Result)[0].subtype == "success"


def test_roles_have_their_own_tools_and_can_be_overridden(tmp_path: Path) -> None:
    for name in ("playwright-mapper", "playwright-planner", "playwright-generator",
                 "playwright-runner", "playwright-healer"):  # fmt: skip
        assert flow.ROLES[name].tools and "Browser" in flow.ROLES[name].tools
    assert "Write" not in flow.ROLES["playwright-runner"].tools  # it changes no file
    assert "Bash" not in flow.ROLES["playwright-planner"].tools  # it only looks and writes the plan
    ide = SimpleNamespace(
        langchain_tools=[
            SimpleNamespace(name="ide_open_browser_page"),
            SimpleNamespace(name="Read"),
        ],
        definitions=lambda: {},
    )
    with_ide = flow.role_definition(ide, "playwright-planner")  # type: ignore[arg-type]
    assert with_ide.tools and "ide_open_browser_page" in with_ide.tools
    assert "Read" in with_ide.tools and with_ide.tools.count("Read") == 1
    mine = SimpleNamespace(name="mine")
    custom = SimpleNamespace(definitions=lambda: {"playwright-healer": mine}, langchain_tools=[])
    assert flow.role_definition(custom, "playwright-healer") is mine  # type: ignore[arg-type]
