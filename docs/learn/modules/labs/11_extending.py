"""Chapter 11 lab: an MCP server's tools as LangChain tools, its prompt as a
command, and a custom command and a skill from a project's .cmcoder folder.

No mock server needed: this lab never calls a model.

    python 11_extending.py <project dir>
"""

import asyncio
import sys
from pathlib import Path

from cmcoder.config.settings import Settings
from cmcoder.core.commands import load_commands, mcp_prompt_commands, substitute
from cmcoder.core.skills import SkillInput, SkillTool, load_skills, skills_prompt
from cmcoder.mcp.manager import McpManager, prompt_text
from cmcoder.tools.base import ToolContext

REPO = Path(__file__).resolve().parents[4]
DEMO_SERVER = REPO / "tests" / "mcp_servers" / "demo_server.py"


async def mcp_part(project: Path) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": "http://127.0.0.1:1/v1"}},
            "model": "mock:qwen3-27b",
            # A stdio MCP server in *your* settings: no approval needed
            # (a project's .mcp.json server would ask first).
            "mcpServers": {
                "demo": {
                    "command": sys.executable,
                    "args": [str(DEMO_SERVER)],
                    "toolTimeout": 2,
                }
            },
        }
    )
    manager = McpManager(settings, project)
    print("start warnings:", await manager.start(None))
    tools = manager.langchain_tools()
    print("\nMCP tools, as the agent sees them (LangChain BaseTools):")
    for t in tools:
        print(f"   {t.name:22} {type(t).__name__:15} {t.description[:50]!r}")
    by_name = {t.name: t for t in tools}

    # A tool call, the way the tools node makes one: a ToolCall dict in, a ToolMessage out.
    add = by_name["mcp__demo__add"]
    msg = await add.ainvoke(
        {"name": add.name, "args": {"a": 2, "b": 3}, "id": "call-1", "type": "tool_call"}
    )
    print("\nmcp__demo__add(2, 3) ->", type(msg).__name__, repr(msg.content), msg.status)

    # The tool interceptor's timeout (toolTimeout: 2s) turns a hang into an error result.
    slow = by_name["mcp__demo__slow"]
    msg = await slow.ainvoke(
        {"name": slow.name, "args": {"seconds": 5}, "id": "call-2", "type": "tool_call"}
    )
    print("mcp__demo__slow(5)   ->", type(msg).__name__, repr(msg.content), msg.status)

    # The server's prompts become slash commands.
    commands = mcp_prompt_commands(manager.connected())
    for name, cmd in commands.items():
        print(f"\nMCP prompt as a command: /{name} {cmd.argument_hint}")
        server, prompt = cmd.mcp
        print("   expands to:", repr(await prompt_text(server, prompt.name, {"file": "app.py"})))
    await manager.close()


def files_part(project: Path) -> None:
    # A custom command: .cmcoder/commands/explain.md becomes /explain.
    commands_dir = project / ".cmcoder" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "explain.md").write_text(
        "---\ndescription: Explain a file for a new team member\nargument-hint: <file>\n---\n"
        "Explain $1 to a new team member: what it does and how it fits in. Focus: $ARGUMENTS\n"
    )
    cmd = load_commands(project)["explain"]
    print(f"\ncustom command /explain ({cmd.origin}): {cmd.description!r}")
    print("   /explain app.py ->", repr(substitute(cmd.body, "app.py")))

    # A skill: only its description goes in the system prompt...
    skill_dir = project / ".cmcoder" / "skills" / "release-notes"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Writes release notes in our format.\n---\n"
        "1. Run git log since the last tag.\n2. Group the changes. See template.md.\n"
    )
    (skill_dir / "template.md").write_text("## Added\n## Fixed\n")
    skills = load_skills(project)
    print("\nskills section of the system prompt:\n" + (skills_prompt(skills) or ""))


async def skill_part(project: Path) -> None:
    # ...and the Skill tool returns the rest when the model asks for it.
    tool = SkillTool(load_skills(project))
    result = await tool.run(
        SkillInput(skill="release-notes", file=None), ToolContext(cwd=project, project_root=project)
    )
    print("\nSkill(release-notes) ->", repr(result.content))


async def main(root: Path) -> None:
    await mcp_part(root)
    files_part(root)
    await skill_part(root)


asyncio.run(main(Path(sys.argv[1]).resolve()))
