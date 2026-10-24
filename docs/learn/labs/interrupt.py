"""Human-in-the-loop by hand: the graph pauses at interrupt(), we answer, it resumes."""

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings


async def main(base_url: str, project: str, answer: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )
    # interactive=True: a call that needs approval pauses the graph instead of being denied.
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=True))
    config: RunnableConfig = {"configurable": {"thread_id": "lab-hitl"}}
    graph_input = {"messages": [HumanMessage("fix add")]}
    while True:
        async for _ in agent.graph.astream(graph_input, config, context=RunContext(turn=1)):
            pass
        state = await agent.graph.aget_state(config)
        if not state.interrupts:
            break
        for intr in state.interrupts:  # what PermissionMiddleware passed to interrupt()
            v = intr.value
            print(f"PAUSED: {v['label']}  (reason: {v['reason']})")
            print("  proposed change:", repr(v["change"]["after"]) if v.get("change") else None)
        allow = answer == "allow"
        print(f"ANSWER: {'allow' if allow else 'deny'}")
        graph_input = Command(resume={i.id: {"allow": allow} for i in state.interrupts})
    last = state.values["messages"][-1]
    print(f"\nlast message: {type(last).__name__}: {last.content!r}")
    await agent.close()


asyncio.run(main(*sys.argv[1:4]))
print(Path(sys.argv[2], "app.py").read_text())  # did the edit happen?
