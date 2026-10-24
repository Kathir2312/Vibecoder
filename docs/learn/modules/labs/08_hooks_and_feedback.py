"""Chapter 08 lab: a PreToolUse hook blocks one call, the user denies another
with feedback, and the model is asked again with both answers.

Script: labs/08_feedback.json. The model asks for `rm -rf build` (a hook
blocks it: no prompt, the tool never runs), then for an Edit (the graph
pauses on `interrupt()`; we deny *with feedback*), then answers.

    python 08_hooks_and_feedback.py http://127.0.0.1:8765/v1 <project dir>
"""

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings

# A PreToolUse hook: the event comes as JSON on stdin; exit 2 blocks, stderr is the reason.
NO_DELETE = (
    "grep -q 'rm -rf' && { echo 'Deleting folders is not allowed here: use git clean' >&2; "
    "exit 2; } || exit 0"
)


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": NO_DELETE}]}
                ]
            },
        }
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=True))
    config: RunnableConfig = {"configurable": {"thread_id": "lab-08"}}
    graph_input: object = {"messages": [HumanMessage("clean up and fix add")]}
    while True:
        async for _ in agent.graph.astream(graph_input, config, context=RunContext(turn=1)):
            pass
        state = await agent.graph.aget_state(config)
        if not state.interrupts:
            break
        for intr in state.interrupts:
            print(f"PAUSED on {intr.value['label']}: the user denies, with feedback")
        feedback = {"allow": False, "feedback": "Don't change add(); rename it to sum()."}
        graph_input = Command(resume={i.id: feedback for i in state.interrupts})

    print("\nWhat the model was told (the tool results in the state):")
    for m in state.values["messages"]:
        if isinstance(m, ToolMessage):
            print(f"  {m.name}: {m.text}")
    replies = [m for m in state.values["messages"] if isinstance(m, AIMessage)]
    print(f"\nModel calls this turn: {len(replies)}; last reply: {replies[-1].text!r}")
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
print(Path(sys.argv[2], "app.py").read_text())  # unchanged: the Edit never ran
