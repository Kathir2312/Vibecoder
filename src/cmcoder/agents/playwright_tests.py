"""`/playwright all`: map, plan, write, run and heal Playwright tests, area by area, as a LangGraph graph.

The step commands (`/playwright plan`, `generate`, `run`, `heal`) each send one request to the model.
This is the same work as one tool, `PlaywrightTests`, so a long job doesn't depend on one
conversation staying on course or on one request's budget of model calls. Like the team
(agents/team.py) it is an explicit `StateGraph`: the order, the loop and the round limit are code,
so a mid-size model can't skip the run or heal forever.

    START ─► prepare ─► next_area ──(none left)──► END
                 ▲          │
                 │          ▼
                 │        plan ─► generate ─► run ──(TEST failures, rounds left)──► heal ─┐
                 │          │ no file   │ no file    │ (all pass / none to heal /        │
                 │          ▼           ▼            ▼   no rounds left)                 │
                 └──────── finish_area ◄──────────────┘◄─────────────────────────────────┘

* `prepare`: set up? (else it says so and ends); no site map yet: the mapper writes one; the areas
  to do are the site map's (or the one asked for).
* Each node runs one role as a subagent (`run_agent`): its own conversation and budget of model
  calls (`subagentMaxTurns`), shown as its own row in the IDE. The request it gets is the same
  text as the matching step command (playwright_tests/prompts.py).
* What already exists is not done again: an area with a plan skips `plan`, one with tests skips
  `generate`, so running it again carries on where it stopped (a limit, a stop, a crash).
* Only the app's own problems are never "healed": the healer marks them `test.fixme` and the
  final report lists them for the user.

LANGGRAPH / LANGCHAIN CONCEPTS (as in team.py)

* `StateGraph` with a typed state; `app` and `done` have reducers (`operator.add`): every area
  appends its problems and its result line instead of replacing the last.
* Conditional edges: `route_after_plan` / `route_after_generate` / `route_after_run` read state
  fields, never prose; the run's `RunReport` is structured output (`ToolStrategy`).
* `context_schema` + `Runtime`: the subagent runtime, the UI writer and the main graph's
  `RunContext` travel in `FlowContext`, not in the state.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain.agents.structured_output import ToolStrategy
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from ..playwright_tests import prompts, status
from ..playwright_tests.command import WEB_SERVER, app_url, config_text
from ..playwright_tests.probe import probe
from ..protocol import events as ev
from .subagents import AgentDefinition, SubagentResult, SubagentRuntime, Writer, _emit, run_agent

REPORT_CHARS = 1_500

MAPPER_PROMPT = """\
You are a careful web tester. You look at a web app in a browser and describe what is there. You \
change only the file you are asked to write."""

PLANNER_PROMPT = """\
You are a test planner for a web app. You use the app like a person to find what is worth testing, \
and you write test plans. You change only the plan file you are asked to write."""

GENERATOR_PROMPT = """\
You write Playwright tests in TypeScript that pass on the first run. You find elements the way a \
user does (by role, label and text), you never guess a label, and you change only the test files \
you are asked to write."""

RUNNER_PROMPT = """\
You run Playwright tests and say, for every failure, whether the test or the app is wrong. You \
change no file."""

HEALER_PROMPT = """\
You fix failing Playwright tests. You change only test files. A failure that is the app's own \
(an error page, a broken link, wrong data) is never hidden: you mark that test test.fixme with a \
comment and report it."""

# What each role may use (the IDE's browser tools are added when it has some).
_FILES = ["Read", "Glob", "Grep"]
ROLES = {
    "playwright-mapper": AgentDefinition(
        "playwright-mapper",
        "Browses the app and lists its pages by area.",
        MAPPER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Browser"],
    ),
    "playwright-planner": AgentDefinition(
        "playwright-planner",
        "Writes the test plan of an area of the app.",
        PLANNER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Browser"],
    ),
    "playwright-generator": AgentDefinition(
        "playwright-generator",
        "Writes Playwright tests from a plan.",
        GENERATOR_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Edit", "Bash", "Browser"],
    ),
    "playwright-runner": AgentDefinition(
        "playwright-runner",
        "Runs the tests and says who is wrong for each failure.",
        RUNNER_PROMPT,
        "built-in",
        tools=[*_FILES, "Bash", "Browser"],
    ),
    "playwright-healer": AgentDefinition(
        "playwright-healer",
        "Fixes the tests that are wrong; marks the app's own problems.",
        HEALER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Edit", "Bash", "Browser"],
    ),
}


# --- What the roles hand back (structured output) ---------------------------------------------


class Problem(BaseModel):
    test: str = Field(description="The failing test's name")
    kind: Literal["TEST", "APP"] = Field(description="TEST: the test is wrong. APP: the app is")
    what: str = Field(description="What happened, and what was expected")
    where: str = Field("", description="The page and steps (APP), or the file and line (TEST)")


class RunReport(BaseModel):
    """The result of running the tests."""

    passed: int = Field(description="Tests that passed")
    failed: int = Field(description="Tests that failed (not the skipped ones)")
    problems: list[Problem] = Field(default_factory=list, description="One per failing test")
    summary: str = Field(description="One or two sentences")


class HealReport(BaseModel):
    """What the healing did."""

    summary: str = Field(description="One or two sentences: what you fixed")
    app_problems: list[Problem] = Field(
        default_factory=list, description="Each test you marked test.fixme because the app is wrong"
    )


class StageReport(BaseModel):
    """What the step did."""

    summary: str = Field(description="One or two sentences")


# --- The graph ----------------------------------------------------------------------------------


class FlowState(TypedDict):
    only: str  # the one area asked for ("" = every area)
    max_rounds: NotRequired[int]  # heal rounds per area (`playwright.maxHealRounds`)
    ready: NotRequired[bool]  # set up, and an address known
    areas: NotRequired[list[str]]  # names to work through ("" = the whole app, no site map)
    index: NotRequired[int]
    current: NotRequired[str]
    ok: NotRequired[bool]  # the step just done left what the next one needs
    round: NotRequired[int]  # heal rounds in this area
    report: NotRequired[RunReport | None]
    app: NotRequired[Annotated[list[Problem], operator.add]]  # the app's problems, all areas
    done: NotRequired[Annotated[list[str], operator.add]]  # one result line per area
    notes: NotRequired[Annotated[list[str], operator.add]]  # why something didn't happen


@dataclass
class FlowContext:
    rt: SubagentRuntime
    writer: Writer  # the main graph's custom stream (the UI)
    call_id: str  # the PlaywrightTests tool call's id
    run: Any = None  # the main graph's RunContext, passed on to the roles


def _ide_tools(rt: SubagentRuntime) -> list[str]:
    return sorted(t.name for t in rt.langchain_tools if t.name.startswith("ide_"))


def role_definition(rt: SubagentRuntime, name: str) -> AgentDefinition:
    """Your `.cmcoder/agents/<name>.md` if you defined one, else the built-in (with the IDE's
    browser tools added to its list when it has some)."""
    mine = rt.definitions().get(name)
    if mine is not None:
        return mine
    role = ROLES[name]
    if role.tools is None:
        return role
    return AgentDefinition(
        role.name, role.description, role.prompt, role.origin, tools=[*role.tools, *_ide_tools(rt)]
    )


async def run_stage(
    ctx: FlowContext,
    role: str,
    step: str,
    description: str,
    prompt: str,
    schema: type[BaseModel] | None = None,
) -> SubagentResult:
    """Run one role as a subagent, shown in the UI as its own Task card."""
    call_id = f"{ctx.call_id}-{step}"  # stable: a resumed node reuses its run
    if call_id not in ctx.rt.runs:
        _emit(
            ctx.writer,
            ev.ToolUse(
                id=call_id,
                name="Task",
                input={"subagent_type": role, "description": description},
                label=f"{role}: {description}",
            ),
        )
    if schema is not None:
        prompt += f"\n\nWhen you are done, call the {schema.__name__} tool exactly once."
    result = await run_agent(
        ctx.rt,
        role_definition(ctx.rt, role),
        description,
        prompt,
        ctx.writer,
        call_id,
        response_format=ToolStrategy(schema) if schema is not None else None,
        context=ctx.run,
    )
    shown = result.structured.summary if result.structured is not None else result.report
    _emit(ctx.writer, ev.ToolResult(id=call_id, name="Task", content=(shown or "")[:2000]))
    return result


def _facts(ctx: FlowContext) -> tuple[Any, str | None, list[str]]:
    """(the probe, the app's address, the IDE's browser tools) as they are now."""
    settings = ctx.rt.settings
    p = probe(ctx.rt.root, settings.playwright.dir, settings.browser.executable_path)
    return p, app_url(p, settings), _ide_tools(ctx.rt)


def _label(area: str) -> str:
    return area or "the whole app"


async def prepare(state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
    ctx = runtime.context
    p, url, ide = _facts(ctx)
    if not p.ready or url is None:
        why = (
            "Playwright isn't set up here (type /playwright setup <address>)"
            if not p.ready
            else "the app's address isn't known (type /playwright setup <address>)"
        )
        return {"ready": False, "areas": [], "notes": [why]}
    if not status.state(p).has_map:
        await run_stage(
            ctx,
            "playwright-mapper",
            "map",
            "list the pages of the app",
            prompts.map_prompt(p, url, ide),
            StageReport,
        )
    s = status.state(probe(ctx.rt.root, p.dir, ctx.rt.settings.browser.executable_path))
    names = [a.name for a in s.areas]
    notes: list[str] = []
    if state["only"]:
        area = s.find(state["only"])
        if area is None:
            return {
                "ready": True,
                "areas": [],
                "notes": [
                    f'No area "{state["only"]}": the areas are ' + (", ".join(names) or "none")
                ],
            }
        names = [area.name]
    elif not names:
        names = [""]  # no site map came out of it: the whole app, as one area
        notes.append("There is no site map, so the whole app is one area.")
    rounds = ctx.rt.settings.playwright.max_heal_rounds
    return {"ready": True, "areas": names, "index": -1, "max_rounds": rounds, "notes": notes}


def route_prepare(state: FlowState) -> Literal["next_area", "__end__"]:
    return "next_area" if state.get("ready") and state.get("areas") else "__end__"


async def next_area(state: FlowState) -> dict[str, Any]:
    index = state.get("index", -1) + 1
    areas = state.get("areas", [])
    if index >= len(areas):
        return {"index": index}
    return {"index": index, "current": areas[index], "round": 0, "report": None, "ok": True}


def route_next(state: FlowState) -> Literal["plan", "__end__"]:
    return "plan" if state.get("index", 0) < len(state.get("areas", [])) else "__end__"


def _area(ctx: FlowContext, name: str) -> status.Area | None:
    p = probe(ctx.rt.root, ctx.rt.settings.playwright.dir, None)
    return status.state(p).find(name) if name else None


def _plan_file(ctx: FlowContext, name: str) -> Path:
    p = probe(ctx.rt.root, ctx.rt.settings.playwright.dir, None)
    return p.folder / "specs" / f"{prompts.slug(name or 'site')}.md"


def _tests(ctx: FlowContext, name: str) -> list[Path]:
    p = probe(ctx.rt.root, ctx.rt.settings.playwright.dir, None)
    return sorted((p.folder / "tests" / prompts.slug(name or "site")).glob("*.spec.ts"))


async def plan(state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
    ctx, name = runtime.context, state.get("current", "")
    if _plan_file(ctx, name).is_file():  # done in an earlier run
        return {"ok": True}
    p, url, ide = _facts(ctx)
    area = _area(ctx, name)
    await run_stage(
        ctx,
        "playwright-planner",
        f"plan-{prompts.slug(name or 'site')}",
        f"plan the tests of {_label(name)}",
        prompts.plan_prompt(p, url or "", name or None, area.pages if area else [], ide),
        StageReport,
    )
    ok = _plan_file(ctx, name).is_file()
    return {"ok": ok, "notes": [] if ok else [f"{_label(name)}: no plan was written"]}


async def generate(state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
    ctx, name = runtime.context, state.get("current", "")
    if _tests(ctx, name):  # done in an earlier run
        return {"ok": True}
    p, _, ide = _facts(ctx)
    await run_stage(
        ctx,
        "playwright-generator",
        f"generate-{prompts.slug(name or 'site')}",
        f"write the tests of {_label(name)}",
        prompts.generate_prompt(p, name or None, ide),
        StageReport,
    )
    ok = bool(_tests(ctx, name))
    return {"ok": ok, "notes": [] if ok else [f"{_label(name)}: no tests were written"]}


async def run_tests(state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
    ctx, name = runtime.context, state.get("current", "")
    p, url, ide = _facts(ctx)
    starts = bool(WEB_SERVER.search(config_text(p)))
    n = state.get("round", 0)
    result = await run_stage(
        ctx,
        "playwright-runner",
        f"run-{prompts.slug(name or 'site')}-{n}",
        f"run the tests of {_label(name)}",
        prompts.run_prompt(p, url or "", prompts.slug(name or "site"), starts, ide),
        RunReport,
    )
    report = result.structured if isinstance(result.structured, RunReport) else None
    found = [x for x in (report.problems if report else []) if x.kind == "APP"]
    return {"report": report, "app": found}


def test_failures(report: RunReport | None) -> list[Problem]:
    return [x for x in (report.problems if report else []) if x.kind == "TEST"]


def route_after_run(state: FlowState) -> Literal["heal", "finish_area"]:
    report = state.get("report")
    if report is None or report.failed == 0 or not test_failures(report):
        return "finish_area"
    return "heal" if state.get("round", 0) < state.get("max_rounds", 3) else "finish_area"


async def heal(state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
    ctx, name = runtime.context, state.get("current", "")
    p, _, ide = _facts(ctx)
    n = state.get("round", 0) + 1
    result = await run_stage(
        ctx,
        "playwright-healer",
        f"heal-{prompts.slug(name or 'site')}-{n}",
        f"fix the failing tests of {_label(name)} (round {n})",
        prompts.heal_prompt(p, prompts.slug(name or "site"), 1, ide),
        HealReport,
    )
    marked = result.structured.app_problems if isinstance(result.structured, HealReport) else []
    return {"round": n, "app": marked}


def route_after_step(state: FlowState) -> Literal["next", "finish_area"]:
    return "next" if state.get("ok") else "finish_area"


async def finish_area(state: FlowState) -> dict[str, Any]:
    name, report = state.get("current", ""), state.get("report")
    if not state.get("ok", True):
        line = f"{_label(name)}: stopped (see the notes)"
    elif report is None:
        line = f"{_label(name)}: the tests were written but not run"
    else:
        left = len(test_failures(report))
        line = f"{_label(name)}: {report.passed} passed, {report.failed} failed"
        if left and state.get("round", 0) >= state.get("max_rounds", 3):
            line += f" ({left} test(s) still wrong after {state.get('round', 0)} rounds)"
    return {"done": [line]}


def build_flow_graph() -> Any:
    graph = StateGraph(FlowState, context_schema=FlowContext)
    graph.add_node("prepare", prepare)
    graph.add_node("next_area", next_area)
    graph.add_node("plan", plan)
    graph.add_node("generate", generate)
    graph.add_node("run", run_tests)
    graph.add_node("heal", heal)
    graph.add_node("finish_area", finish_area)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", route_prepare)
    graph.add_conditional_edges("next_area", route_next)
    graph.add_conditional_edges(
        "plan", route_after_step, {"next": "generate", "finish_area": "finish_area"}
    )
    graph.add_conditional_edges(
        "generate", route_after_step, {"next": "run", "finish_area": "finish_area"}
    )
    graph.add_conditional_edges("run", route_after_run)
    graph.add_edge("heal", "run")
    graph.add_edge("finish_area", "next_area")
    # No checkpointer: inside the PlaywrightTests tool it uses the main graph's.
    return graph.compile(name="playwright-tests")


FLOW_GRAPH = build_flow_graph()


def flow_summary(state: FlowState) -> str:
    """The tool's result: what the model tells the user."""
    lines = [f"- {x}" for x in state.get("done", [])]
    parts = ["Playwright tests, area by area:" if lines else "Nothing was run."]
    parts += lines
    seen: set[str] = set()
    problems = [x for x in state.get("app", []) if not (x.test in seen or seen.add(x.test))]
    if problems:
        parts.append("\nProblems of the app that the tests found (for the user to fix):")
        for x in problems:
            where = f" ({x.where})" if x.where else ""
            parts.append(f"- {x.test}: {x.what[:REPORT_CHARS]}{where}")
    elif lines:
        parts.append("\nNo problems of the app were found.")
    if notes := state.get("notes"):
        parts.append("\nNotes: " + "; ".join(notes))
    return "\n".join(parts)


# --- The PlaywrightTests tool -------------------------------------------------------------------


class PlaywrightTestsInput(BaseModel):
    area: str = Field(
        "",
        description="One area of the site map to do; empty for every area. The tool finds the "
        "app, the folder and the areas itself.",
    )


DESCRIPTION = """\
Do the whole Playwright test job for this project's web app: browse it and list its pages (if \
there is no site map yet), then, area by area, plan the tests, write them, run them and fix the \
tests that are wrong, up to {rounds} rounds. It never changes the app: the app's own problems \
are listed in the result. Call it once, when the user asks for all of it (/playwright all). It \
returns a report: summarise it for the user."""


def build_playwright_tool(rt: SubagentRuntime) -> BaseTool:
    async def playwright_tests(runtime: ToolRuntime, area: str = "") -> str:
        final = await FLOW_GRAPH.ainvoke(
            {"only": area.strip()},
            context=FlowContext(
                rt, runtime.stream_writer, runtime.tool_call_id or "playwright", runtime.context
            ),
        )
        return flow_summary(final)

    return StructuredTool.from_function(
        coroutine=playwright_tests,
        name="PlaywrightTests",
        description=DESCRIPTION.format(rounds=rt.settings.playwright.max_heal_rounds),
        args_schema=PlaywrightTestsInput,
        handle_tool_error=True,
    )
