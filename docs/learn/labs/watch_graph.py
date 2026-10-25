"""See the agent graph's steps: which node ran, and what it added to the state."""

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            "permissions": {"allow": ["Edit"]},
        }
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=False))
    config: RunnableConfig = {"configurable": {"thread_id": "lab-1"}}
    async for part in agent.graph.astream(
        {"messages": [HumanMessage("fix add")]},
        config,
        context=RunContext(turn=1),
        stream_mode=["updates"],
        version="v2",
    ):
        data = part["data"]  # with "updates": {node name: what the node returned}
        if not isinstance(data, dict):
            continue
        for node, update in data.items():
            msgs = (update or {}).get("messages", []) if isinstance(update, dict) else []
            print(f"{node:45} {[type(m).__name__ for m in msgs]}")
    state = await agent.graph.aget_state(config)
    print("\nmessages in the checkpoint:", len(state.values["messages"]))
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
