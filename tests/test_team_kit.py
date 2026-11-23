"""The software team kit (docs/team-kit) works the way cmcoder loads it.

The kit is files people copy into a project, so nothing else would notice if one stopped
loading: a skill with no description is silently skipped, an agent with an unknown tool is
silently limited. This copies the kit into a temporary project and loads it with the real
loaders: agents, skills (and the files the Skill tool reads), commands (expanded with an
argument), and the permission rule its README tells people to add.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.agents.subagents import load_agents
from cmcoder.agents.team import TEAM_ROLES
from cmcoder.core.commands import BUILT_IN as FILE_BUILT_IN
from cmcoder.core.commands import load_commands, parse_file
from cmcoder.core.permissions import Decision, PermissionPolicy
from cmcoder.core.skills import MAX_DESCRIPTION_CHARS, SkillInput, SkillTool, load_skills
from cmcoder.runtime.commands import BUILT_IN as RUNTIME_BUILT_IN
from cmcoder.runtime.commands import run_command
from cmcoder.runtime.session import Session
from cmcoder.tools.base import ToolContext
from cmcoder.tools.files import WriteInput, WriteTool
from cmcoder.tools.registry import default_tools

from .conftest import API_KEY
from .helpers import make_session, mock_settings, run_turn

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "docs" / "team-kit"
FILES = KIT / "project-files"

AGENTS = (
    "tech-lead", "pm", "architect", "designer", "developer", "reviewer", "tester", "security",
    "devops", "writer", "debugger",
)  # fmt: skip
TEAMMATES = tuple(a for a in AGENTS if a != "tech-lead")
SKILLS = (
    "team-workflow", "write-prd", "design-solution", "design-ui", "plan-work", "implement-task",
    "review-code", "test-feature", "security-review", "fix-bug", "release", "document-change",
)  # fmt: skip
COMMANDS = ("kickoff", "feature", "breakdown", "bugfix", "release", "team-status")
# The skill each teammate is told to load.
SKILL_OF = {
    "tech-lead": "team-workflow", "pm": "write-prd", "architect": "design-solution",
    "designer": "design-ui", "developer": "implement-task", "reviewer": "review-code",
    "tester": "test-feature", "security": "security-review", "devops": "release",
    "writer": "document-change", "debugger": "fix-bug",
}  # fmt: skip
WRITE_RULE = "Edit(docs/team/**)"


@pytest.fixture
def kit_project(project: Path) -> Path:
    shutil.copytree(FILES, project, dirs_exist_ok=True)
    return project


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backticked(text: str) -> set[str]:
    return set(re.findall(r"`([^`\n]+)`", text))


# --- what is in the kit -----------------------------------------------------------------------


def test_the_kit_has_exactly_the_agents_skills_and_commands_it_lists() -> None:
    cm = FILES / ".cmcoder"
    assert {p.stem for p in (cm / "agents").glob("*.md")} == set(AGENTS)
    assert {p.parent.name for p in (cm / "skills").glob("*/SKILL.md")} == set(SKILLS)
    assert {p.stem for p in (cm / "commands").glob("*.md")} == set(COMMANDS)
    assert (FILES / "docs" / "team" / "PROJECT.md").is_file()


def test_the_readme_names_everything_in_the_kit() -> None:
    readme = text_of(KIT / "README.md")
    for name in (*AGENTS, *SKILLS):
        assert f"`{name}`" in readme, f"README.md doesn't mention {name}"
    for name in COMMANDS:
        assert f"/{name}" in readme, f"README.md doesn't mention /{name}"
    for example in (KIT / "examples").glob("*.md"):
        assert example.name in readme, f"README.md doesn't mention {example.name}"
    assert "CMCODER-snippet.md" in readme and (KIT / "CMCODER-snippet.md").is_file()


# --- agents -------------------------------------------------------------------------------------


def test_every_agent_loads_as_a_project_agent_once_the_project_is_trusted(
    kit_project: Path,
) -> None:
    assert not set(AGENTS) & set(load_agents(kit_project, project_trusted=False))
    found = load_agents(kit_project, project_trusted=True)
    known = {t.name for t in default_tools()} | {"Skill", "CodeSearch"}
    for name in AGENTS:
        agent = found[name]
        assert agent.origin == "project" and agent.folder == ".cmcoder/agents", name
        assert 20 < len(agent.description) <= 300, name
        assert agent.model == "inherit", name
        assert agent.tools is None or set(agent.tools) <= known, (name, agent.tools)
        assert len(agent.prompt) < 6_000, f"{name}'s prompt is too long for a mid-sized model"
        assert "docs/team/PROJECT.md" in agent.prompt, name
        assert "## You may edit" in agent.prompt or name == "tech-lead", name
        assert f"`{SKILL_OF[name]}`" in agent.prompt, f"{name} doesn't name its skill"


def test_a_teammate_ends_with_the_handoff_block(kit_project: Path) -> None:
    found = load_agents(kit_project, True)
    for name in TEAMMATES:
        prompt = found[name].prompt
        assert "## Handoff" in prompt and "Status: done | blocked | needs-changes" in prompt, name
        assert "Never commit, push, deploy or publish" in prompt, name
        assert "Nobody answers questions" in prompt, name  # teammates cannot ask


def test_file_names_match_the_names_inside() -> None:
    for path in (FILES / ".cmcoder" / "agents").glob("*.md"):
        assert parse_file(text_of(path))[0]["name"] == path.stem, path.name  # Claude Code needs it
    for path in (FILES / ".cmcoder" / "skills").glob("*/SKILL.md"):
        assert parse_file(text_of(path))[0]["name"] == path.parent.name, path.parent.name


def test_the_kit_takes_over_every_role_of_the_built_in_team_command(kit_project: Path) -> None:
    found = load_agents(kit_project, True)
    assert set(TEAM_ROLES) <= set(found)  # /team uses pm, developer, tester and devops by name
    assert "verdict: pass" in found["tester"].prompt  # what the tester's structured report needs
    assert "acceptance criteria" in found["pm"].prompt and "Write" in (found["pm"].tools or [])
    assert "build, CI or deployment files must change" in found["pm"].prompt


# --- skills -------------------------------------------------------------------------------------


def test_every_skill_loads_and_its_description_is_not_cut(kit_project: Path) -> None:
    found = load_skills(kit_project)
    assert set(SKILLS) <= set(found)
    for name in SKILLS:
        raw = parse_file(text_of(FILES / ".cmcoder" / "skills" / name / "SKILL.md"))[0]
        assert 40 < len(raw["description"]) <= MAX_DESCRIPTION_CHARS, name  # longer is cut off
        assert found[name].origin == "project"


async def test_the_skill_tool_reads_every_file_a_skill_names(kit_project: Path) -> None:
    tool = SkillTool(load_skills(kit_project))
    ctx = ToolContext(cwd=kit_project, project_root=kit_project)
    for skill in SKILLS:
        folder = FILES / ".cmcoder" / "skills" / skill
        body = text_of(folder / "SKILL.md")
        named = {n for n in backticked(body) if n.endswith(".md") and "/" not in n}
        extra = {p.name for p in folder.glob("*.md") if p.name != "SKILL.md"}
        assert named == extra, f"{skill}: names {sorted(named)}, has {sorted(extra)}"
        for file in sorted(extra):
            res = await tool.run(SkillInput(skill=skill, file=file), ctx)
            assert not res.is_error and len(res.content) > 100, (skill, file, res.content[:100])


def test_every_skill_is_used_by_an_agent_a_command_or_the_workflow() -> None:
    cm = FILES / ".cmcoder"
    homes = [*(cm / "agents").glob("*.md"), *(cm / "commands").glob("*.md")]
    homes.append(cm / "skills" / "team-workflow" / "SKILL.md")
    text = "\n".join(text_of(p) for p in homes)
    for skill in SKILLS:
        assert f"`{skill}`" in text, f"nothing uses the skill {skill}"


def test_the_workflow_names_every_teammate_every_gate_and_the_loop_limits() -> None:
    workflow = text_of(FILES / ".cmcoder" / "skills" / "team-workflow" / "SKILL.md")
    for name in TEAMMATES:
        assert f"`{name}`" in workflow, f"the workflow table lacks {name}"
    for gate in "ABCDE":
        assert f"**Gate {gate}" in workflow, f"Gate {gate}"
    project = text_of(FILES / "docs" / "team" / "PROJECT.md")
    review = re.search(r"\*\*(\d+) review rounds\*\*", workflow)
    tests = re.search(r"\*\*(\d+) test rounds\*\*", workflow)
    assert review and tests
    assert f"Review rounds per task: {review.group(1)}" in project  # one number, two places
    assert f"Test rounds per task: {tests.group(1)}" in project


# --- commands -----------------------------------------------------------------------------------


def test_every_command_loads_and_none_replaces_a_built_in(kit_project: Path) -> None:
    found = load_commands(kit_project)
    built_in = set(FILE_BUILT_IN) | {name for name, _, _ in RUNTIME_BUILT_IN}
    for name in COMMANDS:
        command = found[name]
        assert name not in built_in, f"/{name} would clash with a built-in command"
        assert "$ARGUMENTS" in command.body, name
        assert command.description, name
    assert found["team-status"].allowed_tools == []  # read-only
    assert all(found[n].allowed_tools == [WRITE_RULE] for n in COMMANDS if n != "team-status")


def test_commands_and_the_lead_only_name_teammates_and_skills_that_exist() -> None:
    cm = FILES / ".cmcoder"
    allowed = {*AGENTS, *SKILLS, "subagent_type"}
    for path in [*(cm / "commands").glob("*.md"), cm / "agents" / "tech-lead.md"]:
        for token in backticked(text_of(path)):
            if re.fullmatch(r"[a-z][a-z_-]+", token):  # looks like a name, not a path or a command
                assert token in allowed, f"{path.name} names `{token}`, which is not in the kit"


@pytest.mark.parametrize("trusted", [True, False])
async def test_each_command_expands_with_its_argument(kit_project: Path, trusted: bool) -> None:
    session = make_session("http://127.0.0.1:9", kit_project)
    session.agent.spec["settings"].project_trusted = trusted
    try:
        for name in COMMANDS:
            result = await run_command(session, f"/{name} export orders to CSV")
            assert result.send is not None and "export orders to CSV" in result.send, name
            if name != "team-status":
                assert "team-workflow" in result.send, name
            # A repository's commands get their allow rules only in a trusted project.
            expected = [WRITE_RULE] if trusted and name != "team-status" else []
            assert result.allow == expected, (name, trusted)
    finally:
        await session.agent.close()


# --- the files for a project ---------------------------------------------------------------------


def headings(path: Path) -> list[str]:
    return [line for line in text_of(path).splitlines() if line.startswith("#")][1:]


def test_the_project_template_has_placeholders_and_the_examples_have_none() -> None:
    template = FILES / "docs" / "team" / "PROJECT.md"
    text = text_of(template)
    assert text.count("<<") >= 25  # what the lead looks for to know the file is not filled in
    for row in ("| Build |", "| Test, everything |", "| Install dependencies |"):
        assert row in text
    examples = sorted((KIT / "examples").glob("PROJECT.*.md"))
    assert len(examples) >= 2
    for example in examples:
        assert "<<" not in text_of(example), example.name
        assert headings(example)[1:] == headings(template)[1:], f"{example.name} has other sections"


def test_the_rule_the_readme_recommends_lets_the_team_write_only_its_own_files(
    project: Path,
) -> None:
    assert f'"{WRITE_RULE}"' in text_of(KIT / "README.md")
    policy = PermissionPolicy(allow=[WRITE_RULE])
    ctx = ToolContext(cwd=project, project_root=project)
    prd = WriteInput(file_path="docs/team/prd/export.md", content="x")
    code = WriteInput(file_path="src/app.py", content="x")
    assert policy.check(WriteTool(), prd, ctx).decision == Decision.ALLOW
    assert policy.check(WriteTool(), code, ctx).decision == Decision.ASK


# --- the whole thing, with a scripted model ------------------------------------------------------


async def test_the_lead_hands_work_to_a_teammate_that_has_only_its_own_tools(
    mock_server: object, kit_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main agent sees the team in the Task tool and the skills in its prompt; the `pm` it
    starts gets the `pm` prompt and exactly the tools its file lists."""
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    brief = {"description": "write the PRD", "prompt": "Write the PRD", "subagent_type": "pm"}
    server = mock_server(  # type: ignore[operator]
        [
            {"tool_calls": [{"id": "t1", "name": "Task", "arguments": brief}]},
            {"content": "PRD written: docs/team/prd/export.md"},
            {"content": "Gate A: please approve the PRD."},
        ]
    )
    settings = mock_settings(server.base_url)
    settings.project_trusted = True
    agent = build_agent(settings, AgentOptions(cwd=kit_project))
    try:
        await run_turn(Session(agent), "Deliver the export feature")
    finally:
        await agent.close()
    lead, pm = server.requests[0], server.requests[1]
    task = next(t["function"] for t in lead["tools"] if t["function"]["name"] == "Task")
    for name in AGENTS:
        assert f"- {name}: " in task["description"], f"the lead can't see {name}"
    system = str(lead["messages"][0]["content"])
    assert "# Skills" in system and "- team-workflow: " in system and "- write-prd: " in system
    assert {t["function"]["name"] for t in pm["tools"]} == {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Skill",
    }
    assert "You are the team's product manager" in str(pm["messages"][0]["content"])
