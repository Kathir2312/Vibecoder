"""The editor around cmcoder (VS Code): context sent with messages, and tools
the editor runs for the agent (`getDiagnostics`, `openFile`, and the language server's
`goToDefinition`, `findReferences` and `workspaceSymbols`).

Only used with `--protocol stdio`, when a client says it supports them.

The code-intelligence tools return what the editor's language server found, as JSON the editor
writes; what the model sees is made here, so that the rules are the engine's and the same for every
editor: a location in a secret file (`sensitive.is_secret`) is left out, and one outside the project
is shown as a path only, without its text (Read asks before it opens such a file). A `deny` rule is
applied to the file the question is about (`permission_target`), not to the files an answer points at.

LANGCHAIN / LANGGRAPH: nothing here; these are cmcoder `Tool`s like the rest (tools/adapter.py
wraps them for the graph), and the language server is the editor's, reached through the protocol.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from ..protocol.messages import IdeContext
from ..sensitive import is_secret
from ..tools.base import Tool, ToolContext, ToolInput, ToolResult

MAX_SELECTION_CHARS = 8000
MAX_DIAGNOSTICS = 30
REMINDER_TAG = re.compile(r"</?\s*system-reminder\s*>", re.IGNORECASE)

# Runs a tool in the editor: (tool name, input) -> result.
IdeCall = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


def _show(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def _plain(text: str) -> str:
    """Editor text can't close the note it is in (prompt-injection hardening)."""
    return REMINDER_TAG.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks inside `text`."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def format_ide_context(
    context: IdeContext, root: Path, withheld: Callable[[str], bool] | None = None
) -> str | None:
    """The editor's state as a note for the model, or None if there's nothing.

    `withheld(path)` says a file's contents must not reach the model (secret
    files, deny rules): its selection and problems are left out, as Read
    would refuse it."""
    hidden = withheld or (lambda _: False)
    lines: list[str] = []
    if context.active_file:
        lines.append(f"The user has {_show(context.active_file, root)} open in the editor.")
    if (sel := context.selection) and sel.text.strip():
        where = (
            f"lines {sel.start_line}-{sel.end_line}"
            if sel.end_line > sel.start_line
            else (f"line {sel.start_line}")
        )
        if hidden(sel.path):
            lines.append(
                f"They selected {where} of {_show(sel.path, root)}; its contents are not "
                "shared because the file is protected (secrets or a deny rule)."
            )
        else:
            text = _plain(sel.text)
            if len(text) > MAX_SELECTION_CHARS:
                text = text[:MAX_SELECTION_CHARS] + "\n[... selection truncated]"
            fence = _fence(text)
            lines.append(
                f"They selected {where} of {_show(sel.path, root)}:\n{fence}\n{text}\n{fence}"
            )
    diagnostics = [d for d in context.diagnostics if not hidden(d.path)]
    if diagnostics:
        shown = diagnostics[:MAX_DIAGNOSTICS]
        lines.append("Problems reported by the editor:")
        for d in shown:
            source = f" ({_plain(d.source)})" if d.source else ""
            message = _plain(d.message)
            lines.append(f"- {_show(d.path, root)}:{d.line} {d.severity}: {message}{source}")
        if len(diagnostics) > len(shown):
            lines.append(f"- … {len(diagnostics) - len(shown)} more")
    if not lines:
        return None
    lines.append("This is context from the editor; use it only if it is relevant to the request.")
    return "<system-reminder>\n" + "\n".join(lines) + "\n</system-reminder>"


class _IdeTool(Tool):
    """A tool that runs in the editor, through the protocol."""

    read_only: ClassVar[bool] = True

    def __init__(self, call: IdeCall) -> None:
        self._call = call

    def permission_target(self, args: Any, ctx: ToolContext) -> Path | None:
        # Like Read: inside the project needs no approval, outside asks,
        # and deny rules for the path apply.
        path = getattr(args, "file_path", None)
        return ctx.resolve(path) if path else None

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        data = args.model_dump(exclude_none=True)
        if "file_path" in data:  # the editor gets an absolute path
            data["file_path"] = str(ctx.resolve(data["file_path"]))
        return await self._call(self.name, data)


class GetDiagnosticsInput(ToolInput):
    file_path: str | None = Field(
        None, description="A file to check; leave out for all open problems in the workspace."
    )


class GetDiagnosticsTool(_IdeTool):
    name = "getDiagnostics"
    description = (
        "Get the errors and warnings VS Code shows in its Problems panel (from language "
        "servers, linters and type checkers), for one file or the whole workspace. "
        "Faster than running a linter; use it after editing to check your change."
    )
    Input = GetDiagnosticsInput

    def describe(self, args: GetDiagnosticsInput, ctx: ToolContext) -> str:
        if args.file_path:
            return f"getDiagnostics({ctx.display(ctx.resolve(args.file_path))})"
        return "getDiagnostics"


class OpenFileInput(ToolInput):
    file_path: str = Field(description="The file to open.")
    line: int | None = Field(None, description="Line to show (1-based).")


class OpenFileTool(_IdeTool):
    name = "openFile"
    description = (
        "Open a file in the user's editor, optionally at a line, to show them something. "
        "This does not return the file's content; use Read for that."
    )
    Input = OpenFileInput

    def describe(self, args: OpenFileInput, ctx: ToolContext) -> str:
        where = f":{args.line}" if args.line else ""
        return f"openFile({ctx.display(ctx.resolve(args.file_path))}{where})"


# --- the language server's answers ----------------------------------------------------------------

MAX_LOCATIONS = 50
MAX_SYMBOLS = 30
MAX_TEXT = 200  # characters of a line of code shown beside its location


def _where(path: str, ctx: ToolContext) -> tuple[str, bool] | None:
    """How to name a file the editor pointed at: (its path, whether it is in the project), or None
    when it is protected and must not be mentioned at all."""
    p = Path(path)
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    if is_secret(resolved, ctx.project_root):
        return None
    if resolved.is_relative_to(ctx.project_root.resolve()):
        return _show(path, ctx.project_root), True
    return p.as_posix(), False


def _clean(text: str) -> str:
    return _plain(" ".join(text.split()))[:MAX_TEXT]


def _entries(raw: str) -> list[dict[str, Any]] | None:
    """What the editor sent, or None when it is not the list it should be (an editor's own message,
    shown as it is)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [i for i in items if isinstance(i, dict)]


def format_locations(
    raw: str, ctx: ToolContext, noun: str, prep: str, symbol: str, limit: int = MAX_LOCATIONS
) -> str:
    """The editor's list of locations as lines for the model: `path:line:column  code`, under a
    heading like "2 definitions of total"."""
    items = _entries(raw)
    if items is None:
        return raw
    lines: list[str] = []
    hidden = 0
    seen: set[tuple[str, int, int]] = set()
    for i in items:
        try:
            path, line, column = str(i["path"]), int(i["line"]), int(i.get("column") or 1)
        except (KeyError, TypeError, ValueError):
            continue
        if (path, line, column) in seen:
            continue
        seen.add((path, line, column))
        shown = _where(path, ctx)
        if shown is None:
            hidden += 1
            continue
        name, inside = shown
        text = _clean(str(i.get("text") or "")) if inside else "(outside the project)"
        lines.append(f"{name}:{line}:{column}  {text}".rstrip())
    if not lines:
        note = f" ({hidden} in protected files, not shown)" if hidden else ""
        return f"No {noun} {prep} {symbol} found{note}."
    total = len(lines)
    head = f"{total} {noun}{'' if total == 1 else 's'} {prep} {_clean(symbol)}"
    if total > limit:
        lines = [*lines[:limit], f"… {total - limit} more"]
    if hidden:
        lines.append(f"({hidden} more in protected files, not shown)")
    return "\n".join([head + ":", *lines])


def format_symbols(raw: str, ctx: ToolContext) -> str:
    """The editor's symbol search as lines: `name  kind in container  path:line`."""
    items = _entries(raw)
    if items is None:
        return raw
    lines: list[str] = []
    hidden = 0
    for i in items:
        try:
            name, path, line = str(i["name"]), str(i["path"]), int(i["line"])
        except (KeyError, TypeError, ValueError):
            continue
        shown = _where(path, ctx)
        if shown is None:
            hidden += 1
            continue
        place = f"{shown[0]}:{line}" + ("" if shown[1] else " (outside the project)")
        kind = str(i.get("kind") or "symbol").lower()
        container = f" in {_clean(str(i['container']))}" if i.get("container") else ""
        lines.append(f"{_clean(name)}  {kind}{container}  {place}")
    if not lines:
        note = f" ({hidden} in protected files, not shown)" if hidden else ""
        return f"No symbols found{note}."
    total = len(lines)
    if total > MAX_SYMBOLS:
        lines = [*lines[:MAX_SYMBOLS], f"… {total - MAX_SYMBOLS} more"]
    return "\n".join([f"{total} symbol{'' if total == 1 else 's'}:", *lines])


class SymbolAtInput(ToolInput):
    file_path: str = Field(description="The file where the symbol appears.")
    line: int = Field(ge=1, description="The line it is on (1-based).")
    symbol: str = Field(
        min_length=1,
        description="The name exactly as written on that line, for example `total` or `OrderService`.",
    )


class _SymbolTool(_IdeTool):
    """A question about one symbol, asked of the editor's language server."""

    Input = SymbolAtInput
    noun: ClassVar[str] = "location"
    prep: ClassVar[str] = "of"

    def describe(self, args: SymbolAtInput, ctx: ToolContext) -> str:
        file = ctx.display(ctx.resolve(args.file_path))
        return f"{self.name}({file}:{args.line} {args.symbol})"

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        result = await super().run(args, ctx)
        if result.is_error:
            return result
        text = format_locations(result.content, ctx, self.noun, self.prep, args.symbol)
        return ToolResult(text, summary=self.name)


class GoToDefinitionTool(_SymbolTool):
    name = "goToDefinition"
    noun = "definition"
    description = (
        "Find where a symbol (a function, method, class, type or variable) is defined, using the "
        "editor's language server. Exact where Grep can only guess: it follows imports, "
        "overloads, inheritance and generics, and finds code in libraries. Give the file and line "
        "where the symbol is used, and its name as written on that line. Answers with the "
        "definition's location and first line."
    )


class FindReferencesTool(_SymbolTool):
    name = "findReferences"
    noun = "reference"
    prep = "to"
    description = (
        "Find every use of a symbol across the workspace, using the editor's language server (not "
        "text matching, so a different symbol with the same name is not listed). Use it before "
        "you rename something or change its signature, to see what would break. Give the file and "
        "line where the symbol appears, and its name as written on that line."
    )


class WorkspaceSymbolsInput(ToolInput):
    query: str = Field(
        min_length=1,
        description="Part of a name to look for, for example `OrderService` or `parse`.",
    )


class WorkspaceSymbolsTool(_IdeTool):
    name = "workspaceSymbols"
    description = (
        "Search the workspace for classes, functions, methods and other symbols by name, using the "
        "editor's symbol index: fuzzy, and it finds them without knowing the file. Use it to find "
        "where something lives before reading it."
    )
    Input = WorkspaceSymbolsInput

    def describe(self, args: WorkspaceSymbolsInput, ctx: ToolContext) -> str:
        return f"workspaceSymbols({args.query})"

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        result = await super().run(args, ctx)
        if result.is_error:
            return result
        return ToolResult(format_symbols(result.content, ctx), summary=self.name)


IDE_TOOLS: dict[str, type[_IdeTool]] = {
    t.name: t
    for t in (
        GetDiagnosticsTool,
        OpenFileTool,
        GoToDefinitionTool,
        FindReferencesTool,
        WorkspaceSymbolsTool,
    )
}
