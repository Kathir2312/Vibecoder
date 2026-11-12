"""Eval runner: runs cmcoder on small task repos and checks the result.

  # Against your real gateway (uses your settings / CMCODER_* env):
  uv run python evals/run.py
  uv run python evals/run.py --model corp:qwen3-7b --task fix-off-by-one

  # Harness-only check with scripted replies (CI; no model needed):
  uv run python evals/run.py --mock

  # Every run writes scorecard.json and scorecard.md next to results.json (evals/scorecard.py):
  # pass rate, model calls and tokens per run. Compare with an earlier run, and fail if something
  # that passed before fails now:
  uv run python evals/run.py --label qwen3-27b --baseline evals/.runs/<earlier>/scorecard.json --fail-on-regression

  # Through the IDE protocol (cmcoder --protocol stdio), or both ways,
  # checking that each task gives the same result and tool calls:
  uv run python evals/run.py --mock --via both

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
import threading
import time
from pathlib import Path
from typing import Any

import scorecard

from cmcoder.compat import SHELL_HELP, find_shell, to_shell_path
from cmcoder.core.steer import file_work_redirect

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
FILE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")
# The plan phase this engine has reached; tasks needing a later phase are skipped.
BUILT_PHASE = 5


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


def run_cli(
    cmd: list[str], prompt: str, cwd: Path, env: dict[str, str], timeout: float
) -> tuple[str, str, bool]:
    """`cmcoder -p` for one prompt: (stream-json output, stderr, timed out)."""
    try:
        proc = subprocess.run(
            [*cmd, "-p", prompt, "--output-format", "stream-json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return stdout, "timed out", True
    return proc.stdout, proc.stderr, False


def run_stdio(
    cmd: list[str], prompt: str, cwd: Path, env: dict[str, str], timeout: float
) -> tuple[str, str, bool]:
    """Drive `cmcoder --protocol stdio` like the IDE plugins do, for one prompt.
    Permission requests are denied, as `-p` does. Returns (the events as
    stream-json lines, stderr, timed out)."""
    proc = subprocess.Popen(  # an argument list, no shell
        [*cmd, "--protocol", "stdio"],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stderr: list[str] = []
    reader = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
    reader.start()
    timer = threading.Timer(timeout, proc.kill)
    timer.start()

    def send(message: dict[str, object]) -> None:
        proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    lines: list[str] = []
    try:
        send({"type": "user_message", "text": prompt})
        for line in proc.stdout:
            lines.append(line)
            event = json.loads(line)
            if event.get("type") == "permission_request":
                send(
                    {
                        "type": "permission_response",
                        "request_id": event["request_id"],
                        "allow": False,
                    }
                )
            elif event.get("type") == "result":
                break
        send({"type": "shutdown"})
        proc.stdin.close()
        proc.wait(timeout=30)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        proc.kill()
    finally:
        timed_out = not timer.is_alive() and proc.returncode not in (0, None)
        timer.cancel()
        reader.join(timeout=5)
    return "".join(lines), "".join(stderr), timed_out


def run_task(
    task_dir: Path,
    run_dir: Path,
    *,
    mock: bool,
    model: str | None,
    timeout: float,
    shell: str,
    via: str = "cli",
    rag: str = "on",
) -> dict[str, Any]:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    work = run_dir / via / task_dir.name
    shutil.copytree(task_dir / "repo", work)
    _git_init(work)

    env = dict(os.environ)
    env["PYTHON"] = to_shell_path(sys.executable)
    env["CMCODER_EVAL_PYTHON"] = sys.executable  # native path, e.g. for an MCP server command
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
                # The mock's embedding model, for tasks with "index": true.
                "CMCODER_EMBEDDING_MODEL": "text-embedding-3-small",
            }
        )
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env.pop(var, None)

    cmd = [
        sys.executable,
        "-m",
        "cmcoder",
        "--permission-mode",
        spec.get("permission_mode", "bypassPermissions"),
        "--max-turns",
        str(spec.get("max_turns", 30)),
        *[str(a) for a in spec.get("args", [])],  # e.g. --mcp-config
    ]
    if model:
        cmd += ["--model", model]
    indexed = False
    if rag == "off":
        env["CMCODER_RAG"] = "off"
    elif spec.get("index"):
        # Code search: index the working copy first (a failure leaves the
        # task to run without it, as a project without an index would).
        built = subprocess.run(
            [sys.executable, "-m", "cmcoder", "index"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
        indexed = built.returncode == 0
        if not indexed:
            print(f"      (no code index: {(built.stderr or built.stdout).strip()[-200:]})")
    started = time.monotonic()
    run = run_stdio if via == "stdio" else run_cli
    try:
        stdout, stderr, timed_out = run(cmd, spec["prompt"], work, env, timeout)
    finally:
        if indexed:  # its index lives in the config folder: don't leave it behind
            subprocess.run(
                [sys.executable, "-m", "cmcoder", "index", "--clear"],
                cwd=work,
                env=env,
                capture_output=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
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
    ap.add_argument(
        "--via",
        choices=["cli", "stdio", "both"],
        default="cli",
        help="drive cmcoder with -p (cli), through the IDE protocol (stdio), or both and "
        "check they agree",
    )
    ap.add_argument(
        "--rag",
        choices=["on", "off"],
        default="on",
        help="code search for tasks with an index (off: compare without it)",
    )
    ap.add_argument("--label", help="what to call the model on the scorecard (default: --model)")
    ap.add_argument("--baseline", type=Path, help="an earlier run's scorecard.json to compare with")
    ap.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="with --baseline: exit 1 if a task that passed there fails now",
    )
    args = ap.parse_args()

    shell = find_shell()
    if shell is None:
        print(SHELL_HELP, file=sys.stderr)
        return 2
    run_dir = ROOT / ".runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    vias = ["cli", "stdio"] if args.via == "both" else [args.via]
    results = []
    mismatches: list[str] = []
    for task in load_tasks(args.task, args.all):
        by_via: dict[str, dict[str, Any]] = {}
        for via in vias:
            r = run_task(
                task,
                run_dir,
                mock=args.mock,
                model=args.model,
                timeout=args.timeout,
                shell=shell,
                via=via,
                rag=args.rag,
            )
            r["via"] = via
            by_via[via] = r
            results.append(r)
            used = " ".join(f"{k}:{v}" for k, v in sorted(r["tools"].items()))
            how = f" [{via}]" if len(vias) > 1 else ""
            print(
                f"{'PASS' if r['passed'] else 'FAIL'}  {r['task']:<24}{how} {r['agent_status']!s:<10} "
                f"turns={r['turns']} tokens={r['prompt_tokens']}/{r['completion_tokens']} "
                f"{r['seconds']}s tools=[{used}] bash-file-work={r['bash_file_work']}",
                flush=True,
            )
            if not r["passed"]:
                print(f"      check: {str(r['check_output']).strip()[-300:]}")
                print(f"      workdir: {r['workdir']}")
        if len(by_via) == 2:
            cli, stdio = by_via["cli"], by_via["stdio"]
            keys = ("passed", "agent_status", "turns", "tools")
            if any(cli[k] != stdio[k] for k in keys):
                diff = ", ".join(f"{k}: {cli[k]} vs {stdio[k]}" for k in keys if cli[k] != stdio[k])
                mismatches.append(f"{task.name} ({diff})")
                print(f"      PARITY MISMATCH cli vs stdio: {diff}")
    (run_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    label = args.label or args.model or ("mock" if args.mock else "(settings default)")
    card = scorecard.build(results, model=label, via=args.via, mock=args.mock)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    (run_dir / "scorecard.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    (run_dir / "scorecard.md").write_text(scorecard.markdown(card, baseline), encoding="utf-8")
    worse = scorecard.regressions(card, baseline) if baseline else []
    passed = sum(1 for r in results if r["passed"])
    file_tools = sum(r["file_tool_calls"] for r in results)
    via_bash = sum(r["bash_file_work"] for r in results)
    print(f"\n{passed}/{len(results)} passed · results in {run_dir / 'results.json'}")
    print(
        f"Tool choice: {file_tools} file-tool calls, {via_bash} attempts at file work through Bash "
        f"({100 * file_tools // max(1, file_tools + via_bash)}% done with the file tools)"
    )
    if len(vias) == 2:
        tasks = len(results) // 2
        print(
            f"Parity (-p vs --protocol stdio): {tasks - len(mismatches)}/{tasks} tasks gave the "
            "same result and tool calls"
            + (f"; differ: {'; '.join(mismatches)}" if mismatches else "")
        )
    print(f"Scorecard: {run_dir / 'scorecard.md'}")
    if baseline is not None:
        diff = scorecard.compare(card, baseline)
        print(
            f"Since the baseline: pass rate {diff['pass_rate'] * 100:+.1f} points, "
            f"model calls {diff['model_calls']:+.1f} per run, tokens {diff['tokens']:+,.0f} per run"
        )
        for why in worse:
            print(f"  REGRESSION: {why}")
    if args.fail_on_regression and worse:
        return 1
    return 0 if passed == len(results) and not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
