"""Background jobs: a command that keeps running while the agent goes on (`Bash` with
`run_in_background`), read and stopped with the `Jobs` tool, listed by `/jobs` and in the chat."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import rebuild_agent
from cmcoder.agents.subagents import NOT_FOR_SUBAGENTS
from cmcoder.compat import find_shell
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import BUILT_IN, run_command
from cmcoder.tools.base import ToolContext
from cmcoder.tools.bash import BashInput, BashTool
from cmcoder.tools.jobs import (
    MAX_OUTPUT_CHARS,
    MAX_RUNNING,
    JobError,
    JobManager,
    JobsInput,
    JobsTool,
)

from .conftest import API_KEY
from .helpers import make_session, run_turn
from .test_stdio import Agent

pytestmark = pytest.mark.skipif(find_shell() is None, reason="needs bash")


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


@pytest.fixture
async def manager(project: Path) -> Any:
    m = JobManager(None)
    yield m
    await m.close()


async def finished(manager: JobManager, job_id: int, seconds: float = 20) -> None:
    job = manager.get(job_id)
    assert job.task is not None
    await asyncio.wait_for(job.task, seconds)


def shell() -> str:
    found = find_shell()
    assert found is not None
    return found


# --- the manager ------------------------------------------------------------------------------


async def test_a_job_runs_on_while_it_prints_and_stopping_it_ends_it(
    manager: JobManager, project: Path
) -> None:
    job = manager.start("echo ready; sleep 60", project, shell(), sandboxed=False)
    assert job.id == 1 and job.state == "running"
    job.changed.clear()
    await asyncio.wait_for(job.changed.wait(), 10)
    assert "ready" in job.output and job.state == "running"
    stopped = await manager.stop(1)
    assert stopped.state == "stopped" and stopped.ended is not None
    assert stopped.elapsed() < 30


async def test_a_job_that_ends_on_its_own_keeps_its_exit_code(
    manager: JobManager, project: Path
) -> None:
    job = manager.start("echo one; (exit 3)", project, shell(), sandboxed=False)
    await finished(manager, job.id)
    assert (job.state, job.exit_code) == ("exited", 3) and "one" in job.output
    ok = manager.start("true", project, shell(), sandboxed=False)
    await finished(manager, ok.id)
    assert (ok.state, ok.exit_code) == ("exited", 0)


async def test_no_more_than_five_run_at_once_and_a_finished_one_makes_room(
    manager: JobManager, project: Path
) -> None:
    for _ in range(MAX_RUNNING):
        manager.start("sleep 60", project, shell(), sandboxed=False)
    with pytest.raises(JobError, match="5 background jobs are already running"):
        manager.start("sleep 60", project, shell(), sandboxed=False)
    await manager.stop(1)
    assert manager.start("sleep 60", project, shell(), sandboxed=False).id == MAX_RUNNING + 1


async def test_only_the_end_of_a_very_long_output_is_kept(
    manager: JobManager, project: Path
) -> None:
    job = manager.start(
        "for i in $(seq 1 12000); do echo line$i-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done",
        project,
        shell(),
        sandboxed=False,
    )
    await finished(manager, job.id, 60)
    assert len(job.output) <= MAX_OUTPUT_CHARS
    assert job.output.rstrip().endswith("line12000-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    assert "line1-" not in job.output


async def test_the_chat_is_told_when_a_job_starts_prints_and_ends(
    manager: JobManager, project: Path
) -> None:
    told: list[ev.JobsChanged] = []
    manager.on_change = lambda: told.append(manager.snapshot())
    job = manager.start("echo hello; sleep 0.3", project, shell(), sandboxed=False)
    assert told[0].jobs[0].state == "running" and told[0].jobs[0].id == job.id
    await finished(manager, job.id)
    last = told[-1].jobs[0]
    assert (last.state, last.exit_code) == ("exited", 0)
    assert last.tail == "hello" and last.elapsed_ms >= 250
    assert len(told) <= 4, "output is sent at most once a second, not line by line"


async def test_closing_ends_every_running_job(project: Path) -> None:
    m = JobManager(None)
    jobs = [m.start("sleep 60", project, shell(), sandboxed=False) for _ in range(2)]
    await m.close()
    assert [j.state for j in jobs] == ["stopped", "stopped"]


async def test_unknown_jobs_are_said_so(manager: JobManager, project: Path) -> None:
    manager.start("sleep 60", project, shell(), sandboxed=False)
    with pytest.raises(JobError, match=r"There is no background job 9 \(jobs: 1\)"):
        manager.get(9)


# --- the tools --------------------------------------------------------------------------------


async def test_bash_starts_a_job_at_once_and_the_job_tools_read_and_stop_it(project: Path) -> None:
    ctx = ToolContext(cwd=project, project_root=project)
    ctx.jobs = JobManager(None)
    try:
        started = await BashTool().run(
            BashInput.model_validate(
                dict(command="echo server-up; sleep 60", run_in_background=True)
            ),
            ctx,
        )
        assert not started.is_error and "Started background job 1" in started.content
        assert "Jobs(action=output, job_id=1" in started.content
        read = await JobsTool().run(
            JobsInput.model_validate(dict(action="output", job_id=1, wait_seconds=10)), ctx
        )
        assert "job 1 running" in read.content and "server-up" in read.content
        stop = await JobsTool().run(JobsInput.model_validate(dict(action="stop", job_id=1)), ctx)
        assert "job 1 stopped" in stop.content
        again = await JobsTool().run(JobsInput.model_validate(dict(action="output", job_id=1)), ctx)
        assert "job 1 stopped" in again.content and "server-up" in again.content
        missing = await JobsTool().run(
            JobsInput.model_validate(dict(action="output", job_id=7)), ctx
        )
        assert missing.is_error and "no background job 7" in missing.content
    finally:
        await ctx.close_shells()


async def test_without_a_job_manager_the_tools_say_so(project: Path) -> None:
    ctx = ToolContext(cwd=project, project_root=project)
    for result in (
        await BashTool().run(
            BashInput.model_validate(dict(command="sleep 1", run_in_background=True)), ctx
        ),
        await JobsTool().run(JobsInput.model_validate(dict(action="output", job_id=1)), ctx),
        await JobsTool().run(JobsInput.model_validate(dict(action="stop", job_id=1)), ctx),
    ):
        assert result.is_error and "not available here" in result.content


async def test_a_job_starts_where_the_agent_is_now(project: Path) -> None:
    (project / "sub").mkdir()
    ctx = ToolContext(cwd=project, project_root=project)
    ctx.jobs = JobManager(None)
    try:
        await BashTool().run(BashInput.model_validate(dict(command="cd sub")), ctx)
        await BashTool().run(
            BashInput.model_validate(dict(command="pwd; sleep 60", run_in_background=True)), ctx
        )
        read = await JobsTool().run(
            JobsInput.model_validate(dict(action="output", job_id=1, wait_seconds=10)), ctx
        )
        assert read.content.rstrip().endswith("sub")
    finally:
        await ctx.close_shells()


def test_the_job_tools_ask_nothing_and_are_not_for_subagents() -> None:
    assert JobsTool.read_only
    assert "Jobs" in NOT_FOR_SUBAGENTS


# --- /jobs ------------------------------------------------------------------------------------


def test_jobs_is_a_built_in_command() -> None:
    assert "jobs" in {name for name, _, _ in BUILT_IN}


async def test_slash_jobs_lists_shows_output_and_stops(project: Path) -> None:
    session = make_session("http://127.0.0.1:9", project)
    try:
        assert "No background jobs" in (await run_command(session, "/jobs")).lines[0]
        manager = session.agent.ctx.jobs
        assert manager is not None
        manager.start("echo alive; sleep 60", project, shell(), sandboxed=False)
        manager.get(1).changed.clear()
        await asyncio.wait_for(manager.get(1).changed.wait(), 10)
        listed = (await run_command(session, "/jobs")).lines
        assert listed[0].startswith("job 1 running for") and "echo alive" in listed[0]
        shown = (await run_command(session, "/jobs output 1")).lines
        assert shown[-1] == "alive"
        stopped = await run_command(session, "/jobs stop 1")
        assert stopped.lines[0].startswith("job 1 stopped") and stopped.quiet
        assert [e.jobs[0].state for e in stopped.events if isinstance(e, ev.JobsChanged)] == [
            "stopped"
        ]
        assert (await run_command(session, "/jobs stop 9")).lines[0].startswith("There is no")
        assert (await run_command(session, "/jobs nonsense")).lines == [
            "Usage: /jobs [output <n> [lines] | stop <n|all>]"
        ]
        manager.start("sleep 60", project, shell(), sandboxed=False)
        manager.start("sleep 60", project, shell(), sandboxed=False)
        assert (await run_command(session, "/jobs stop all")).lines == ["Stopped 2 job(s)."]
    finally:
        await session.agent.close()


# --- the agent --------------------------------------------------------------------------------


async def test_jobs_go_on_through_a_rebuild_and_end_with_the_agent(project: Path) -> None:
    session = make_session("http://127.0.0.1:9", project)
    manager = session.agent.ctx.jobs
    assert manager is not None
    job = manager.start("sleep 60", project, shell(), sandboxed=False)
    session.agent = await rebuild_agent(session.agent, model="mock:qwen3-7b")  # /model, a fallback
    assert session.agent.ctx.jobs is manager and job.state == "running"
    await session.agent.close()
    assert job.state == "stopped"


async def test_a_model_starts_reads_and_stops_a_job_in_one_request(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {"command": "echo ready; sleep 60", "run_in_background": True},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "Jobs",
                        "arguments": {"action": "output", "job_id": 1, "wait_seconds": 10},
                    }
                ]
            },
            {"tool_calls": [{"name": "Jobs", "arguments": {"action": "stop", "job_id": 1}}]},
            {"content": "The server printed ready, and I stopped it."},
        ]
    )

    async def allow(request: ev.PermissionRequest) -> dict[str, Any]:
        return {"allow": True}

    session = make_session(server.base_url, project, ask=allow)
    try:
        events = await run_turn(session, "start the server and check it")
        results = [e for e in events if isinstance(e, ev.ToolResult)]
        assert results[0].content.startswith("Started background job 1")
        assert results[1].content.startswith("job 1 running") and "ready" in results[1].content
        assert results[2].content.startswith("job 1 stopped")
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
        manager = session.agent.ctx.jobs
        assert manager is not None and manager.get(1).state == "stopped"
    finally:
        await session.agent.close()


async def test_over_stdio_the_chat_gets_the_job_list_and_stops_a_job_by_command(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {"command": "echo up; sleep 60", "run_in_background": True},
                    }
                ]
            },
            {"content": "Started."},
        ]
    )
    agent = await Agent.start(project, server, "--permission-mode", "bypassPermissions")
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")
        assert "jobs" in {c["name"] for c in (await agent.until("command_list"))["commands"]}
        await agent.send(type="user_message", text="start it")
        first = await agent.until("jobs_changed")
        assert first["jobs"][0]["state"] == "running" and "echo up" in first["jobs"][0]["command"]
        assert (await agent.until("result"))["subtype"] == "success"

        await agent.send(type="user_message", text="/jobs stop 1")
        while True:
            update = await agent.until("jobs_changed")
            if update["jobs"][0]["state"] == "stopped":
                break
        result = await agent.until("result")
        assert result["subtype"] == "success"
        assert not [
            e
            for e in agent.events
            if e["type"] == "assistant_message"
            and "stopped" in e["text"].lower()
            and "job 1" in e["text"]
        ], "a stop needs no message"
    finally:
        assert await agent.close() == 0


async def test_over_stdio_a_job_is_stopped_while_the_request_that_started_it_is_still_running(
    mock_server: Any, project: Path
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Bash",
                        "arguments": {"command": "sleep 60", "run_in_background": True},
                    }
                ]
            },
            # waits up to 30 s for output the job will never print: the request is busy meanwhile
            {
                "tool_calls": [
                    {
                        "name": "Jobs",
                        "arguments": {"action": "output", "job_id": 1, "wait_seconds": 30},
                    }
                ]
            },
            {"content": "It was stopped."},
        ]
    )
    agent = await Agent.start(project, server, "--permission-mode", "bypassPermissions")
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="start it and watch it")
        assert (await agent.until("jobs_changed"))["jobs"][0]["state"] == "running"
        await agent.until("tool_use")  # the request is now inside the Jobs wait
        await agent.send(type="user_message", text="/jobs stop 1")  # the chat's Stop button
        while (await agent.until("jobs_changed"))["jobs"][0]["state"] != "stopped":
            pass
        assert (await agent.until("result"))["subtype"] == "success"
        assert not [e for e in agent.events if e["type"] == "error"], "not refused as busy"
    finally:
        assert await agent.close() == 0
