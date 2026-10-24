"""Chapter 12 lab: your own StateGraph, no model and no server.

A tiny version of cmcoder's team graph (agents/team.py): a typed state with a
reducer, nodes that return only the keys they change, and a conditional edge
that loops back to the "developer" until the "tester" passes or the rounds run
out. Then it prints the graph as Mermaid.

    python 12_retry_graph.py [fail_times] [max_rounds]
"""

import operator
import sys
from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.graph import START, StateGraph


class State(TypedDict):
    max_rounds: int
    fail_times: int  # how many times the tester fails (the "bug")
    round: NotRequired[int]
    changes: NotRequired[Annotated[list[str], operator.add]]  # reducer: appended
    passed: NotRequired[bool]


def developer(state: State) -> dict:
    round_ = state.get("round", 0) + 1
    print(f"developer: round {round_}")
    # A partial update: only these keys. `changes` is appended to, not replaced.
    return {"round": round_, "changes": [f"change {round_}"]}


def tester(state: State) -> dict:
    passed = state.get("round", 0) > state["fail_times"]
    print(f"tester   : {'pass' if passed else 'fail'}")
    return {"passed": passed}


def route_after_test(state: State) -> Literal["developer", "__end__"]:
    if not state.get("passed") and state.get("round", 0) < state["max_rounds"]:
        return "developer"
    return "__end__"  # langgraph.graph.END


graph = StateGraph(State)
graph.add_node("developer", developer)
graph.add_node("tester", tester)
graph.add_edge(START, "developer")
graph.add_edge("developer", "tester")
graph.add_conditional_edges("tester", route_after_test)
app = graph.compile()

fail_times = int(sys.argv[1]) if len(sys.argv) > 1 else 1
max_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
final = app.invoke({"fail_times": fail_times, "max_rounds": max_rounds})
print("final    :", {k: final[k] for k in ("round", "changes", "passed")})
print()
print(app.get_graph().draw_mermaid())
