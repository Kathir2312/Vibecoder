"""Background jobs: a command that keeps running (a dev server, a test watcher) while the agent goes on.

`Bash` with `run_in_background` starts one and returns at once with its number; the `Jobs` tool
reads what it printed (and can wait a few seconds for more) or ends it; `/jobs` shows them to the
user, and the chat lists them with a Stop button (a `jobs_changed` event). Each job is a
`PersistentShell` of its own, so it runs where Bash would run it (in the sandbox, or outside it when
the user allowed that), the whole process tree is stopped with it, and its output arrives live. Jobs
belong to the conversation's agent and live through its rebuilds (a model switch, a fallback); they
are stopped when the program ends.

LANGCHAIN / LANGGRAPH: nothing here; these are cmcoder `Tool`s like Bash (tools/adapter.py wraps them
for the graph). LangChain has nothing for processes that outlive a tool call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ..protocol import events as ev
from .base import Tool, ToolContext, ToolInput, ToolResult, truncate_middle
from .shell import PersistentShell

if TYPE_CHECKING:
    from ..sandbox import Sandbox

MAX_RUNNING = 5  # at the same time
KEEP_FINISHED = 5  # finished jobs still listed
MAX_JOB_SECONDS = 8 * 3600  # a job nobody stops is ended after this
MAX_OUTPUT_CHARS = 200_000  # kept per job (the end of it)
TAIL_LINES = 12  # shown in the chat's list
TAIL_CHARS = 1_500
NOTIFY_EVERY = 1.0  # seconds between two "output changed" updates to the chat
STOP_WAIT = 5.0  # seconds to wait for a stopped job to be gone

State = Literal["running", "exited", "stopped", "failed"]


class JobError(Exception):
    """A job cannot be started or found; the message is for the model and the user."""


@dataclass
class Job:
    id: int
    command: str
    started: float
    outside_sandbox: bool
    runner: PersistentShell | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # new output, or it ended
    output: str = ""
    state: State = "running"
    exit_code: int | None = None
    ended: float | None = None
    note: str = ""  # why it failed, or that it timed out

    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def tail(self, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
        text = (
            "\n".join(self.output.rstrip("\n").split("\n")[-lines:]) if self.output.strip() else ""
        )
        return text[-chars:]


def duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


class JobManager:
    """The background jobs of one agent. `on_change` is told when the list, a state or (at most
    once a second) the output of a job changes."""

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        self.sandbox = sandbox
        self.on_change: Callable[[], None] | None = None
        self.jobs: dict[int, Job] = {}
        self._next = 1
        self._pending: asyncio.TimerHandle | None = None

    # --- starting and finding ---------------------------------------------------------------
    def start(self, command: str, cwd: Path, shell: str, *, sandboxed: bool) -> Job:
        if sum(j.state == "running" for j in self.jobs.values()) >= MAX_RUNNING:
            raise JobError(
                f"{MAX_RUNNING} background jobs are already running: stop one first (Jobs stop)."
            )
        job = Job(
            self._next,
            command,
            time.monotonic(),
            outside_sandbox=bool(self.sandbox) and not sandboxed,
        )
        self._next += 1
        job.runner = PersistentShell(cwd, shell, sandbox=self.sandbox if sandboxed else None)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job))
        self._prune()
        self._notify(now=True)
        return job

    def get(self, job_id: int) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            known = ", ".join(str(i) for i in self.jobs) or "none"
            raise JobError(f"There is no background job {job_id} (jobs: {known}).")
        return job

    def _prune(self) -> None:
        finished = [j for j in self.jobs.values() if j.state != "running"]
        for job in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            del self.jobs[job.id]

    # --- running ----------------------------------------------------------------------------
    async def _run(self, job: Job) -> None:
        assert job.runner is not None

        def feed(text: str) -> None:
            job.output = (job.output + text)[-MAX_OUTPUT_CHARS:]
            job.changed.set()
            self._notify(now=False)

        try:
            res = await job.runner.run(job.command, MAX_JOB_SECONDS, on_output=feed, stop=job.stop)
            if res.stopped:
                job.state = "stopped"
            elif res.timed_out:
                job.state, job.note = "stopped", f"ended after {duration(MAX_JOB_SECONDS)}"
            else:
                job.state, job.exit_code = "exited", res.exit_code
                if res.exit_code is None:  # the shell itself went away
                    job.state, job.note = "failed", "the shell ended"
        except Exception as e:  # never lets a job take the agent down
            job.state, job.note = "failed", f"{type(e).__name__}: {e}"
        finally:
            job.ended = time.monotonic()
            await job.runner.close()
            job.changed.set()
            self._notify(now=True)

    async def stop(self, job_id: int) -> Job:
        job = self.get(job_id)
        if job.state == "running":
            job.stop.set()
            if job.task is not None:
                await asyncio.wait({job.task}, timeout=STOP_WAIT)
        return job

    async def close(self) -> None:
        """End every running job (the program is ending)."""
        self.on_change = None
        running = [j for j in self.jobs.values() if j.state == "running"]
        for job in running:
            job.stop.set()
        tasks = {j.task for j in running if j.task is not None}
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_WAIT)

    # --- telling the chat -------------------------------------------------------------------
    def _notify(self, *, now: bool) -> None:
        if self.on_change is None:
            return
        if now:
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None
            self.on_change()
        elif self._pending is None:
            self._pending = asyncio.get_running_loop().call_later(NOTIFY_EVERY, self._fire)

    def _fire(self) -> None:
        self._pending = None
        if self.on_change is not None:
            self.on_change()

    def snapshot(self) -> ev.JobsChanged:
        return ev.JobsChanged(
            jobs=[
                ev.JobInfo(
                    id=j.id,
                    command=j.command.strip()[:300],
                    state=j.state,
                    exit_code=j.exit_code,
                    elapsed_ms=int(j.elapsed() * 1000),
                    tail=j.tail(),
                    outside_sandbox=j.outside_sandbox,
                    note=j.note,
                )
                for j in self.jobs.values()
            ]
        )


def status_line(job: Job) -> str:
    """One line about a job, for the model and for `/jobs`."""
    where = ", outside the sandbox" if job.outside_sandbox else ""
    if job.state == "running":
        return f"job {job.id} running for {duration(job.elapsed())}{where}: {job.command.strip()[:120]}"
    how = {
        "exited": f"exited with code {job.exit_code}",
        "stopped": "stopped",
        "failed": "failed",
    }[job.state]
    note = f" ({job.note})" if job.note else ""
    return f"job {job.id} {how}{note} after {duration(job.elapsed())}{where}: {job.command.strip()[:120]}"


def _jobs(ctx: ToolContext) -> JobManager:
    if ctx.jobs is None:
        raise JobError("Background jobs are not available here.")
    return ctx.jobs


class JobsInput(ToolInput):
    action: Literal["output", "stop"] = Field(
        description="output: what the job has printed so far; stop: end it."
    )
    job_id: int = Field(description="The job's number, from Bash's answer.")
    lines: int = Field(40, ge=1, le=400, description="output: how many of the last lines.")
    wait_seconds: int = Field(
        0, ge=0, le=30, description="output: wait up to this long for new output first."
    )


class JobsTool(Tool):
    name = "Jobs"
    description = (
        "Read the output of a background job (started with Bash run_in_background) and see "
        "whether it still runs, or stop it. wait_seconds gives a starting server time to print."
    )
    Input = JobsInput
    # It reads a job's output or ends a process this session started itself, nothing on disk:
    # no question, like TodoWrite.
    read_only = True

    def describe(self, args: JobsInput, ctx: ToolContext) -> str:
        return f"Jobs({args.action} {args.job_id})"

    async def run(self, args: JobsInput, ctx: ToolContext) -> ToolResult:
        try:
            manager = _jobs(ctx)
            if args.action == "stop":
                job = await manager.stop(args.job_id)
                return ToolResult(status_line(job), summary=f"job {job.id} {job.state}")
            job = manager.get(args.job_id)
        except JobError as e:
            return ToolResult(str(e), is_error=True)
        if args.wait_seconds and job.state == "running":
            job.changed.clear()
            try:
                await asyncio.wait_for(job.changed.wait(), args.wait_seconds)
            except TimeoutError:
                pass
        text = job.tail(args.lines, ctx.max_output_chars)
        body = truncate_middle(text, ctx.max_output_chars) if text else "(no output yet)"
        return ToolResult(f"{status_line(job)}\n\n{body}", summary=f"job {job.id} {job.state}")


def start_background(args: Any, ctx: ToolContext, shell: str, *, sandboxed: bool) -> ToolResult:
    """What `Bash` does for `run_in_background`."""
    try:
        cwd = (ctx.shell.current_cwd if ctx.shell is not None else None) or ctx.cwd
        job = _jobs(ctx).start(args.command, cwd, shell, sandboxed=sandboxed)
    except JobError as e:
        return ToolResult(str(e), is_error=True)
    return ToolResult(
        f"Started background job {job.id}: {args.command.strip()[:200]}\n"
        f"It keeps running while you go on. Read what it printed with Jobs(action=output, "
        f"job_id={job.id}, wait_seconds=5), end it with Jobs(action=stop, job_id={job.id}). "
        "It is stopped when this session ends.",
        summary=f"job {job.id} started",
    )
