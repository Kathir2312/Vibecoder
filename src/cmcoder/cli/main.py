"""The `cmcoder` command line (Typer).

cmcoder                    interactive session
cmcoder "fix the test"     interactive, starting with that prompt
cmcoder -p "..."           non-interactive (scripts, CI)
cmcoder doctor             check the gateway and model
cmcoder login / logout     store or delete the API key (OS keychain)
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .. import VERSION_TEXT
from ..compat import use_utf8_stdio
from ..config.settings import Settings, SettingsError, add_mcp_config, load_settings
from ..core.permissions import MODES

main_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
sub_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, no_args_is_help=True)
SUBCOMMANDS = {"doctor", "login", "logout", "sessions", "setup", "protocol-schema"}
err = Console(stderr=True)


# The IDEs that run cmcoder over the protocol (--client).
CLIENTS = ("vscode", "eclipse", "visualstudio", "netbeans", "jetbrains")


def _load(trust_project: bool = False) -> Settings:
    try:
        return load_settings(Path.cwd(), trust_project=trust_project)
    except SettingsError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2) from e


def _split(values: list[str] | None) -> list[str]:
    """Rules may be given as several flags or comma-separated ("Read,Bash(git *)")."""
    out: list[str] = []
    for v in values or []:
        depth, cur = 0, ""
        for ch in v:
            depth += ch == "("
            depth -= ch == ")"
            if ch in ", " and depth == 0:
                if cur.strip():
                    out.append(cur.strip())
                cur = ""
            else:
                cur += ch
        if cur.strip():
            out.append(cur.strip())
    return out


@main_app.command(
    help="Agentic coding assistant. Run without arguments for an interactive session."
)
def main(
    prompt: Annotated[str | None, typer.Argument(help="Prompt to start with.")] = None,
    print_mode: Annotated[
        bool, typer.Option("--print", "-p", help="Run non-interactively.")
    ] = False,
    output_format: Annotated[
        str, typer.Option("--output-format", help="With -p: text, json or stream-json.")
    ] = "text",
    model: Annotated[str | None, typer.Option("--model", "-m", help='"provider:model"')] = None,
    permission_mode: Annotated[
        str | None, typer.Option("--permission-mode", help=f"One of: {', '.join(MODES)}.")
    ] = None,
    allowed_tools: Annotated[
        list[str] | None, typer.Option("--allowedTools", "--allowed-tools", help="Allow rules.")
    ] = None,
    disallowed_tools: Annotated[
        list[str] | None,
        typer.Option("--disallowedTools", "--disallowed-tools", help="Deny rules."),
    ] = None,
    max_turns: Annotated[int | None, typer.Option("--max-turns", help="Max model calls.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="With -p: show tool calls.")] = False,
    continue_session: Annotated[
        bool, typer.Option("--continue", "-c", help="Continue the latest conversation here.")
    ] = False,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Continue a saved conversation (id or prefix; see `cmcoder sessions`).",
        ),
    ] = None,
    protocol: Annotated[
        str | None, typer.Option("--protocol", help="stdio: run as a long-lived agent for an IDE.")
    ] = None,
    client: Annotated[
        str | None,
        typer.Option("--client", help=f"With --protocol: the IDE ({', '.join(CLIENTS)})."),
    ] = None,
    trust_project: Annotated[
        bool,
        typer.Option("--trust-project", help="Use this project's .cmcoder settings for this run."),
    ] = False,
    mcp_config: Annotated[
        list[Path] | None,
        typer.Option("--mcp-config", help="MCP servers for this run, in .mcp.json's format."),
    ] = None,
    version: Annotated[bool, typer.Option("--version", help="Print the version.")] = False,
) -> None:
    if version:
        print(VERSION_TEXT)
        raise typer.Exit()
    # Imported here, not at the top: LangChain + LangGraph take about a second
    # to import, and `--version` / `--help` shouldn't pay for it.
    from ..agents.main import AgentOptions

    if permission_mode and permission_mode not in MODES:
        err.print(f"[red]error:[/red] --permission-mode must be one of {', '.join(MODES)}")
        raise typer.Exit(2)
    if protocol not in (None, "stdio"):
        err.print("[red]error:[/red] --protocol must be stdio")
        raise typer.Exit(2)
    if client is not None and (not protocol or client not in CLIENTS):
        err.print(
            f"[red]error:[/red] --client goes with --protocol stdio and is one of {', '.join(CLIENTS)}"
        )
        raise typer.Exit(2)
    if protocol and (print_mode or prompt):
        err.print("[red]error:[/red] --protocol takes its prompts on stdin, not -p or arguments")
        raise typer.Exit(2)
    if output_format not in ("text", "json", "stream-json"):
        err.print("[red]error:[/red] --output-format must be text, json or stream-json")
        raise typer.Exit(2)
    settings = _load(trust_project)
    for path in mcp_config or []:
        try:
            add_mcp_config(settings, path)
        except SettingsError as e:
            err.print(f"[red]error:[/red] {e}")
            raise typer.Exit(2) from e
    os.environ.update(settings.env)
    opts = AgentOptions(
        cwd=Path.cwd(),
        model=model,
        permission_mode=permission_mode,
        allowed_tools=_split(allowed_tools),
        disallowed_tools=_split(disallowed_tools),
        max_turns=max_turns,
    )
    try:
        if protocol:
            from .stdio import run_stdio

            code = asyncio.run(
                run_stdio(settings, opts, continue_session=continue_session, resume=resume)
            )
        elif print_mode:
            from .headless import run_headless

            text = prompt if prompt is not None else sys.stdin.read()
            code = asyncio.run(
                run_headless(
                    settings,
                    opts,
                    text,
                    output_format,  # type: ignore[arg-type]
                    verbose,
                    continue_session=continue_session,
                    resume=resume,
                )
            )
        else:
            from .repl import run_repl

            code = asyncio.run(
                run_repl(settings, opts, prompt, continue_session=continue_session, resume=resume)
            )
    except SettingsError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2) from e
    raise typer.Exit(code)


@sub_app.command(help="Check the gateway, TLS, API key and model.")
def doctor(
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    no_probe: Annotated[bool, typer.Option("--no-probe", help="Skip model calls.")] = False,
) -> None:
    from .doctor import run_doctor

    raise typer.Exit(asyncio.run(run_doctor(_load(), model, probe=not no_probe)))


@sub_app.command(
    "protocol-schema",
    help="Print the Agent Protocol JSON Schema: agent events and client messages.",
)
def protocol_schema(
    typescript: Annotated[
        bool, typer.Option("--typescript", help="Print TypeScript types.")
    ] = False,
) -> None:
    import json

    from ..protocol.events import protocol_json_schema
    from ..protocol.messages import messages_json_schema
    from ..protocol.typescript import generate_typescript

    if typescript:
        sys.stdout.write(generate_typescript())
        return
    schema = {"events": protocol_json_schema(), "messages": messages_json_schema()}
    print(json.dumps(schema, indent=2))


@sub_app.command(help="List this project's saved conversations.")
def sessions() -> None:
    import time

    from ..config.settings import find_project_root
    from ..runtime.commands import session_lines
    from ..runtime.store import SessionStore

    async def show() -> None:
        async with SessionStore.open(find_project_root(Path.cwd())) as store:
            for line in session_lines(await store.list(), time.time()):
                print(line)

    asyncio.run(show())


@sub_app.command(help="First-time setup: the gateway, the API key and the models.")
def setup(
    gateway: Annotated[
        str | None, typer.Option("--gateway", help="e.g. https://ai.corp/v1")
    ] = None,
    kind: Annotated[str | None, typer.Option("--type", help="openai or openwebui.")] = None,
    name: Annotated[str, typer.Option("--provider", help="A name for the gateway.")] = "corp",
    model: Annotated[str | None, typer.Option("--model", help="The main model.")] = None,
    small: Annotated[str | None, typer.Option("--small-model", help="A small, fast model.")] = None,
    vision: Annotated[
        str | None, typer.Option("--vision-model", help="A model that reads images.")
    ] = None,
    login: Annotated[bool, typer.Option("--login/--no-login", help="Ask for the API key.")] = True,
) -> None:
    from .setup_cmd import setup as run_setup

    run_setup(gateway, kind, name, model, small, vision, login)


@sub_app.command(help="Store the gateway API key in the OS keychain.")
def login(provider: Annotated[str | None, typer.Option("--provider")] = None) -> None:
    from ..providers.auth import store_api_key

    settings = _load()
    name = provider or (settings.default_provider() if settings.providers else "default")
    key = getpass.getpass(f"API key for {name}: ").strip()
    if not key:
        raise typer.Exit(1)
    where = store_api_key(name, key)
    print(f"Saved in {where}.")


@sub_app.command(help="Delete the stored API key.")
def logout(provider: Annotated[str | None, typer.Option("--provider")] = None) -> None:
    from ..providers.auth import delete_api_key

    settings = _load()
    delete_api_key(provider or settings.default_provider())
    print("Deleted.")


def run() -> None:
    use_utf8_stdio()
    # No telemetry by default (requirement): LangSmith tracing only when the
    # user explicitly turns it on, never because a stray env var is set.
    if os.environ.get("CMCODER_ENABLE_LANGSMITH") != "1":
        for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
            os.environ.pop(var, None)
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        sub_app()
    else:
        main_app()


if __name__ == "__main__":
    run()
