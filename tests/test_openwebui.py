"""Open WebUI as a gateway, against the mock server's Open WebUI mode (which
copies what Open WebUI 0.11 does; see models/gateway.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmcoder.agents.main import AgentOptions, build_agent, prepare_agent
from cmcoder.config.settings import ProviderConfig, Settings, env_layer
from cmcoder.models.chat import build_chat_model
from cmcoder.models.gateway import api_base, gateway_for, prepare_models
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session

from .conftest import API_KEY
from .helpers import of_type, run_turn

MODELS = {"qwen3:32b": "ollama", "qwen3:8b": "ollama", "qwen3-vllm": "openai"}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMCODER_API_KEY", API_KEY)


def webui(mock_server: Any, script: list[dict[str, Any]], **kw: Any) -> Any:
    return mock_server(script, openwebui=MODELS, **kw)


def settings(server: Any, model: str = "qwen3:32b", **extra: Any) -> Settings:
    return Settings.model_validate(
        {
            "providers": {"webui": {"type": "openwebui", "baseUrl": server.root_url}},
            "model": f"webui:{model}",
            **extra,
        }
    )


def call(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {"tool_calls": [{"name": n, "arguments": a} for n, a in calls]}


def test_server_address_forms() -> None:
    for url in (
        "https://webui.corp",
        "https://webui.corp/",
        "https://webui.corp/api",
        "https://webui.corp/api/v1/",
    ):
        cfg = ProviderConfig.model_validate({"type": "openwebui", "baseUrl": url})
        assert api_base(cfg) == "https://webui.corp/api"
    plain = ProviderConfig.model_validate({"baseUrl": "https://litellm/v1/"})
    assert api_base(plain) == "https://litellm/v1"
    assert env_layer({"CMCODER_BASE_URL": "https://w", "CMCODER_PROVIDER_TYPE": "openwebui"})[
        "providers"
    ]["default"] == {"baseUrl": "https://w", "type": "openwebui"}


async def test_models_and_backends(mock_server: Any) -> None:
    server = webui(mock_server, [])
    gw = gateway_for(settings(server), "webui")
    try:
        assert await gw.list_models() == ["qwen3-vllm", "qwen3:32b", "qwen3:8b"]  # no arena
        info = await gw.model_info()
        assert info["qwen3:32b"]["backend"] == "ollama"
        assert info["qwen3-vllm"]["backend"] == "openai"
        assert await gw.ollama_context_length("qwen3:32b") == 40960
        assert await gw.ollama_context_length("qwen3-vllm") is None  # not an Ollama model
    finally:
        await gw.aclose()


async def test_an_ollama_model_gets_num_ctx_and_whole_tool_calls(
    mock_server: Any, project: Path
) -> None:
    (project / "a.txt").write_text("A\n")
    (project / "b.txt").write_text("B\n")
    server = webui(
        mock_server,
        [
            call(("Read", {"file_path": "a.txt"}), ("Read", {"file_path": "b.txt"})),
            {"content": "Both read.", "reasoning": "Done."},
        ],
    )
    s = settings(server)
    opts = AgentOptions(cwd=project, interactive=False)
    await prepare_agent(s, opts)
    agent = build_agent(s, opts)
    assert agent.bundle.profile.backend == "ollama"
    assert agent.bundle.profile.context_window == 32768
    session = Session(agent)
    try:
        events = await run_turn(session, "read both")
    finally:
        await agent.close()
    results = of_type(events, ev.ToolResult)
    # Two calls, both numbered 0 by Open WebUI, kept apart by their ids.
    # (They run in parallel, so either may finish first.)
    assert sorted(r.content.strip().splitlines()[-1].split("\t")[-1] for r in results) == [
        "A",
        "B",
    ]
    assert events[-1].result == "Both read." and not events[-1].is_error
    first = server.requests[0]
    assert first["options"] == {"num_ctx": 32768}
    assert "chat_template_kwargs" not in first
    assert server.state.truncated == []
    assert server.probes == []  # Ollama never refuses a long prompt: nothing to probe
    assert session.usage["prompt_tokens"] > 0  # usage from the last content chunk


async def test_window_capped_at_the_trained_length(mock_server: Any) -> None:
    server = webui(mock_server, [], ollama_context={"qwen3:8b": 8192})
    s = settings(server, "qwen3:8b")
    await prepare_models(s, [None])
    bundle = build_chat_model(s)
    assert bundle.profile.context_window == 8192
    assert "trained length" in bundle.profile.context_window_source
    assert bundle.model.extra_body == {"options": {"num_ctx": 8192}}
    # Quick jobs (titles, summaries) switch Ollama's thinking off.
    assert bundle.quick().extra_body == {"options": {"num_ctx": 8192, "think": False}}
    # Your own contextWindow still wins (and is what is sent).
    s2 = settings(server, "qwen3:8b", modelProfiles=[{"match": "qwen3:8b", "contextWindow": 16384}])
    assert build_chat_model(s2).model.extra_body == {"options": {"num_ctx": 16384}}


async def test_an_openai_backend_is_probed_and_left_alone(mock_server: Any) -> None:
    server = webui(mock_server, [], context_window=40960, model_info=False)
    s = settings(server, "qwen3-vllm")
    await prepare_models(s, [None])
    bundle = build_chat_model(s)
    assert bundle.profile.backend == "default"
    assert bundle.profile.context_window == 40960  # probed
    assert bundle.profile.context_window_source == "server limit (probe)"
    assert not (bundle.model.extra_body or {}).get("options")
    assert len(server.probes) == 1
    await prepare_models(s, [None])  # known now: no second probe
    assert len(server.probes) == 1


async def test_errors_in_open_webui_format(mock_server: Any, project: Path) -> None:
    server = webui(mock_server, [])
    s = settings(server, "nope")
    agent = build_agent(s, AgentOptions(cwd=project, interactive=False))
    try:
        events = await run_turn(Session(agent), "hi")
    finally:
        await agent.close()
    errors = of_type(events, ev.Error)
    assert errors and "Model not found" in errors[0].message
    assert events[-1].is_error
