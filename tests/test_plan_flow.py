"""The plan flow: in plan mode the agent calls ExitPlanMode, the run pauses for the user, and approving
ends plan mode and makes the steps the todo list; sending it back is a denial with feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcoder.protocol import events as ev
from cmcoder.tools.plan import PlanInput, PlanStep, steps_from_lines, todos_from

from .conftest import API_KEY
from .helpers import make_session, run_turn
from .test_stdio import Agent

PLAN = {
    "title": "Orders status filter",
    "steps": [
        {"text": "Add a status parameter to Index", "files": ["Controllers/OrdersController.cs"]},
        {"text": "Add the dropdown to the view", "files": ["Views/Orders/Index.cshtml"]},
        {"text": "Add tests", "files": []},
    ],
    "out_of_scope": "The database",
}


def plan_call(**over: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": "ExitPlanMode", "arguments": {**PLAN, **over}}]}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def tool_results(server: Any) -> list[str]:
    """What the model was told in the tool results, in every request it made."""
    out: list[str] = []
    for request in server.requests:
        for m in request["messages"]:
            if m["role"] == "tool":
                out.append(str(m["content"]))
    return out


async def run_plan(
    mock_server: Any, tmp_path: Path, answer: dict[str, Any] | None, *, mode: str = "plan"
) -> tuple[Any, list[ev.Event], list[ev.PermissionRequest], Any]:
    server = mock_server([plan_call(), {"content": "Starting."}])
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict[str, Any]:
        asked.append(req)
        return answer or {"allow": True}

    session = make_session(server.base_url, tmp_path, ask=ask, permission_mode=mode)
    events = await run_turn(session, "add a status filter")
    return session, events, asked, server


async def test_the_plan_pauses_the_run_as_a_request_the_chat_can_draw(
    mock_server: Any, tmp_path: Path
) -> None:
    session, _, asked, _ = await run_plan(mock_server, tmp_path, {"allow": True})
    try:
        (request,) = asked
        assert request.name == "ExitPlanMode" and request.label == "Plan: Orders status filter"
        assert request.can_remember is False and request.high_risk is False
        assert request.input["title"] == "Orders status filter"
        assert [s["text"] for s in request.input["steps"]] == [s["text"] for s in PLAN["steps"]]
        assert request.input["steps"][0]["files"] == ["Controllers/OrdersController.cs"]
        assert request.input["out_of_scope"] == "The database"
    finally:
        await session.agent.close()


async def test_approving_ends_plan_mode_and_the_steps_become_the_todo_list(
    mock_server: Any, tmp_path: Path
) -> None:
    session, events, _, server = await run_plan(mock_server, tmp_path, {"allow": True})
    try:
        assert session.agent.policy.mode == "default"
        assert [e.mode for e in events if isinstance(e, ev.ModeChanged)] == ["default"]
        todos = await session.todos()
        assert [(t["content"], t["status"]) for t in todos] == [
            ("Add a status parameter to Index", "in_progress"),
            ("Add the dropdown to the view", "pending"),
            ("Add tests", "pending"),
        ]
        updates = [e for e in events if isinstance(e, ev.TodoUpdate)]
        assert (
            updates
            and [t["content"] for t in updates[-1].todos][0] == "Add a status parameter to Index"
        )
        told = tool_results(server)
        assert any("The user approved the plan" in t and "3 steps" in t for t in told)
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success"
    finally:
        await session.agent.close()


async def test_approve_and_accept_edits_goes_to_that_mode(mock_server: Any, tmp_path: Path) -> None:
    session, events, _, _ = await run_plan(
        mock_server, tmp_path, {"allow": True, "remember_session": True}
    )
    try:
        assert session.agent.policy.mode == "acceptEdits"
        assert [e.mode for e in events if isinstance(e, ev.ModeChanged)] == ["acceptEdits"]
    finally:
        await session.agent.close()


async def test_the_users_edited_steps_replace_the_plan(mock_server: Any, tmp_path: Path) -> None:
    edited = (
        "1. Add a status parameter to Index\n2. Keep the filter in the paging links\n- Add tests"
    )
    session, _, _, server = await run_plan(
        mock_server, tmp_path, {"allow": True, "feedback": edited}
    )
    try:
        todos = await session.todos()
        assert [t["content"] for t in todos] == [
            "Add a status parameter to Index",
            "Keep the filter in the paging links",
            "Add tests",
        ]
        assert todos[0]["status"] == "in_progress"
        told = " ".join(tool_results(server))
        assert (
            "The user changed the plan. Follow this version:" in told
            and "2. Keep the filter in the paging links" in told
        )
    finally:
        await session.agent.close()


async def test_sending_the_plan_back_with_a_note_keeps_plan_mode_and_the_model_hears_it(
    mock_server: Any, tmp_path: Path
) -> None:
    session, events, _, server = await run_plan(
        mock_server, tmp_path, {"allow": False, "feedback": "drop the tests, we have them"}
    )
    try:
        assert session.agent.policy.mode == "plan"
        assert not [e for e in events if isinstance(e, ev.ModeChanged)]
        assert await session.todos() == []
        assert any(
            "Their feedback: drop the tests, we have them" in t for t in tool_results(server)
        )
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "success", (
            "the model answers the note"
        )
    finally:
        await session.agent.close()


async def test_rejecting_the_plan_without_a_note_stops_the_turn(
    mock_server: Any, tmp_path: Path
) -> None:
    session, events, _, _ = await run_plan(mock_server, tmp_path, {"allow": False})
    try:
        assert session.agent.policy.mode == "plan"
        assert isinstance(events[-1], ev.Result) and events[-1].subtype == "interrupted"
    finally:
        await session.agent.close()


async def test_outside_plan_mode_there_is_no_plan_to_approve(
    mock_server: Any, tmp_path: Path
) -> None:
    session, events, asked, server = await run_plan(
        mock_server, tmp_path, {"allow": True}, mode="default"
    )
    try:
        assert asked == [], "the user is not asked about a plan nobody was making"
        assert any("You are not in plan mode" in t for t in tool_results(server))
        assert session.agent.policy.mode == "default" and await session.todos() == []
    finally:
        await session.agent.close()


async def test_without_anyone_to_ask_a_plan_is_not_approved(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([plan_call(), {"content": "Understood."}])
    session = make_session(
        server.base_url, tmp_path, permission_mode="plan"
    )  # no `ask`: non-interactive
    try:
        await run_turn(session, "add a status filter")
        assert any("needs a person's approval" in t for t in tool_results(server))
        assert session.agent.policy.mode == "plan"
    finally:
        await session.agent.close()


async def test_a_rule_can_never_approve_a_plan_for_the_user(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server([plan_call(), {"content": "Understood."}])
    asked: list[ev.PermissionRequest] = []

    async def ask(req: ev.PermissionRequest) -> dict[str, Any]:
        asked.append(req)
        return {"allow": False, "feedback": "not yet"}

    session = make_session(
        server.base_url, tmp_path, ask=ask, permission_mode="plan", allowed_tools=["ExitPlanMode"]
    )
    try:
        await run_turn(session, "add a status filter")
        assert len(asked) == 1, "an allow rule for ExitPlanMode is not a yes"
        assert session.agent.policy.mode == "plan"
    finally:
        await session.agent.close()


async def test_plan_mode_tells_the_model_where_to_put_its_plan(
    mock_server: Any, tmp_path: Path
) -> None:
    server = mock_server(
        [
            {"tool_calls": [{"name": "Write", "arguments": {"file_path": "a.py", "content": "x"}}]},
            {"content": "ok"},
        ]
    )
    session = make_session(server.base_url, tmp_path, ask=None, permission_mode="plan")
    try:
        await run_turn(session, "write a.py")
        assert any("call ExitPlanMode with your plan instead" in t for t in tool_results(server))
        names = {t["function"]["name"] for t in server.requests[0]["tools"]}
        assert "ExitPlanMode" in names
    finally:
        await session.agent.close()


async def test_a_subagent_is_not_given_the_plan_tool() -> None:
    from cmcoder.agents.subagents import NOT_FOR_SUBAGENTS

    assert "ExitPlanMode" in NOT_FOR_SUBAGENTS


def test_the_conversions_between_a_plan_and_its_todos_and_edited_lines() -> None:
    steps = [PlanStep(text="a", files=["x.cs"]), PlanStep(text="b")]
    assert todos_from(steps) == [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "pending"},
    ]
    kept = steps_from_lines("1) a\n   \n* b changed\n3. new", steps)
    assert [(s.text, s.files) for s in kept] == [("a", ["x.cs"]), ("b changed", []), ("new", [])], (
        "an unchanged step keeps its files"
    )
    assert steps_from_lines("   \n\n", steps) == steps, "nothing left of the edit: the plan stays"
    assert len(steps_from_lines("\n".join(f"s{i}" for i in range(200)), steps)) == 30
    PlanInput.model_validate({"title": "t", "steps": [{"text": "a"}]})
    with pytest.raises(ValueError):
        PlanInput.model_validate({"title": "t", "steps": []})


async def test_over_stdio_the_plan_arrives_as_a_request_and_approving_it_changes_the_mode(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([plan_call(), {"content": "Starting."}])
    agent = await Agent.start(project, server, "--permission-mode", "plan")
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="add a status filter")
        request = await agent.until("permission_request")
        assert (
            request["name"] == "ExitPlanMode"
            and request["input"]["title"] == "Orders status filter"
        )
        await agent.send(type="permission_response", request_id=request["request_id"], allow=True)
        assert (await agent.until("mode_changed"))["mode"] == "default"
        assert (await agent.until("result"))["subtype"] == "success"
        todos = [e for e in agent.events if e["type"] == "todo_update"][-1]["todos"]
        assert (
            todos[0]["content"] == "Add a status parameter to Index"
            and todos[0]["status"] == "in_progress"
        )
    finally:
        assert await agent.close() == 0
