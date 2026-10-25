"""Chapter 12 lab: structured output with create_agent and ToolStrategy.

Plain LangChain (no cmcoder code): the model is given a tool shaped like the
`Verdict` schema; when it calls that tool the run ends and the parsed object is
in `state["structured_response"]`. That's how cmcoder's critic and the team's
PM and tester hand back results the code can route on.

    python 12_structured_output.py http://127.0.0.1:8765/v1

Script: 12_verdict.json (one reply: a Verdict tool call).
"""

import sys
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr


class Verdict(BaseModel):
    """Give your verdict on the code: pass, or fail with the problems."""

    verdict: Literal["pass", "fail"]
    summary: str = Field(description="One sentence: why")
    issues: list[str] = Field(default_factory=list, description="For fail: each problem")


# streaming=True: the mock server always answers with a stream (as cmcoder asks)
model = ChatOpenAI(base_url=sys.argv[1], api_key=SecretStr("x"), model="qwen3-27b", streaming=True)
agent = create_agent(model, tools=[], response_format=ToolStrategy(Verdict))
state = agent.invoke({"messages": [HumanMessage("Review: def add(a, b): return a - b")]})

result = state["structured_response"]
print(type(result).__name__, "->", result)
print("route on a field:", "back to the developer" if result.verdict == "fail" else "done")
print("messages:", [type(m).__name__ for m in state["messages"]])
