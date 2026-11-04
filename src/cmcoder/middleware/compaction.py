"""Auto-compaction: summarise older turns with the small model before the window fills.

LANGCHAIN CONCEPTS IN THIS FILE

* `SummarizationMiddleware` (built in) runs `before_model`: when the
  conversation passes a threshold, it asks a model to summarise the older
  messages and rewrites the state as
  `[RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *recent messages]`.
  `RemoveMessage` is how a node *deletes* messages through the `add_messages`
  reducer, which otherwise only appends or replaces.
* `trigger=("fraction", 0.8)` and `keep=("fraction", 0.25)` are shares of the
  model's `profile["max_input_tokens"]` (set in models/chat.py).

  A trap worth knowing: the middleware counts the tokens of the *messages*
  only. The system prompt and the tool schemas (about 2.5K tokens here) take
  room in the window too, and with a 32K window that difference matters. So
  we hand it a copy of the model whose profile says "the window minus that
  fixed part".
* `summarizer=`: summarise with a different (smaller, faster) model than the
  one whose window is being managed. That's the small/fast Qwen3 here.
* The old messages aren't lost: the checkpointer still has every earlier
  checkpoint, so `/rewind` can go back to before a compaction.

What we add on top, by subclassing:
* the user's latest request is always kept word for word (requirement from
  cmcoderagent), even when it falls in the summarised part;
* `/compact [focus]` runs the same summarisation by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentState, SummarizationMiddleware
from langchain.agents.middleware.summarization import DEFAULT_SUMMARY_PROMPT
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from ..core.hooks import ApproveHookFn, HookRunner
from .vision import images_as_text

# Recognised by the mock server in tests; harmless to real models.
COMPACTION_TAG = "<cmcoder-compaction>"
SUMMARY_SOURCE = "summarization"  # LangChain marks its summary message with this
KEEP_FRACTION = 0.25


def is_summary(m: AnyMessage) -> bool:
    return isinstance(m, HumanMessage) and m.additional_kwargs.get("lc_source") == SUMMARY_SOURCE


def latest_request(messages: list[AnyMessage]) -> HumanMessage | None:
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not is_summary(m):
            return m
    return None


@dataclass
class _Pending:
    """A summary made to get one request through (`emergency`), waiting to be
    written into the conversation at the next step."""

    summarised: set[str | None]  # ids of the messages the summary replaces
    head: list[AnyMessage]  # the summary, and the latest request word for word


class CompactionBlocked(Exception):
    """`/compact` was asked for, and a PreCompact hook said no (the message says why)."""


class CompactionMiddleware(SummarizationMiddleware):
    """LangChain's summarisation, keeping the latest user request verbatim."""

    def __init__(
        self,
        model: BaseChatModel,
        summarizer: BaseChatModel,
        *,
        threshold: float = 0.8,
        overhead_tokens: int = 0,
        focus: str | None = None,
        always: bool = False,
        hooks: HookRunner | None = None,
        approve_hook: ApproveHookFn | None = None,
    ) -> None:
        self.overhead_tokens = overhead_tokens
        self.hooks = hooks
        self.approve_hook = approve_hook
        # Why the last attempt didn't compact, when a PreCompact hook said no.
        self.blocked: str | None = None
        self.pending: _Pending | None = None
        self.trigger_name = "manual" if always else "auto"
        prompt = f"{COMPACTION_TAG}\n{DEFAULT_SUMMARY_PROMPT}"
        if focus:
            # `{messages}` is filled in by LangChain; keep it last.
            head, tail = prompt.rsplit("<messages>", 1)
            safe = focus.replace("{", "{{").replace("}", "}}")  # the prompt is a format string
            prompt = f"{head}Pay special attention to: {safe}\n\n<messages>{tail}"
        super().__init__(
            model.model_copy(),  # its own copy: the profile is changed below
            # `always`: /compact by hand, whatever the size.
            trigger=("messages", 2) if always else ("fraction", threshold),
            # By hand, keep only the last exchange; automatically, the newest quarter.
            keep=("messages", 2) if always else ("fraction", KEEP_FRACTION),
            summarizer=summarizer,
            summary_prompt=prompt,
        )

        window = (model.profile or {}).get("max_input_tokens")
        if window:
            self.set_window(window)

    def set_window(self, window: int) -> None:
        """The room messages really have: the window minus the system prompt and tools."""
        room = max(1024, window - self.overhead_tokens)
        self.model.profile = {**(self.model.profile or {}), "max_input_tokens": room}

    def _trim_messages_for_summary(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        # The summary prompt is text: images go in as their descriptions
        # (middleware/vision.py), never as base64.
        return super()._trim_messages_for_summary(images_as_text(messages))

    async def emergency(self, messages: list[AnyMessage]) -> list[AnyMessage] | None:
        """`messages` with everything but the last exchange summarised, for a request
        the server refused as too long (middleware/model.py retries with this).

        The threshold of `abefore_model` is an estimate, and the estimate can be
        low (code, JSON); this is the safety net behind it. The summary is kept,
        and `abefore_model` writes it into the conversation at the next step, so
        the next request doesn't go through the same trouble. None: nothing worth
        summarising, or a PreCompact hook declined."""
        self._ensure_message_ids(messages)
        cutoff = self._find_safe_cutoff(messages, 2)  # keep the last exchange
        if cutoff <= 0:
            return None
        to_summarize, preserved = self._partition_messages(messages, cutoff)
        request = latest_request(messages)
        if request is not None and {m.id for m in to_summarize} <= {request.id}:
            return None  # only the current request would go: nothing to gain
        if self.hooks is not None and self.hooks.has("PreCompact"):
            outcome = await self.hooks.run(
                "PreCompact", {"trigger": "overflow"}, approve_fn=self.approve_hook
            )
            if outcome.block:
                return None
        head: list[AnyMessage] = list(
            self._build_new_messages(await self._acreate_summary(to_summarize))
        )
        if request is not None and request.id not in {m.id for m in preserved}:
            head.append(request.model_copy())  # the latest request, word for word
        self.pending = _Pending({m.id for m in to_summarize}, head)
        return [*head, *preserved]

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        pending, self.pending = self.pending, None
        if pending is not None and pending.summarised <= {m.id for m in state["messages"]}:
            # An emergency summary got the last request through: make it permanent.
            rest = [m for m in state["messages"] if m.id not in pending.summarised]
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *pending.head, *rest]}
        # LangChain's own steps (SummarizationMiddleware.abefore_model), taken
        # one by one so that every reason *not* to compact is checked before the
        # summarizer is called: a summary that is then thrown away still costs a
        # model call.
        messages = state["messages"]
        self.blocked = None
        self._ensure_message_ids(messages)
        if not self._should_summarize(messages, self.token_counter(messages)):
            return None
        cutoff = self._determine_cutoff_index(messages)
        if cutoff <= 0:
            return None
        to_summarize, preserved = self._partition_messages(messages, cutoff)
        request = latest_request(messages)
        if request is not None and {m.id for m in to_summarize} <= {request.id}:
            # Only the current request would go, and it is kept anyway: a
            # summary would add tokens, not save them. Leave the state as it is.
            return None
        if self.hooks is not None and self.hooks.has("PreCompact"):
            outcome = await self.hooks.run(
                "PreCompact", {"trigger": self.trigger_name}, approve_fn=self.approve_hook
            )
            if outcome.block:  # the hook said no: keep the conversation as it is
                self.blocked = outcome.reason or "a PreCompact hook declined"
                return None
        summary = self._build_new_messages(await self._acreate_summary(to_summarize))
        new: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *summary]
        if request is not None and request.id not in {m.id for m in preserved}:
            # Right after the summary: the request the model is answering, verbatim.
            new.append(HumanMessage(content=request.content, id=request.id))
        return {"messages": [*new, *preserved]}
