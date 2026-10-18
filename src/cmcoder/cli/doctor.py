"""`cmcoder doctor`: the environment, the network path to the gateway, and the models.

Sections: the local environment (settings, managed settings, project trust,
bash, ripgrep, git); for each gateway the network (proxy, DNS, TCP, TLS with
the certificate's issuer when it isn't trusted), the API (key, models, model
info) and, with probes, each model (context window, streaming, thinking
switch, tool calling, images); then the vision model, MCP servers, hooks, the
Bash sandbox, telemetry, and the project's commands, agents and skills.

LANGCHAIN CONCEPTS IN THIS FILE

* `model.astream([...])`: the chunks as they arrive, to time the first token.
* `model.bind_tools([tool])`: a copy of the model that sends the tool's JSON
  Schema with every request. If the reply's `tool_calls` contains our tool,
  the server supports native tool calling. Behind LiteLLM that depends on the
  backend (vLLM needs its tool parser switched on).
* `@tool`: the quickest way to make a LangChain tool from a typed function;
  the docstring becomes the description, the type hints become the schema.
* Multimodal `HumanMessage` content (text + `image_url` blocks) for the
  image check.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import platform
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import tool
from rich.console import Console
from rich.text import Text

from ..compat import SHELL_HELP, find_program, find_shell
from ..config.settings import (
    Settings,
    config_fingerprint,
    find_project_root,
    ignored_settings_message,
    is_approved,
    managed_settings_path,
)
from ..core.hooks import HookRunner
from ..models.chat import REASONING_KEY, ModelBundle, api_key_for, build_chat_model
from ..models.errors import classify
from ..models.gateway import Gateway, GatewayError, gateway_for, prepare_models, profile_for
from ..models.windows import PROBE_SOURCE, save_window
from ..providers.auth import ApiKeyAuth
from ..providers.profiles import ModelProfile, resolve_profile
from ..providers.transport import TransportOptions, build_ssl_context, proxy_bypassed
from .symbols import sym

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
# The image check: a plain square of one colour, which a model that sees images names.
TEST_COLOUR = ("green", (0, 170, 0))


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny in {city}"


def _style(status: str) -> tuple[str, str]:
    s = sym()
    return {
        OK: (s.ok, "green"),
        WARN: (s.warn, "yellow"),
        FAIL: (s.error, "red"),
        INFO: ("•", "cyan"),
    }[status]


class Doctor:
    def __init__(self, settings: Settings, console: Console) -> None:
        self.settings = settings
        self.console = console
        self.failed = False

    def report(self, status: str, title: str, detail: str = "") -> None:
        icon, style = _style(status)
        self.console.print(Text(f"{icon} ", style=style) + Text(title))
        for line in detail.splitlines():
            self.console.print(Text(f"    {line}", style="dim"))
        if status == FAIL:
            self.failed = True

    def section(self, title: str) -> None:
        self.console.print(Text(f"\n{title}", style="bold"))

    # --- the local environment ---------------------------------------------------

    def check_managed(self) -> None:
        """The organisation's managed settings, and whether this user could
        change them (then they don't protect anything)."""
        s = self.settings
        path = Path(s.managed_path) if s.managed_path else managed_settings_path()
        if not s.managed_path:
            self.report(INFO, "No managed settings", f"(an administrator can create {path})")
            return
        p = s.permissions
        enforced = [
            f"bypassPermissions {'disabled' if p.disable_bypass_permissions_mode else 'allowed'}",
            f"auto mode {'disabled' if p.disable_auto_mode else 'allowed'}",
            f"high-risk commands: {p.high_risk_commands}",
            f"{len(p.deny)} deny rule(s)",
        ]
        if p.allow_managed_permission_rules_only:
            enforced.append("only managed allow rules")
        if s.lock_providers:
            enforced.append(f"providers locked to: {', '.join(s.providers)}")
        self.report(OK, f"Managed settings in effect: {path}", "; ".join(enforced))
        writable = [q for q in (path, path.parent) if os.access(q, os.W_OK)]
        if writable:
            self.report(
                WARN,
                "Managed settings can be changed by the current user",
                f"{writable[0]} is writable without administrator rights here (or you are "
                "running as an administrator). Restrict it so only administrators can write it.",
            )

    def check_local(self) -> None:
        self.section("Local environment")
        s = self.settings
        self.report(
            INFO, "Settings loaded from", "\n".join(s.sources) or "(no settings files; defaults)"
        )
        self.check_managed()
        cwd = Path.cwd().resolve()
        root = find_project_root(cwd)
        self.report(
            INFO,
            f"Project root: {root}",
            "the current folder"
            if root == cwd
            else "found by a .git or .cmcoder folder above the current one; reads inside it "
            "need no approval, so check it is the project you mean",
        )
        if warning := ignored_settings_message(s):
            self.report(WARN, "Project settings", warning)
        else:
            self.report(
                INFO,
                "Project settings",
                "trusted (cmcoder trust)" if s.project_trusted else "not trusted; nothing ignored",
            )
        self.report(
            INFO,
            f"Platform: {platform.system()} {platform.release()}, Python {platform.python_version()}",
        )
        shell = find_shell()
        if shell:
            self.report(OK, "bash found (Bash tool)", shell)
        else:
            self.report(FAIL, "bash not found (Bash tool)", SHELL_HELP)
        rg = find_program("rg")
        if rg:
            self.report(OK, "ripgrep found (fast Grep/Glob)", rg)
        else:
            self.report(
                WARN,
                "ripgrep (rg) not found",
                "Grep/Glob use the slower built-in search. Install ripgrep for large repos "
                "(winget install BurntSushi.ripgrep.MSVC, brew install ripgrep, or apt install "
                "ripgrep).",
            )
        git = find_program("git")
        if git:
            self.report(OK, "git found", git)
        else:
            self.report(WARN, "git not found", "Prompts won't include git status.")
        chosen = sym()
        why = "symbols setting" if s.symbols != "auto" else "detected"
        hint = (
            "the classic Windows console's fonts lack some symbols (they showed as ?); "
            'Windows Terminal has them ("symbols": "unicode" forces the full set)'
            if chosen.name == "basic"
            else '"symbols": "basic" if you see "?" instead of symbols'
        )
        self.report(INFO, f"Terminal symbols: {chosen.name} ({why})", hint)
        if not s.providers:
            self.report(
                FAIL,
                "No model provider configured",
                "Run `cmcoder setup`, add `providers` to ~/.cmcoder/settings.json, or set "
                "CMCODER_BASE_URL.",
            )
        if not s.model:
            self.report(FAIL, "No model configured", "Set `model` in settings or CMCODER_MODEL.")
        else:
            self.report(OK, f"Main model: {s.model}")
        if s.small_fast_model:
            self.report(OK, f"Small/fast model: {s.small_fast_model}")
        else:
            self.report(
                WARN, "No smallFastModel set", "Quick jobs (titles, summaries) use the main model."
            )
        if s.model:
            main = s.model.partition(":")[2] or s.model
            if s.vision_model:
                self.report(OK, f"Images: described by {s.vision_model} when {main} can't see them")
            elif resolve_profile(main, s.model_profiles).vision:
                self.report(OK, f"Images: {main} sees them")
            else:
                self.report(
                    INFO,
                    f"Images: {main} can't see images, and no visionModel is set",
                    'Pasted images need a model that sees them: set "visionModel" to one on '
                    "your gateway (a Qwen-VL, for example). If the gateway says otherwise, "
                    'set "vision" in modelProfiles.',
                )

    # --- the network path ---------------------------------------------------------

    async def check_network(self, provider: str) -> bool:
        cfg = self.settings.providers[provider]
        url = urlparse(cfg.base_url)
        host = url.hostname or ""
        port = url.port or (443 if url.scheme == "https" else 80)
        self.section(f"Model server: {provider} ({cfg.base_url})")
        if url.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            self.report(
                WARN,
                "Plain http:// to a remote host",
                "Code would be sent unencrypted. Use https://.",
            )
        bypass = proxy_bypassed(cfg.base_url)
        if bypass is None:
            self.report(OK, "No HTTPS proxy configured")
        elif bypass:
            self.report(OK, "HTTPS proxy set, and NO_PROXY covers this host")
        else:
            self.report(
                WARN,
                "HTTPS_PROXY is set but NO_PROXY does not include this host",
                f"Internal servers usually need bypassing: add {host} (or its domain) to NO_PROXY.",
            )
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
            self.report(
                OK, f"DNS: {host} resolves", ", ".join(sorted({str(i[4][0]) for i in infos}))
            )
        except OSError as e:
            self.report(
                FAIL,
                f"DNS: cannot resolve {host}",
                f"{e}\nThis name only resolves on the company network: connect to the VPN.",
            )
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), cfg.connect_timeout
            )
            writer.close()
            self.report(OK, f"TCP: connected to {host}:{port}")
        except (OSError, TimeoutError) as e:
            self.report(
                FAIL,
                f"TCP: cannot connect to {host}:{port}",
                f"{e or type(e).__name__}\nCheck VPN, firewall or proxy.",
            )
            return False
        if url.scheme == "https":
            ctx = build_ssl_context(TransportOptions(ca_cert_path=cfg.ca_cert_path))
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ctx, server_hostname=host),
                    cfg.connect_timeout,
                )
                cert = writer.get_extra_info("peercert") or {}
                writer.close()
                self.report(OK, "TLS: certificate verified", _cert_summary(cert))
            except ssl.SSLCertVerificationError as e:
                cert = await _fetch_unverified_cert(host, port, cfg.connect_timeout)
                detail = f"{e.verify_message or e}\n{_cert_summary(cert)}" if cert else str(e)
                self.report(
                    FAIL,
                    "TLS: certificate not trusted",
                    detail + "\nInstall the company root CA in the OS trust store, or set "
                    "`caCertPath` in settings (or CMCODER_CA_CERT) to the CA's .pem file.",
                )
                return False
            except (OSError, ssl.SSLError, TimeoutError) as e:
                self.report(FAIL, "TLS: handshake failed", str(e) or type(e).__name__)
                return False
        return True

    # --- the API and the models ---------------------------------------------------

    async def check_api(self, provider: str, models: list[str], probe: bool) -> None:
        cfg = self.settings.providers[provider]
        if cfg.auth.type == "apiKey":
            if api_key_for(provider, cfg):
                self.report(OK, f"API key found: {_describe_key(provider)}")
            else:
                self.report(FAIL, "No API key", "Run `cmcoder login` or set CMCODER_API_KEY.")
                return
        gw = gateway_for(self.settings, provider)
        try:
            try:
                available = await gw.list_models()
            except (GatewayError, httpx.HTTPError, ValueError) as e:
                hint = (
                    f"\ncmcoder sent the {_describe_key(provider)}. If that's not the key you "
                    "expect, check CMCODER_API_KEY, then run `cmcoder login`."
                    if isinstance(e, GatewayError) and e.status in (401, 403)
                    else ""
                )
                self.report(FAIL, "API: GET /models failed", f"{e}{hint}")
                return
            self.report(
                OK,
                f"API: authenticated, {len(available)} models available",
                ", ".join(available[:20]),
            )
            for m in models:
                if m in available:
                    self.report(OK, f"Model '{m}' is available")
                else:
                    self.report(
                        FAIL,
                        f"Model '{m}' is not in the server's model list",
                        "Use one of the names above (LiteLLM aliases or Open WebUI model ids).",
                    )
            try:
                info = await gw.model_info()
            except (GatewayError, httpx.HTTPError, ValueError):
                info = {}
            self._report_info(gw, models, info)
            if probe:
                # The same caches the agent reads (Open WebUI backends, Ollama
                # windows), so the probes are sent the way a session sends them.
                await prepare_models(
                    self.settings, [f"{provider}:{m}" for m in models], probe=False
                )
                small = self._small_fast_model()
                for m in models:
                    if m in available:
                        await self.probe_model(gw, m, info.get(m))
                        if (provider, m) != small:  # images never go to that one
                            await self.check_images(provider, m)
        finally:
            await gw.aclose()

    def _report_info(self, gw: Gateway, models: list[str], info: dict[str, Any]) -> None:
        if gw.kind == "openwebui":
            for m in models:
                backend = info.get(m, {}).get("backend", "?")
                where = (
                    "Ollama"
                    if backend == "ollama"
                    else "an OpenAI-compatible server"
                    if backend == "openai"
                    else f"a '{backend}' connection"
                )
                self.report(INFO, f"{m}: served by {where} through Open WebUI")
        elif info:
            for m in models:
                if mi := info.get(m, {}):
                    self.report(
                        INFO,
                        f"{m}: server reports context {mi.get('max_input_tokens', '?')} tokens, "
                        f"max output {mi.get('max_output_tokens', '?')}, "
                        f"tool calling {mi.get('supports_function_calling', '?')}",
                    )
        else:
            self.report(INFO, "Server does not expose /model/info; using built-in model profiles")

    def _small_fast_model(self) -> tuple[str, str] | None:
        s = self.settings
        if not s.small_fast_model or s.small_fast_model == s.model:
            return None
        try:
            return s.resolve_model(s.small_fast_model)
        except Exception:
            return None

    def _bundle(self, provider: str, model: str) -> ModelBundle:
        return build_chat_model(self.settings, f"{provider}:{model}")

    async def check_context_window(
        self, gw: Gateway, model: str, server_info: dict[str, Any] | None
    ) -> ModelProfile:
        """Ask the server for the real window (fresh, not from the cache) and
        report which value cmcoder will use and where it came from."""
        profile = profile_for(self.settings, gw.name, model, server_info)
        if profile.backend == "ollama":
            self.report(
                OK,
                f"{model}: context window {profile.context_window:,} tokens, sent to Ollama "
                "as num_ctx with every request",
                f"from {profile.context_window_source}. Ollama drops the start of longer "
                "prompts silently, so cmcoder sets it itself. A larger value needs more GPU "
                'memory on the server: set "contextWindow" in modelProfiles to change it.',
            )
            return profile
        tokens = await gw.probe_context_window(model)
        save_window(gw.base_url, model, tokens, PROBE_SOURCE)
        profile = profile_for(self.settings, gw.name, model, server_info)
        heard = f"server says {tokens:,} tokens" if tokens else "server did not state its limit"
        detail = f"{heard}; using {profile.context_window:,} from {profile.context_window_source}."
        if tokens and profile.context_window > tokens:
            self.report(
                WARN,
                f"{model}: context window set to {profile.context_window:,} tokens, "
                f"but the server allows {tokens:,}",
                f"{detail} Requests will fail until contextWindow in modelProfiles is lowered.",
            )
        elif profile.context_window <= 32_768:
            self.report(
                WARN,
                f"{model}: context window is {profile.context_window:,} tokens",
                f"{detail} Small for an agent: conversations will be summarised often. If GPU "
                "memory allows, ask the admin to serve a longer context (e.g. vLLM "
                "--max-model-len 65536 or more; Qwen3 supports long context via YaRN).",
            )
        else:
            self.report(OK, f"{model}: context window {profile.context_window:,} tokens", detail)
        return profile

    async def probe_model(self, gw: Gateway, model: str, info: dict[str, Any] | None) -> None:
        self.console.print(Text(f"  probing {model} …", style="dim"))
        profile = await self.check_context_window(gw, model, info)
        bundle = self._bundle(gw.name, model)
        # 1. Streaming and latency.
        start = time.monotonic()
        first: float | None = None
        final: BaseMessage | None = None
        try:
            async for chunk in bundle.model.astream(
                [HumanMessage("Reply with exactly the word OK.")], max_tokens=512
            ):
                if first is None and chunk.text:
                    first = time.monotonic() - start
                final = chunk if final is None else final + chunk  # type: ignore[operator]
        except Exception as e:
            err = classify(e)
            self.report(FAIL, f"{model}: chat request failed", f"{err.message} {err.hint or ''}")
            return
        total = time.monotonic() - start
        if final is None:
            self.report(FAIL, f"{model}: stream ended without a reply")
            return
        usage = "reported" if getattr(final, "usage_metadata", None) else "not reported"
        thinks = _reasoning(final)
        self.report(
            OK,
            f"{model}: streaming works",
            f"first token {f'{first:.1f}s' if first is not None else 'n/a'}, total {total:.1f}s, "
            f"usage {usage}, reasoning {'present' if thinks else 'none'}",
        )
        # 2. The thinking switch (quick jobs turn thinking off).
        switch = "Ollama's think option" if profile.backend == "ollama" else profile.thinking_switch
        if profile.thinking_switch != "none":
            try:
                quick = await bundle.quick().ainvoke([HumanMessage("Say hi.")], max_tokens=256)
                if _reasoning(quick):
                    self.report(
                        WARN,
                        f"{model}: thinking could not be switched off via {switch}",
                        "The gateway may drop extra params. Set modelProfiles thinkingSwitch "
                        'to "prompt".',
                    )
                else:
                    self.report(OK, f"{model}: thinking can be switched off ({switch})")
            except Exception as e:
                self.report(
                    WARN,
                    f"{model}: request with thinking switch rejected",
                    f'{classify(e).message}\nSet modelProfiles thinkingSwitch to "prompt" or "none".',
                )
        # 3. Tool calling.
        await self._check_tools(gw, model, bundle)

    async def _check_tools(self, gw: Gateway, model: str, bundle: ModelBundle) -> None:
        from ..middleware.tool_text import extract, no_tool_support

        try:
            reply = await bundle.model.bind_tools([get_weather]).ainvoke(
                [HumanMessage("What is the weather in Paris? Use the get_weather tool.")],
                max_tokens=1024,
            )
        except Exception as e:
            self.report(
                WARN if no_tool_support(e) else FAIL,
                f"{model}: request with tools rejected",
                f"{classify(e).message}\nThe backend has no native tool calling. cmcoder switches "
                'to prompted tool calls automatically; set "toolCalling": "prompted" in '
                "modelProfiles to skip the retry, or enable tool calling on the backend.",
            )
            return
        calls = reply.tool_calls
        if calls and calls[0]["name"] == "get_weather" and "paris" in str(calls[0]["args"]).lower():
            self.report(
                OK, f"{model}: native tool calling works", f"get_weather({calls[0]['args']})"
            )
        elif extract(reply.text, {"get_weather"})[1]:
            self.report(
                WARN,
                f"{model}: tool calls come back as text (<tool_call> tags)",
                "cmcoder parses them automatically. Enabling the backend's tool parser is still "
                "better (vLLM --enable-auto-tool-choice --tool-call-parser hermes).",
            )
        elif "get_weather" in reply.text:
            self.report(
                FAIL,
                f"{model}: model wrote the tool call as text instead of a tool call",
                'Set "toolCalling": "prompted" in modelProfiles, or enable tool calling on the '
                "backend (vLLM --enable-auto-tool-choice --tool-call-parser hermes).",
            )
        else:
            hint = "The backend may not support tool calling for this model."
            if gw.kind == "openwebui":
                hint += (
                    " In Open WebUI, set the model's Function Calling to Native (Admin Panel → "
                    "Settings → Models → the model → Advanced Params); with an Ollama backend "
                    "the model itself must support tools."
                )
            self.report(FAIL, f"{model}: no tool call returned", hint)

    # --- images ---------------------------------------------------------------------

    async def check_images(self, provider: str, model: str) -> None:
        """Shows the model a test image: does it see images, and does cmcoder know?"""
        bundle = self._bundle(provider, model)
        sees, said = await sees_images(bundle)
        helper = self.settings.vision_model
        rule = f'{{"match": "{model}", "vision": {"true" if sees else "false"}}}'
        if sees and bundle.profile.vision:
            self.report(OK, f"{model}: sees images", f"it named the test image's colour: {said}")
        elif sees:
            instead = f" instead of being described by {helper}" if helper else ""
            self.report(
                WARN,
                f"{model}: sees images, but cmcoder doesn't know it",
                f"Add {rule} to modelProfiles in your settings, so pasted images go "
                f"straight to it{instead}.",
            )
        elif bundle.profile.vision:
            self.report(
                WARN,
                f"{model}: didn't see the test image, but cmcoder sends it images",
                f'It said: {said}\nAdd {rule} to modelProfiles, and set "visionModel" to a '
                "model that sees images.",
            )
        elif helper:
            self.report(OK, f"{model}: can't see images; {helper} describes them for it")
        else:
            self.report(
                INFO,
                f"{model}: can't see images, and no visionModel is set",
                f"It said: {said}\nPasted images need a model that sees them: set "
                '"visionModel" to one on your gateway (a Qwen-VL, for example).',
            )

    async def check_vision_model(self) -> None:
        """visionModel: is it there, and does it see images?"""
        ref = self.settings.vision_model
        if not ref:
            return
        try:
            bundle = build_chat_model(self.settings, ref)
        except Exception as e:
            self.report(FAIL, f"visionModel {ref}: {e}")
            return
        sees, said = await sees_images(bundle)
        if sees:
            self.report(OK, f"visionModel {bundle.name}: sees images", f"it said: {said}")
        else:
            self.report(
                FAIL,
                f"visionModel {bundle.name}: didn't see the test image",
                f"It said: {said}\nChoose a model that sees images (a Qwen-VL, for example).",
            )

    # --- extensions -------------------------------------------------------------------

    def check_hooks(self) -> None:
        runner = HookRunner(self.settings, find_project_root(Path.cwd().resolve()))
        if not runner.hooks:
            return
        self.section("Hooks")
        for hook in runner.hooks:
            match = f" [{hook.matcher}]" if hook.matcher else ""
            approved = ""
            if hook.origin == "project":
                fp = config_fingerprint(hook.command)
                approved = (
                    " · approved"
                    if is_approved(runner.root, hook.key, fp)
                    else " · asks before it first runs"
                )
            self.report(
                INFO, f"{hook.event}{match}: {hook.command.command}", f"{hook.origin}{approved}"
            )
        if find_shell() is None:
            self.report(FAIL, "Hooks need bash to run", SHELL_HELP)

    def check_sandbox(self) -> None:
        from ..sandbox import detect

        cfg = self.settings.sandbox
        self.section("Bash sandbox")
        if cfg.enabled is False:
            self.report(INFO, "Off in settings (sandbox.enabled: false)")
            return
        avail = detect()
        if not avail.ok:
            status = INFO if sys.platform == "win32" else WARN
            self.report(status, "Not available: Bash commands run unsandboxed", avail.reason)
            return
        how = "bubblewrap" if avail.kind == "bwrap" else "macOS sandbox-exec"
        hosts = ", ".join(cfg.network.allowed_hosts) or "none (add sandbox.network.allowedHosts)"
        extra = f" + {', '.join(cfg.writable_paths)}" if cfg.writable_paths else ""
        prompts = "none for sandboxed commands" if cfg.auto_allow else "as usual"
        leaving = "asks each time" if cfg.allow_unsandboxed_commands else "not allowed"
        self.report(
            OK,
            f"Bash commands run in a sandbox ({how})",
            f"writes: the project and a temp folder{extra}\nnetwork: {hosts}\n"
            f"prompts: {prompts}; leaving the sandbox: {leaving}",
        )

    async def check_telemetry(self) -> None:
        from ..telemetry import from_settings

        telemetry = from_settings(self.settings.telemetry)
        if telemetry is None:
            if self.settings.telemetry.enabled:
                self.section("Telemetry")
                self.report(WARN, "Telemetry is on but has no endpoint", "Set telemetry.endpoint.")
            return
        self.section("Telemetry")
        try:
            problem = await telemetry.check()
        finally:
            await telemetry.client.aclose()
        if problem:
            self.report(FAIL, f"OpenTelemetry collector not reachable: {telemetry.url}", problem)
        else:
            self.report(
                OK,
                f"OpenTelemetry metrics go to {telemetry.url}",
                "Counts and timings only: no prompts, code, paths or commands.",
            )

    def check_extensions(self) -> None:
        """Custom commands, subagents and skills: what's found, and from where."""
        from ..agents.subagents import load_agents, untrusted_project_agents
        from ..core.commands import load_commands
        from ..core.skills import load_skills

        root = find_project_root(Path.cwd().resolve())
        trusted = self.settings.project_trusted
        commands = load_commands(root)
        agents = {n: a for n, a in load_agents(root, trusted).items() if a.origin != "built-in"}
        skills = load_skills(root)
        ignored = [] if trusted else untrusted_project_agents(root)
        if not (commands or agents or skills or ignored):
            return
        self.section("Commands, agents and skills")
        for c in commands.values():
            tools = (
                f" · allows {', '.join(c.allowed_tools)} for its turn" if c.allowed_tools else ""
            )
            if c.origin == "project" and c.allowed_tools and not trusted:
                tools = " · its allowed-tools apply once the project is trusted"
            self.report(INFO, f"/{c.name}: {c.description or '(no description)'}", c.origin + tools)
        for a in agents.values():
            self.report(
                INFO,
                f"agent {a.name}: {a.description}",
                f"{a.origin} · tools: {', '.join(a.tools) if a.tools else 'all'}"
                f" · model: {a.model or 'inherit'}",
            )
        if ignored:
            self.report(
                WARN,
                f"Project agents not used: {', '.join(ignored)}",
                "the project isn't trusted (`cmcoder trust`)",
            )
        for sk in skills.values():
            self.report(INFO, f"skill {sk.name}: {sk.description}", sk.origin)

    async def check_mcp(self, probe: bool) -> None:
        """MCP servers: configured, approved, and (with probes) whether they start."""
        from ..mcp import McpManager

        s = self.settings
        if not s.mcp_servers and not s.project_mcp_servers:
            return
        self.section("MCP servers")
        manager = McpManager(s, find_project_root(Path.cwd().resolve()))
        if not probe:
            for server in manager.servers:
                self.report(INFO, f"{server.name}: {server.config.describe()}", server.origin)
            return
        try:
            await manager.start(None)  # each failed server is reported below
            for server in manager.servers:
                if server.status == "connected":
                    self.report(
                        OK, f"{server.name}: {len(server.tools)} tools", server.config.describe()
                    )
                elif server.status == "not approved":
                    self.report(
                        WARN, f"{server.name}: not approved", f"cmcoder mcp approve {server.name}"
                    )
                elif server.status in ("disabled", "blocked"):
                    self.report(INFO, f"{server.name}: {server.status}", server.error)
                else:
                    self.report(FAIL, f"{server.name}: could not start", server.error)
        finally:
            await manager.close()

    async def check_code_search(self, probe: bool) -> None:
        """The embedding model answers, and the project's index."""
        cfg = self.settings.rag
        if cfg.enabled is False:
            return
        self.section("Code search (RAG)")
        if not cfg.embedding_model:
            self.report(INFO, "Not set up", "`cmcoder rag setup` chooses an embedding model.")
            return
        try:
            provider, model = self.settings.resolve_model(cfg.embedding_model)
            gateway = gateway_for(self.settings, provider)
        except Exception as e:  # a settings problem, reported as such
            self.report(FAIL, f"Embedding model {cfg.embedding_model}: {e}")
            return
        try:
            if probe:
                from ..rag.common import EmbeddingError
                from ..rag.embed import GatewayEmbeddings

                embedder = GatewayEmbeddings(gateway, model)
                try:
                    await embedder.embed(["def hello(): return 'world'"])
                except EmbeddingError as e:
                    hint = f"\n{e.hint}" if e.hint else ""
                    self.report(FAIL, f"Embedding model {provider}:{model}", f"{e}{hint}")
                    return
                self.report(
                    OK,
                    f"Embedding model {provider}:{model} answers "
                    f"({embedder.dim} dimensions, {embedder.gateway_kind})",
                )
            else:
                self.report(INFO, f"Embedding model {provider}:{model} (not checked)")
        finally:
            await gateway.aclose()
        await self._check_index()

    async def _check_index(self) -> None:
        from ..config.settings import SettingsError
        from ..rag.common import StoreError
        from ..rag.index import open_index
        from ..rag.setup import age
        from ..rag.stores import ChromaVectorStore

        root = find_project_root(Path.cwd().resolve())
        try:
            index = open_index(self.settings, root)
        except (SettingsError, StoreError) as e:
            self.report(FAIL, "Index store", str(e))
            return
        if index is None:
            return
        try:
            if isinstance(index.store, ChromaVectorStore):
                try:
                    await index.store.check()
                except StoreError as e:
                    self.report(FAIL, index.store.describe(), str(e))
                    return
            st = await index.status()
            if st.files or (st.read_only and st.chunks):
                self.report(
                    OK,
                    f"This project's index: {st.files:,} files, {st.chunks:,} pieces, "
                    f"updated {age(st.updated)}",
                    st.store + (" (read-only)" if st.read_only else ""),
                )
            else:
                self.report(INFO, "No index for this project yet", "`cmcoder index` builds it.")
        finally:
            await index.close()

    # ------------------------------------------------------------------------------------

    async def run(self, model_refs: list[str], probe: bool) -> int:
        self.check_local()
        by_provider: dict[str, list[str]] = {}
        for ref in model_refs:
            try:
                p, m = self.settings.resolve_model(ref)
            except Exception as e:
                self.report(FAIL, str(e))
                continue
            if p not in self.settings.providers:
                self.report(FAIL, f"Unknown provider {p!r} in {ref!r}")
                continue
            models = by_provider.setdefault(p, [])
            if m not in models:
                models.append(m)
        for provider, models in by_provider.items():
            if await self.check_network(provider):
                await self.check_api(provider, models, probe)
        if probe:
            await self.check_vision_model()
        await self.check_mcp(probe)
        self.check_hooks()
        self.check_sandbox()
        await self.check_code_search(probe)
        await self.check_telemetry()
        self.check_extensions()
        self.console.print()
        if self.failed:
            self.console.print(
                f"[bold red]Some checks failed.[/bold red] Fix the items marked {sym().error} above."
            )
            return 1
        self.console.print("[bold green]All checks passed.[/bold green]")
        return 0


def _describe_key(provider: str) -> str:
    from ..config.settings import env_api_key_source

    key, source = env_api_key_source()
    return ApiKeyAuth(
        provider, explicit_key=key, explicit_source=source or "environment"
    ).describe()


def _reasoning(message: BaseMessage) -> bool:
    return bool(message.additional_kwargs.get(REASONING_KEY)) or "</think>" in message.text


def _test_image() -> dict[str, Any]:
    from PIL import Image as PilImage

    raw = io.BytesIO()
    PilImage.new("RGB", (64, 64), TEST_COLOUR[1]).save(raw, "PNG")
    data = base64.b64encode(raw.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


async def sees_images(bundle: ModelBundle) -> tuple[bool, str]:
    """Whether the model named the test image's colour, and what it said (or the error)."""
    from ..middleware.vision import VisionError, ask

    question: list[str | dict[str, Any]] = [
        {"type": "text", "text": "What colour is this image? Answer with one word."},
        _test_image(),
    ]
    try:
        said = await ask(bundle.quick(), bundle.name, [HumanMessage(question)], max_tokens=1024)
    except VisionError as e:
        return False, f"({e})"
    except Exception as e:
        return False, f"(the request failed: {classify(e).message})"
    said = " ".join(said.split())[:200]
    return TEST_COLOUR[0] in said.lower(), repr(said)


def _name(parts: Any) -> str:
    try:
        for rdn in parts:
            for key, value in rdn:
                if key == "commonName":
                    return str(value)
        return ", ".join(f"{k}={v}" for rdn in parts for k, v in rdn)
    except (TypeError, ValueError):
        return "?"


def _cert_summary(cert: dict[str, Any] | None) -> str:
    if not cert:
        return ""
    return (
        f"subject: {_name(cert.get('subject', ()))}\n"
        f"issuer:  {_name(cert.get('issuer', ()))}\n"
        f"expires: {cert.get('notAfter', '?')}"
    )


async def _fetch_unverified_cert(host: str, port: int, timeout: float) -> dict[str, Any] | None:
    """Fetch the server certificate *without* verifying it, only to show who
    issued it. No application data is sent on this connection."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout
        )
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
    except (OSError, ssl.SSLError, TimeoutError, AttributeError):
        return None
    if not der:
        return None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            name = f.name
        try:
            return dict(ssl._ssl._test_decode_cert(name))  # type: ignore[attr-defined]
        finally:
            os.unlink(name)
    except Exception:
        return None


async def run_doctor(settings: Settings, model_refs: list[str] | None, probe: bool) -> int:
    refs = model_refs or [m for m in (settings.model, settings.small_fast_model) if m]
    return await Doctor(settings, Console()).run(refs, probe)
