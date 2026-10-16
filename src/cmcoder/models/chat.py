"""`CmcoderChatModel`: LangChain's ChatOpenAI, adapted to LiteLLM/Open WebUI + Qwen3.

LANGCHAIN CONCEPTS IN THIS FILE

* `BaseChatModel` / `ChatOpenAI`: a chat model takes a list of messages
  (`SystemMessage`, `HumanMessage`, `AIMessage`, `ToolMessage`) and returns an
  `AIMessage`. `ChatOpenAI(base_url=...)` works with any server that speaks the
  OpenAI Chat Completions format, not only OpenAI itself.
* Streaming: `astream()` yields `AIMessageChunk`s. Chunks can be added
  together (`chunk1 + chunk2`); LangChain merges text, tool-call pieces and
  `additional_kwargs` for us.
* `additional_kwargs`: where provider-specific fields live on a message. We
  keep Qwen3's reasoning there under `reasoning_content`.
* Subclassing a provider class is the standard way to support a server's
  non-standard fields. LangChain's own `ChatDeepSeek` and `ChatQwQ` packages
  do exactly what `CmcoderChatModel` does here.

What stays outside LangChain: the TLS setup (company CA via `truststore`), the
API key lookup (OS keychain) and the model profiles were already solved in
cmcoderagent and are reused unchanged (providers/transport.py, auth.py,
profiles.py). We hand LangChain a ready `httpx.AsyncClient`, so all of that
still applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.model_profile import ModelProfile as LCModelProfile
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from ..config.settings import ProviderConfig, Settings, SettingsError, env_api_key_source
from ..providers.auth import ApiKeyAuth
from ..providers.profiles import ModelProfile
from ..providers.transport import TransportOptions, build_client
from .gateway import api_base, profile_for, server_info_for

REASONING_KEY = "reasoning_content"
# Field names servers use for reasoning text: LiteLLM/vLLM use
# "reasoning_content"; some newer servers use "reasoning".
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _reasoning_of(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    for name in _REASONING_FIELDS:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


class CmcoderChatModel(ChatOpenAI):
    """ChatOpenAI that keeps the `reasoning_content` field Qwen3 servers send.

    Plain `ChatOpenAI` only reads fields from the official OpenAI API, so it
    drops reasoning text. We override the two hooks it uses to turn a server
    response into LangChain objects, call the original, and copy the reasoning
    into `additional_kwargs`.

    The reasoning is never sent back to the model on later turns: ChatOpenAI
    doesn't serialise unknown `additional_kwargs` into requests, which is what
    Qwen's guidance asks for.
    """

    # Which request field carries the output limit. ChatOpenAI always sends the
    # newer `max_completion_tokens`; some self-hosted servers only read
    # `max_tokens` (the model profile says which).
    max_tokens_param: str = "max_tokens"

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.max_tokens_param == "max_tokens" and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload

    # --- streaming: one server chunk -> one ChatGenerationChunk ----------------
    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is None:
            return None
        choices = chunk.get("choices") or []
        reasoning = _reasoning_of(choices[0].get("delta")) if choices else None
        if reasoning and isinstance(generation.message, AIMessageChunk):
            # When chunks are added together, LangChain concatenates string
            # values in additional_kwargs, so the full reasoning builds up.
            generation.message.additional_kwargs[REASONING_KEY] = reasoning
        return generation

    # --- non-streaming: a whole response -> ChatResult -------------------------
    def _create_chat_result(
        self, response: Any, generation_info: dict[str, Any] | None = None
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data.get("choices") or [], strict=False):
            reasoning = _reasoning_of(choice.get("message"))
            if reasoning:
                generation.message.additional_kwargs[REASONING_KEY] = reasoning
        return result


@dataclass
class ModelBundle:
    """A chat model plus what the rest of cmcoder needs to know about it."""

    model: CmcoderChatModel
    profile: ModelProfile
    provider: str  # settings key, e.g. "corp"
    name: str  # model name at the gateway, e.g. a LiteLLM alias
    base_url: str = ""

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.name}"

    def quick(self) -> CmcoderChatModel:
        """This model with thinking switched off, for quick jobs (titles,
        summaries): a copy (`model_copy`) with these request fields added.
        A copy, not `.bind(...)`: LangChain's SummarizationMiddleware wants a
        chat model, and a bound runnable isn't one.

        The switch depends on the server (the profile's `thinkingSwitch`):
        Qwen3 on vLLM reads `chat_template_kwargs`, Ollama behind Open WebUI
        reads `options.think`. "prompt" (`/no_think` in the text) is left to
        the caller; "none": the model doesn't think, or can't be told."""
        p = self.profile
        extra = dict(self.model.extra_body or {})
        if p.backend == "ollama" and p.thinking_switch != "none":
            extra["options"] = {**extra.get("options", {}), "think": False}
        elif p.thinking_switch == "chat_template_kwargs":
            kwargs = extra.get("chat_template_kwargs", {})
            extra["chat_template_kwargs"] = {**kwargs, "enable_thinking": False}
        else:
            return self.model
        return self.model.model_copy(update={"extra_body": extra})

    def set_context_window(self, window: int, source: str) -> None:
        """Use a newly learned window everywhere: our profile and LangChain's."""
        self.profile.context_window = window
        self.profile.context_window_source = source
        self.model.profile = _langchain_profile(self.profile)
        if self.profile.backend == "ollama" and self.model.extra_body:
            self.model.extra_body["options"]["num_ctx"] = window


def _langchain_profile(profile: ModelProfile) -> LCModelProfile:
    """LangChain's own model profile (`BaseChatModel.profile`).

    LangChain middleware reads limits from here, e.g. `SummarizationMiddleware`
    with `trigger=("fraction", 0.8)` means 80% of `max_input_tokens`.
    """
    return LCModelProfile(
        max_input_tokens=profile.context_window,
        max_output_tokens=profile.max_output,
        tool_calling=profile.tool_calling != "prompted",
    )


def api_key_for(provider: str, cfg: ProviderConfig) -> str | None:
    """CMCODER_API_KEY wins, then the OS keychain (`cmcoder login`)."""
    if cfg.auth.type == "none":
        return None
    key, source = env_api_key_source()
    return ApiKeyAuth(
        provider, explicit_key=key, explicit_source=source or "environment"
    ).resolve_key()


def build_chat_model(settings: Settings, ref: str | None = None) -> ModelBundle:
    """Create the chat model for `ref` ("provider:model"; default: settings.model)."""
    provider, name = settings.resolve_model(ref)
    cfg = settings.providers.get(provider)
    if cfg is None:
        raise SettingsError(f"Unknown provider {provider!r}.")
    # Open WebUI serves the OpenAI API under /api (models/gateway.py).
    base_url = api_base(cfg)
    # The profile from everything known, most trusted last: built-in < the
    # gateway's model info (cached by prepare_models) < learned from the server
    # (its "maximum context length" error, or a probe) < an explicit setting.
    profile = profile_for(settings, provider, name, server_info_for(settings, provider, name))

    # Our own httpx client: company CA trust, proxies from the environment and
    # timeouts sized for a slow first token. LangChain passes it to the OpenAI SDK.
    client = build_client(
        TransportOptions(
            ca_cert_path=cfg.ca_cert_path,
            connect_timeout=cfg.connect_timeout,
            read_timeout=cfg.read_timeout,
        ),
        headers=cfg.headers,
    )

    kwargs: dict[str, Any] = {
        "model": name,
        "base_url": base_url,
        # The OpenAI SDK insists on a key; "none" is harmless for servers without auth.
        "api_key": api_key_for(provider, cfg) or "none",
        "default_headers": cfg.headers or None,
        "http_async_client": client,
        # Keep httpx's own proxy handling (HTTPS_PROXY / NO_PROXY) for the
        # sync client LangChain creates too; corporate networks rely on it.
        "http_socket_options": (),
        "max_retries": cfg.max_retries,
        # Stream even for `ainvoke` (titles, summaries, doctor): one code path
        # for every call, and a slow first token can't hit a whole-response timeout.
        "streaming": True,
        # Ask the server to include token counts in the stream (usage_metadata).
        "stream_usage": profile.stream_usage,
        "temperature": profile.temperature,
        "top_p": profile.top_p,
        "max_tokens_param": profile.max_tokens_param,
        "profile": _langchain_profile(profile),
    }
    extra_body = dict(profile.extra_body)
    if profile.backend == "ollama":
        # An Ollama model behind Open WebUI: Open WebUI passes `options` on to
        # Ollama, which silently drops the start of a prompt longer than num_ctx.
        extra_body["options"] = {"num_ctx": profile.context_window}
    if extra_body:
        # Extra JSON fields sent with every request, e.g. Qwen3's
        # {"chat_template_kwargs": {"enable_thinking": false}}.
        kwargs["extra_body"] = extra_body
    model = CmcoderChatModel(**{k: v for k, v in kwargs.items() if v is not None})
    return ModelBundle(
        model=model, profile=profile, provider=provider, name=name, base_url=base_url
    )
