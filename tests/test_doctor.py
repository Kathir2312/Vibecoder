"""`cmcoder doctor`, against the mock gateway (and its Open WebUI mode)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from cmcoder.cli.doctor import Doctor
from cmcoder.config.settings import Settings
from cmcoder.models.gateway import gateway_for
from cmcoder.models.windows import PROBE_SOURCE

from .conftest import API_KEY


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def settings_for(server: Any, model: str = "qwen3-27b", **extra: Any) -> Settings:
    return Settings.model_validate(
        {"providers": {"mock": {"baseUrl": server.base_url}}, "model": f"mock:{model}", **extra}
    )


def console() -> Console:
    return Console(record=True, width=300)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": args}]}


async def test_a_full_run_against_a_working_gateway(mock_server: Any, project: Path) -> None:
    server = mock_server(
        [
            {"content": "OK"},
            {"content": "hi"},
            call("get_weather", city="Paris"),
            {"content": "green"},
        ],
        context_window=40960,
        model_info=False,
    )
    c = console()
    code = await Doctor(settings_for(server), c).run(["mock:qwen3-27b"], probe=True)
    out = c.export_text()
    assert "DNS: 127.0.0.1 resolves" in out and "TCP: connected" in out
    assert "API: authenticated" in out and "Model 'qwen3-27b' is available" in out
    assert "qwen3-27b: streaming works" in out
    assert "thinking can be switched off (chat_template_kwargs)" in out
    assert "native tool calling works" in out
    assert "qwen3-27b: sees images, but cmcoder doesn't know it" in out  # it said green
    assert "Bash sandbox" in out
    assert code == 0 and "All checks passed." in out
    # The quick request switched thinking off.
    quick = server.requests[1]
    assert quick["chat_template_kwargs"] == {"enable_thinking": False}


async def test_a_wrong_key_and_an_unknown_model(
    mock_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock_server([])
    monkeypatch.setenv("CMCODER_API_KEY", "sk-wrong")
    c = console()
    d = Doctor(settings_for(server, "nope"), c)
    await d.check_api("mock", ["nope"], probe=False)
    out = c.export_text()
    assert "API: GET /models failed" in out and "cmcoder sent the" in out and d.failed
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)
    c = console()
    await Doctor(settings_for(server, "nope"), c).check_api("mock", ["nope"], probe=False)
    assert "Model 'nope' is not in the server's model list" in c.export_text()


async def test_doctor_reports_the_window_and_its_source(mock_server: Any) -> None:
    server = mock_server([], context_window=40960, model_info=False)
    c = console()
    gw = gateway_for(settings_for(server), "mock")
    try:
        await Doctor(settings_for(server), c).check_context_window(gw, "qwen3-27b", None)
        s = settings_for(server, modelProfiles=[{"match": "qwen3*", "contextWindow": 65536}])
        await Doctor(s, c).check_context_window(gw, "qwen3-27b", None)
    finally:
        await gw.aclose()
    out = c.export_text()
    assert "context window 40,960 tokens" in out
    assert f"server says 40,960 tokens; using 40,960 from {PROBE_SOURCE}" in out
    # A setting larger than what the server allows is flagged.
    assert "context window set to 65,536 tokens, but the server allows 40,960" in out


async def test_tool_calls_as_text_are_recognised(mock_server: Any) -> None:
    text_call = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
    server = mock_server([{"content": "OK"}, {"content": "hi"}, {"content": text_call}])
    c = console()
    gw = gateway_for(settings_for(server), "mock")
    try:
        await Doctor(settings_for(server), c).probe_model(gw, "qwen3-27b", None)
    finally:
        await gw.aclose()
    assert "tool calls come back as text (<tool_call> tags)" in c.export_text()


async def test_doctor_explains_open_webui(mock_server: Any) -> None:
    server = mock_server(
        [
            {"content": "OK"},
            {"content": "hi"},
            call("get_weather", city="Paris"),
            {"content": "green"},
        ],
        openwebui={"qwen3:32b": "ollama"},
    )
    s = Settings.model_validate(
        {
            "providers": {"webui": {"type": "openwebui", "baseUrl": server.root_url}},
            "model": "webui:qwen3:32b",
        }
    )
    c = console()
    gw = gateway_for(s, "webui")
    try:
        await Doctor(s, c).check_api("webui", ["qwen3:32b"], probe=True)
    finally:
        await gw.aclose()
    out = c.export_text()
    assert "qwen3:32b: served by Ollama through Open WebUI" in out
    assert "context window 32,768 tokens, sent to Ollama as num_ctx" in out
    assert "thinking can be switched off (Ollama's think option)" in out
    assert "native tool calling works" in out
    assert server.requests[1]["options"] == {"num_ctx": 32768, "think": False}


# --- images ---------------------------------------------------------------------------------


async def doctor_images(server: Any, model: str, **extra: Any) -> str:
    c = console()
    d = Doctor(settings_for(server, model, **extra), c)
    await d.check_images("mock", model)
    await d.check_vision_model()
    return c.export_text()


def image_requests(server: Any) -> list[dict[str, Any]]:
    return [r for r in server.requests if "image_url" in str(r["messages"])]


async def test_doctor_finds_a_model_that_sees_images_unknown_to_cmcoder(mock_server: Any) -> None:
    server = mock_server([{"content": "Green."}])
    out = await doctor_images(server, "qwen3-27b")
    assert "qwen3-27b: sees images, but cmcoder doesn't know it" in out
    assert 'Add {"match": "qwen3-27b", "vision": true} to modelProfiles' in out
    content = image_requests(server)[0]["messages"][-1]["content"]
    assert content[-1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_doctor_a_model_that_sees_images(mock_server: Any) -> None:
    out = await doctor_images(mock_server([{"content": "green"}]), "qwen2.5-vl-7b")
    assert "qwen2.5-vl-7b: sees images" in out and "doesn't know" not in out


async def test_doctor_a_text_only_model_without_a_vision_model(mock_server: Any) -> None:
    out = await doctor_images(mock_server([{"content": "I can't see images."}]), "qwen3-27b")
    assert "qwen3-27b: can't see images, and no visionModel is set" in out
    assert 'It said: "I can\'t see images."' in out


async def test_doctor_checks_the_vision_model_too(mock_server: Any) -> None:
    server = mock_server([{"content": "Text only here."}, {"content": "Blue."}])
    out = await doctor_images(server, "qwen3-27b", visionModel="mock:qwen2.5-vl-7b")
    assert "qwen3-27b: can't see images; mock:qwen2.5-vl-7b describes them for it" in out
    assert "visionModel qwen2.5-vl-7b: didn't see the test image" in out  # it said blue
    assert "It said: 'Blue.'" in out
    server = mock_server([{"content": "Nothing."}, {"content": "It is green."}])
    out = await doctor_images(server, "qwen3-27b", visionModel="mock:qwen2.5-vl-7b")
    assert "visionModel qwen2.5-vl-7b: sees images" in out


# --- the rest ---------------------------------------------------------------------------------


async def test_doctor_telemetry() -> None:
    from .test_telemetry import Collector

    collector = Collector()
    try:
        c = console()
        s = Settings.model_validate({"telemetry": {"enabled": True, "endpoint": collector.url}})
        await Doctor(s, c).check_telemetry()
        assert "OpenTelemetry metrics go to" in c.export_text()
        assert collector.posts[-1][1] == {"resourceMetrics": []}  # no fake metric sent
    finally:
        collector.httpd.shutdown()
        collector.httpd.server_close()


def test_doctor_lists_the_projects_agents_and_says_why_unused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".cmcoder" / "agents").mkdir(parents=True)
    (project / ".cmcoder" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\nReview it."
    )
    monkeypatch.chdir(project)
    c = console()
    Doctor(Settings.model_validate({}), c).check_extensions()
    assert "Project agents not used: reviewer" in c.export_text()
