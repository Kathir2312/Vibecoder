"""cmcoder's LangChain middleware: the hooks that customise the agent loop.

- permission.py  ask before tools run (LangGraph `interrupt()`)
- model.py       per-model settings and the context budget; <think> splitting
- guards.py      repair broken tool calls, stop loops, steer shell file work
"""
