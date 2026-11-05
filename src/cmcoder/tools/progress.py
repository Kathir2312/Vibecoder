"""What a running tool can tell the chat, and how the user stops it.

LANGGRAPH / LANGCHAIN CONCEPTS IN THIS FILE

* **`get_stream_writer()`** (langgraph.config): code running inside a graph step, a tool
  included, writes to the run's `custom` stream, which the session turns into protocol
  events (runtime/session.py). That is how a tool's output reaches the chat while the tool
  is still running, instead of only when it returns.
* **A tool doesn't know its own call id.** LangChain's `ToolNode` knows it (it is the id of
  the model's tool call, which the chat shows as `tool_use.id`), but a cmcoder `Tool`
  gets only its arguments. `middleware/toolcall.py` is a LangChain middleware
  (`awrap_tool_call`) that puts the id in a `ContextVar` for the duration of the call; the
  tool reads it here. The same mechanism lets "Stop this command" find the right command:
  a running tool registers a function under its call id, and `stop(call_id)` calls it.

Plain Python by decision: LangChain has no stop-one-tool-call and no live-tool-output
facility beyond the stream writer, which is what this uses.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from ..protocol import events as ev

# (call id, tool name, the Task call it belongs to) of the tool call now running.
current_call: ContextVar[tuple[str, str] | None] = ContextVar("cmcoder_tool_call", default=None)

# Where a tool leaves what the chat shows besides its text (images, exit code and time) for
# the call now running; middleware/toolcall.py puts it on the tool message afterwards. It is
# needed because a failed tool call (Bash exiting non-zero) becomes an error message through
# LangChain's error handling, which carries no artifact.
call_artifact: ContextVar[dict[str, object] | None] = ContextVar(
    "cmcoder_tool_artifact", default=None
)

# Functions that stop a running tool call, by call id.
_stoppers: dict[str, Callable[[], None]] = {}

# What is sent while a command runs: at most this often, and this much in all (the
# result still carries the whole output; this is only what the chat shows live).
MIN_INTERVAL = 0.2
MAX_LIVE_CHARS = 200_000
MAX_CHUNK_CHARS = 8_000


def register_stop(call_id: str, stop: Callable[[], None]) -> None:
    if call_id:
        _stoppers[call_id] = stop


def unregister_stop(call_id: str) -> None:
    _stoppers.pop(call_id, None)


def stop(call_id: str) -> bool:
    """Stop the tool call `call_id` if it is running and can be stopped."""
    function = _stoppers.get(call_id)
    if function is None:
        return False
    function()
    return True


def _write(event: ev.Event) -> None:
    from langgraph.config import get_stream_writer

    try:
        get_stream_writer()({"type": "event", "event": event.model_dump()})
    except RuntimeError:  # not inside a graph run (a test, a direct call): nobody to tell
        pass


@dataclass
class LiveOutput:
    """Sends a command's output to the chat as it arrives, in a few batches a second."""

    call_id: str
    name: str
    started: float
    sent: int = 0
    last: float = 0.0
    pending: str = ""

    @classmethod
    def for_current_call(cls) -> LiveOutput | None:
        call = current_call.get()
        if call is None:
            return None
        return cls(call[0], call[1], time.monotonic())

    def feed(self, text: str) -> None:
        if self.sent >= MAX_LIVE_CHARS:
            return
        self.pending += text
        if time.monotonic() - self.last >= MIN_INTERVAL:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        room = MAX_LIVE_CHARS - self.sent
        text, self.pending = self.pending[:room], ""
        for i in range(0, len(text), MAX_CHUNK_CHARS):
            chunk = text[i : i + MAX_CHUNK_CHARS]
            self.sent += len(chunk)
            _write(
                ev.ToolProgress(
                    id=self.call_id,
                    name=self.name,
                    text=chunk,
                    elapsed_ms=int((time.monotonic() - self.started) * 1000),
                )
            )
        self.last = time.monotonic()
