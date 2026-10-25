"""Chapter 04 lab: a tool that gets `ToolRuntime` injected, in a plain create_agent.

The model only sees the `note` argument; LangChain fills in `runtime` (the
tool call's id, the graph state, a stream writer) when the tool runs.

    python 04_tool_runtime.py http://127.0.0.1:8765/v1

Mock server script: labs/04_tool_runtime.json
"""

import asyncio
import json
import sys

from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


@tool
def inspect_run(note: str, runtime: ToolRuntime) -> str:
    """Report what the tool can see of the run it is part of."""
    messages = runtime.state["messages"]
    runtime.stream_writer({"tool_says": f"hello from {runtime.tool_call_id}"})
    return f"note={note!r}; call id={runtime.tool_call_id}; {len(messages)} messages in state"


async def main(base_url: str) -> None:
    print("The model is shown:", json.dumps(convert_to_openai_tool(inspect_run)["function"]))
    # streaming=True: the mock server (like many gateways) always answers as a stream.
    model = ChatOpenAI(base_url=base_url, api_key=SecretStr("x"), model="qwen3-27b", streaming=True)
    agent = create_agent(model, tools=[inspect_run])
    async for mode, data in agent.astream(
        {"messages": [HumanMessage("Look around")]}, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            print("custom stream:", data)
        elif isinstance(data, dict) and "tools" in data:
            print("tool result  :", data["tools"]["messages"][-1].content)
        elif isinstance(data, dict) and "model" in data:
            last = data["model"]["messages"][-1]
            print("model        :", last.tool_calls or last.content)


asyncio.run(main(sys.argv[1]))
