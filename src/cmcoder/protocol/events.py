"""Agent Protocol events: what the agent core emits to front ends.

Used by `-p --output-format stream-json` and by `--protocol stdio` (the VS
Code extension), whose client messages are in `messages.py`. These pydantic
models are the single source of truth; TypeScript types are generated from
`protocol_json_schema()`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1


class _Event(BaseModel):
    pass


class SystemInit(_Event):
    type: Literal["system_init"] = "system_init"
    protocol_version: int = PROTOCOL_VERSION
    session_id: str
    cwd: str
    model: str
    provider: str
    tools: list[str]
    permission_mode: str
    critique: bool = False  # the critic reviews answers (see critique_changed)
    # Which cmcoder this is ("0.1.0 (aa3dc83 2026-10-07)"), for the IDE's log.
    version: str = ""
    # What it can do beyond the base protocol: "images" (user_message images).
    # An older cmcoder sends none, and front ends then don't send images to it.
    features: list[str] = Field(default_factory=list)


class AssistantDelta(_Event):
    type: Literal["assistant_delta"] = "assistant_delta"
    text: str


class ReasoningDelta(_Event):
    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class AssistantMessage(_Event):
    type: Literal["assistant_message"] = "assistant_message"
    text: str
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None


class ToolUse(_Event):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]
    label: str
    # Set on a subagent's events: the id of the Task call that started it.
    parent_tool_use_id: str | None = None


class ToolResult(_Event):
    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str
    content: str
    is_error: bool = False
    summary: str | None = None
    # Set on a subagent's events: the id of the Task call that started it.
    parent_tool_use_id: str | None = None
    # Pictures to show under the result (a browser screenshot), as data URLs.
    images: list[str] = []
    # Bash: how it ended. exit_code is None when the shell died, the command timed out or
    # the user stopped it; total_lines is the output's length before any shortening.
    exit_code: int | None = None
    duration_ms: int | None = None
    total_lines: int | None = None
    stopped: bool = False


class ToolProgress(_Event):
    """Output a tool has produced so far (Bash): `text` is the new part, appended to
    what came before. Sent while the tool runs, so the chat can show it live."""

    type: Literal["tool_progress"] = "tool_progress"
    id: str  # the tool call's id (`tool_use.id`)
    name: str
    text: str
    elapsed_ms: int
    # Set on a subagent's events: the id of the Task call that started it.
    parent_tool_use_id: str | None = None


class PermissionDenied(_Event):
    type: Literal["permission_denied"] = "permission_denied"
    id: str
    name: str
    reason: str
    # Set on a subagent's events: the id of the Task call that started it.
    parent_tool_use_id: str | None = None


class UsageUpdate(_Event):
    type: Literal["usage"] = "usage"
    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False
    cost: float | None = None
    context_window: int | None = None


class Warning(_Event):  # noqa: A001 - protocol name
    type: Literal["warning"] = "warning"
    message: str


# What an IDE can offer next to an error ("Retry", ...): the engine says which make sense for it.
ErrorAction = Literal["retry", "switch_model", "compact", "new_conversation", "open_log"]


class Error(_Event):
    type: Literal["error"] = "error"
    kind: str
    message: str
    hint: str | None = None
    # A plain-language headline ("The model gateway is not answering (502)") and what the user can do
    # about it. Empty for errors that have neither (an IDE then shows `message` and `hint` as a line).
    title: str = ""
    actions: list[ErrorAction] = Field(default_factory=list)


class Compacted(_Event):
    """The older part of the conversation was replaced by a summary."""

    type: Literal["compacted"] = "compacted"
    trigger: Literal["auto", "manual"]
    summarized_messages: int
    tokens_before: int  # estimates
    tokens_after: int
    model: str


class FileChange(BaseModel):
    """A proposed file change, for review in a diff editor."""

    path: str  # absolute
    before: str | None  # None: a new file
    after: str


class DiffLine(BaseModel):
    kind: Literal["ctx", "add", "del"]
    text: str


class ChangedHunk(BaseModel):
    """A part of a file's change: the changed lines with a few around them."""

    id: int  # what `/revert` names
    old_start: int  # 1-based line in the file as it was
    new_start: int  # 1-based line in the file as it is
    lines: list[DiffLine]
    reverted: bool = False


class ChangedFile(BaseModel):
    index: int  # what `/revert` names
    path: str  # relative to the project when it is inside it
    status: Literal["modified", "added"]
    added: int  # lines
    removed: int
    hunks: list[ChangedHunk]
    binary: bool = False  # no text diff: only the whole file can be taken back
    truncated: bool = (
        False  # a rewrite too big to draw: counted, and the whole file can be taken back
    )
    reverted: bool = False  # taken back completely
    note: str = ""  # why the last attempt to take it back was refused


class TurnChanges(_Event):
    """The files a request wrote or edited, for review. Sent at the end of the request, and again
    (with `reverted` marks) each time part of it is taken back with `/revert`."""

    type: Literal["turn_changes"] = "turn_changes"
    turn: int
    files: list[ChangedFile]
    added: int
    removed: int


class PermissionRequest(_Event):
    """The agent needs the user's approval for a tool call (stdio protocol).

    Answer with a `permission_response` carrying the same `request_id`."""

    type: Literal["permission_request"] = "permission_request"
    request_id: str
    tool_use_id: str
    name: str
    label: str
    input: dict[str, Any]
    suggested_rule: str
    reason: str = ""
    # False for high-risk commands: offer "allow once" only, never "always".
    can_remember: bool = True
    # Write/Edit: the whole file before and after (absent for big files).
    change: FileChange | None = None
    # Bash: the folder the command runs in.
    cwd: str | None = None
    # Why it is asked, in a few words each ("outside the project", "recursive delete").
    reasons: list[str] = []
    # A command that needs a person's approval every time (no "always allow").
    high_risk: bool = False
    # A subagent's name; None for the main agent.
    agent: str | None = None


class TodoUpdate(_Event):
    """The todo list changed (stdio protocol)."""

    type: Literal["todo_update"] = "todo_update"
    todos: list[dict[str, Any]]


class ModeChanged(_Event):
    type: Literal["mode_changed"] = "mode_changed"
    mode: str


class ModelChanged(_Event):
    type: Literal["model_changed"] = "model_changed"
    model: str
    context_window: int | None = None


class IdeToolRequest(_Event):
    """Run an IDE tool in the client; answer with `ide_tool_result`."""

    type: Literal["ide_tool_request"] = "ide_tool_request"
    request_id: str
    name: str
    input: dict[str, Any]


class SessionSummary(BaseModel):
    id: str
    title: str
    updated: float  # Unix time
    messages: int


class SessionList(_Event):
    type: Literal["session_list"] = "session_list"
    sessions: list[SessionSummary]


class AgentInfo(BaseModel):
    """An agent a conversation can be switched to (`set_agent`): a subagent definition."""

    name: str
    description: str  # one line (the definition's, without its examples)
    origin: Literal["built-in", "user", "project"]
    # False: listed but not available, and `hint` says why (a project's agents are used only
    # once the project is trusted: `cmcoder trust`).
    usable: bool = True
    hint: str = ""
    # A heading of its own in the list ("Playwright"), instead of its origin's.
    group: str = ""


class AgentList(_Event):
    """The agents to choose from, and the one in use (None: the default). Sent at start and after
    each switch."""

    type: Literal["agent_list"] = "agent_list"
    agents: list[AgentInfo]
    current: str | None = None


class AgentChanged(_Event):
    """The conversation now talks to this agent (None: the default)."""

    type: Literal["agent_changed"] = "agent_changed"
    agent: str | None = None
    # "step": a command (/playwright plan) runs this one request as the matching agent; "restore":
    # the agent from before it is back (nothing to tell the user); "": the user chose it.
    reason: Literal["", "step", "restore"] = ""


class CommandInfo(BaseModel):
    name: str  # without the "/"
    description: str
    argument_hint: str
    origin: Literal["built-in", "user", "project", "mcp"]


class CommandList(_Event):
    """The slash commands a `user_message` can start with (for completion)."""

    type: Literal["command_list"] = "command_list"
    commands: list[CommandInfo]


class RewindPoint(BaseModel):
    turn: int
    text: str  # the user message (shortened)
    files_changed: int  # files the agent changed since, restorable
    outside_files: list[str]  # of those, the ones outside the project


class RewindPoints(_Event):
    """Earlier user messages to rewind to (answer with a `rewind` message)."""

    type: Literal["rewind_points"] = "rewind_points"
    points: list[RewindPoint]


class RewindAction(BaseModel):
    path: str
    action: str  # "restored", "deleted", "skipped (too big)", "failed: ..."


class Rewound(_Event):
    """The result of a `rewind`. When the conversation was rewound, a
    `history` event follows with the conversation as it is now."""

    type: Literal["rewound"] = "rewound"
    code: bool
    conversation: bool
    actions: list[RewindAction]
    prompt: str | None  # the rewound message, to edit and send again


class HistoryItem(BaseModel):
    role: Literal["user", "assistant", "tool"]
    text: str  # for "tool": the call's label, e.g. Edit(app.py)


class History(_Event):
    """A resumed conversation so far, so the client can show it."""

    type: Literal["history"] = "history"
    messages: list[HistoryItem]


class CodeContextItem(BaseModel):
    path: str
    start_line: int
    end_line: int
    symbol: str | None = None
    score: float


class CodeContext(_Event):
    """Code from the project's index added to the user's message (automatic context)."""

    type: Literal["code_context"] = "code_context"
    items: list[CodeContextItem]
    tokens: int  # estimate

    def summary(self) -> str:
        where = ", ".join(f"{i.path}:{i.start_line}-{i.end_line}" for i in self.items[:4])
        more = f" and {len(self.items) - 4} more" if len(self.items) > 4 else ""
        return f"Added code from the index: {where}{more} (≈{self.tokens:,} tokens)"


class ImagesDescribing(_Event):
    """The main model can't see images: the vision model (settings `visionModel`)
    is reading the user's images now (ImagesDescribed, or an Error, follows)."""

    type: Literal["images_describing"] = "images_describing"
    model: str  # the vision model
    count: int

    def summary(self) -> str:
        return f"{self.model} is reading the image{'s' if self.count > 1 else ''}…"


class ImagesDescribed(_Event):
    """The main model can't see images: the vision model (settings `visionModel`)
    described the user's images, and the descriptions went to it instead."""

    type: Literal["images_described"] = "images_described"
    model: str  # the vision model
    count: int

    def summary(self) -> str:
        what = "1 image" if self.count == 1 else f"{self.count} images"
        return f"{self.model} described {what} for the main model (it can't see images)"


class IndexStatus(_Event):
    """Code search's state for this project (VS Code's status bar)."""

    type: Literal["index_status"] = "index_status"
    set_up: bool  # an embedding model is configured and code search isn't off
    active: bool  # this session searches the index
    model: str | None = None
    store: str | None = None
    files: int = 0
    chunks: int = 0
    updated: float | None = None  # seconds since the epoch
    read_only: bool = False
    updating: bool = False
    error: str | None = None
    lines: list[str] = Field(default_factory=list)  # the same text as /index status


class IndexProgress(_Event):
    type: Literal["index_progress"] = "index_progress"
    done: int
    total: int
    chunks: int


class RagCandidatesList(_Event):
    type: Literal["rag_candidates"] = "rag_candidates"
    likely: list[str]  # "provider:model" that look like embedding models
    other: list[str]
    errors: dict[str, str]  # provider -> why its models couldn't be listed


class RagSetupResult(_Event):
    type: Literal["rag_setup_result"] = "rag_setup_result"
    ok: bool
    message: str
    settings_file: str | None = None


SubagentState = Literal[
    "queued",  # waiting for a free slot (maxParallelSubagents)
    "running",
    "waiting",  # for the user's answer to a permission question
    "stopping",  # asked to stop: writing its report
    "done",
    "limit",  # reached its step limit; wrote its report
    "stopped",  # stopped by the user (with a report when it had started)
    "failed",
]


class SubagentStatus(_Event):
    """A subagent (Task call) changed: sent when it is queued, starts, calls a
    tool, waits for a permission, and ends. The front ends draw the agent map
    from these; `stop_subagent` stops one."""

    type: Literal["subagent_status"] = "subagent_status"
    id: str  # the Task call's id (its events carry it as parent_tool_use_id)
    number: int  # 1, 2, ... in this session (`/agents <number>`)
    description: str
    agent_type: str
    model: str
    state: SubagentState
    steps: int  # model calls so far
    max_steps: int
    tool_uses: int
    tokens: int  # prompt + completion, all its model calls
    elapsed_ms: int
    activity: str = ""  # what it's doing: a tool call's label, "" while the model works


class CritiqueChanged(_Event):
    """Critique was switched on or off: by `set_critique`, `/critic`, or the
    saved setting (`critic.enabled` in the user's settings) changed in
    another window or the terminal."""

    type: Literal["critique_changed"] = "critique_changed"
    enabled: bool
    # "you": this client asked for it; "saved": the saved setting changed elsewhere.
    source: Literal["you", "saved"] = "you"


class ReviewIssue(BaseModel):
    severity: Literal["high", "medium", "low"] = "medium"
    problem: str
    where: str = ""
    fix: str = ""


class ReviewResult(_Event):
    """Critique (`/critic on`): the critic's verdict on the turn's answer.
    Not final: the agent is fixing the problems and will be reviewed again.
    Final: the answer has just been shown (passed, out of rounds, or not
    reviewed: verdict "none")."""

    type: Literal["review_result"] = "review_result"
    round: int
    max_rounds: int
    verdict: Literal["pass", "fail", "none"]
    final: bool
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)


class Result(_Event):
    type: Literal["result"] = "result"
    subtype: Literal["success", "error", "max_turns", "interrupted"]
    is_error: bool
    result: str
    num_turns: int
    duration_ms: int
    usage: dict[str, Any]
    session_id: str
    # With critique on: {"verdict": "pass"|"fail"|"none", "rounds": n, "issues": [...]}.
    review: dict[str, Any] | None = None


Event = Annotated[
    SystemInit
    | AssistantDelta
    | ReasoningDelta
    | AssistantMessage
    | ToolUse
    | ToolResult
    | ToolProgress
    | PermissionDenied
    | UsageUpdate
    | Warning
    | Error
    | Compacted
    | PermissionRequest
    | TodoUpdate
    | ModeChanged
    | TurnChanges
    | TurnChanges
    | ModelChanged
    | IdeToolRequest
    | AgentList
    | AgentChanged
    | SessionList
    | CommandList
    | RewindPoints
    | Rewound
    | History
    | CodeContext
    | ImagesDescribing
    | ImagesDescribed
    | IndexStatus
    | IndexProgress
    | RagCandidatesList
    | SubagentStatus
    | ReviewResult
    | CritiqueChanged
    | RagSetupResult
    | Result,
    Field(discriminator="type"),
]

_adapter: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(data: dict[str, Any]) -> Event:
    return _adapter.validate_python(data)


def protocol_json_schema() -> dict[str, Any]:
    return _adapter.json_schema()
