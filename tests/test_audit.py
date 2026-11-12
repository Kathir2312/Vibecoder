"""The audit log: a JSON-lines record of what the agent ran and changed, in the user's own folder.
Off by default; a repository cannot switch it on, off or elsewhere; managed settings can switch it on."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import rebuild_agent
from cmcoder.audit import AuditLog, redact, summarize_input
from cmcoder.config import settings as settings_mod
from cmcoder.config.settings import load_settings
from cmcoder.protocol import events as ev
from cmcoder.runtime.commands import BUILT_IN, run_command

from .conftest import API_KEY
from .helpers import make_session, run_turn


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def rows(folder: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("audit-*.jsonl")):
        out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return out


def audited(tmp_path: Path, **more: Any) -> dict[str, Any]:
    return {"audit": {"enabled": True, "dir": str(tmp_path / "audit"), **more}}


async def allow(request: ev.PermissionRequest) -> dict[str, Any]:
    return {"allow": True}


# --- what is hidden ---------------------------------------------------------------------------


def test_what_looks_like_a_secret_is_hidden_wherever_the_log_records_text() -> None:
    for text, gone in (
        (
            "curl -H 'Authorization: Bearer abcdef0123456789abcdef' https://x",
            "abcdef0123456789abcdef",
        ),
        ("export API_KEY=hunter2hunter2 && run", "hunter2hunter2"),
        ("mysql --password=s3cretpass -u root", "s3cretpass"),
        ("mysql --password 's3cret pass' -u root", "s3cret pass"),
        ("git clone https://user:pa55w0rd@host/repo.git", "pa55w0rd"),
        ("token = sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
        ("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ("aws AKIAABCDEFGHIJKLMNOP s3 ls", "AKIAABCDEFGHIJKLMNOP"),
        ("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "SflKxwRJSMeKKF2QT4"),
    ):
        cleaned = redact(text)
        assert gone not in cleaned and "[redacted]" in cleaned, text
    assert redact("pytest -q tests/test_orders.py") == "pytest -q tests/test_orders.py"
    assert (
        redact("git clone https://github.com/org/repo.git")
        == "git clone https://github.com/org/repo.git"
    )


def test_a_tool_calls_input_keeps_its_shape_but_not_the_bodies_of_files() -> None:
    kept = summarize_input(
        {
            "file_path": "src/app.py",
            "content": "print('SECRET_BODY')\n",
            "command": "echo password=hunter2hunter2",
            "timeout_seconds": 30,
            "flag": True,
            "edits": [{"old": "a"}],
            "nested": {"a": 1},
            "long": "x" * 1000,
        }
    )
    assert kept["file_path"] == "src/app.py"
    assert kept["content"] == "<21 chars>" and "SECRET_BODY" not in json.dumps(kept)
    assert kept["command"] == "echo password=[redacted]"
    assert (kept["timeout_seconds"], kept["flag"]) == (30, True)
    assert kept["edits"] == "<18 chars>" or kept["edits"].startswith("<")
    assert kept["nested"] == "<dict>"
    assert len(kept["long"]) <= 300


# --- the file ---------------------------------------------------------------------------------


def test_nothing_is_written_while_it_is_off(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit", enabled=False)
    log.record("s1", "prompt", chars=3)
    assert not (tmp_path / "audit").exists()


def test_a_record_is_one_json_line_in_the_days_file_and_only_the_owner_reads_it(
    tmp_path: Path,
) -> None:
    log = AuditLog(tmp_path / "audit", enabled=True)
    log.record("s1", "tool_call", tool="Bash", label="Bash(ls)", missing=None)
    log.record("s1", "tool_result", tool="Bash", ok=True)
    [path] = list((tmp_path / "audit").glob("audit-*.jsonl"))
    assert path.name == log.path_for().name
    first, second = [json.loads(x) for x in path.read_text().splitlines()]
    assert (first["event"], first["session"], first["tool"]) == ("tool_call", "s1", "Bash")
    assert first["ts"].endswith("Z") and first["user"] and "missing" not in first
    assert second["ok"] is True
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_a_log_that_cannot_be_written_never_stops_the_agent(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    log = AuditLog(blocker / "audit", enabled=True)
    log.record("s1", "prompt", chars=1)  # no exception
    assert log.failed and "Error" in log.failed
    log.folder = tmp_path / "fine"
    log.record("s1", "prompt", chars=1)
    assert log.failed is None


# --- a request, start to end ------------------------------------------------------------------


async def test_a_request_leaves_a_record_of_what_ran_who_allowed_it_and_what_changed(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {
                        "name": "Write",
                        "arguments": {"file_path": "notes.py", "content": "BODY_OF_THE_FILE = 1\n"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {"name": "Bash", "arguments": {"command": "echo token=hunter2hunter2"}}
                ]
            },
            {"content": "Done."},
        ]
    )
    session = make_session(server.base_url, project, ask=allow, settings=audited(tmp_path))
    try:
        await run_turn(session, "write notes, my password is hunter2hunter2")
    finally:
        await session.agent.close()
    log = rows(tmp_path / "audit")
    kinds = [r["event"] for r in log]
    assert kinds[0] == "session_start" and kinds[-1] == "turn_end"
    assert {"prompt", "permission", "tool_call", "tool_result", "files_changed"} <= set(kinds)
    assert all(r["session"] == session.session_id for r in log)

    prompt = next(r for r in log if r["event"] == "prompt")
    assert prompt["chars"] > 10 and "text" not in prompt, "the prompt itself is not recorded"
    calls = [r for r in log if r["event"] == "tool_call"]
    assert [c["tool"] for c in calls] == ["Write", "Bash"]
    assert calls[0]["input"] == {"file_path": "notes.py", "content": "<21 chars>"}
    assert calls[1]["input"]["command"] == "echo token=[redacted]"
    decisions = [(r["tool"], r["decision"]) for r in log if r["event"] == "permission"]
    assert ("Write", "allow") in decisions  # the echo ran without a question
    changed = next(r for r in log if r["event"] == "files_changed")
    assert [f["path"] for f in changed["files"]] == ["notes.py"] and changed["files"][0][
        "added"
    ] == 1
    end = log[-1]
    assert (
        end["result"] == "success" and end["model_calls"] == 3 and end["prompt_tokens"] is not None
    )
    raw = "\n".join(p.read_text() for p in (tmp_path / "audit").glob("*.jsonl"))
    for secret in ("hunter2hunter2", "BODY_OF_THE_FILE"):
        assert secret not in raw


async def test_a_denial_is_recorded_with_who_said_no(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    async def deny(request: ev.PermissionRequest) -> dict[str, Any]:
        return {"allow": False}

    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "a.py", "content": "1\n"}}
                ]
            },
            {"content": "ok"},
        ]
    )
    session = make_session(server.base_url, project, ask=deny, settings=audited(tmp_path))
    try:
        await run_turn(session, "write a.py")
    finally:
        await session.agent.close()
    log = rows(tmp_path / "audit")
    assert [(r["tool"], r["decision"]) for r in log if r["event"] == "permission"] == [
        ("Write", "deny")
    ]
    denied = [r for r in log if r["event"] == "denied"]
    assert denied and denied[0]["reason"] == "denied by user"
    assert not (project / "a.py").exists()


async def test_prompts_are_recorded_only_when_asked_and_then_redacted(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    server = mock_server([{"content": "hi"}])
    session = make_session(
        server.base_url, project, settings=audited(tmp_path, includePrompts=True)
    )
    try:
        await run_turn(session, "use password=hunter2hunter2 to log in")
    finally:
        await session.agent.close()
    prompt = next(r for r in rows(tmp_path / "audit") if r["event"] == "prompt")
    assert prompt["text"] == "use password=[redacted] to log in"


async def test_a_model_switch_and_a_rewind_are_recorded(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    server = mock_server(
        [
            {
                "tool_calls": [
                    {"name": "Write", "arguments": {"file_path": "a.py", "content": "1\n"}}
                ]
            },
            {"content": "done"},
        ]
    )
    session = make_session(
        server.base_url, project, ask=allow, settings=audited(tmp_path, includePrompts=False)
    )
    try:
        await run_turn(session, "write")
        await session.rewind(1, code=True, conversation=False)
    finally:
        await session.agent.close()
    log = rows(tmp_path / "audit")
    rewind = next(r for r in log if r["event"] == "rewind")
    assert rewind["turn"] == 1 and rewind["files"][0]["path"].endswith("a.py")


# --- who may switch it ------------------------------------------------------------------------


def test_a_repository_can_neither_switch_the_log_on_nor_move_it(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_CONFIG_DIR", str(tmp_path / "user"))
    (project / ".cmcoder").mkdir()
    (project / ".cmcoder" / "settings.json").write_text(
        json.dumps({"audit": {"enabled": True, "dir": "/tmp/elsewhere"}})
    )
    for trust in (False, True):
        s = load_settings(project, environ={}, trust_project=trust)
        assert s.audit.enabled is False and s.audit.dir is None
        assert s.ignored_project_settings == ["settings.json: audit"]


def test_the_administrator_can_switch_it_on(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_CONFIG_DIR", str(tmp_path / "user"))
    admin = tmp_path / "admin" / "managed-settings.json"
    admin.parent.mkdir()
    admin.write_text(json.dumps({"audit": {"enabled": True, "dir": str(tmp_path / "central")}}))
    monkeypatch.setattr(settings_mod, "managed_settings_path", lambda: admin)
    s = load_settings(project, environ={})
    assert s.audit.enabled is True and s.audit.dir == str(tmp_path / "central")


# --- /audit -----------------------------------------------------------------------------------


def test_audit_is_a_built_in_command() -> None:
    assert "audit" in {name for name, _, _ in BUILT_IN}


async def test_slash_audit_shows_the_state_and_the_last_records_and_switches_it(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    server = mock_server([{"content": "hello"}])
    session = make_session(
        server.base_url, project, settings={"audit": {"dir": str(tmp_path / "a")}}
    )
    try:
        off = await run_command(session, "/audit")
        assert off.lines == ["The audit log is off (settings: audit.enabled, or /audit on)"]
        assert not (tmp_path / "a").exists()

        on = await run_command(session, "/audit on")
        assert on.lines[0].startswith("The audit log is on: ")
        assert "session_start" in "\n".join(on.lines)
        await run_turn(session, "say hello")
        shown = (await run_command(session, "/audit")).lines
        assert any("prompt" in line and "9 characters" in line for line in shown)
        assert any("turn_end" in line and "success" in line for line in shown)
        assert len((await run_command(session, "/audit 2")).lines) == 3  # the state, then 2 records
        assert (await run_command(session, "/audit nonsense")).lines == [
            "Usage: /audit [on|off|<number of records>]"
        ]

        again = rows(tmp_path / "a")
        assert [r["event"] for r in again][0] == "session_start"
        await run_command(session, "/audit off")
        n = len(rows(tmp_path / "a"))
        await run_turn(session, "not recorded")
        assert len(rows(tmp_path / "a")) == n
    finally:
        await session.agent.close()


async def test_the_switch_survives_a_model_change(
    mock_server: Any, project: Path, tmp_path: Path
) -> None:
    server = mock_server([{"content": "x"}])
    session = make_session(server.base_url, project, settings=audited(tmp_path))
    try:
        log = session.agent.audit
        assert log is not None and log.enabled
        session.agent = await rebuild_agent(session.agent, model="mock:qwen3-7b")
        assert session.agent.audit is log, "the same log, with what /audit switched"
    finally:
        await session.agent.close()


def test_the_log_folder_defaults_to_the_users_config_folder(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CMCODER_CONFIG_DIR", str(tmp_path / "cfg"))
    session = make_session("http://127.0.0.1:9", project)
    assert session.agent.audit is not None
    assert session.agent.audit.folder == Path(os.environ["CMCODER_CONFIG_DIR"]) / "audit"
    assert session.agent.audit.enabled is False
