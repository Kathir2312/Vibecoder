"""Chapter 12 lab: the team StateGraph, run for real inside the Team tool.

The main agent calls the Team tool; inside it agents/team.py's StateGraph runs
PM -> developer -> tester -> (fail) developer -> tester -> devops, each role a
subagent graph of its own. Every Write asks for permission: the interrupt
travels up through the team graph and the main graph, we answer, and the run
resumes where it paused (finished roles are not run again).

    python 12_team.py http://127.0.0.1:8765/v1 <project dir>

Script: 12_team.json (twelve replies: the lead, then each role's model calls).
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            "team": {"enabled": True},
            "permissions": {"allow": ["Read"]},
        }
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=True))

    async def ask(request: ev.PermissionRequest) -> dict[str, Any]:
        print(f"    PERMISSION  {request.label}: allow")
        return {"allow": True}

    session = Session(agent, ask=ask)
    async for event in session.run("build greet with the team"):
        if isinstance(event, ev.ToolUse) and event.parent_tool_use_id is None:
            role = event.input.get("subagent_type", "")
            print(f"{event.name:5} {role:10} {event.label}")
        elif isinstance(event, ev.ToolUse):
            print(f"    tool        {event.label}")
        elif isinstance(event, ev.ToolResult) and event.name == "Team":
            print("\n" + event.content.splitlines()[0])
        elif isinstance(event, ev.Result):
            print("lead's answer:", event.result)
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
print("\ngreet.py:\n" + Path(sys.argv[2], "greet.py").read_text())
