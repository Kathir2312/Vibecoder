"""Expose cmcoder's tools to LangChain as `BaseTool`s (the Adapter pattern).

LANGCHAIN CONCEPTS IN THIS FILE

* `BaseTool`: what LangChain agents call. It has a `name`, a `description`
  (the model reads it to decide when to use the tool) and an `args_schema`
  (a pydantic model). LangChain turns the schema into the JSON Schema sent to
  the model, and validates the model's arguments against it before running
  the tool.
* `_arun`: the async body of a tool. LangChain calls it with the validated
  arguments as keyword arguments.
* `ToolException` + `handle_tool_error=True`: the standard way for a tool to
  report a failure *to the model* (the message becomes a `ToolMessage` with
  `status="error"`) without crashing the agent run.

WHY AN ADAPTER INSTEAD OF REWRITING EVERY TOOL?
The Read/Write/Edit/Glob/Grep/Bash tools from cmcoderagent are already
hardened and tested (read-before-write, CRLF handling, secret-file filtering,
process-tree kills on Windows). One small adapter makes all of them LangChain
tools without touching that logic, and their tests carry over as they are.

WHY THE SESSION CONTEXT LIVES HERE AND NOT IN GRAPH STATE
`ToolContext` holds a live shell process and the files read so far. LangGraph
state is saved by the checkpointer after every step, so it must be
serialisable data; a process isn't. Live resources belong to the session
object (here, captured by the adapter). Only data that should survive a
restart belongs in state. LangChain's `ToolRuntime` is the way in when a
tool needs to read graph state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool, ToolException
from pydantic import ConfigDict

from .base import Tool, ToolContext, ToolResult, truncate_middle


class CmcoderTool(BaseTool):
    """A LangChain tool that runs a cmcoder `Tool` in a session's `ToolContext`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Typed `Any` for pydantic: BaseTool is a pydantic model, and these are
    # plain Python objects (with a live shell inside) that it shouldn't validate.
    inner: Any  # Tool
    ctx: Any  # ToolContext

    def __init__(self, inner: Tool, ctx: ToolContext, **kwargs: Any) -> None:
        super().__init__(
            name=inner.name,
            description=inner.description,
            args_schema=inner.Input,
            inner=inner,
            ctx=ctx,
            # Errors raised as ToolException go back to the model as error ToolMessages.
            handle_tool_error=True,
            **kwargs,
        )

    def _run(self, *args: Any, **kwargs: Any) -> str:
        # cmcoder is async end to end (streaming, subprocesses); there is no
        # sync path. LangGraph calls `_arun` when the graph runs with `astream`.
        raise NotImplementedError("cmcoder tools are async-only; use ainvoke/astream")

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        # LangChain has already validated the arguments against `args_schema`;
        # rebuild the pydantic object the cmcoder tool expects.
        kwargs.pop("run_manager", None)
        params = self.inner.Input.model_validate(kwargs)
        try:
            result = await self.inner.run(params, self.ctx)
        except asyncio.CancelledError:
            raise  # Ctrl+C / interrupt: let the cancellation through
        except Exception as e:  # a tool bug must not end the session
            result = ToolResult(f"{self.name} failed: {type(e).__name__}: {e}", is_error=True)
        content = truncate_middle(result.content, self.ctx.max_output_chars)
        if result.is_error:
            raise ToolException(content)
        return content


def as_langchain_tools(tools: list[Tool], ctx: ToolContext) -> list[BaseTool]:
    return [CmcoderTool(t, ctx) for t in tools]
