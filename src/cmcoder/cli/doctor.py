"""`cmcoder doctor`: check that the gateway and model work, step by step.

LANGCHAIN CONCEPTS IN THIS FILE

* `model.ainvoke([...messages])`: one plain, non-streaming model call.
* `model.bind_tools([tool])`: returns a copy of the model that sends the tool's
  JSON Schema with every request. If the reply's `tool_calls` contains our
  tool, the server supports native tool calling. Behind LiteLLM that depends
  on the backend (vLLM needs its tool parser switched on).
* `@tool`: the quickest way to make a LangChain tool from a typed function;
  the docstring becomes the description, the type hints become the schema.
"""

from __future__ import annotations

import time

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from rich.console import Console

from ..config.settings import Settings
from ..models.chat import api_key_for, build_chat_model
from ..models.errors import classify
from ..providers.auth import mask_key
from ..providers.transport import TransportOptions, build_ssl_context


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny in {city}"


async def run_doctor(settings: Settings, model_ref: str | None, probe: bool) -> int:
    console = Console()
    ok = True

    def report(passed: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "[green]✓[/green]" if passed else "[red]✗[/red]"
        console.print(f"{mark} {what}" + (f" [dim]{detail}[/dim]" if detail else ""))

    report(True, "settings", ", ".join(settings.sources) or "defaults only")
    try:
        provider, name = settings.resolve_model(model_ref)
    except Exception as e:
        report(False, "model configured", str(e))
        return 1
    cfg = settings.providers[provider]
    key = api_key_for(provider, cfg)
    report(cfg.auth.type == "none" or bool(key), "API key", mask_key(key) if key else "missing")

    # 1. Reachability, TLS and auth: GET /models with the same TLS settings.
    ssl_ctx = build_ssl_context(TransportOptions(ca_cert_path=cfg.ca_cert_path))
    headers = {**cfg.headers, **({"Authorization": f"Bearer {key}"} if key else {})}
    try:
        async with httpx.AsyncClient(verify=ssl_ctx, timeout=cfg.connect_timeout) as client:
            resp = await client.get(cfg.base_url.rstrip("/") + "/models", headers=headers)
        ids = [m.get("id") for m in resp.json().get("data", [])] if resp.is_success else []
        report(resp.is_success, f"GET {cfg.base_url}/models", f"HTTP {resp.status_code}")
        if ids:
            report(name in ids, f"model {name!r} listed", ", ".join(map(str, ids[:8])))
    except Exception as e:
        report(False, f"GET {cfg.base_url}/models", f"{type(e).__name__}: {e}")
        return 1
    if not probe:
        return 0 if ok else 1

    bundle = build_chat_model(settings, model_ref)
    # 2. A plain chat call, timed.
    try:
        start = time.monotonic()
        reply = await bundle.model.ainvoke([HumanMessage("Reply with the single word OK.")])
        report(True, "chat completion", f"{time.monotonic() - start:.1f}s: {reply.text[:40]!r}")
    except Exception as e:
        err = classify(e)
        report(False, "chat completion", f"{err.message} {err.hint or ''}")
        return 1
    # 3. Native tool calling.
    try:
        reply = await bundle.model.bind_tools([get_weather]).ainvoke(
            [HumanMessage("What's the weather in Paris? Use the tool.")]
        )
        names = [c["name"] for c in reply.tool_calls]
        report("get_weather" in names, "native tool calling", ", ".join(names) or "no tool call")
    except Exception as e:
        report(False, "native tool calling", classify(e).message)
    return 0 if ok else 1
