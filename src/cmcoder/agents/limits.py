"""How many graph steps a run may take before LangGraph stops it (its `recursion_limit`).

Plain Python: LangChain and LangGraph have no way to size this from a graph.
"""

from __future__ import annotations

from typing import Any


def recursion_limit_for(graph: Any, max_turns: int) -> int:
    """LangGraph's step limit for a run of at most `max_turns` model calls.

    `create_agent` makes every middleware hook (`before_model`, `after_model`, ...) a node of
    its own, so one model call is not 2 steps (model, tools) but one per node of the loop: 9
    here, with the hooks of the permission, compaction, limit, reasoning, todo and repair
    middleware. A fixed "4 steps per turn" ended a long task after 24 tool calls with a
    GraphRecursionError, long before `maxTurns`. The graph's own node count is an upper bound
    for the steps of one iteration; the limit that stops a run is ModelCallLimitMiddleware's,
    this one only catches a graph that never stops.
    """
    nodes = sum(1 for name in graph.nodes if not name.startswith("__"))
    return max_turns * max(nodes, 2) + 25
