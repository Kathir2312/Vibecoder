"""Run the agent graph for one user turn and turn what happens into protocol events.

LANGGRAPH CONCEPTS IN THIS FILE

* `graph.astream(input, config, context=..., stream_mode=[...], version="v2")`
  runs the graph and yields typed "stream parts" (`{"type", "ns", "data"}`):
    - `"messages"`: `(AIMessageChunk, metadata)` tokens as the LLM writes
      them. `metadata["langgraph_node"]` says which node made the call; we only
      show tokens from the agent's `"model"` node (not the summariser's).
    - `"updates"`: `{node_name: state_update}` after each node finishes. This
      is where finished `AIMessage`s (with tool calls), `ToolMessage`s (tool
      results), todo-list changes and compactions appear.
    - `"tasks"`: a node starting (and finishing). We use the start of the
      `"tools"` node to show tool calls before they run.
* `config = {"configurable": {"thread_id": ...}}`: the thread id is the key
  the checkpointer stores the conversation under. Same thread id = same
  conversation, so we never resend the history ourselves.
* Human-in-the-loop: when a middleware calls `interrupt()`, the stream ends
  early. `await graph.aget_state(config)` then lists the pending interrupts in
  `state.interrupts`. We ask the user, and continue with
  `graph.astream(Command(resume={interrupt_id: answer}), config)`. Answering
  by interrupt id matters when several tool calls are waiting at once (the
  model asked for parallel calls).
* Reading and writing state from outside a run: `aget_state(config)` returns
  the latest `StateSnapshot` (`.values`, `.next`, `.interrupts`);
  `aupdate_state(config, values)` writes to it as if a node had returned
  `values`, through the same reducers.
* Time travel (`/rewind`): `aget_state_history(config)` lists every saved
  checkpoint, newest first. Calling `aupdate_state` with an *older*
  snapshot's config forks the thread there: the new checkpoint becomes the
  thread's latest, and the newer ones stay in the database untouched.
* `add_messages`: the reducer the graph uses for its `messages` key (append,
  replace by id, `RemoveMessage`). We use the same function to keep our own
  running copy of the conversation during a turn.

The protocol event types (protocol/events.py) are the same ones cmcoderagent
sends, so the terminal UI and all IDE clients keep working.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import add_messages
from langgraph.types import Command

from .. import __version__
from ..agents.context import RunContext
from ..agents.main import Agent
from ..core.checkpoints import Checkpoints
from ..images import Image
from ..middleware.compaction import CompactionBlocked, CompactionMiddleware, is_summary
from ..middleware.permission import PERMISSION_KIND, STOPPED, stopped
from ..middleware.tool_text import ToolCallHoldback
from ..middleware.vision import DESCRIPTIONS_KEY, describe
from ..models.chat import REASONING_KEY
from ..models.errors import classify
from ..models.reasoning import ThinkSplitter, split_think
from ..protocol import events as ev
from .store import SessionStore, sessions_dir
from .titles import fallback_title, make_title

# Called with a permission request; returns {"allow", "remember", "feedback"}.
AskFn = Callable[[ev.PermissionRequest], Awaitable[dict[str, Any]]]

MODEL_NODE = "model"  # the node name create_agent gives the LLM call
TOOLS_NODE = "tools"
# The IDE context note (core/ide.py) is left out when a conversation is shown again.
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
DENIED_PREFIX = "Permission denied: "  # middleware/permission.py
# Stored on each user message, so turn numbers survive --resume.
TURN_KEY = "cmcoder_turn"


def _text(message: AnyMessage) -> str:
    return message.content if isinstance(message.content, str) else message.text


# What this cmcoder can do beyond the base protocol (system_init.features): "images" in
# a message; "stopTool" ends one running command (`stop_tool`); "sessionRules" takes an
# "always allow" for the session only (`remember_session`); "agents" lists the agents and
# switches the conversation to one (`agent_list`, `set_agent`). An IDE panel shows a control
# only when this list and the IDE's own (data-features) both have it.
FEATURES = ("images", "stopTool", "sessionRules", "agents")

# What LangChain's ModelCallLimitMiddleware (exit_behavior="end") says as the turn's last message
# when `maxTurns` is used up. It reads like an answer, so the turn is reported as a stop instead
# (`limit_stop`), with what to do about it.
MODEL_LIMIT_TEXT = "Model call limits exceeded"


def limit_stop(calls: int, max_turns: int) -> str:
    """What the user is told when a request used all of its model calls."""
    more = min(max(max_turns * 2, 100), 1000)
    return (
        f"Stopped after {calls or max_turns} model calls, the limit for one request (maxTurns). "
        f'The work so far is kept: say "continue" to go on with a fresh budget, or raise the '
        f"limit now with /turns {more} (maxTurns in your settings changes it for good)."
    )


class Session:
    """One conversation (= one LangGraph thread) with the agent."""

    def __init__(
        self,
        agent: Agent,
        *,
        ask: AskFn | None = None,
        session_id: str | None = None,
        store: SessionStore | None = None,
    ):
        self.agent = agent
        self.ask = ask
        self.store = store
        self.turn = 0  # the number of the last user turn
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.cost = 0.0  # USD, when the gateway says (LiteLLM)
        self._titled = False
        self._session_started = False  # SessionStart hooks have run
        self._background: set[asyncio.Task[Any]] = set()
        self._use_thread(session_id or uuid.uuid4().hex)
        if agent.telemetry is not None:  # counted when it starts, even without a turn
            agent.telemetry.session_started(agent.frontend, self.session_id)

    def run_config(self) -> RunnableConfig:
        """The config for running the graph: the thread, and LangGraph's safety limit on graph
        steps. The limit is sized from the graph that is *in use now* (agents/main.py
        `recursion_limit_for`: every middleware hook is a step), so it is read at each run: the
        agent is rebuilt when an agent, a model or IDE tools are chosen, and a limit worked out for
        the old graph could be too small for the new one. What ends a turn that goes on is
        `maxTurns` (ModelCallLimitMiddleware); this only catches a graph that never stops."""
        limit = self.agent.recursion_limit or self.agent.max_turns * 12 + 25
        return {**self.config, "recursion_limit": limit}

    # --- threads ---------------------------------------------------------------
    def _use_thread(self, thread_id: str) -> None:
        self.session_id = thread_id
        self.config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        folder = None
        if self.store is not None and self.store.persistent:
            folder = sessions_dir(self.agent.ctx.project_root) / f"{thread_id}.checkpoints"
        self.checkpoints = Checkpoints(folder)

    async def resume(self, thread_id: str) -> None:
        """Continue a saved conversation: just point at its thread."""
        self._use_thread(thread_id)
        messages = await self.messages()
        self.turn = max((int(m.additional_kwargs.get(TURN_KEY, 0)) for m in messages), default=0)
        self._titled = bool(messages)  # a resumed session keeps its title

    async def clear(self) -> None:
        """`/clear`: start a new conversation (the old one stays saved)."""
        self._use_thread(uuid.uuid4().hex)
        self.turn = 0
        self._titled = False

    async def messages(self) -> list[AnyMessage]:
        snapshot = await self.agent.graph.aget_state(self.config)
        return list(snapshot.values.get("messages", [])) if snapshot.values else []

    async def todos(self) -> list[dict[str, Any]]:
        snapshot = await self.agent.graph.aget_state(self.config)
        return list(snapshot.values.get("todos") or []) if snapshot.values else []

    async def close(self) -> None:
        """Wait for background jobs (titles) before the program exits."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def init_event(self) -> ev.SystemInit:
        """`system_init`: the session, and what this cmcoder can do beyond the
        base protocol (`features`; IDE panels show the image button for "images")."""
        a = self.agent
        return ev.SystemInit(
            session_id=self.session_id,
            cwd=str(a.ctx.cwd),
            model=a.bundle.name,
            provider=a.bundle.provider,
            tools=[
                *a.tools,
                # (an agent has the todo list and the Task tool only if its tools list names them)
                *(["write_todos", "Task"] if a.persona is None else []),
                *(["Team"] if a.team and a.persona is None else []),
            ],
            permission_mode=a.policy.mode,
            version=__version__,
            features=list(FEATURES),
        )

    # --- one user turn -----------------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        note: str | None = None,
        images: list[Image] | None = None,
        allow: list[str] | None = None,
    ) -> AsyncIterator[ev.Event]:
        """Run one user turn; yields protocol events, ending with a `result` event.

        `note`: context for the model that isn't part of the prompt (the IDE's
        open file, selection and problems), sent before it in the same message.
        `images`: sent as image content blocks when the model can see images.
        `allow`: permission rules for this turn only (a custom command's `allowed-tools`).
        """
        self.agent.policy.set_turn_allow(allow or [])
        telemetry = self.agent.telemetry
        if telemetry is not None:
            telemetry.turn_started(self.session_id)
        try:
            async for event in self._run(prompt, note=note, images=images):
                if telemetry is not None:
                    telemetry.observe(event, self.agent.bundle.name, self.session_id)
                yield event
        finally:
            self.agent.policy.set_turn_allow([])

    async def _run(
        self, prompt: str, *, note: str | None, images: list[Image] | None
    ) -> AsyncIterator[ev.Event]:
        started = time.monotonic()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        # Hooks that run outside the graph: SessionStart (once) and
        # UserPromptSubmit (may block the prompt or add context).
        extra: list[str] = []
        if not self._session_started:
            for w in self.agent.startup_warnings:  # e.g. a sandbox that can't run here
                yield ev.Warning(message=w)
        hooks = self.agent.hooks
        if hooks is not None:
            opts = self.agent.spec.get("opts")
            approve = opts.approve_hook if opts is not None else None
            if not self._session_started and hooks.has("SessionStart"):
                out = await hooks.run(
                    "SessionStart",
                    {"source": "resume" if self.turn else "startup"},
                    approve_fn=approve,
                )
                extra += out.context
                for w in out.warnings:
                    yield ev.Warning(message=w)
            if hooks.has("UserPromptSubmit"):
                out = await hooks.run("UserPromptSubmit", {"prompt": prompt}, approve_fn=approve)
                for w in out.warnings:
                    yield ev.Warning(message=w)
                if out.block:
                    reason = f"A UserPromptSubmit hook blocked the prompt: {out.reason}"
                    yield ev.Error(kind="hook", message=reason)
                    yield ev.Result(
                        subtype="error",
                        is_error=True,
                        result=reason,
                        num_turns=0,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        usage=usage,
                        session_id=self.session_id,
                    )
                    return
                extra += out.context
        self._session_started = True
        if extra:
            note = "\n\n".join(x for x in (note, *extra) if x)
        text = f"{note}\n\n{prompt}" if note else prompt
        content: str | list[str | dict[str, Any]] = text
        kwargs: dict[str, Any] = {}
        if images:
            # LangChain's standard multimodal content: a list of blocks.
            blocks = [{"type": "image_url", "image_url": {"url": i.data_url}} for i in images]
            content = [{"type": "text", "text": text}, *blocks]
            if not self.agent.bundle.profile.vision:
                # A model that can't see images gets descriptions (middleware/vision.py),
                # written now; without them the turn doesn't start.
                failure: str | None = None
                async for event in self._describe(blocks, prompt, kwargs):
                    if isinstance(event, ev.Error):
                        failure = event.message
                    yield event
                if failure is not None:
                    yield ev.Result(
                        subtype="error",
                        is_error=True,
                        result=failure,
                        num_turns=0,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        usage=usage,
                        session_id=self.session_id,
                    )
                    return
        self.turn += 1
        context = RunContext(turn=self.turn, checkpoints=self.checkpoints)
        critic = self.agent.critic
        # With the critic on, the answer is held until it is reviewed: the
        # user sees only the final one, not drafts the critic sent back.
        state = _TurnState(await self.messages(), hold_answer=bool(critic and critic.enabled))
        message = HumanMessage(content, additional_kwargs={TURN_KEY: self.turn, **kwargs})
        state.apply([message])
        graph_input: Any = {"messages": [message]}
        error: ev.Error | None = None
        step_limit = False  # the graph's safety limit ended the turn
        try:
            while True:
                async for event in self._stream(graph_input, context, state, usage):
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
                        cwd=value.get("cwd"),
                        reasons=list(value.get("reasons") or []),
                        high_risk=bool(value.get("high_risk")),
                        agent=value.get("agent"),
                    )
                    yield request
                    if hooks is not None and hooks.has("Notification"):
                        msg = f"cmcoder needs your permission to use {request.label}"
                        task = asyncio.create_task(hooks.run("Notification", {"message": msg}))
                        self._background.add(task)  # type: ignore[arg-type]
                        task.add_done_callback(self._background.discard)  # type: ignore[arg-type]
                    answer = await self.ask(request)
                    answers[intr.id] = answer
                    if not answer.get("allow"):
                        yield ev.PermissionDenied(
                            id=request.tool_use_id, name=request.name, reason="denied by user"
                        )
                # Resume the paused graph with the user's answers.
                graph_input = Command(resume=answers)
        except asyncio.CancelledError:
            await self.repair_after_interrupt()
            raise
        except GraphRecursionError:
            # Not a crash: the graph ran on past its step budget (a safety net above
            # `maxTurns`). The conversation is intact; saying "continue" starts a new budget.
            step_limit = True
            yield ev.Warning(
                message=f"Stopped: the agent used its whole step budget "
                f"({self.run_config().get('recursion_limit')} graph steps) without finishing. "
                'Say "continue" to go on, or raise maxTurns in your settings.'
            )
            await self.repair_after_interrupt()
        except Exception as e:
            error = classify(e)
            yield error
            await self.repair_after_interrupt()
        if state.held is not None:
            yield ev.AssistantDelta(text=state.held.text)
            yield state.held
        for key in usage:
            self.usage[key] += usage[key]
        # A user's "Deny" ended the turn (middleware/permission.py).
        denied = error is None and stopped(await self.messages())
        calls_limit = (
            error is None and not step_limit and state.final_text.startswith(MODEL_LIMIT_TEXT)
        )
        await self._after_turn(prompt)
        yield ev.Result(
            subtype="error"
            if error
            else "max_turns"
            if step_limit or calls_limit
            else "interrupted"
            if denied
            else "success",
            is_error=error is not None or step_limit or calls_limit,
            result=error.message
            if error
            else "Stopped at the step limit."
            if step_limit
            else limit_stop(state.model_calls, self.agent.max_turns)
            if calls_limit
            else STOPPED
            if denied
            else state.final_text,
            review=state.review,
            num_turns=state.model_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            usage=usage,
            session_id=self.session_id,
        )

    async def _after_turn(self, prompt: str) -> None:
        if self.store is None:
            return
        await self.store.touch(
            self.session_id, cwd=str(self.agent.ctx.cwd), model=self.agent.bundle.ref
        )
        if not self._titled:
            self._titled = True
            thread_id = self.session_id
            store = self.store
            if not self.agent.has_small_model:
                # No small model: don't spend a main-model call on a title.
                await store.set_title(thread_id, fallback_title(prompt))
                return

            async def title() -> None:
                await store.set_title(thread_id, await make_title(self.agent.small.quick(), prompt))

            task = asyncio.create_task(title())  # in the background: nothing waits for it
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _describe(
        self, blocks: list[dict[str, Any]], prompt: str, kwargs: dict[str, Any]
    ) -> AsyncIterator[ev.Event]:
        """The vision model's descriptions of the user's images, into `kwargs`."""
        vision = self.agent.vision
        if vision is None:
            yield ev.Error(
                kind="images",
                message=f"{self.agent.bundle.name} can't see images, and no visionModel is set "
                'to describe them: set "visionModel" in your settings to a model on your '
                "gateway that sees images (a Qwen-VL, for example).",
            )
            return
        yield ev.ImagesDescribing(model=vision.name, count=len(blocks))
        try:
            descriptions = [
                await describe(vision.quick(), vision.name, block, prompt) for block in blocks
            ]
        except Exception as e:  # VisionError, or the gateway refused
            yield ev.Error(
                kind="images",
                message=f"The vision model {vision.name} couldn't describe the image: {e}",
            )
            return
        kwargs[DESCRIPTIONS_KEY] = descriptions
        yield ev.ImagesDescribed(model=vision.name, count=len(blocks))

    async def _stream(
        self, graph_input: Any, context: RunContext, state: _TurnState, usage: dict[str, int]
    ) -> AsyncIterator[ev.Event]:
        config: RunnableConfig = self.run_config()
        if self.agent.telemetry is not None:
            # A LangChain callback handler: it sees every model call and tool
            # run of this turn, the subagents' too (telemetry.py).
            config = {**config, "callbacks": [self.agent.telemetry.callback(self.session_id)]}
        async for part in self.agent.graph.astream(
            graph_input,
            config,
            context=context,
            # "tasks" says when a node *starts*: the tools node starting means
            # the model's reply is final, so its tool calls can be shown now,
            # before a long command finishes.
            # "custom": what middleware writes with get_stream_writer() (warnings).
            stream_mode=["messages", "updates", "tasks", "custom"],
            version="v2",
        ):
            if part["type"] == "messages":
                chunk, meta = part["data"]
                # Only the main agent's own tokens: a subagent's LLM calls run
                # inside a tool, under a nested namespace ("tools:…|model:…").
                nested = "|" in str(meta.get("langgraph_checkpoint_ns", ""))
                if (
                    isinstance(chunk, AIMessageChunk)
                    and meta.get("langgraph_node") == MODEL_NODE
                    and not nested
                ):
                    for event in state.on_token(chunk):
                        yield event
                continue
            if part["type"] == "custom":
                data = part["data"]
                if isinstance(data, dict) and data.get("type") == "warning":
                    yield ev.Warning(message=str(data.get("message")))
                elif isinstance(data, dict) and data.get("type") == "event":
                    # A subagent's status and tool calls (agents/subagents.py),
                    # and the critic's verdict (middleware/critic.py).
                    event = ev.parse_event(data["event"])
                    if isinstance(event, ev.ReviewResult) and event.final:
                        state.review = {
                            "verdict": event.verdict,
                            "rounds": event.round,
                            "issues": [i.model_dump() for i in event.issues],
                        }
                    yield event
                continue
            if part["type"] == "tasks":
                data = part["data"]
                if data.get("name") == TOOLS_NODE and "result" not in data:
                    for event in state.flush():
                        yield event
                continue
            if part["type"] != "updates":
                continue
            for node, update in part["data"].items():
                if node == "__interrupt__" or not isinstance(update, dict):
                    continue
                if node == TOOLS_NODE:
                    for event in state.flush():
                        yield event
                if update.get("todos") is not None:  # TodoListMiddleware's write_todos
                    yield ev.TodoUpdate(todos=[dict(t) for t in update["todos"]])
                messages = list(update.get("messages") or [])
                if any(isinstance(m, RemoveMessage) for m in messages):
                    yield state.compacted(messages, "auto", self.agent.small.name)
                    continue
                state.apply(messages)
                for msg in messages:
                    if isinstance(msg, AIMessage):
                        if node == MODEL_NODE:
                            for event in state.flush():  # a new reply: finish the last one
                                yield event
                            state.model_calls += 1
                            if msg.usage_metadata:
                                usage["prompt_tokens"] += msg.usage_metadata["input_tokens"]
                                usage["completion_tokens"] += msg.usage_metadata["output_tokens"]
                                cost = msg.response_metadata.get("cost")
                                self.cost += cost or 0.0  # LiteLLM's cost header, in USD
                                yield ev.UsageUpdate(
                                    prompt_tokens=msg.usage_metadata["input_tokens"],
                                    completion_tokens=msg.usage_metadata["output_tokens"],
                                    context_window=self.agent.bundle.profile.context_window,
                                    cost=cost,
                                )
                        # After-model middlewares (repair, <think> split) send a
                        # fixed copy with the same id: the latest one wins.
                        state.pending_ai = msg
                    elif isinstance(msg, ToolMessage):
                        text = _text(msg)
                        if text.startswith(DENIED_PREFIX):  # a rule or mode said no
                            yield ev.PermissionDenied(
                                id=msg.tool_call_id,
                                name=msg.name or "",
                                reason=text[len(DENIED_PREFIX) :],
                            )
                        yield _tool_result_event(msg)

    async def repair_after_interrupt(self) -> None:
        """Answer tool calls left without results, so the next turn is valid.

        OpenAI-style APIs reject a history where an assistant message with tool
        calls isn't followed by a result for each call.
        """
        messages = await self.messages()
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        missing = [
            ToolMessage(
                content="Interrupted by the user before this ran.",
                tool_call_id=c["id"] or "",
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

    # --- history (shown again by an IDE after --resume / a rewind) -----------------
    def label(self, call: ToolCall) -> str:
        """The UI label of a tool call, e.g. `Read(src/app.py)`."""
        if call["name"] == "write_todos":
            todos = call.get("args", {}).get("todos") or []
            done = sum(t.get("status") == "completed" for t in todos if isinstance(t, dict))
            return f"write_todos({done}/{len(todos)} done)"
        tool = self.agent.tools.get(call["name"])
        if tool is not None:
            try:
                return tool.describe(tool.Input.model_validate(call["args"]), self.agent.ctx)
            except ValueError:
                pass
        return _label(call)

    async def history(self) -> list[ev.HistoryItem]:
        items: list[ev.HistoryItem] = []
        for m in await self.messages():
            if isinstance(m, HumanMessage):
                text = REMINDER.sub("", _text(m)).strip()
                if text and not is_summary(m):
                    items.append(ev.HistoryItem(role="user", text=text))
            elif isinstance(m, AIMessage):
                visible, _ = split_think(_text(m))
                if visible.strip():
                    items.append(ev.HistoryItem(role="assistant", text=visible.strip()))
                for call in m.tool_calls:
                    items.append(ev.HistoryItem(role="tool", text=self.label(call)))
        return items

    # --- /compact ------------------------------------------------------------------
    async def compact(self, focus: str | None = None) -> ev.Compacted | None:
        """Summarise the conversation now, with the same middleware auto-compaction uses.

        Raises `CompactionBlocked` when a PreCompact hook declines."""
        messages = await self.messages()
        if len(messages) < 3:
            return None
        opts = self.agent.spec.get("opts")
        mw = CompactionMiddleware(
            self.agent.bundle.model,
            self.agent.small.quick(),
            always=True,
            focus=focus,
            hooks=self.agent.hooks,  # PreCompact runs for /compact too (trigger "manual")
            approve_hook=opts.approve_hook if opts is not None else None,
        )
        # Middleware hooks are plain async methods: callable outside a graph too.
        update = await mw.abefore_model({"messages": messages}, cast(Any, None))
        if mw.blocked is not None:
            raise CompactionBlocked(mw.blocked)
        if not update:
            return None
        await self.agent.graph.aupdate_state(self.config, update)
        return _TurnState(messages).compacted(update["messages"], "manual", self.agent.small.name)

    # --- /rewind ---------------------------------------------------------------------
    async def rewind_points(self) -> list[ev.RewindPoint]:
        root = self.agent.ctx.project_root
        points = []
        for m in await self.messages():
            turn = m.additional_kwargs.get(TURN_KEY) if isinstance(m, HumanMessage) else None
            if not turn or is_summary(m):
                continue
            changes = self.checkpoints.changes_since(int(turn))
            outside = [e.path for e in changes if not Path(e.path).is_relative_to(root)]
            text = " ".join(_text(m).split())[:80]
            points.append(
                ev.RewindPoint(
                    turn=int(turn), text=text, files_changed=len(changes), outside_files=outside
                )
            )
        return points

    async def rewind(
        self, turn: int, *, code: bool = True, conversation: bool = True, outside: bool = False
    ) -> ev.Rewound:
        """Go back to just before user turn `turn`: files, conversation, or both."""
        actions: list[ev.RewindAction] = []
        if code:
            root = self.agent.ctx.project_root
            for a in self.checkpoints.restore(turn, include_outside=None if outside else root):
                actions.append(ev.RewindAction(path=str(a.path), action=a.action))
        prompt: str | None = None
        if conversation:
            target = next(
                (
                    m
                    for m in await self.messages()
                    if isinstance(m, HumanMessage) and m.additional_kwargs.get(TURN_KEY) == turn
                ),
                None,
            )
            if target is not None:
                prompt = _text(target)
                await self._fork_before(target.id or "")
                self.turn = turn - 1
        return ev.Rewound(code=code, conversation=conversation, actions=actions, prompt=prompt)

    async def _fork_before(self, message_id: str) -> None:
        """Make the last checkpoint without `message_id` the thread's latest state."""
        async for snapshot in self.agent.graph.aget_state_history(self.config):
            ids = {m.id for m in snapshot.values.get("messages", [])} if snapshot.values else set()
            if message_id not in ids and not snapshot.next:
                # Writing to an older checkpoint forks the thread from there.
                await self.agent.graph.aupdate_state(snapshot.config, {"messages": []})
                return
        # Nothing before it (the first turn): continue in a fresh thread.
        self._use_thread(uuid.uuid4().hex)
        self._titled = False


class _TurnState:
    """Bookkeeping while the stream runs: tokens, the reply in progress, the messages."""

    def __init__(self, messages: list[AnyMessage], *, hold_answer: bool = False) -> None:
        self.messages: list[AnyMessage] = list(messages)
        self.hold_answer = hold_answer
        self.held: ev.AssistantMessage | None = None  # the latest draft answer
        self.review: dict[str, Any] | None = None  # the critic's final verdict
        self.splitters: dict[str, ThinkSplitter] = {}
        self.holdback = ToolCallHoldback()
        self.pending_ai: AIMessage | None = None
        self.final_text = ""
        self.model_calls = 0

    def apply(self, update: list[Any]) -> None:
        self.messages = cast(list[AnyMessage], add_messages(cast(Any, self.messages), update))

    def compacted(self, update: list[Any], trigger: str, model: str) -> ev.Compacted:
        before = self.messages
        self.apply(update)
        kept = [m for m in self.messages if not is_summary(m)]
        return ev.Compacted(
            trigger="auto" if trigger == "auto" else "manual",
            summarized_messages=max(0, len(before) - len(kept)),
            tokens_before=count_tokens_approximately(before),
            tokens_after=count_tokens_approximately(self.messages),
            model=model,
        )

    def on_token(self, chunk: AIMessageChunk) -> list[ev.Event]:
        out: list[ev.Event] = []
        reasoning = chunk.additional_kwargs.get(REASONING_KEY)
        if reasoning:
            out.append(ev.ReasoningDelta(text=reasoning))
        text = chunk.content if isinstance(chunk.content, str) else chunk.text
        if text:
            splitter = self.splitters.setdefault(chunk.id or "", ThinkSplitter())
            for kind, piece in splitter.feed(text):
                if kind == "reasoning":
                    out.append(ev.ReasoningDelta(text=piece))
                elif self.hold_answer:
                    continue  # shown when the review is done
                elif shown := self.holdback.feed(piece):  # hide text tool calls
                    out.append(ev.AssistantDelta(text=shown))
        return out

    def flush(self) -> list[ev.Event]:
        """Emit the finished assistant message and its tool calls."""
        msg, self.pending_ai = self.pending_ai, None
        if msg is None:
            return []
        out: list[ev.Event] = []
        for splitter in self.splitters.values():
            for kind, piece in splitter.flush():
                if kind == "reasoning":
                    out.append(ev.ReasoningDelta(text=piece))
                elif shown := self.holdback.feed(piece):
                    out.append(ev.AssistantDelta(text=shown))
        if rest := self.holdback.flush():
            out.append(ev.AssistantDelta(text=rest))
        self.splitters.clear()
        visible, think = split_think(_text(msg))
        reasoning = "\n".join(
            x for x in (str(msg.additional_kwargs.get(REASONING_KEY) or ""), think) if x
        )
        self.final_text = visible
        message = ev.AssistantMessage(
            text=visible,
            reasoning=reasoning,
            tool_calls=[dict(c) for c in msg.tool_calls],
            model=msg.response_metadata.get("model_name"),
        )
        if self.hold_answer and not msg.tool_calls:
            out = [e for e in out if not isinstance(e, ev.AssistantDelta)]
            self.held = message  # a later draft replaces it
            return out
        out.append(message)
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


def _artifact_images(artifact: object) -> list[str]:
    """The pictures a tool left for the chat (its message's `artifact`), as data URLs.
    Only image files are read, shrunk like pictures the user attaches (images.py)."""
    from ..images import ImageError, prepare

    paths = artifact.get("images") if isinstance(artifact, dict) else None
    urls: list[str] = []
    for path in paths if isinstance(paths, list) else []:
        try:
            urls.append(prepare(Path(str(path)).read_bytes(), Path(str(path)).name).data_url)
        except (OSError, ImageError):  # gone (the session's temp folder) or not an image
            continue
    return urls


def _tool_result_event(msg: ToolMessage) -> ev.Event:
    content = msg.content if isinstance(msg.content, str) else msg.text
    meta = msg.artifact.get("meta") if isinstance(msg.artifact, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    return ev.ToolResult(
        id=msg.tool_call_id,
        name=msg.name or "",
        content=content,
        is_error=msg.status == "error",
        images=_artifact_images(msg.artifact),
        exit_code=meta.get("exit_code"),
        duration_ms=meta.get("duration_ms"),
        total_lines=meta.get("total_lines"),
        stopped=bool(meta.get("stopped")),
    )
