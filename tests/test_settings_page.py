"""The settings page: a fixed, safe set of settings edited in the user's settings.json, put to work in
the running session, with a connection test. The API key is never typed into the chat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmcoder.config import editor
from cmcoder.config import settings as settings_mod
from cmcoder.config.editor import (
    BY_KEY,
    FIELDS,
    SettingsEditError,
    describe_lines,
    parse,
    state,
    value_of,
    write_value,
)
from cmcoder.config.settings import Settings, config_dir
from cmcoder.runtime.commands import BUILT_IN, run_command

from .conftest import API_KEY
from .helpers import make_session, mock_settings
from .test_stdio import Agent


def user_file() -> Path:
    return config_dir() / "settings.json"


def saved() -> dict[str, Any]:
    return json.loads(user_file().read_text(encoding="utf-8"))


# --- reading and parsing ----------------------------------------------------------------------


def test_every_setting_of_the_page_has_a_value_and_a_unique_key() -> None:
    settings = Settings.model_validate({})
    assert len({f.key for f in FIELDS}) == len(FIELDS)
    for f in FIELDS:
        value_of(settings, f.key)  # no setting is listed that cannot be read
    assert "bypassPermissions" not in BY_KEY["permissions.defaultMode"].choices


def test_values_are_read_as_the_text_of_the_page_gives_them() -> None:
    f = BY_KEY
    assert parse(f["audit.enabled"], "true") is True and parse(f["audit.enabled"], "false") is False
    assert parse(f["maxTurns"], "25") == 25
    assert parse(f["model"], "corp:qwen3-27b") == "corp:qwen3-27b"
    assert parse(f["model"], '"has space"') == "has space"
    assert parse(f["permissions.defaultMode"], "acceptEdits") == "acceptEdits"
    assert parse(f["verify.commands"], '["npm test", "  ", "npm run build "]') == [
        "npm test",
        "npm run build",
    ]
    for text in ("", "null", "default", "  "):
        assert parse(f["model"], text) is None, "back to the default"
    assert parse(f["verify.commands"], "[]") is None


@pytest.mark.parametrize(
    ("key", "text", "message"),
    [
        ("maxTurns", "abc", "expected a whole number"),
        ("maxTurns", "0", "a number from 1 to 1000"),
        ("maxTurns", "5000", "a number from 1 to 1000"),
        ("audit.enabled", "yes", "expected true or false"),
        ("verify.commands", "npm test", "expected a list"),
        ("verify.commands", "[1, 2]", "expected a list"),
        ("verify.maxRounds", "9", "a number from 1 to 5"),
        ("permissions.defaultMode", "bypassPermissions", "one of"),
        ("permissions.defaultMode", "nonsense", "one of"),
    ],
)
def test_a_value_that_does_not_fit_is_refused_with_what_would(
    key: str, text: str, message: str
) -> None:
    with pytest.raises(SettingsEditError, match=message):
        parse(BY_KEY[key], text)


# --- writing ----------------------------------------------------------------------------------


def test_a_change_is_saved_in_the_users_file_and_everything_else_in_it_stays() -> None:
    user_file().parent.mkdir(parents=True, exist_ok=True)
    user_file().write_text(
        json.dumps(
            {
                "providers": {"corp": {"baseUrl": "https://gw.example/v1"}},
                "model": "corp:a",
                "hooks": {"Stop": []},
                "sandbox": {"enabled": False},
            }
        )
    )
    value, message = write_value("model", "corp:b")
    assert value == "corp:b" and message == "Saved Main model: corp:b."
    write_value("verify.commands", '["pytest -q"]')
    write_value("audit.enabled", "true")
    data = saved()
    assert data["model"] == "corp:b" and data["verify"] == {"commands": ["pytest -q"]}
    assert data["audit"] == {"enabled": True}
    assert data["providers"] == {"corp": {"baseUrl": "https://gw.example/v1"}}
    assert data["hooks"] == {"Stop": []} and data["sandbox"] == {"enabled": False}
    assert not list(user_file().parent.glob("*.tmp")), "replaced in one step, no leftover"


def test_the_file_is_made_when_there_is_none_and_putting_a_setting_back_leaves_no_empty_section() -> (
    None
):
    assert not user_file().exists()
    write_value("verify.maxRounds", "3")
    assert saved() == {"verify": {"maxRounds": 3}}
    value, message = write_value("verify.maxRounds", "default")
    assert (
        value is None and message == "Saved Attempts to fix a failing check: back to its default."
    )
    assert saved() == {}
    write_value("model", "null")  # nothing to take out: no error


def test_only_the_settings_of_the_page_can_be_written_never_a_gateway_a_hook_or_the_sandbox() -> (
    None
):
    for key in (
        "providers.corp.baseUrl",
        "hooks",
        "sandbox.enabled",
        "env",
        "permissions.allow",
        "../x",
    ):
        with pytest.raises(SettingsEditError, match="not a setting this page edits"):
            write_value(key, "x")
    assert not user_file().exists()


def test_a_file_that_is_not_json_is_left_alone() -> None:
    user_file().parent.mkdir(parents=True, exist_ok=True)
    user_file().write_text("{ not json")
    with pytest.raises(SettingsEditError):
        write_value("maxTurns", "5")
    assert user_file().read_text() == "{ not json"


def test_a_value_the_whole_settings_do_not_accept_is_refused_before_anything_is_written() -> None:
    user_file().parent.mkdir(parents=True, exist_ok=True)
    user_file().write_text(json.dumps({"model": "a"}))
    with pytest.raises(SettingsEditError, match="Permission mode"):
        # accepted by the parser's list, refused by the settings model: stays out of the file
        spec = BY_KEY["permissions.defaultMode"]
        object.__setattr__(spec, "choices", (*spec.choices, "bogus"))
        try:
            write_value("permissions.defaultMode", "bogus")
        finally:
            object.__setattr__(spec, "choices", spec.choices[:-1])
    assert saved() == {"model": "a"}


def test_what_the_organisation_fixes_is_shown_and_cannot_be_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = tmp_path / "admin" / "managed-settings.json"
    admin.parent.mkdir()
    admin.write_text(json.dumps({"audit": {"enabled": True}, "maxTurns": 20}))
    monkeypatch.setattr(settings_mod, "managed_settings_path", lambda: admin)
    with pytest.raises(SettingsEditError, match="set by your organisation"):
        write_value("audit.enabled", "false")
    with pytest.raises(SettingsEditError, match="set by your organisation"):
        write_value("maxTurns", "99")
    write_value("model", "free-to-change")  # the rest is the user's
    page = state(Settings.model_validate({"audit": {"enabled": True}}))
    locked = {f.key: f.note for f in page.fields if f.locked}
    assert locked == {
        "audit.enabled": "set by your organisation",
        "maxTurns": "set by your organisation",
    }


# --- the page ---------------------------------------------------------------------------------


def test_the_page_lists_every_setting_with_its_value_and_whether_each_gateway_has_a_key() -> None:
    settings = Settings.model_validate(
        {
            "providers": {
                "corp": {"baseUrl": "https://gw.example/v1"},
                "local": {"baseUrl": "http://localhost:11434/v1", "auth": {"type": "none"}},
            },
            "model": "corp:qwen3-27b",
            "verify": {"commands": ["pytest -q"]},
        }
    )
    page = state(settings, notice="hi", models=["a", "b"], keys={"corp": True})
    assert [f.key for f in page.fields] == [f.key for f in FIELDS]
    values = {f.key: f.value for f in page.fields}
    assert values["model"] == "corp:qwen3-27b" and values["verify.commands"] == ["pytest -q"]
    assert values["maxTurns"] == 50 and values["audit.enabled"] is False
    assert [(g.name, g.key) for g in page.gateways] == [("corp", "set"), ("local", "not needed")]
    assert state(settings, keys={}).gateways[0].key == "missing"
    assert page.notice == "hi" and page.models == ["a", "b"] and page.file == str(user_file())
    assert "key" not in json.dumps([g.model_dump() for g in page.gateways]).replace('"key": "', "")


def test_settings_is_a_built_in_command_and_a_terminal_gets_the_values_as_text() -> None:
    assert "settings" in {name for name, _, _ in BUILT_IN}
    lines = describe_lines(Settings.model_validate({"model": "corp:x"}))
    assert lines[0].startswith("Your settings file: ")
    assert any(line.strip().startswith("Main model") and "corp:x" in line for line in lines)


async def test_in_a_terminal_slash_settings_lists_them(project: Path) -> None:
    session = make_session("http://127.0.0.1:9", project)
    try:
        lines = (await run_command(session, "/settings")).lines
        assert any("Model calls per request" in line and "50" in line for line in lines)
    finally:
        await session.agent.close()


# --- over the protocol ------------------------------------------------------------------------


async def test_over_stdio_a_change_is_saved_and_in_use_at_once(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="list_commands")
        assert "settings" in {c["name"] for c in (await agent.until("command_list"))["commands"]}

        await agent.send(type="user_message", text="/settings")
        page = await agent.until("settings_state")
        assert [f["key"] for f in page["fields"]][:2] == ["model", "smallFastModel"]
        assert page["gateways"][0]["key"] == "set" and page["file"].endswith("settings.json")
        assert (await agent.until("result"))["subtype"] == "success"

        await agent.send(type="user_message", text="/settings set permissions.defaultMode plan")
        assert (await agent.until("mode_changed"))["mode"] == "plan"
        page = await agent.until("settings_state")
        assert page["notice"] == "Saved Permission mode: plan. In use now."
        assert saved()["permissions"] == {"defaultMode": "plan"}

        await agent.send(type="user_message", text="/settings set critic.enabled true")
        assert (await agent.until("critique_changed"))["enabled"] is True
        await agent.until("settings_state")

        await agent.send(type="user_message", text="/settings set maxTurns 7")
        page = await agent.until("settings_state")
        assert page["notice"] == "Saved Model calls per request: 7. In use now."
        await agent.send(type="user_message", text="/turns")
        assert "7" in (await agent.until("assistant_message"))["text"]

        # The harness starts cmcoder with CMCODER_MODEL, which outranks the settings file: the page
        # saves the choice and says plainly that something else decides for now.
        await agent.send(type="user_message", text="/settings set model qwen3-7b")
        page = await agent.until("settings_state")
        assert page["notice"].startswith("Saved Main model: qwen3-7b. Something with more say")
        assert saved()["model"] == "qwen3-7b"

        await agent.send(type="user_message", text='/settings set verify.commands ["npm test"]')
        page = await agent.until("settings_state")
        values = {f["key"]: f["value"] for f in page["fields"]}
        assert values["verify.commands"] == ["npm test"] and values["maxTurns"] == 7
        assert saved()["verify"] == {"commands": ["npm test"]}
    finally:
        assert await agent.close() == 0


async def test_over_stdio_a_change_that_cannot_be_made_says_why_and_changes_nothing(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        for text, message in (
            ("/settings set maxTurns lots", "expected a whole number"),
            ("/settings set hooks {}", "not a setting this page edits"),
            ("/settings set", "Usage: /settings"),
            ("/settings nonsense", "Usage: /settings"),
        ):
            await agent.send(type="user_message", text=text)
            assert message in (await agent.until("settings_state"))["notice"], text
        assert not user_file().exists()
    finally:
        assert await agent.close() == 0


async def test_over_stdio_the_connection_test_lists_the_gateways_models(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="/settings test")
        tested = await agent.until("connection_tested")
        assert tested["ok"] is True and tested["message"] == "2 models available"
        page = await agent.until("settings_state")
        assert page["models"] == ["qwen3-27b", "qwen3-7b"] and "Connected" in page["notice"]
        await agent.send(type="user_message", text="/settings")  # the list is kept for the page
        assert (await agent.until("settings_state"))["models"] == ["qwen3-27b", "qwen3-7b"]
    finally:
        assert await agent.close() == 0


async def test_over_stdio_a_refused_key_is_reported_with_the_fix(
    mock_server: Any, project: Path
) -> None:
    server = mock_server([{"content": "ok"}])
    server.state.api_key = "a-different-key"  # the key cmcoder has is not the one it accepts now
    agent = await Agent.start(project, server)
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="/settings test")
        tested = await agent.until("connection_tested")
        assert tested["ok"] is False and "cmcoder login" in tested["message"]
    finally:
        assert await agent.close() == 0


async def test_over_stdio_the_page_can_be_read_while_a_request_runs_but_not_changed(
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
            {
                "tool_calls": [
                    {
                        "name": "Jobs",
                        "arguments": {"action": "output", "job_id": 1, "wait_seconds": 30},
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    agent = await Agent.start(project, server, "--permission-mode", "bypassPermissions")
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="start it and watch")
        await agent.until("jobs_changed")
        await agent.until("tool_use")  # inside the wait: the request is running
        await agent.send(type="user_message", text="/settings")
        assert (await agent.until("settings_state"))["fields"]
        await agent.send(type="user_message", text="/settings set maxTurns 9")
        page = await agent.until("settings_state")
        assert "between requests" in page["notice"] and not user_file().exists()
        await agent.send(type="user_message", text="/jobs stop 1")
        assert (await agent.until("result"))["subtype"] == "success"
        assert not [e for e in agent.events if e["type"] == "error"], "never refused as busy"
    finally:
        assert await agent.close() == 0


def test_the_module_offers_no_way_to_set_a_key() -> None:
    names = {f.key.lower() for f in FIELDS}
    assert not [n for n in names if "key" in n or "token" in n or "password" in n]
    assert not hasattr(editor, "write_key")
    assert mock_settings  # (the helper is used elsewhere; keeps the import honest)


async def test_over_stdio_a_model_chosen_on_the_page_answers_the_next_request(
    mock_server: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock_server([{"content": "answered by the new model"}])
    user_file().parent.mkdir(parents=True, exist_ok=True)
    user_file().write_text(
        json.dumps({"providers": {"mock": {"baseUrl": server.base_url}}, "model": "mock:qwen3-27b"})
    )
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    agent = await Agent.start(project, None)  # configured by the file alone: nothing outranks it
    try:
        await agent.until("agent_list")
        await agent.send(type="user_message", text="/settings set model mock:qwen3-7b")
        while (await agent.until("model_changed"))["model"] != "qwen3-7b":
            pass
        page = await agent.until("settings_state")
        assert page["notice"] == "Saved Main model: mock:qwen3-7b. In use now."
        await agent.until("result")  # of the /settings command itself
        await agent.send(type="user_message", text="hello")
        assert (await agent.until("result"))["result"] == "answered by the new model"
        assert server.requests[-1]["model"] == "qwen3-7b"
    finally:
        assert await agent.close() == 0
