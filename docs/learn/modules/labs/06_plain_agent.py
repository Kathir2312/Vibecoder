"""Chapter 06 lab, part 1: create_agent with nothing of cmcoder's.

A plain LangChain v1 agent: ChatOpenAI against the mock server, one tool, one
middleware, an in-memory checkpointer. Then a second run on the same thread
shows the checkpointer kept the conversation.

    python 06_plain_agent.py http://127.0.0.1:8765/v1

Mock server script: labs/06_plain_agent.json (three replies).
"""

import asyncio
import sys
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime


@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


class CountCalls(AgentMiddleware):
    """A node before every model call: how many messages will the model see?"""

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        print(f"  [before_model] {len(state['messages'])} messages")
        return None


async def main(base_url: str) -> None:
    # streaming=True: the mock server (like many gateways) answers as a stream.
    model = ChatOpenAI(base_url=base_url, api_key="x", model="qwen3-27b", streaming=True)  # type: ignore[arg-type]
    agent = create_agent(
        model=model,
        tools=[word_count],
        system_prompt="You are a helpful assistant.",
        middleware=[CountCalls()],
        checkpointer=InMemorySaver(),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "t1"}}

    print("run 1:")
    state = await agent.ainvoke({"messages": [HumanMessage("How many words: a b c?")]}, config)
    for m in state["messages"]:
        print(f"  {type(m).__name__:12} {m.text!r} {getattr(m, 'tool_calls', '') or ''}")

    print("run 2, same thread_id (only the new message is sent):")
    state = await agent.ainvoke({"messages": [HumanMessage("Thanks!")]}, config)
    print(f"  the thread now holds {len(state['messages'])} messages")

    print("graph nodes:", list(agent.get_graph().nodes))


asyncio.run(main(sys.argv[1]))
