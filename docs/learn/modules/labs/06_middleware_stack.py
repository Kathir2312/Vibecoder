"""Chapter 06 lab, part 2: the middleware stack build_agent hands to create_agent.

We wrap `create_agent` to see its arguments, build cmcoder's agent, and print
each middleware in order with the hooks it implements, the tools, and the
nodes of the compiled graph. Nothing is sent to a model, so no mock server
is needed.

    python 06_middleware_stack.py <project dir>
"""

import sys
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware

import cmcoder.agents.main as agent_main
from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings

HOOKS = [
    "abefore_agent",
    "abefore_model",
    "awrap_model_call",
    "aafter_model",
    "awrap_tool_call",
    "aafter_agent",
]

captured: dict[str, Any] = {}
real_create_agent = agent_main.create_agent


def spy(**kwargs: Any) -> Any:
    captured.update(kwargs)
    return real_create_agent(**kwargs)


agent_main.create_agent = spy  # type: ignore[assignment]  # build_agent looks it up here


def hooks_of(m: AgentMiddleware) -> list[str]:
    """The hooks this middleware overrides (sync or async), without the 'a'."""
    found = []
    for h in HOOKS:
        sync = h[1:]  # "abefore_model" -> "before_model"
        cls = type(m)
        if getattr(cls, h) is not getattr(AgentMiddleware, h) or getattr(cls, sync) is not getattr(
            AgentMiddleware, sync
        ):
            found.append(sync)
    return found


def main(project: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": "http://127.0.0.1:9/v1"}}, "model": "mock:qwen3-27b"}
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=True))

    print("middleware, in the order given to create_agent (first = outermost):")
    for i, m in enumerate(captured["middleware"], 1):
        print(f"  {i:2}. {type(m).__name__:26} {', '.join(hooks_of(m))}")

    print("\ntools:", ", ".join(t.name for t in captured["tools"]))
    print("context_schema:", captured["context_schema"].__name__)
    print("checkpointer:", type(captured["checkpointer"]).__name__)
    print("\ngraph nodes:", ", ".join(n for n in agent.graph.get_graph().nodes))


main(sys.argv[1])
