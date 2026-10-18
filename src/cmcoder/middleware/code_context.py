"""Automatic code context: the index's best matches, added to the user's message.

When the project is indexed (rag/), each user message of a few words is
searched for in the index, and the best matches go in front of it inside a
`<system-reminder>` (within a token budget, at most a small share of the
context window; code sent once isn't sent again in the same session). The
UI shows what was added (`code_context`). `/rewind` and the history show what
the user wrote, without it. A failing index never stops the turn: a warning,
once.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* **`abefore_agent`** runs once at the start of each graph run, that is once
  per user turn, and not again when LangGraph resumes after a permission
  prompt (the run continues from where it paused). That's the place to
  change the user's message once.
* Changing a message in state: return it with the **same id**; the
  `add_messages` reducer replaces it instead of appending a new one.
* `get_stream_writer()` sends the `code_context` event to the UI on the
  custom stream, like the critic's events.
* This is retrieval-augmented generation (RAG) by middleware: the retriever
  runs before the model, with the user's own words as the query. The
  CodeSearch tool (tools/code_search.py) is the other half: the model asking.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ..config.settings import RagAutoContextConfig
from ..protocol import events as ev

MIN_WORDS = 3  # shorter messages ("ok", "thanks") get nothing
WINDOW_SHARE = 0.08  # at most this share of the context window
REMINDER = (
    "<system-reminder>\nCode from the project's index that may be relevant to this "
    "message (found by meaning; it can be a little behind the files, so Read a file "
    "before editing it):\n\n{blocks}\n</system-reminder>"
)


def _emit(event: ev.Event) -> None:
    try:
        get_stream_writer()({"type": "event", "event": event.model_dump()})
    except RuntimeError:  # not inside a graph run (a unit test)
        pass


def _warn(message: str) -> None:
    try:
        get_stream_writer()({"type": "warning", "message": message})
    except RuntimeError:
        pass


class AutoContextMiddleware(AgentMiddleware):
    def __init__(self, index: Any, cfg: RagAutoContextConfig, context_window: int) -> None:
        super().__init__()
        self.index = index  # rag.index.CodeIndex
        self.cfg = cfg
        self.context_window = context_window
        self._sent: set[str] = set()  # chunk ids already sent this session
        self._failed = False

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        if not self.cfg.enabled or self._failed:
            return None
        messages = state["messages"]
        last = messages[-1] if messages else None
        if not isinstance(last, HumanMessage):
            return None
        prompt = _prompt_text(last)
        if len(prompt.split()) < MIN_WORDS or prompt.lstrip().startswith("/"):
            return None
        try:
            hits = await self.index.search(prompt, self.cfg.top_k)
        except Exception:  # the gateway or the store is down: the turn goes on
            self._failed = True
            _warn(
                "Code search isn't answering, so no code context was added. "
                "`cmcoder doctor` checks it."
            )
            return None
        budget = min(self.cfg.max_tokens, int(self.context_window * WINDOW_SHARE))
        picked, used = [], 0
        for hit in hits:
            if hit.score < self.cfg.min_score or hit.chunk.id in self._sent:
                continue
            cost = len(hit.chunk.text) // 4 + 20
            if used + cost > budget:
                continue
            picked.append(hit)
            used += cost
        if not picked:
            return None
        self._sent.update(h.chunk.id for h in picked)
        blocks = []
        for h in picked:
            c = h.chunk
            name = f" ({c.symbol})" if c.symbol else ""
            blocks.append(f"### {c.location}{name}\n```{c.language}\n{c.text}\n```")
        reminder = REMINDER.format(blocks="\n\n".join(blocks))
        _emit(
            ev.CodeContext(
                items=[
                    ev.CodeContextItem(
                        path=h.chunk.path,
                        start_line=h.chunk.start_line,
                        end_line=h.chunk.end_line,
                        symbol=h.chunk.symbol,
                        score=round(h.score, 3),
                    )
                    for h in picked
                ],
                tokens=used,
            )
        )
        if isinstance(last.content, str):
            content: Any = f"{reminder}\n\n{last.content}"
        else:  # with images: a text block in front
            content = [{"type": "text", "text": reminder}, *last.content]
        return {"messages": [last.model_copy(update={"content": content})]}


def _prompt_text(message: HumanMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(
        str(b.get("text", "")) for b in message.content if isinstance(b, dict) and b.get("text")
    )
