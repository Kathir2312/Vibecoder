"""The team: PM → Developer → Tester (→ Developer again on a failure) → DevOps.

In cmcoderagent the team was a process written in CMCODER.md that the main
agent followed by calling the Task tool. Here it is an explicit LangGraph
`StateGraph`: the order, the retry loop and the round limit are code, not a
request to the model, so a 27B model can't skip the tester or loop forever.
The main agent starts it with the `Team` tool (when `team.enabled` is on;
`/team <feature>` asks it to).

    START ──(stories given?)──► developer
      │ no                         ▲   │
      ▼                     fail,  │   ▼
      pm ──────────────────► developer ──► tester ──(pass, needs CI?)──► devops ──► END
                            rounds left    │
                                           └─(pass, no CI / fail, no rounds left)──► END

Each node runs one role as a subagent (agents/subagents.py `run_agent`): the
same tools, permission rules and hooks, its own conversation. The roles are
`.cmcoder/agents/pm.md`, `developer.md`, `tester.md`, `devops.md` when you
define them (in your user folder, or in a trusted project), else the
built-in defaults below.

LANGGRAPH / LANGCHAIN CONCEPTS IN THIS FILE

* **`StateGraph` with a typed state.** `TeamState` is a `TypedDict`; each node
  returns only the keys it changes and LangGraph merges them. `changes` has a
  **reducer** (`Annotated[list[str], operator.add]`): every developer round
  *appends* its report instead of replacing the last one.
* **Conditional edges.** `add_conditional_edges(node, route)` calls `route`
  with the state after `node` and goes where it says. `route_start` picks the
  first node (skip the PM when the stories already exist); `route_after_test`
  is the loop: a failed test goes back to the developer while rounds are left.
* **`context_schema` + `Runtime`.** Things a run needs but that aren't state
  (the subagent runtime, the UI writer, the main graph's `RunContext`) go in
  `TeamContext`, passed as `ainvoke(..., context=...)`; a node declares
  `runtime: Runtime[TeamContext]` to get it. A nested graph sees its *own*
  parent's context, so the roles get the main `RunContext` passed on
  explicitly (their file snapshots belong to the user's turn).
* **Structured output per role.** The PM ends with a `Stories` object and the
  tester with a `TestReport` (`ToolStrategy`), so routing reads fields,
  never parses prose.
* **Nesting and interrupts.** The graph is compiled once, without a
  checkpointer, and run inside the Team tool: it inherits the main graph's
  checkpointer (under its own namespace). A permission prompt in, say, the
  tester's Bash call pauses the main graph; on resume the finished nodes
  (pm, developer) are *not* run again, LangGraph picks up at the tester.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain.agents.structured_output import ToolStrategy
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from ..protocol import events as ev
from .subagents import AgentDefinition, SubagentResult, SubagentRuntime, Writer, _emit, run_agent

ROLE_MAX_TURNS = None  # None: `subagentMaxTurns`
REPORT_CHARS = 6_000  # of one role's report passed on to the next role

PM_PROMPT = """\
You are the team's product manager. Read the relevant code to understand what
exists. Write docs/stories/<feature>.md with: goal, user stories, acceptance
criteria (each one testable), out of scope, open questions. Do not change code."""

DEVELOPER_PROMPT = """\
You are the team's developer. Implement exactly what the task describes; keep
changes minimal and in the existing style of the code. Build or run the project
after your change and fix what breaks. Report every file you changed (path and
what changed), how you checked it, and anything left open."""

TESTER_PROMPT = """\
You are the team's tester. Write tests for each acceptance criterion you are
given, then run the project's whole test suite. Never change product code,
only test files. Report each failure with the test's name, the exact error and
the file and line it points to."""

DEVOPS_PROMPT = """\
You are the team's DevOps engineer. Change only build, CI and deployment files
(pipelines, Dockerfiles, build scripts). Never push, deploy or publish: prepare
the files and say what a person must run. Report the files you changed and how
to verify them."""

TEAM_ROLES = {
    "pm": AgentDefinition(
        "pm",
        "Turns a feature request into stories with acceptance criteria.",
        PM_PROMPT,
        "built-in",
        tools=["Read", "Glob", "Grep", "Write", "Skill", "CodeSearch"],
    ),
    "developer": AgentDefinition(
        "developer", "Implements a story or fixes test failures.", DEVELOPER_PROMPT, "built-in"
    ),
    "tester": AgentDefinition(
        "tester", "Tests a change against its acceptance criteria.", TESTER_PROMPT, "built-in"
    ),
    "devops": AgentDefinition(
        "devops", "Updates build and CI files for a change.", DEVOPS_PROMPT, "built-in"
    ),
}


# --- What the roles hand back (structured output) ------------------------------


class Stories(BaseModel):
    """The stories you wrote for the feature."""

    file: str = Field(description="The stories file you wrote (path)")
    summary: str = Field(description="The stories in a few lines")
    criteria: list[str] = Field(description="The acceptance criteria, one testable item each")
    needs_devops: bool = Field(
        False, description="True if build, CI or deployment files must change for this feature"
    )


class Failure(BaseModel):
    test: str = Field(description="The failing test (or check)")
    error: str = Field(description="The exact error output, shortened")
    where: str = Field("", description="file:line it points to")


class TestReport(BaseModel):
    """Your test result: pass, or fail with each failure."""

    verdict: Literal["pass", "fail"]
    summary: str = Field(description="One or two sentences: what you tested and the result")
    failures: list[Failure] = Field(default_factory=list, description="For fail: each failure")


# --- The graph ------------------------------------------------------------------


class TeamState(TypedDict):
    feature: str  # the request, as the lead passed it on
    stories_file: NotRequired[str]  # given by the lead: skip the PM
    max_rounds: NotRequired[int]  # developer ↔ tester rounds
    stories: NotRequired[Stories | None]
    round: NotRequired[int]  # developer rounds so far
    changes: NotRequired[Annotated[list[str], operator.add]]  # each developer round's report
    test: NotRequired[TestReport | None]
    devops: NotRequired[str]


@dataclass
class TeamContext:
    rt: SubagentRuntime
    writer: Writer  # the main graph's custom stream (the UI)
    call_id: str  # the Team tool call's id
    run: Any = None  # the main graph's RunContext, passed on to the roles


def role_definition(rt: SubagentRuntime, name: str) -> AgentDefinition:
    """Your `.cmcoder/agents/<name>.md` if you defined one, else the built-in."""
    return rt.definitions().get(name) or TEAM_ROLES[name]


async def run_role(
    ctx: TeamContext,
    name: str,
    step: str,
    description: str,
    prompt: str,
    schema: type[BaseModel] | None = None,
) -> SubagentResult:
    """Run one role as a subagent, shown in the UI as its own Task card."""
    call_id = f"{ctx.call_id}-{step}"  # stable: a resumed node reuses its run
    if call_id not in ctx.rt.runs:  # not when the node runs again after a prompt
        _emit(
            ctx.writer,
            ev.ToolUse(
                id=call_id,
                name="Task",
                input={"subagent_type": name, "description": description},
                label=f"{name}: {description}",
            ),
        )
    if schema is not None:
        prompt += f"\n\nWhen you are done, call the {schema.__name__} tool exactly once."
    result = await run_agent(
        ctx.rt,
        role_definition(ctx.rt, name),
        description,
        prompt,
        ctx.writer,
        call_id,
        response_format=ToolStrategy(schema) if schema is not None else None,
        max_turns=ROLE_MAX_TURNS,
        context=ctx.run,
    )
    shown = result.structured.summary if result.structured is not None else result.report
    _emit(ctx.writer, ev.ToolResult(id=call_id, name="Task", content=(shown or "")[:2000]))
    return result


def _clip(text: str) -> str:
    return text if len(text) <= REPORT_CHARS else text[:REPORT_CHARS] + "\n... [shortened]"


def _last_change(state: TeamState) -> str:
    changes = state.get("changes") or [""]
    return changes[-1]


def _criteria(state: TeamState) -> str:
    stories = state.get("stories")
    if stories is None:
        return f"Stories: {state.get('stories_file', '')} (read it for the acceptance criteria)"
    listed = "\n".join(f"- {c}" for c in stories.criteria) or "(see the stories file)"
    return f"Stories: {stories.file}\nAcceptance criteria:\n{listed}"


async def pm(state: TeamState, runtime: Runtime[TeamContext]) -> dict[str, Any]:
    result = await run_role(
        runtime.context,
        "pm",
        "pm",
        "write the stories",
        f"Feature request:\n{state['feature']}\n\n"
        "Write the stories and acceptance criteria for it.",
        Stories,
    )
    stories = result.structured
    if not isinstance(stories, Stories):  # no structured answer: go on with the report
        stories = Stories(file="", summary=result.report[:500], criteria=[], needs_devops=False)
    return {"stories": stories}


async def developer(state: TeamState, runtime: Runtime[TeamContext]) -> dict[str, Any]:
    round_ = state.get("round", 0) + 1
    task = f"Feature request:\n{state['feature']}\n\n{_criteria(state)}\n\n"
    test = state.get("test")
    if test is not None and test.verdict == "fail":
        failures = "\n".join(
            f"- {f.test}: {f.error}" + (f" ({f.where})" if f.where else "") for f in test.failures
        )
        task += (
            f"The tester found failures in your change:\n{failures or test.summary}\n\n"
            f"Your earlier report:\n{_clip(_last_change(state))}\n\n"
            "Fix them in the product code."
        )
        description = f"fix the test failures (round {round_})"
    else:
        task += "Implement it."
        description = "implement the feature"
    result = await run_role(runtime.context, "developer", f"developer-{round_}", description, task)
    return {"round": round_, "changes": [result.report or "(no report)"]}


async def tester(state: TeamState, runtime: Runtime[TeamContext]) -> dict[str, Any]:
    round_ = state.get("round", 1)
    result = await run_role(
        runtime.context,
        "tester",
        f"tester-{round_}",
        f"test the change (round {round_})",
        f"Feature request:\n{state['feature']}\n\n{_criteria(state)}\n\n"
        f"The developer's report:\n{_clip(_last_change(state))}\n\n"
        "Test the change against the acceptance criteria and run the whole test suite.",
        TestReport,
    )
    report = result.structured
    if not isinstance(report, TestReport):
        report = TestReport(
            verdict="fail", summary="The tester gave no verdict: " + result.report[:500]
        )
    return {"test": report}


async def devops(state: TeamState, runtime: Runtime[TeamContext]) -> dict[str, Any]:
    result = await run_role(
        runtime.context,
        "devops",
        "devops",
        "update build and CI",
        f"Feature request:\n{state['feature']}\n\n{_criteria(state)}\n\n"
        f"The developer's report:\n{_clip(_last_change(state))}\n\n"
        "Update the build and CI files this change needs (for example, run the new tests).",
    )
    return {"devops": result.report or "(no report)"}


def route_start(state: TeamState) -> Literal["pm", "developer"]:
    return "developer" if state.get("stories_file") else "pm"


def route_after_test(state: TeamState) -> Literal["developer", "devops", "__end__"]:
    test = state.get("test")
    if test is None or test.verdict == "fail":
        return "developer" if state.get("round", 0) < state.get("max_rounds", 3) else "__end__"
    stories = state.get("stories")
    return "devops" if stories is not None and stories.needs_devops else "__end__"


def build_team_graph() -> Any:
    graph = StateGraph(TeamState, context_schema=TeamContext)
    graph.add_node("pm", pm)
    graph.add_node("developer", developer)
    graph.add_node("tester", tester)
    graph.add_node("devops", devops)
    graph.add_conditional_edges(START, route_start)
    graph.add_edge("pm", "developer")
    graph.add_edge("developer", "tester")
    graph.add_conditional_edges("tester", route_after_test)
    graph.add_edge("devops", END)
    # No checkpointer: inside the Team tool it uses the main graph's.
    return graph.compile(name="team")


TEAM_GRAPH = build_team_graph()


def team_summary(state: TeamState) -> str:
    """The Team tool's result: what the lead needs to tell the user."""
    test = state.get("test")
    stories = state.get("stories")
    rounds, limit = state.get("round", 0), state.get("max_rounds", 3)
    if test is None:
        outcome = "The team stopped before testing."
    elif test.verdict == "pass":
        outcome = f"PASS after {rounds} developer round(s)."
    else:
        outcome = f"FAIL: the tests still fail after {rounds} of {limit} developer rounds."
    parts = [f"Team result: {outcome}"]
    if stories is not None:
        parts.append(f"## Stories ({stories.file or 'no file'})\n{stories.summary}")
    elif stories_file := state.get("stories_file"):
        parts.append(f"## Stories\n{stories_file}")
    if changes := state.get("changes"):
        parts.append(f"## Developer (last round)\n{_clip(changes[-1])}")
    if test is not None:
        failures = "\n".join(
            f"- {f.test}: {f.error}" + (f" ({f.where})" if f.where else "") for f in test.failures
        )
        parts.append(f"## Tester\n{test.summary}" + (f"\n{failures}" if failures else ""))
    if devops_report := state.get("devops"):
        parts.append(f"## DevOps\n{_clip(devops_report)}")
    return "\n\n".join(parts)


# --- The Team tool --------------------------------------------------------------


class TeamInput(BaseModel):
    feature: str = Field(
        description="The feature request, complete: the team doesn't see this conversation"
    )
    stories_file: str = Field(
        "", description="An existing stories file to implement (skips the PM); else empty"
    )


TEAM_DESCRIPTION = """\
Run the whole team on a feature: a PM writes stories and acceptance criteria,
a developer implements them, a tester tests them (failures go back to the
developer, up to {rounds} rounds) and DevOps updates build and CI when needed.
Use it when the user asks for a feature to be built by the team (or /team).
It returns the team's result: summarise it for the user (stories, files
changed, test result, anything open)."""


def build_team_tool(rt: SubagentRuntime, max_rounds: int) -> BaseTool:
    async def team(feature: str, runtime: ToolRuntime, stories_file: str = "") -> str:
        final = await TEAM_GRAPH.ainvoke(
            {
                "feature": feature,
                "stories_file": stories_file,
                "max_rounds": max_rounds,
                "round": 0,
            },
            context=TeamContext(
                rt, runtime.stream_writer, runtime.tool_call_id or "team", runtime.context
            ),
        )
        return team_summary(final)

    return StructuredTool.from_function(
        coroutine=team,
        name="Team",
        description=TEAM_DESCRIPTION.format(rounds=max_rounds),
        args_schema=TeamInput,
        handle_tool_error=True,
    )
