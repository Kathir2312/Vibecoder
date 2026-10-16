"""Separate a model's private reasoning from the answer it shows the user.

Qwen3 writes its reasoning ("thinking") in one of three ways, depending on the
server in front of it:

1. a separate `reasoning_content` field (LiteLLM, vLLM with a reasoning parser);
   chat.py picks this up;
2. `<think> ... </think>` inside the normal text;
3. only a closing `</think>` (Qwen3 Thinking-2507 chat templates start the
   reply inside the thinking block).

This module handles 2 and 3. It has no LangChain code: keeping pure text logic
out of the framework layer makes it easy to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPEN = "<think>"
CLOSE = "</think>"


def split_think(text: str) -> tuple[str, str]:
    """Split a complete reply into (visible text, reasoning)."""
    if CLOSE not in text and OPEN not in text:
        return text, ""
    reasoning: list[str] = []
    visible: list[str] = []
    rest = text
    if CLOSE in rest and (OPEN not in rest or rest.index(CLOSE) < rest.index(OPEN)):
        # Case 3: everything before the first </think> was reasoning.
        head, rest = rest.split(CLOSE, 1)
        reasoning.append(head)
    while OPEN in rest:
        before, after = rest.split(OPEN, 1)
        visible.append(before)
        if CLOSE in after:
            thought, rest = after.split(CLOSE, 1)
        else:  # cut off mid-thought
            thought, rest = after, ""
        reasoning.append(thought)
    visible.append(rest)
    return "".join(visible).strip(), "\n".join(r.strip() for r in reasoning if r.strip())


@dataclass
class ThinkSplitter:
    """Streaming version of `split_think`: feed text chunks as they arrive.

    A tag can be cut across two chunks ("<thi" + "nk>"), so text that could be
    the start of a tag is held back until the next chunk decides it.
    """

    inside: bool = False  # True when the reply starts inside <think> (Thinking-2507)
    _buffer: str = field(default="", repr=False)

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Return [(kind, text)] where kind is "text" or "reasoning"."""
        self._buffer += chunk
        out: list[tuple[str, str]] = []
        while True:
            tag = CLOSE if self.inside else OPEN
            idx = self._buffer.find(tag)
            if idx >= 0:
                self._emit(out, self._buffer[:idx])
                self._buffer = self._buffer[idx + len(tag) :]
                self.inside = not self.inside
                continue
            # A lone </think> while "outside" means case 3: what we showed was
            # really reasoning. We can't take back streamed text, so we just
            # drop the tag; the final message is split correctly by split_think.
            if not self.inside and CLOSE in self._buffer:
                before, self._buffer = self._buffer.split(CLOSE, 1)
                self._emit(out, before)
                continue
            keep = _partial_tag_suffix(self._buffer)
            self._emit(out, self._buffer[: len(self._buffer) - keep])
            self._buffer = self._buffer[len(self._buffer) - keep :]
            return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._emit(out, self._buffer)
        self._buffer = ""
        return out

    def _emit(self, out: list[tuple[str, str]], text: str) -> None:
        if text:
            out.append(("reasoning" if self.inside else "text", text))


def _partial_tag_suffix(text: str) -> int:
    """Length of the longest end of `text` that could begin <think> or </think>."""
    for n in range(min(len(text), len(CLOSE) - 1), 0, -1):
        tail = text[-n:]
        if OPEN.startswith(tail) or CLOSE.startswith(tail):
            return n
    return 0
