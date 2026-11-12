"""Who does each stage of the Playwright tests: five agents, built in and overridable.

`/playwright all` runs each stage as one of them in a subagent (agents/playwright_tests.py), and a
step command (`/playwright plan`, ...) makes the chat talk to the matching one for that step, so
the Agent list in the IDE shows who is working and the step has only the tools its job needs.
They are listed with the other agents (group "Playwright"), so a person can also pick one by hand.

Your own `.cmcoder/agents/<name>.md` of the same name replaces a role (subagents.py load_agents: a
project's, then yours, win over the built-in). `ide_*` in a tool list means every tool the IDE
offers whose name starts with it (its own browser, in VS Code).
"""

from __future__ import annotations

from .subagents import AgentDefinition

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

# What each role may use: the browser (cmcoder's own, or the IDE's `ide_*`), the files, and only
# what its job needs.
_FILES = ["Read", "Glob", "Grep"]
_BROWSER = ["Browser", "ide_*"]
ROLES = {
    "playwright-mapper": AgentDefinition(
        "playwright-mapper",
        "Browses the app and lists its pages by area.",
        MAPPER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", *_BROWSER],
    ),
    "playwright-planner": AgentDefinition(
        "playwright-planner",
        "Writes the test plan of an area of the app.",
        PLANNER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", *_BROWSER],
    ),
    "playwright-generator": AgentDefinition(
        "playwright-generator",
        "Writes Playwright tests from a plan.",
        GENERATOR_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Edit", "Bash", *_BROWSER],
    ),
    "playwright-runner": AgentDefinition(
        "playwright-runner",
        "Runs the tests and says who is wrong for each failure.",
        RUNNER_PROMPT,
        "built-in",
        tools=[*_FILES, "Bash", *_BROWSER],
    ),
    "playwright-healer": AgentDefinition(
        "playwright-healer",
        "Fixes the tests that are wrong; marks the app's own problems.",
        HEALER_PROMPT,
        "built-in",
        tools=[*_FILES, "Write", "Edit", "Bash", *_BROWSER],
    ),
}
ROLE_NAMES = frozenset(ROLES)
