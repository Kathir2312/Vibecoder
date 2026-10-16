"""Run the agent graph for one user turn and turn what happens into protocol events.

LANGGRAPH CONCEPTS IN THIS FILE

* `graph.astream(input, config, stream_mode=[...], version="v2")` runs the
  graph and yields typed "stream parts" (`{"type", "ns", "data"}`):
    - `"messages"`: `(AIMessageChunk, metadata)` tokens as the LLM writes
      them. `metadata["langgraph_node"]` says which node made the call; we only
      show tokens from the agent's `"model"` node.
    - `"updates"`: `{node_name: state_update}` after each node finishes. This
      is where finished `AIMessage`s (with tool calls) and `ToolMessage`s
      (tool results) appear.
* `config = {"configurable": {"thread_id": ...}}`: the thread id is the key
  the checkpointer stores the conversation under. Same thread id = same
  conversation, so we never resend the history ourselves.
* Human-in-the-loop: when a middleware calls `interrupt()`, the stream ends
  early. `await graph.aget_state(config)` then lists the pending interrupts in
  `state.interrupts`. We ask the user, and continue with
  `graph.astream(Command(resume={interrupt_id: answer}), config)`. Answering
  by interrupt id matters when several tool calls are waiting at once (the
  model asked for parallel calls).

The protocol event types (protocol/events.py) are the same ones cmcoderagent
sends, so the terminal UI and all IDE clients keep working.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from .. import __version__
from ..agents.main import Agent
from ..middleware.permission import PERMISSION_KIND
from ..models.chat import REASONING_KEY
from ..models.errors import classify
from ..models.reasoning import ThinkSplitter, split_think
from ..protocol import events as ev

# Called with a permission request; returns {"allow", "remember", "feedback"}.
AskFn = Callable[[ev.PermissionRequest], Awaitable[dict[str, Any]]]

MODEL_NODE = "model"  # the node name create_agent gives the LLM call
TOOLS_NODE = "tools"


def _text(message: AIMessage) -> str:
    return message.content if isinstance(message.content, str) else message.text


class Session:
    """One conversation (= one LangGraph thread) with the agent."""

    def __init__(self, agent: Agent, *, ask: AskFn | None = None, session_id: str | None = None):
        self.agent = agent
        self.ask = ask
        self.session_id = session_id or uuid.uuid4().hex
        self.config: RunnableConfig = {
            "configurable": {"thread_id": self.session_id},
            # LangGraph's own safety limit on graph steps (each model call and
            # each tool call is a step); the turn limit is ModelCallLimitMiddleware.
            "recursion_limit": agent.max_turns * 4 + 20,
        }

    def init_event(self) -> ev.SystemInit:
        a = self.agent
        return ev.SystemInit(
            session_id=self.session_id,
            cwd=str(a.ctx.cwd),
            model=a.bundle.name,
            provider=a.bundle.provider,
            tools=list(a.tools),
            permission_mode=a.policy.mode,
            version=__version__,
        )

    async def run(self, prompt: str) -> AsyncIterator[ev.Event]:
        """Run one user turn; yields protocol events, ending with a `result` event."""
        started = time.monotonic()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        state = _TurnState()
        graph_input: Any = {"messages": [HumanMessage(prompt)]}
        error: ev.Error | None = None
        try:
            while True:
                async for event in self._stream(graph_input, state, usage):
                    yield event
                for event in state.flush():
                    yield event
                pending = (await self.agent.graph.aget_state(self.config)).interrupts
                if not pending:
                    break
                answers: dict[str, Any] = {}
                for intr in pending:
                    value = dict(intr.value)
                    if value.get("kind") != PERMISSION_KIND or self.ask is None:
                        answers[intr.id] = {"allow": False}
                        continue
                    request = ev.PermissionRequest(
                        request_id=intr.id,
                        tool_use_id=value["tool_use_id"],
                        name=value["name"],
                        label=value["label"],
                        input=value["input"],
                        suggested_rule=value["suggested_rule"],
                        reason=value.get("reason") or "",
                        can_remember=value.get("can_remember", True),
                        change=ev.FileChange(**value["change"]) if value.get("change") else None,
                    )
                    yield request
                    answers[intr.id] = await self.ask(request)
                # Resume the paused graph with the user's answers.
                graph_input = Command(resume=answers)
        except asyncio.CancelledError:
            await self.repair_after_interrupt()
            raise
        except Exception as e:
            error = classify(e)
            yield error
            await self.repair_after_interrupt()
        yield ev.Result(
            subtype="error" if error else "success",
            is_error=error is not None,
            result=error.message if error else state.final_text,
            num_turns=state.model_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            usage=usage,
            session_id=self.session_id,
        )

    async def _stream(
        self, graph_input: Any, state: _TurnState, usage: dict[str, int]
    ) -> AsyncIterator[ev.Event]:
        async for part in self.agent.graph.astream(
            graph_input, self.config, stream_mode=["messages", "updates"], version="v2"
        ):
            if part["type"] == "messages":
                chunk, meta = part["data"]
                if isinstance(chunk, AIMessageChunk) and meta.get("langgraph_node") == MODEL_NODE:
                    for event in state.on_token(chunk):
                        yield event
            elif part["type"] == "updates":
                for node, update in part["data"].items():
                    if node == "__interrupt__" or not isinstance(update, dict):
                        continue
                    if node == TOOLS_NODE:
                        for event in state.flush():
                            yield event
                    for msg in update.get("messages") or []:
                        if isinstance(msg, AIMessage):
                            if node == MODEL_NODE:
                                for event in state.flush():  # a new reply: finish the last one
                                    yield event
                                state.model_calls += 1
                                if msg.usage_metadata:
                                    usage["prompt_tokens"] += msg.usage_metadata["input_tokens"]
                                    usage["completion_tokens"] += msg.usage_metadata[
                                        "output_tokens"
                                    ]
                                    yield ev.UsageUpdate(
                                        prompt_tokens=msg.usage_metadata["input_tokens"],
                                        completion_tokens=msg.usage_metadata["output_tokens"],
                                        context_window=self.agent.bundle.profile.context_window,
                                    )
                            # After-model middlewares (repair, <think> split)
                            # send a fixed copy with the same id: latest wins.
                            state.pending_ai = msg
                        elif isinstance(msg, ToolMessage):
                            yield _tool_result_event(msg)

    async def repair_after_interrupt(self) -> None:
        """Answer tool calls left without results, so the next turn is valid.

        OpenAI-style APIs reject a history where an assistant message with tool
        calls isn't followed by a result for each call. `aupdate_state` writes
        to the checkpointed state directly, as if a node had returned it.
        """
        snapshot = await self.agent.graph.aget_state(self.config)
        messages = snapshot.values.get("messages", []) if snapshot.values else []
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        missing = [
            ToolMessage(
                content="Interrupted by the user before this ran.",
                tool_call_id=c["id"],
                name=c["name"],
                status="error",
            )
            for m in messages
            if isinstance(m, AIMessage)
            for c in m.tool_calls
            if c["id"] not in answered
        ]
        if missing:
            await self.agent.graph.aupdate_state(self.config, {"messages": missing})


class _TurnState:
    """Bookkeeping while the stream runs: token splitting and the reply in progress."""

    def __init__(self) -> None:
        self.splitters: dict[str, ThinkSplitter] = {}
        self.pending_ai: AIMessage | None = None
        self.final_text = ""
        self.model_calls = 0

    def on_token(self, chunk: AIMessageChunk) -> list[ev.Event]:
        out: list[ev.Event] = []
        reasoning = chunk.additional_kwargs.get(REASONING_KEY)
        if reasoning:
            out.append(ev.ReasoningDelta(text=reasoning))
        text = chunk.content if isinstance(chunk.content, str) else chunk.text
        if text:
            splitter = self.splitters.setdefault(chunk.id or "", ThinkSplitter())
            for kind, piece in splitter.feed(text):
                out.append(
                    ev.ReasoningDelta(text=piece)
                    if kind == "reasoning"
                    else ev.AssistantDelta(text=piece)
                )
        return out

    def flush(self) -> list[ev.Event]:
        """Emit the finished assistant message and its tool calls."""
        msg, self.pending_ai = self.pending_ai, None
        if msg is None:
            return []
        out: list[ev.Event] = []
        for splitter in self.splitters.values():
            out += [
                ev.ReasoningDelta(text=t) if k == "reasoning" else ev.AssistantDelta(text=t)
                for k, t in splitter.flush()
            ]
        self.splitters.clear()
        visible, think = split_think(_text(msg))
        reasoning = "\n".join(
            x for x in (str(msg.additional_kwargs.get(REASONING_KEY) or ""), think) if x
        )
        self.final_text = visible
        out.append(
            ev.AssistantMessage(
                text=visible,
                reasoning=reasoning,
                tool_calls=[dict(c) for c in msg.tool_calls],
                model=msg.response_metadata.get("model_name"),
            )
        )
        for c in msg.tool_calls:
            out.append(
                ev.ToolUse(id=c["id"] or "", name=c["name"], input=c["args"], label=_label(c))
            )
        return out


def _label(call: ToolCall) -> str:
    args = call.get("args") or {}
    target = args.get("file_path") or args.get("command") or args.get("pattern") or ""
    target = str(target).splitlines()[0][:80] if target else ""
    return f"{call['name']}({target})" if target else call["name"]


def _tool_result_event(msg: ToolMessage) -> ev.Event:
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    return ev.ToolResult(
        id=msg.tool_call_id,
        name=msg.name or "",
        content=content,
        is_error=msg.status == "error",
    )
