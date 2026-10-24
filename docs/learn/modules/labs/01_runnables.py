"""Chapter 01 lab: one interface everywhere (Runnables), and the agent's graph.

A chat model, a tool and a whole compiled agent are all LangChain Runnables:
they share `invoke` / `ainvoke` / `stream` / `astream`. Then we print the
graph `create_agent` built for cmcoder, as Mermaid.

    python 01_runnables.py http://127.0.0.1:8765/v1 <project dir>

(The mock server's script needs two replies: see labs/README.md.)
"""

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import tool

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings
from cmcoder.models.chat import build_chat_model


@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )

    # 1. A chat model is a Runnable: messages in, an AIMessage out.
    model = build_chat_model(settings).model
    reply = await model.ainvoke([HumanMessage("Say hello")])
    print("model  :", type(model).__name__, "is a Runnable:", isinstance(model, Runnable))
    print("         ->", type(reply).__name__, repr(reply.text))

    # 2. A tool is a Runnable too: a dict of arguments in, the result out.
    print("tool   :", type(word_count).__name__, "is a Runnable:", isinstance(word_count, Runnable))
    print("         ->", await word_count.ainvoke({"text": "one two three"}))

    # 3. The whole agent is a compiled LangGraph graph, and also a Runnable.
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=False))
    graph = agent.graph
    print("agent  :", type(graph).__name__, "is a Runnable:", isinstance(graph, Runnable))
    config: RunnableConfig = {"configurable": {"thread_id": "lab-01"}}
    state = await graph.ainvoke(
        {"messages": [HumanMessage("hi")]}, config, context=RunContext(turn=1)
    )
    print(
        "         ->", len(state["messages"]), "messages; last:", repr(state["messages"][-1].text)
    )

    # 4. The graph create_agent built: nodes are the model, the tools and the
    #    middleware hooks; edges are the loop.
    print("\n" + graph.get_graph().draw_mermaid())
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
