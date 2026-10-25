"""`RunContext`: per-run data handed to middleware through LangGraph's runtime.

LANGGRAPH CONCEPTS IN THIS FILE

* **Runtime context.** `create_agent(..., context_schema=RunContext)`
  declares a context type, and each run passes one:
  `graph.astream(input, config, context=RunContext(...))`. Middleware reads
  it as `runtime.context` (model hooks) or `request.runtime.context` (tool
  hooks).
* **Context vs. state.** Context is *not* part of the graph state: it isn't
  checkpointed and can hold live objects. That makes it the right place for
  "which turn is this" and the file-snapshot store, while the conversation
  itself lives in state (saved after every step, restored on resume).
* **Nested graphs.** A graph run inside a node or tool sees the context its
  own caller passed, not the top-level one (agents/team.py passes this one on
  explicitly).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.checkpoints import Checkpoints


@dataclass
class RunContext:
    turn: int = 0  # the user turn this run belongs to (1, 2, ...)
    # File snapshots for /rewind (core/checkpoints.py); taken before edits.
    checkpoints: Checkpoints = field(default_factory=lambda: Checkpoints(None))
