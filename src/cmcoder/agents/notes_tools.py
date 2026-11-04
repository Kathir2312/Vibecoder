"""The `Remember`, `Recall` and `Forget` tools (auto memory on a LangGraph store, see notes.py).

A note the user asked for is saved only if the model quotes words the user
actually wrote in this conversation. The check reads the conversation state
through the injected `ToolRuntime`, with `<system-reminder>` blocks (added by
cmcoder, not typed by the user) removed, so text from a file, a web page or a
tool result can't make cmcoder write one. Without a quote the note counts as
something cmcoder learned itself (`memory.auto`): project scope only, marked as
such, and the first to be dropped when the project is full.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from ..notes import MAX_BODY, MAX_TITLE, Memory, NoteError

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def user_text(messages: list[Any]) -> str:
    parts = []
    for m in messages:
        if isinstance(m, HumanMessage):
            content = m.content
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            parts.append(_REMINDER.sub("", str(content)))
    return _norm("\n".join(parts))


class RememberInput(BaseModel):
    title: str = Field(description=f"Short title, at most {MAX_TITLE} characters.")
    note: str = Field(description=f"The fact to keep, at most {MAX_BODY} characters.")
    quote: str = Field(
        default="",
        description="Words copied exactly from the user's own message that ask for or state "
        "this. Leave empty only for something you worked out yourself (project scope).",
    )
    scope: Literal["project", "user"] = Field(
        default="project",
        description="'project' for this project; 'user' for a preference that applies to "
        "every project (needs a quote).",
    )


class NoteRef(BaseModel):
    note: str = Field(description="The note's title or file name, as in the notes list.")


def build_notes_tools(memory: Memory) -> list[BaseTool]:
    async def remember(
        title: str, note: str, runtime: ToolRuntime, quote: str = "", scope: str = "project"
    ) -> str:
        store = memory.user if scope == "user" else memory.project
        if quote.strip():
            said = user_text(list(runtime.state.get("messages", [])))
            q = _norm(quote)
            if len(q) < 8 or q not in said:
                raise ToolException(
                    "Not saved: the quote isn't in the user's own messages. Quote the user "
                    "exactly, or leave the quote empty for something you learned yourself."
                )
            source = "user"
        else:
            if scope == "user":
                raise ToolException("Not saved: a note for every project needs the user's quote.")
            if not memory.auto:
                raise ToolException(
                    "Not saved: only what the user says is saved here. Quote their words."
                )
            source = "agent"
        try:
            saved = store.save(title, note, source)
        except NoteError as e:
            raise ToolException(f"Not saved: {e}") from e
        return f"Saved {scope} note {saved.title!r}. It is in the notes list from the next session."

    async def recall(note: str) -> str:
        found = memory.get(note.strip())
        if found is None:
            raise ToolException(f"No note matches {note!r}.")
        return f"{found.title} ({found.scope}, {found.source}):\n{found.body}"

    async def forget(note: str) -> str:
        for store in memory.stores():
            gone = store.forget(note.strip())
            if gone is not None:
                return f"Forgot {gone.title!r}."
        raise ToolException(f"No note matches {note!r}.")

    return [
        StructuredTool.from_function(
            coroutine=remember,
            name="Remember",
            description=(
                "Save a short note for future sessions. Use it when the user asks you to "
                "remember something or states a lasting preference or correction (quote "
                "them), and for project facts you worked out that will save time next "
                "time (build/test commands, fixes). Never for secrets or guesses."
            ),
            args_schema=RememberInput,
            handle_tool_error=True,
        ),
        StructuredTool.from_function(
            coroutine=recall,
            name="Recall",
            description="Read the full text of a saved note listed in the notes between sessions.",
            args_schema=NoteRef,
            handle_tool_error=True,
        ),
        StructuredTool.from_function(
            coroutine=forget,
            name="Forget",
            description="Delete a saved note when the user asks you to forget it.",
            args_schema=NoteRef,
            handle_tool_error=True,
        ),
    ]
