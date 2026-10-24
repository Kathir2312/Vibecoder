"""Your first middleware: watch every model call and time every tool call."""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings


class WatchMiddleware(AgentMiddleware):
    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        print(f"[before_model] the model will see {len(state['messages'])} messages")
        return None  # nothing to change in the state

    async def awrap_tool_call(self, request, handler):
        started = time.perf_counter()
        result = await handler(request)  # run the tool (and the middleware inside this one)
        ms = (time.perf_counter() - started) * 1000
        print(f"[tool] {request.tool_call['name']} took {ms:.1f} ms")
        return result


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            "permissions": {"allow": ["Edit"]},
        }
    )
    agent = build_agent(
        settings,
        AgentOptions(cwd=Path(project), interactive=False),
        extra_middleware=[WatchMiddleware()],
    )
    config: RunnableConfig = {"configurable": {"thread_id": "lab-mw"}}
    result = await agent.graph.ainvoke(
        {"messages": [HumanMessage("fix add")]}, config, context=RunContext(turn=1)
    )
    print("answer:", result["messages"][-1].content)
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
