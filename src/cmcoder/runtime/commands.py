"""Built-in slash commands and `#` memory notes, shared by every front end.

Each command works on a `Session` and returns lines to show. Front ends (the
REPL, `-p` and the IDE protocol) only render the result, so the
commands behave the same everywhere.

Most of them are thin wrappers over LangGraph operations on the thread:
`/clear` starts a new thread, `/resume` points at an old one, `/rewind` forks
from an earlier checkpoint, `/compact` writes a summary with `aupdate_state`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.commands import (
    CommandSource,
    SlashCommand,
    mcp_prompt_commands,
    prompt_arguments,
    substitute,
)
from ..core.commands import help_lines as custom_help_lines
from ..core.permissions import ModeNotAllowed
from ..middleware.compaction import CompactionBlocked
from .session import Session
from .store import SessionInfo

# (name, argument hint, description)
BUILT_IN: list[tuple[str, str, str]] = [
    ("help", "", "Show the commands"),
    ("clear", "", "Start a new conversation (the old one stays saved)"),
    ("compact", "[focus]", "Summarise the conversation now to free context"),
    ("resume", "[id]", "List saved conversations, or continue one"),
    ("rewind", "[turn] [code|conversation|both]", "Go back to before an earlier message"),
    ("mode", "[mode]", "Show or change the permission mode (also Shift+Tab)"),
    ("model", "[provider:model]", "Show or switch the model"),
    ("turns", "[number]", "Show or change how many model calls one request may use (maxTurns)"),
    ("todos", "", "Show the todo list"),
    ("mcp", "", "MCP servers: status and tools"),
    ("agents", "", "Subagent types, and this session's subagent runs"),
    ("critic", "[on|off]", "A critic agent reviews each answer before you see it"),
    ("team", "<feature>", "PM, developer, tester and DevOps build a feature (team.enabled)"),
    ("review", "[staged|branch|path]", "Review the changes for bugs (default: uncommitted)"),
    ("webtest", "[url] [what to test]", "Start the web app and test it in a real browser"),
    (
        "playwright",
        "[setup|map|plan|generate|run|heal] [area]",
        "Playwright tests for the web app: set up, plan, write, run, heal",
    ),
    ("init", "[focus]", "Learn this project and save notes about it for future sessions"),
    ("memory", "[forget <n>]", "Notes cmcoder keeps between sessions; delete one"),
    ("index", "[status|rebuild|clear]", "Build or update the code index (code search)"),
    ("cost", "", "Token use in this session"),
    ("context", "", "How full the context window is"),
    ("exit", "", "Quit"),
]

# Rebuilds the agent with another model, keeping the same checkpointer.
SwitchModel = Callable[[str], Awaitable[None]]
# The same, with another limit on the model calls one request may use.
SetMaxTurns = Callable[[int], Awaitable[None]]
# What /turns accepts: more is a sign of a typo, and a request that long is a runaway.
MAX_TURNS_CEILING = 1000


@dataclass
class CommandResult:
    lines: list[str] = field(default_factory=list)
    prompt: str | None = None  # text to put back in the input box (after /rewind)
    exit: bool = False
    # A custom command or MCP prompt: send this as the user's message, with
    # these allow rules for that turn only (the command's `allowed-tools`).
    send: str | None = None
    allow: list[str] = field(default_factory=list)


def help_lines(custom: dict[str, SlashCommand] | None = None) -> list[str]:
    width = max(len(f"/{n} {a}") for n, a, _ in BUILT_IN)
    lines = [f"  {f'/{n} {a}'.ljust(width)}  {d}" for n, a, d in BUILT_IN]
    lines.append(f"  {'# note'.ljust(width)}  Add a line to the project's CMCODER.md")
    if custom:
        lines += ["", "Custom commands and MCP prompts:", *custom_help_lines(custom)]
    return lines


def custom_commands(session: Session) -> dict[str, SlashCommand]:
    """Markdown commands (.cmcoder/commands, ~/.cmcoder/commands) and MCP prompts.

    Read again on each use, so a new or edited file works without a restart."""
    a = session.agent
    settings = a.spec.get("settings")
    trusted = bool(settings and settings.project_trusted)
    found = CommandSource(a.ctx.project_root, trusted).load()
    mcp = a.spec.get("mcp")
    if mcp is not None:
        found.update(mcp_prompt_commands(mcp.connected()))
    return found


async def expand(command: SlashCommand, arguments: str) -> CommandResult:
    if command.mcp is not None:
        from ..mcp.manager import prompt_text

        server, prompt = command.mcp
        names = [x.name for x in (prompt.arguments or [])]
        text = await prompt_text(server, prompt.name, prompt_arguments(names, arguments))
        return CommandResult(send=text)
    return CommandResult(send=substitute(command.body, arguments), allow=command.allowed_tools)


def age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def session_lines(sessions: list[SessionInfo], now: float) -> list[str]:
    if not sessions:
        return ["No saved conversations in this project."]
    return [
        f"  {s.thread_id[:8]}  {age(now - s.updated):>8}  {s.turns:>3} turns  {s.title or '(untitled)'}"
        for s in sessions[:20]
    ]


async def turns_command(
    session: Session, arg: str, set_max_turns: SetMaxTurns | None
) -> CommandResult:
    """`/turns [N]`: how many model calls one request may use, changed while it runs.

    The limit is ModelCallLimitMiddleware's, fixed when the graph is compiled, so a change is a
    new graph on the same checkpointer (as for /model): the conversation goes on unchanged."""
    a = session.agent
    settings = a.spec.get("settings")
    subagents = settings.subagent_max_turns if settings is not None else None
    sub = f"; a subagent {subagents} (subagentMaxTurns in your settings)" if subagents else ""
    if not arg:
        return CommandResult(
            [
                f"Model calls one request may use: {a.max_turns}{sub}.",
                "Change it for this conversation window with /turns <number>, e.g. /turns 200;",
                "set maxTurns in your settings to change it for good.",
            ]
        )
    if not arg.isdigit() or not 1 <= int(arg) <= MAX_TURNS_CEILING:
        return CommandResult(
            [f"Give a whole number from 1 to {MAX_TURNS_CEILING}, e.g. /turns 200."]
        )
    if set_max_turns is None:
        return CommandResult(["Changing it isn't available here."])
    await set_max_turns(int(arg))
    return CommandResult(
        [
            f"Model calls one request may use: {session.agent.max_turns}{sub}.",
            "It holds until you close this window (maxTurns in your settings keeps it for good).",
        ]
    )


def add_memory(project_root: Path, note: str) -> Path:
    """`# note`: append a line to the project's CMCODER.md."""
    path = project_root / "CMCODER.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{sep}- {note.strip()}\n", encoding="utf-8")
    return path


def memory_command(session: Session, arg: str) -> list[str]:
    settings = session.agent.spec.get("settings")
    if settings is not None and not settings.memory.enabled:
        return ["Memory is off (memory.enabled in settings)."]
    memory = session.agent.memory
    if memory is None:
        return ["Memory is off."]
    entries = [(s, n) for s in memory.stores() for n in s.notes()]
    verb, _, ref = arg.partition(" ")
    if verb == "forget":
        ref = ref.strip()
        if ref.isdigit() and 1 <= int(ref) <= len(entries):
            store, note = entries[int(ref) - 1]
            gone = store.forget(note.slug)
        else:
            gone = next((g for s in memory.stores() if (g := s.forget(ref))), None)
        return [f"Forgot: {gone.title}"] if gone else [f"No note matches {ref!r}."]
    if not entries:
        return [
            "No notes yet. Tell cmcoder to remember something, "
            'e.g. "remember to use pytest -x"; it also saves what it learns about the project.'
        ]
    lines = [f"{len(entries)} note(s):"]
    for i, (_, n) in enumerate(entries, 1):
        tag = "yours" if n.scope == "user" else "project"
        if n.source == "agent":
            tag += ", learned by cmcoder"
        lines.append(f"  {i}. [{tag}] {n.title}: {' '.join(n.body.split())[:100]}")
    lines.append("Delete one with /memory forget <n>. New notes apply from the next session.")
    return lines


def init_command(session: Session, focus: str) -> CommandResult:
    """/init: the model explores the project and saves what it learns as notes."""
    from ..notes import init_prompt

    a = session.agent
    if a.memory is None:
        return CommandResult(['Memory is off: set "memory": {"enabled": true} in your settings.'])
    if not a.memory.auto:
        return CommandResult(
            ['/init saves what cmcoder learns: set "memory": {"auto": true} in your settings.']
        )
    titles = [n.title for n in a.memory.project.notes()]
    return CommandResult(send=init_prompt(titles, focus))


def webtest_command(session: Session, arg: str) -> CommandResult:
    """/webtest: the model starts the app and tests it with the Browser tool."""
    from ..webtest import webtest_prompt

    a = session.agent
    if a.ctx.browser is None and not a.ide_tool_names:
        return CommandResult(
            [
                "The Browser tool is off here: it is switched off in settings (browser.enabled), "
                "or this is a slim build without Playwright."
            ]
        )
    return CommandResult(send=webtest_prompt(a.ctx.project_root, arg, a.ide_tool_names))


def playwright_command(session: Session, arg: str) -> CommandResult:
    """/playwright: set up, plan, write, run and heal Playwright tests (playwright_tests/)."""
    from ..playwright_tests.command import command

    a = session.agent
    settings = a.spec.get("settings")
    if settings is None:
        return CommandResult(["The /playwright commands need the settings; none here."])
    stage = command(
        arg,
        root=a.ctx.project_root,
        settings=settings,
        ide_tools=a.ide_tool_names,
        trusted=bool(settings.project_trusted),
    )
    return CommandResult(stage.lines, send=stage.prompt, allow=stage.allow)


async def review_command(session: Session, target: str) -> CommandResult:
    """/review: collect the change, then the model reviews it (read-only)."""
    from ..review import ReviewError, collect, review_prompt

    try:
        change = await collect(session.agent.ctx.project_root, target)
    except ReviewError as e:
        return CommandResult([str(e)])
    return CommandResult(send=review_prompt(change))


async def run_command(
    session: Session,
    line: str,
    *,
    switch_model: SwitchModel | None = None,
    set_max_turns: SetMaxTurns | None = None,
) -> CommandResult:
    name, _, arg = line.lstrip("/").partition(" ")
    arg = arg.strip()
    a = session.agent
    if name in ("exit", "quit"):
        return CommandResult(exit=True)
    if name == "help":
        return CommandResult(help_lines(custom_commands(session)))
    if name == "clear":
        await session.clear()
        return CommandResult(["Started a new conversation."])
    if name == "compact":
        try:
            result = await session.compact(arg or None)
        except CompactionBlocked as e:
            return CommandResult([f"Not compacted: a PreCompact hook declined ({e})."])
        if result is None:
            return CommandResult(["Nothing to compact yet."])
        return CommandResult(
            [
                f"Summarised {result.summarized_messages} messages with {result.model}: "
                f"~{result.tokens_before} -> ~{result.tokens_after} tokens."
            ]
        )
    if name == "resume":
        if session.store is None:
            return CommandResult(["Sessions aren't saved (persistSessions is off)."])
        if not arg:
            return CommandResult(session_lines(await session.store.list(), time.time()))
        found = await session.store.find(arg)
        if found is None:
            return CommandResult([f"No single saved conversation matches {arg!r}."])
        await session.resume(found.thread_id)
        return CommandResult(
            [f"Resumed: {found.title or found.thread_id[:8]} ({found.turns} turns)."]
        )
    if name == "rewind":
        points = await session.rewind_points()
        if not arg:
            if not points:
                return CommandResult(["Nothing to rewind to yet."])
            return CommandResult(
                [
                    f"  {p.turn:>3}  {p.text}  ({p.files_changed} files changed since)"
                    for p in points
                ]
                + ["Use /rewind <turn> [code|conversation|both]."]
            )
        turn_text, _, what = arg.partition(" ")
        if not turn_text.isdigit() or int(turn_text) not in {p.turn for p in points}:
            return CommandResult([f"No message number {turn_text!r}; /rewind lists them."])
        what = what.strip() or "both"
        result = await session.rewind(
            int(turn_text),
            code=what in ("code", "both"),
            conversation=what in ("conversation", "both"),
        )
        lines = [f"  {x.action}: {x.path}" for x in result.actions]
        lines.append(
            "Rewound "
            + {"code": "the files", "conversation": "the conversation"}.get(
                what, "files and conversation"
            )
            + "."
        )
        return CommandResult(lines, prompt=result.prompt)
    if name == "mode":
        if not arg:
            return CommandResult(
                [f"Mode: {a.policy.mode} (available: {', '.join(a.policy.available_modes())})"]
            )
        try:
            a.policy.mode = arg
        except (ValueError, ModeNotAllowed) as e:
            return CommandResult([str(e)])
        return CommandResult([f"Mode: {a.policy.mode}"])
    if name == "model":
        if not arg:
            return CommandResult([f"Model: {a.bundle.ref} (small/fast: {a.small.ref})"])
        if switch_model is None:
            return CommandResult(["Switching models isn't available here."])
        await switch_model(arg)
        return CommandResult([f"Model: {session.agent.bundle.ref}"])
    if name == "turns":
        return await turns_command(session, arg, set_max_turns)
    if name == "mcp":
        from ..mcp import status_lines

        return CommandResult(status_lines(a.spec.get("mcp")))
    if name == "agents":
        rt = a.subagents
        if rt is None:
            return CommandResult(["No subagents."])
        lines = [f"  {d.name} ({d.origin}): {d.description}" for d in rt.definitions().values()]
        for r in rt.runs.values():
            lines.append(
                f"  #{r.number} {r.agent_type}: {r.description} · {r.state} · "
                f"{r.steps} steps · {r.tool_uses} tools · {r.tokens} tokens"
            )
        return CommandResult(lines)
    if name == "critic":
        if a.critic is None:
            return CommandResult(["No critic here."])
        if arg in ("on", "off"):
            a.critic.enabled = arg == "on"
        state = "on" if a.critic.enabled else "off"
        return CommandResult(
            [f"Critique is {state} (up to {a.critic.max_rounds} reviews per answer)."]
        )
    if name == "memory":
        return CommandResult(memory_command(session, arg))
    if name == "init":
        return init_command(session, arg)
    if name == "review":
        return await review_command(session, arg)
    if name == "webtest":
        return webtest_command(session, arg)
    if name == "playwright":
        return playwright_command(session, arg)
    if name == "index":
        from ..rag.session import index_command

        return CommandResult(await index_command(session, arg))
    if name == "team":
        if not a.team:
            return CommandResult(
                ['The team is off: set "team": {"enabled": true} in your settings.']
            )
        if not arg:
            return CommandResult(["Usage: /team <the feature to build>"])
        return CommandResult(
            send="Run the Team tool for this feature request (pass it on complete, "
            f"as `feature`), then summarise the team's result for me:\n\n{arg}"
        )
    if name == "todos":
        todos = await session.todos()
        marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        return CommandResult(
            [f"  {marks.get(t.get('status', ''), '[ ]')} {t.get('content', '')}" for t in todos]
            or ["No todo list."]
        )
    if name == "cost":
        u = session.usage
        cost = f" Cost: ${session.cost:.4f} (from the gateway)." if session.cost else ""
        return CommandResult(
            [f"Tokens this session: {u['prompt_tokens']} in, {u['completion_tokens']} out.{cost}"]
        )
    if name == "context":
        from langchain_core.messages.utils import count_tokens_approximately

        used = count_tokens_approximately(await session.messages())
        window = a.bundle.profile.context_window
        return CommandResult(
            [
                f"Conversation: ~{used} tokens of a {window}-token window "
                f"({100 * used // max(1, window)}%; {a.bundle.profile.context_window_source}). "
                f"Auto-compaction at {int(a.auto_compact_threshold * 100)}%."
            ]
        )
    if (command := custom_commands(session).get(name)) is not None:
        return await expand(command, arg)
    return CommandResult([f"Unknown command /{name}. /help lists them."])
