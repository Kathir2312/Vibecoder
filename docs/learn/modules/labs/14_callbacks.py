"""Chapter 14 lab: a LangChain callback handler watches a whole run,
the subagent's model calls and tools included.

    python 14_callbacks.py http://127.0.0.1:8765/v1 <project dir>

Mock server script: 14_subagent.json. The project needs an app.py with
`def add` in it (see the course index).

This is how cmcoder's TelemetryCallback (telemetry.py) counts tokens and tool
calls, and how LangSmith tracing works: a handler in the run's config.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import RunnableConfig

from cmcoder.agents.context import RunContext
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings


class TimingHandler(AsyncCallbackHandler):
    """Prints each model call and tool run, indented by how deeply it is nested."""

    def __init__(self) -> None:
        self.started: dict[UUID, float] = {}  # run_id -> start time
        self.depth: dict[UUID, int] = {}  # run_id -> nesting depth
        self.t0 = time.perf_counter()

    def _start(self, run_id: UUID, parent_run_id: UUID | None) -> int:
        self.started[run_id] = time.perf_counter()
        # Callbacks see every run, including the chains (graph nodes) between
        # a tool and the subagent's model; count only what we print.
        depth = self.depth.get(parent_run_id, 0) if parent_run_id else 0
        self.depth[run_id] = depth
        return depth

    def _ms(self, run_id: UUID) -> float:
        return (time.perf_counter() - self.started.pop(run_id)) * 1000

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        # Graph nodes are chains: pass the depth on without printing them.
        self.depth[run_id] = self.depth.get(parent_run_id, 0) if parent_run_id else 0

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        d = self._start(run_id, parent_run_id)
        node = (metadata or {}).get("langgraph_checkpoint_ns", "")
        where = "subagent" if "tools:" in node else "main agent"
        print(f"{'  ' * d}model start  ({where}, {len(messages[0])} messages)")

    async def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None, **kw: Any
    ) -> None:
        d = self.depth.get(run_id, 0)
        message = getattr(response.generations[0][0], "message", None)
        calls = [c["name"] for c in getattr(message, "tool_calls", [])]
        text = getattr(message, "text", "")
        what = f"tool calls {calls}" if calls else repr(text[:50])
        print(f"{'  ' * d}model end    {self._ms(run_id):6.1f} ms -> {what}")

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        d = self._start(run_id, parent_run_id)
        self.depth[run_id] = d + 1  # what runs inside this tool is nested one level
        print(f"{'  ' * d}tool start   {serialized.get('name')}")

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        d = self.depth.get(run_id, 1) - 1
        print(f"{'  ' * d}tool end     {self._ms(run_id):6.1f} ms")


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=False))
    # The handler goes in the run's config: it then sees the whole run,
    # nested graphs (the subagent inside the Task tool) included.
    config: RunnableConfig = {
        "configurable": {"thread_id": "lab-14"},
        "callbacks": [TimingHandler()],
    }
    state = await agent.graph.ainvoke(
        {"messages": [HumanMessage("where is add?")]}, config, context=RunContext(turn=1)
    )
    print("\nanswer:", state["messages"][-1].text)
    await agent.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
