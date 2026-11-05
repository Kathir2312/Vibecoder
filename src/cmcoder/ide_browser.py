"""Tools the IDE defines itself, offered to the model as they are: VS Code's built-in browser.

VS Code has an Integrated Browser (a browser tab inside the editor) and registers tools to drive
it (open a page, read it, click, type, take a screenshot, run Playwright code) that Copilot's
agent uses. cmcoder's own `Browser` tool starts a separate Chrome or Edge window instead. With
this, the VS Code extension finds those tools at run time, sends their names and JSON schemas
with `ide_capabilities.tool_defs`, and every call travels back as an `ide_tool_request`: the page
opens in VS Code, where the user can watch it, and the engine's own Browser tool steps aside.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* **`StructuredTool` with a JSON-schema `args_schema` and a coroutine**: a tool whose inputs are
  not a pydantic class but a schema someone else defined. This is what langchain-mcp-adapters
  does for MCP tools, and the permission rules see these tools the same way (`McpPermissionTool`).
* **`response_format="content_and_artifact"`**: the text goes to the model, the pictures ride
  along in `ToolMessage.artifact` for the user's chat only (tools/adapter.py does the same).

The renaming, the read-only guess and the size limit are plain code: LangChain has nothing for
them.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from .mcp.manager import McpPermissionTool
from .protocol.messages import IdeToolDef
from .tools.base import Tool, ToolResult, truncate_middle

# Runs a tool in the editor: (the IDE's own name for it, its input) -> the result.
IdeCall = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]

PREFIX = "ide_"
MAX_NAME = 64
# A tool that only looks (reads a page, takes a screenshot) needs no question; clicking, typing,
# opening a page or running code does. Judged by name, lower-case without separators.
READ_ONLY_HINTS = ("readpage", "screenshot", "getpage", "listpage", "snapshot")


def tool_name(ide_name: str, taken: set[str]) -> str:
    """A name a model API accepts ([A-Za-z0-9_-], at most 64), unique among `taken`."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", ide_name).strip("_") or "tool"
    if not base.startswith(PREFIX):
        base = PREFIX + base
    base = base[:MAX_NAME]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[: MAX_NAME - len(suffix)] + suffix, n + 1
    taken.add(name)
    return name


def looks_read_only(ide_name: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", ide_name.lower())
    return any(hint in flat for hint in READ_ONLY_HINTS)


def object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The tool's inputs as the object schema model APIs want (they refuse anything else)."""
    out = {k: v for k, v in schema.items() if k != "$schema"}
    out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    return out


class _AskTool(McpPermissionTool):
    """Asks first (default mode): it changes something or goes somewhere."""


class _ReadTool(McpPermissionTool):
    read_only = True


class IdeToolSet:
    """The IDE's own tools, as LangChain tools plus the stand-ins the permission rules use."""

    def __init__(self, defs: list[IdeToolDef], call: IdeCall, max_chars: int = 30_000) -> None:
        self.call = call
        self.max_chars = max_chars
        taken: set[str] = set()
        # (name for the model, the IDE's definition)
        self.defs = [(tool_name(d.name, taken), d) for d in defs]

    def __bool__(self) -> bool:
        return bool(self.defs)

    def langchain_tools(self) -> list[BaseTool]:
        return [self._tool(name, d) for name, d in self.defs]

    def permission_tools(self) -> list[Tool]:
        return [
            (_ReadTool if looks_read_only(d.name) else _AskTool)(name, d.description)
            for name, d in self.defs
        ]

    def _tool(self, name: str, d: IdeToolDef) -> BaseTool:
        async def run(**kwargs: Any) -> tuple[str, dict[str, Any] | None]:
            result = await self.call(d.name, kwargs)
            text = truncate_middle(result.content or "(no output)", self.max_chars)
            if result.is_error:
                raise ToolException(text)
            return text, ({"images": [str(p) for p in result.images]} if result.images else None)

        return StructuredTool(
            name=name,
            description=(d.description or f"The IDE's {d.name} tool.").strip(),
            args_schema=object_schema(d.input_schema),
            coroutine=run,
            handle_tool_error=True,  # a ToolException goes back to the model as an error result
            response_format="content_and_artifact",
        )
