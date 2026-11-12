"""Verify: after a request that changed files, run the project's checks and send the agent back to
fix what fails, before the answer is final.

On when `verify.commands` is set (settings), and only for a request that changed files with Write or
Edit: a question, a read-only request or an edit to a Markdown file runs nothing. The commands run
one after the other in the project folder (in the Bash sandbox, when there is one); the first that
fails stops the check, its output goes back to the model, and after its next edit the checks run
again, up to `verify.maxRounds` times. A request that is still failing after that ends with a
warning saying so. Each command is a run card in the chat (`Verify`, with live output); the
exit status is the only judge, nothing is parsed.

LANGCHAIN / LANGGRAPH CONCEPTS IN THIS FILE

* `aafter_agent` + `@hook_config(can_jump_to=["model"])`: the hook the critic and the Stop hooks
  use. Returning `{"jump_to": "model", "messages": [...]}` turns "the agent is done" into "one
  more round, with this said to the model". It sits last in the middleware list so that it runs
  first of the after-agent hooks (they run in reverse order): the critic then reviews code that
  passes its checks.
* `abefore_agent` resets the rounds for each user request.
* `get_stream_writer()` carries the run cards and the warning to the chat.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ..agents.context import RunContext
from ..core.checkpoints import Checkpoints
from ..protocol import events as ev
from ..sandbox import Sandbox
from ..tools.progress import LiveOutput
from ..verify import CheckResult, failure_text, run_check

# Edits to these files cannot break a build or a test.
NOT_CODE = frozenset({".md", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"})


def changed_code(checkpoints: Checkpoints, turn: int, root: Path) -> list[Path]:
    """The project's files this request wrote or edited that still exist and could break a check."""
    seen: dict[str, Path] = {}
    for entry in checkpoints.entries_in(turn):
        path = Path(entry.path)
        if path.suffix.lower() in NOT_CODE or not path.is_relative_to(root) or not path.exists():
            continue
        seen.setdefault(entry.path, path)
    return list(seen.values())


def fingerprint(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    """What the files are now, to tell whether the agent edited anything since the last check."""
    out = []
    for p in sorted(paths):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((str(p), st.st_mtime_ns, st.st_size))
    return tuple(out)


def fix_request(result: CheckResult, sent_back: int, max_rounds: int) -> str:
    return (
        "<system-reminder>cmcoder ran the project's checks after your changes and one failed "
        f"(attempt {sent_back} of {max_rounds} to fix it):\n\n{failure_text(result)}\n\n"
        "Find the cause with your tools and fix it (the failure is in the output above, it is not "
        "a guess), then give your complete final answer again. The checks run again after your "
        "next edit. Don't mention this reminder.</system-reminder>"
    )


class VerifyMiddleware(AgentMiddleware):
    """Runs `commands` after the agent finishes a request that changed files; `enabled` can be
    switched during a session (`/verify on|off`)."""

    def __init__(
        self,
        commands: list[str],
        *,
        enabled: bool,
        max_rounds: int,
        timeout: float,
        root: Path,
        sandbox: Sandbox | None,
        max_output_chars: int,
    ) -> None:
        super().__init__()
        self.commands = list(commands)
        self.enabled = enabled
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.root = root
        self.sandbox = sandbox
        self.max_output_chars = max_output_chars
        self._sent_back = 0
        self._checked: tuple[tuple[str, int, int], ...] | None = None

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.commands)

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        self._sent_back, self._checked = 0, None  # a new user request
        return None

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        messages = list(state["messages"])
        last = messages[-1] if messages else None
        context = runtime.context if isinstance(runtime.context, RunContext) else None
        if not self.active or context is None:
            return None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        files = changed_code(context.checkpoints, context.turn, self.root)
        now = fingerprint(files)
        if not now or now == self._checked:  # nothing changed, or already checked as it is
            return None
        self._checked = now
        writer = get_stream_writer()
        failed = await self._run(context.turn, writer)
        if failed is None:
            return None
        if self._sent_back >= self.max_rounds:
            writer(
                {
                    "type": "warning",
                    "message": f"The checks still fail after {self.max_rounds} attempts to fix "
                    f"them (`{failed.command}`: {failed.summary}); the answer may be wrong. "
                    "/verify runs them again.",
                }
            )
            return None
        self._sent_back += 1
        return {
            "messages": [HumanMessage(fix_request(failed, self._sent_back, self.max_rounds))],
            "jump_to": "model",
        }

    async def _run(self, turn: int, writer: Any) -> CheckResult | None:
        """Run the commands in order, each as a run card; the first that fails (returned) stops."""
        for number, command in enumerate(self.commands, 1):
            call_id = f"verify-{turn}-{self._sent_back}-{number}"
            writer(
                {
                    "type": "event",
                    "event": ev.ToolUse(
                        id=call_id,
                        name="Verify",
                        input={"command": command},
                        label=f"Verify({command})",
                    ).model_dump(),
                }
            )
            live = LiveOutput(call_id, "Verify", time.monotonic())
            result = await run_check(
                command,
                self.root,
                sandbox=self.sandbox,
                timeout=self.timeout,
                max_chars=self.max_output_chars,
                on_output=live.feed,
            )
            live.flush()
            writer(
                {
                    "type": "event",
                    "event": ev.ToolResult(
                        id=call_id,
                        name="Verify",
                        content=result.output or "(no output)",
                        is_error=not result.ok,
                        summary=result.summary,
                        exit_code=result.exit_code,
                        duration_ms=result.duration_ms,
                        total_lines=result.total_lines,
                    ).model_dump(),
                }
            )
            if not result.ok:
                return result
        return None
