"""The gateway's own endpoints: what `ChatOpenAI` doesn't cover.

`ChatOpenAI` (models/chat.py) does chat completions. A gateway also says
which models it serves and their limits, and cmcoder uses that:

| Gateway | Models | Per-model info |
|---|---|---|
| LiteLLM, vLLM, any OpenAI-compatible (`"type": "openai"`) | `GET /models` | LiteLLM's `GET /model/info`: context window, max output, tool calling |
| Open WebUI (`"type": "openwebui"`) | `GET /api/models` (with `owned_by`: ollama / openai) | the backend; for Ollama, the trained length from `/ollama/api/show` |

Open WebUI serves the OpenAI-compatible API under `/api`
(`/api/chat/completions`), so `api_base()` turns its address into the base
URL `ChatOpenAI` gets. For an Ollama model behind it, every request carries
`options.num_ctx` (models/chat.py): Ollama silently drops the start of a
longer prompt, so cmcoder sends its own window.

`prepare_models()` runs once before the agent is built (it's async; building
the agent isn't): it caches the model info for a day and, when nothing better
is known, probes the real context window (a request with an impossibly large
`max_tokens` that the server rejects, stating its limit).

LANGCHAIN NOTE: none here, on purpose. LangChain's model classes cover
inference, not a gateway's admin endpoints, so this is plain `httpx` with the
same TLS, proxy and auth settings as the chat model.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from ..config.settings import ProviderConfig, Settings, SettingsError, config_dir
from ..providers.profiles import ModelProfile, resolve_profile
from ..providers.transport import TransportOptions, build_client
from .windows import PROBE_SOURCE, learned_entry, parse_context_window, save_window

MODEL_INFO_TTL = 24 * 3600
SKIPPED_OWNERS = {"arena"}  # Open WebUI entries that aren't one model to talk to


class GatewayError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def api_base(cfg: ProviderConfig) -> str:
    """The base URL for chat completions: Open WebUI's `/api`, else `baseUrl`."""
    url = cfg.base_url.rstrip("/")
    if cfg.type != "openwebui":
        return url
    for suffix in ("/api/v1", "/api"):  # accept either form in settings
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return f"{url}/api"


class Gateway:
    def __init__(self, name: str, cfg: ProviderConfig, api_key: str | None) -> None:
        self.name = name
        self.cfg = cfg
        self.kind = cfg.type
        self.base_url = api_base(cfg)
        auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = build_client(
            TransportOptions(
                ca_cert_path=cfg.ca_cert_path,
                connect_timeout=cfg.connect_timeout,
                read_timeout=cfg.read_timeout,
            ),
            headers={**cfg.headers, **auth},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get_json(self, url: str) -> Any:
        resp = await self.client.get(url)
        if resp.status_code != 200:
            raise GatewayError(_error_text(resp), resp.status_code)
        return resp.json()

    async def _models(self) -> list[dict[str, Any]]:
        data = await self._get_json(f"{self.base_url}/models")
        return [
            m
            for m in data.get("data", [])
            if isinstance(m, dict) and m.get("id") and m.get("owned_by") not in SKIPPED_OWNERS
        ]

    async def list_models(self) -> list[str]:
        return sorted(str(m["id"]) for m in await self._models())

    async def model_info(self) -> dict[str, dict[str, Any]]:
        """Per model: LiteLLM's limits, or Open WebUI's backend. {} if unknown."""
        if self.kind == "openwebui":
            return await self._openwebui_info()
        candidates = [f"{self.base_url}/model/info"]
        if self.base_url.endswith("/v1"):  # LiteLLM serves it at the root too
            candidates.append(f"{self.base_url[:-3]}/model/info")
        for url in candidates:
            try:
                data = await self._get_json(url)
            except (GatewayError, ValueError):
                continue
            return {
                str(item["model_name"]): dict(item.get("model_info") or {})
                for item in data.get("data", [])
                if isinstance(item, dict) and item.get("model_name")
            }
        return {}

    async def _openwebui_info(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for m in await self._models():
            entry: dict[str, Any] = {"backend": str(m.get("owned_by") or "openai")}
            info = m.get("info")
            if isinstance(info, dict) and info.get("base_model_id"):
                entry["base_model"] = str(info["base_model_id"])  # a preset in Open WebUI
            meta = info.get("meta") if isinstance(info, dict) else None
            caps = meta.get("capabilities") if isinstance(meta, dict) else None
            if isinstance(caps, dict) and isinstance(caps.get("vision"), bool):
                entry["supports_vision"] = caps["vision"]
            out[str(m["id"])] = entry
        return out

    async def ollama_context_length(self, model: str, timeout: float = 3.0) -> int | None:
        """An Ollama model's trained context length, through Open WebUI."""
        server = self.base_url[: -len("/api")]
        try:
            resp = await asyncio.wait_for(
                self.client.post(f"{server}/ollama/api/show", json={"model": model}), timeout
            )
            data = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, TimeoutError, ValueError):
            return None
        info = data.get("model_info") if isinstance(data, dict) else None
        for key, value in (info or {}).items():
            if key.endswith(".context_length") and isinstance(value, int) and value > 0:
                return value
        return None

    async def probe_context_window(self, model: str, timeout: float = 8.0) -> int | None:
        """Ask the server for the model's real window.

        A one-word prompt with an impossibly large `max_tokens`: vLLM and
        similar servers reject it before generating anything, stating their
        limit. A server that accepts it is disconnected at once (closing the
        stream stops generation). None when the server doesn't say."""
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10_000_000,
            "stream": True,
        }

        async def go() -> int | None:
            url = f"{self.base_url}/chat/completions"
            async with self.client.stream("POST", url, json=body) as resp:
                if resp.status_code == 200:
                    return None
                return parse_context_window((await resp.aread()).decode(errors="replace"))

        try:
            return await asyncio.wait_for(go(), timeout)
        except (TimeoutError, httpx.HTTPError):
            return None


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    if isinstance(data, dict):
        err = data.get("error") or data.get("detail") or data
        if isinstance(err, dict):
            err = err.get("message") or err
        return f"HTTP {resp.status_code}: {err}"
    return f"HTTP {resp.status_code}"


def gateway_for(settings: Settings, provider: str) -> Gateway:
    from .chat import api_key_for

    cfg = settings.providers.get(provider)
    if cfg is None:
        raise SettingsError(f"Unknown provider {provider!r}.")
    return Gateway(provider, cfg, api_key_for(provider, cfg))


# --- The model-info cache (read synchronously by build_chat_model) -------------


def _cache_file(provider: str) -> Path:
    return config_dir() / "cache" / f"model_info_{provider}.json"


def cached_model_info(provider: str, base_url: str) -> dict[str, dict[str, Any]]:
    """What `prepare_models` saved for this gateway, if still fresh; else {}."""
    try:
        data = json.loads(_cache_file(provider).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("base_url") != base_url or time.time() - data.get("at", 0) > MODEL_INFO_TTL:
        return {}
    return dict(data.get("models") or {})


def _save_model_info(provider: str, base_url: str, info: dict[str, dict[str, Any]]) -> None:
    path = _cache_file(provider)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"at": time.time(), "base_url": base_url, "models": info}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a cache: not being able to write it changes nothing else


def server_info_for(settings: Settings, provider: str, model: str) -> dict[str, Any] | None:
    cfg = settings.providers[provider]
    return cached_model_info(provider, api_base(cfg)).get(model)


async def prepare_models(
    settings: Settings, refs: list[str | None], *, probe: bool = True, timeout: float = 3.0
) -> None:
    """Fill the caches `build_chat_model` reads: model info and, when nothing
    better is known, the probed context window. Best effort, quick."""
    by_provider: dict[str, set[str]] = {}
    for ref in refs:
        try:
            provider, model = settings.resolve_model(ref)
        except Exception:
            continue
        if provider in settings.providers:
            by_provider.setdefault(provider, set()).add(model)
    for provider, models in by_provider.items():
        gateway = gateway_for(settings, provider)
        try:
            await _prepare(settings, gateway, models, probe=probe, timeout=timeout)
        finally:
            await gateway.aclose()


async def _prepare(
    settings: Settings, gw: Gateway, models: set[str], *, probe: bool, timeout: float
) -> None:
    info = cached_model_info(gw.name, gw.base_url)
    if not info:
        try:
            info = await asyncio.wait_for(gw.model_info(), timeout)
        except (GatewayError, httpx.HTTPError, TimeoutError, ValueError):
            info = {}
        # An Ollama model's window: cmcoder's usual one, never more than it was trained for.
        for model, entry in info.items():
            if model in models and entry.get("backend") == "ollama":
                trained = await gw.ollama_context_length(str(entry.get("base_model") or model))
                if trained:
                    entry["trained_context"] = trained
        if info:
            _save_model_info(gw.name, gw.base_url, info)
    if not probe:
        return
    for model in models:
        profile = profile_for(settings, gw.name, model, info.get(model))
        if profile.context_window_source != "built-in default" or profile.backend == "ollama":
            continue  # known already; Ollama never refuses a long prompt (it truncates)
        if learned_entry(gw.base_url, model) is not None:
            continue  # probed recently (maybe without an answer)
        tokens = await gw.probe_context_window(model)
        save_window(gw.base_url, model, tokens, PROBE_SOURCE)


def profile_for(
    settings: Settings, provider: str, model: str, server_info: dict[str, Any] | None
) -> ModelProfile:
    """The model's profile from everything known (see `resolve_profile`)."""
    from .windows import learned_window

    cfg = settings.providers[provider]
    info = dict(server_info or {})
    if info.get("backend") == "ollama" and info.get("trained_context"):
        base = resolve_profile(model, settings.model_profiles)
        if info["trained_context"] < base.context_window:
            info["max_input_tokens"] = info["trained_context"]
            info["window_source"] = "Ollama model (trained length, via Open WebUI)"
    return resolve_profile(
        model,
        settings.model_profiles,
        info or None,
        learned_window(api_base(cfg), model),
    )
