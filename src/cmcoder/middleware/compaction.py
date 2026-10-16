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

from typing import Any

from langchain.agents.middleware import AgentState, SummarizationMiddleware
from langchain.agents.middleware.summarization import DEFAULT_SUMMARY_PROMPT
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

from ..core.hooks import HookRunner

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
    ) -> None:
        self.overhead_tokens = overhead_tokens
        self.hooks = hooks
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

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        update = await super().abefore_model(state, runtime)
        if not update:
            return None
        new = update["messages"]
        request = latest_request(state["messages"])
        kept_ids = {m.id for m in new if not isinstance(m, RemoveMessage)}
        summarised = {m.id for m in state["messages"]} - kept_ids
        if request is not None and summarised <= {request.id}:
            # Only the current request would go, and it is kept anyway: a
            # summary would add tokens, not save them. Leave the state as it is.
            return None
        if self.hooks is not None and self.hooks.has("PreCompact"):
            outcome = await self.hooks.run("PreCompact", {"trigger": self.trigger_name})
            if outcome.block:  # the hook said no: keep the conversation as it is
                return None
        if request is not None and request.id not in kept_ids:
            # [RemoveMessage, summary, *kept]: put the request right after the summary.
            new.insert(2, HumanMessage(content=request.content, id=request.id))
        return update
