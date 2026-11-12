"""`/jobs`: the background jobs (commands started with `run_in_background`).

`/jobs`               list them
`/jobs output <n>`    what job n printed (the last 40 lines; `/jobs output <n> <lines>` for more)
`/jobs stop <n>`      stop job n (`/jobs stop all` stops every running one)

LANGCHAIN / LANGGRAPH: nothing here; see tools/jobs.py.
"""

from __future__ import annotations

from ..tools.jobs import JobError, status_line
from .commands import CommandResult
from .session import Session

USAGE = "Usage: /jobs [output <n> [lines] | stop <n|all>]"


async def jobs_lines(session: Session, arg: str) -> CommandResult:
    manager = session.agent.ctx.jobs
    if manager is None:
        return CommandResult(["Background jobs are not available here."])
    words = arg.split()
    if not words:
        if not manager.jobs:
            return CommandResult(
                ["No background jobs. The agent starts one with Bash's run_in_background."]
            )
        return CommandResult([status_line(j) for j in manager.jobs.values()])
    try:
        if words[0] == "stop" and len(words) == 2:
            # The chat's list shows the result by itself (a `jobs_changed` event): no message.
            if words[1] == "all":
                running = [j.id for j in manager.jobs.values() if j.state == "running"]
                for job_id in running:
                    await manager.stop(job_id)
                lines = [f"Stopped {len(running)} job(s)."]
            else:
                lines = [status_line(await manager.stop(int(words[1])))]
            return CommandResult(lines, events=[manager.snapshot()], quiet=True)
        if words[0] == "output" and len(words) in (2, 3):
            job = manager.get(int(words[1]))
            tail = job.tail(int(words[2]) if len(words) == 3 else 40, 20_000)
            return CommandResult([status_line(job), "", tail or "(no output yet)"])
    except JobError as e:
        return CommandResult([str(e)])
    except ValueError:
        pass
    return CommandResult([USAGE])
