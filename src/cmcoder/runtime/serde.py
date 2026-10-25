"""How checkpoints are (de)serialised: only the types cmcoder saves.

LangGraph stores graph state with `JsonPlusSerializer` (msgpack). A class in
the state (a pydantic model, say) is saved as "module + class name + data",
and loading it imports that module and builds the object. By default any
importable class is allowed. cmcoder lists exactly the ones it saves, so a
checkpoint database that was tampered with can't make cmcoder build anything
else; LangGraph's own safe types (messages, Send, Interrupt, ...) are always
allowed.

LANGGRAPH CONCEPTS IN THIS FILE

* **Checkpoint serialisation**: every value in graph state is written with
  the checkpointer's serializer (`JsonPlusSerializer`, msgpack) and read back
  on resume. Keep state to messages, dicts and a few pydantic models.
* **`JsonPlusSerializer(allowed_msgpack_modules=[...])`**, given to a
  checkpointer as `serde=` (`AsyncSqliteSaver(conn, serde=...)`,
  `InMemorySaver(serde=...)`): an allow-list of the classes a checkpoint may
  build. The same as `LANGGRAPH_STRICT_MSGPACK=true`, plus our own types.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def checkpoint_serde() -> JsonPlusSerializer:
    # The structured outputs kept in state: the critic's verdict (middleware/
    # critic.py) and the team's stories and test reports (agents/team.py).
    from ..agents.team import Failure, Stories, TestReport
    from ..middleware.critic import Issue, Verdict

    return JsonPlusSerializer(
        allowed_msgpack_modules=[Verdict, Issue, Stories, TestReport, Failure]
    )
