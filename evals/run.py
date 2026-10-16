"""Eval runner: runs cmcoder on small task repos and checks the result.

  # Against your real gateway (uses your settings / CMCODER_* env):
  uv run python evals/run.py
  uv run python evals/run.py --model corp:qwen3-7b --task fix-off-by-one

  # Harness-only check with scripted replies (CI; no model needed):
  uv run python evals/run.py --mock

Each task folder holds:
  task.json         {"prompt": ..., "check": "<shell command>", "permission_mode": ...,
                     "max_turns": ..., "phase": N (optional: needs a later phase of the plan)}
  repo/             the starting files (copied to a fresh git repo per run)
  mock_script.json  scripted model replies used with --mock

The check runs with bash (Git Bash on Windows) in the task's working copy and
passes on exit code 0. $PYTHON is the current Python interpreter, and
CMCODER_EVAL_OUTPUT points at a file holding the agent's final answer.

Scored per run: pass/fail, model calls (turns), tokens, time, the tools used,
and how often the model tried to do file work through Bash instead of the
file tools (see cmcoder/core/steer.py).

Tasks marked with a later "phase" (subagents, MCP, the critic, code search)
are skipped until that phase is built; `--all` runs them anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cmcoder.compat import SHELL_HELP, find_shell, to_shell_path
from cmcoder.core.steer import file_work_redirect

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
FILE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")
# The plan phase this engine has reached; tasks needing a later phase are skipped.
BUILT_PHASE = 1


def load_tasks(names: list[str] | None = None, include_later: bool = False) -> list[Path]:
    tasks = sorted(p for p in TASKS.iterdir() if (p / "task.json").exists())
    if names:
        return [t for t in tasks if t.name in names]
    if include_later:
        return tasks
    return [
        t
        for t in tasks
        if json.loads((t / "task.json").read_text(encoding="utf-8")).get("phase", 0) <= BUILT_PHASE
    ]


def _git_init(work: Path) -> None:
    for cmd in (
        ["git", "init", "-q"],
        # Keep task files byte for byte, whatever the user's core.autocrlf is.
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=eval",
            "-c",
            "user.email=eval@example.invalid",
            "commit",
            "-qm",
            "start",
        ],
    ):
        subprocess.run(cmd, cwd=work, check=True, capture_output=True)


def run_task(
    task_dir: Path, run_dir: Path, *, mock: bool, model: str | None, timeout: float, shell: str
) -> dict[str, Any]:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    work = run_dir / task_dir.name
    shutil.copytree(task_dir / "repo", work)
    _git_init(work)

    env = dict(os.environ)
    env["PYTHON"] = to_shell_path(sys.executable)
    server = None
    if mock:
        from cmcoder.testing.mock_server import MockServer, MockState

        script = json.loads((task_dir / "mock_script.json").read_text(encoding="utf-8"))
        server = MockServer(MockState(script, api_key="sk-eval"))
        server.__enter__()
        env.update(
            {
                "CMCODER_BASE_URL": server.base_url,
                "CMCODER_API_KEY": "sk-eval",
                "CMCODER_MODEL": "qwen3-27b",
                "CMCODER_CONFIG_DIR": str(run_dir / ".config"),
            }
        )
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env.pop(var, None)

    cmd = [
        sys.executable,
        "-m",
        "cmcoder",
        "-p",
        spec["prompt"],
        "--output-format",
        "stream-json",
        "--permission-mode",
        spec.get("permission_mode", "bypassPermissions"),
        "--max-turns",
        str(spec.get("max_turns", 30)),
    ]
    if model:
        cmd += ["--model", model]
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr, timed_out = "timed out", True
    finally:
        if server is not None:
            server.__exit__(None, None, None)
    duration = time.monotonic() - started

    result: dict[str, Any] = {}
    tools: dict[str, int] = {}
    bash_file_work = 0
    for line in stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "result":
            result = data
        elif data.get("type") == "tool_use":
            name = str(data.get("name"))
            tools[name] = tools.get(name, 0) + 1
            command = (data.get("input") or {}).get("command")
            if name == "Bash" and isinstance(command, str) and file_work_redirect(command):
                bash_file_work += 1
    output_file = work / ".cmcoder_eval_output.txt"
    output_file.write_text(str(result.get("result", "")), encoding="utf-8")
    (work / ".cmcoder_eval_stderr.txt").write_text(stderr, encoding="utf-8")
    check = subprocess.run(
        [shell, "-c", spec["check"]],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**env, "CMCODER_EVAL_OUTPUT": to_shell_path(output_file)},
    )
    usage = result.get("usage") or {}
    return {
        "task": task_dir.name,
        "passed": check.returncode == 0 and not timed_out,
        "agent_status": "timeout" if timed_out else result.get("subtype", "no result"),
        "turns": result.get("num_turns"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "seconds": round(duration, 1),
        "tools": tools,
        "file_tool_calls": sum(tools.get(t, 0) for t in FILE_TOOLS),
        "bash_file_work": bash_file_work,
        "check_output": (check.stdout + check.stderr)[-500:]
        + (stderr[-500:] if not result else ""),
        "workdir": str(work),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--mock", action="store_true", help="use scripted replies instead of a real model"
    )
    ap.add_argument("--model", help="model to evaluate (default: settings)")
    ap.add_argument("--task", action="append", help="run only these tasks")
    ap.add_argument("--all", action="store_true", help="also run tasks for later phases")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per task")
    args = ap.parse_args()

    shell = find_shell()
    if shell is None:
        print(SHELL_HELP, file=sys.stderr)
        return 2
    run_dir = ROOT / ".runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    results = []
    for task in load_tasks(args.task, args.all):
        r = run_task(
            task, run_dir, mock=args.mock, model=args.model, timeout=args.timeout, shell=shell
        )
        results.append(r)
        used = " ".join(f"{k}:{v}" for k, v in sorted(r["tools"].items()))
        print(
            f"{'PASS' if r['passed'] else 'FAIL'}  {r['task']:<24} {r['agent_status']!s:<10} "
            f"turns={r['turns']} tokens={r['prompt_tokens']}/{r['completion_tokens']} "
            f"{r['seconds']}s tools=[{used}] bash-file-work={r['bash_file_work']}",
            flush=True,
        )
        if not r["passed"]:
            print(f"      check: {str(r['check_output']).strip()[-300:]}")
            print(f"      workdir: {r['workdir']}")
    (run_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r["passed"])
    file_tools = sum(r["file_tool_calls"] for r in results)
    via_bash = sum(r["bash_file_work"] for r in results)
    print(f"\n{passed}/{len(results)} passed · results in {run_dir / 'results.json'}")
    print(
        f"Tool choice: {file_tools} file-tool calls, {via_bash} attempts at file work through Bash "
        f"({100 * file_tools // max(1, file_tools + via_bash)}% done with the file tools)"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
