"""Chapter 07 lab: middleware around the model call.

Adds two middlewares of our own to cmcoder's agent (`extra_middleware`, so
they are the innermost, closest to the model):

* `ShowAndRewrite.awrap_model_call` prints what the outer middleware already
  put in the request (`model_settings` from ModelProfileMiddleware), then
  sends a changed copy: a line added to the system message, a smaller
  `max_tokens`.
* `Stamp.aafter_model` replaces the model's reply in the state with a copy
  that has the *same id*, so the `add_messages` reducer swaps it in place.

The mock server's first reply writes its tool call as *text*
(`<tool_call>...</tool_call>`): TextToolCallsMiddleware turns it into a real
tool call. The second reply has `<think>` text: ReasoningMiddleware moves it
out of the answer. At the end we read what the server really received.

    python 07_model_middleware.py http://127.0.0.1:8765/v1 <project dir> <record file>

Mock server: --script labs/07_model_middleware.json --record <record file>
"""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings


class ShowAndRewrite(AgentMiddleware):
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        print(f"[wrap_model_call] {len(request.messages)} messages, {len(request.tools)} tools")
        print(f"                  model_settings from outer middleware: {request.model_settings}")
        system = request.system_message.text if request.system_message else ""
        changed = request.override(  # a copy: the original request is untouched
            system_message=SystemMessage(system + "\n\nAnswer in one short sentence."),
            model_settings={**request.model_settings, "max_tokens": 300},
        )
        response = await handler(changed)  # the model call (and nothing else: we're innermost)
        reply = response.result[0]
        calls = [c["name"] for c in getattr(reply, "tool_calls", [])]
        print(f"                  reply: {reply.text!r} tool_calls={calls}")
        return response


class Stamp(AgentMiddleware):
    async def aafter_model(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        # Same id -> add_messages replaces the reply instead of appending one.
        return {"messages": [last.model_copy(update={"content": last.text + " [checked]"})]}


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )
    agent = build_agent(
        settings,
        AgentOptions(cwd=Path(project), interactive=False),
        extra_middleware=[ShowAndRewrite(), Stamp()],
    )
    config: RunnableConfig = {"configurable": {"thread_id": "lab-07"}}
    state = await agent.graph.ainvoke(
        {"messages": [HumanMessage("what does add() do?")]}, config, context=RunContext(turn=1)
    )
    print("\nfinal state:")
    for m in state["messages"]:
        extra = m.additional_kwargs.get("reasoning_content", "")
        calls = [c["name"] for c in getattr(m, "tool_calls", [])]
        print(
            f"  {type(m).__name__:12} {m.text[:60]!r} {calls or ''} {'reasoning: ' + repr(extra) if extra else ''}"
        )
    await agent.close()


def show_record(record: str) -> None:
    sent = json.loads(Path(record).read_text().splitlines()[-1])
    print("\nwhat the server received in the last request:")
    print("  max_tokens:", sent.get("max_tokens") or sent.get("max_completion_tokens"))
    print("  system message ends with:", repr(sent["messages"][0]["content"][-30:]))
    print("  assistant message sent back:", repr(sent["messages"][2]))


asyncio.run(main(sys.argv[1], sys.argv[2]))
show_record(sys.argv[3])
