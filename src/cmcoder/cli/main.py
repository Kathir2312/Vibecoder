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
from ..config.settings import Settings, SettingsError, load_settings
from ..core.permissions import MODES

main_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
sub_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, no_args_is_help=True)
SUBCOMMANDS = {"doctor", "login", "logout"}
err = Console(stderr=True)


def _load() -> Settings:
    try:
        return load_settings(Path.cwd())
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
    version: Annotated[bool, typer.Option("--version", help="Print the version.")] = False,
) -> None:
    from ..agents.main import AgentOptions

    if version:
        print(VERSION_TEXT)
        raise typer.Exit()
    if permission_mode and permission_mode not in MODES:
        err.print(f"[red]error:[/red] --permission-mode must be one of {', '.join(MODES)}")
        raise typer.Exit(2)
    if output_format not in ("text", "json", "stream-json"):
        err.print("[red]error:[/red] --output-format must be text, json or stream-json")
        raise typer.Exit(2)
    settings = _load()
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
        if print_mode:
            from .headless import run_headless

            text = prompt if prompt is not None else sys.stdin.read()
            code = asyncio.run(run_headless(settings, opts, text, output_format, verbose))  # type: ignore[arg-type]
        else:
            from .repl import run_repl

            code = asyncio.run(run_repl(settings, opts, prompt))
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
