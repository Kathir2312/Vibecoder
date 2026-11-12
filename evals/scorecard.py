"""A scorecard for an eval run: the few numbers that say how well a model does cmcoder's tasks, and what
changed since an earlier run.

`evals/run.py` writes one next to its results (`scorecard.json` for machines, `scorecard.md` to read) and
can compare it with an earlier card (`--baseline`) and fail when something that used to pass now fails
(`--fail-on-regression`). Run on your own gateway with your own model, it is how a change to cmcoder (or
to the model behind it) is measured instead of felt: pass rate, model calls and tokens per run.

  uv run python evals/scorecard.py evals/.runs/<run>/results.json --model qwen3-27b
  uv run python evals/scorecard.py results.json --baseline old/scorecard.json

Plain Python: nothing in LangChain or LangGraph computes statistics over a list of eval results.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

Result = dict[str, Any]
Card = dict[str, Any]


def _numbers(results: list[Result], key: str) -> list[float]:
    return [float(r[key]) for r in results if isinstance(r.get(key), int | float)]


def _stat(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "max": 0.0, "total": 0.0}
    return {
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "max": round(max(values), 2),
        "total": round(sum(values), 2),
    }


def build(results: list[Result], *, model: str, via: str = "cli", mock: bool = False) -> Card:
    """The card of a run: one `results.json` entry per task run (per way of running it)."""
    passed = [r for r in results if r.get("passed")]
    tokens = [
        float((r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)) for r in results
    ]
    file_calls = sum(int(r.get("file_tool_calls") or 0) for r in results)
    bash_calls = sum(int(r.get("bash_file_work") or 0) for r in results)
    return {
        "model": model,
        "via": via,
        "mock": mock,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runs": len(results),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(results), 4) if results else 0.0,
        "model_calls": _stat(_numbers(results, "turns")),
        "tokens": _stat(tokens),
        "seconds": _stat(_numbers(results, "seconds")),
        "file_tool_share": round(file_calls / (file_calls + bash_calls), 4)
        if file_calls + bash_calls
        else 1.0,
        "per_task": [
            {
                "task": r["task"],
                "via": r.get("via", via),
                "passed": bool(r.get("passed")),
                "status": r.get("agent_status"),
                "model_calls": r.get("turns"),
                "tokens": (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0),
                "seconds": r.get("seconds"),
            }
            for r in results
        ],
    }


def _key(entry: dict[str, Any]) -> tuple[str, str]:
    return (entry["task"], entry.get("via", "cli"))


def compare(card: Card, baseline: Card) -> dict[str, Any]:
    """What changed since `baseline`: the headline numbers, and the tasks that flipped."""
    before = {_key(t): t for t in baseline["per_task"]}
    now = {_key(t): t for t in card["per_task"]}
    return {
        "pass_rate": round(card["pass_rate"] - baseline["pass_rate"], 4),
        "model_calls": round(card["model_calls"]["mean"] - baseline["model_calls"]["mean"], 2),
        "tokens": round(card["tokens"]["mean"] - baseline["tokens"]["mean"], 1),
        "seconds": round(card["seconds"]["mean"] - baseline["seconds"]["mean"], 1),
        "regressed": sorted(
            f"{k[0]} [{k[1]}]"
            for k, t in now.items()
            if k in before and before[k]["passed"] and not t["passed"]
        ),
        "fixed": sorted(
            f"{k[0]} [{k[1]}]"
            for k, t in now.items()
            if k in before and not before[k]["passed"] and t["passed"]
        ),
        "new": sorted(f"{k[0]} [{k[1]}]" for k in now if k not in before),
        "gone": sorted(f"{k[0]} [{k[1]}]" for k in before if k not in now),
    }


def regressions(card: Card, baseline: Card) -> list[str]:
    """Why a run is worse than its baseline, one line each (empty: it is not worse)."""
    diff = compare(card, baseline)
    out = [f"passed before, fails now: {name}" for name in diff["regressed"]]
    if diff["pass_rate"] < 0 and not out:
        out.append(f"the pass rate fell by {abs(diff['pass_rate']) * 100:.1f} points")
    return out


def _signed(value: float, unit: str, digits: int = 1) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else "±"
    return f"{sign}{abs(value):,.{digits}f}{unit}"


def markdown(card: Card, baseline: Card | None = None) -> str:
    """The card for people: a headline, a table per task, and the change since the baseline."""
    calls, tokens, seconds = card["model_calls"], card["tokens"], card["seconds"]
    runs = card["runs"] or 1
    title = f"{card['model']} ({card['via']}{', scripted replies' if card['mock'] else ''})"
    lines = [
        f"# Eval scorecard: {title}",
        "",
        f"**{card['passed']} of {card['runs']} runs passed ({card['pass_rate'] * 100:.1f}%)** · "
        f"{calls['mean']:.1f} model calls per run (median {calls['median']:.0f}, most {calls['max']:.0f}) · "
        f"{tokens['total'] / runs:,.0f} tokens per run · {seconds['mean']:.1f} s per run · "
        f"{card['file_tool_share'] * 100:.0f}% of file work done with the file tools",
        "",
        "| task | result | model calls | tokens | seconds |",
        "|---|---|---:|---:|---:|",
    ]
    for t in card["per_task"]:
        name = t["task"] + (f" [{t['via']}]" if card["via"] == "both" else "")
        result = "pass" if t["passed"] else f"**FAIL** ({t['status']})"
        lines.append(
            f"| {name} | {result} | {t['model_calls'] if t['model_calls'] is not None else '-'} | "
            f"{t['tokens']:,} | {t['seconds'] if t['seconds'] is not None else '-'} |"
        )
    if baseline is not None:
        diff = compare(card, baseline)
        lines += [
            "",
            f"## Since the baseline ({baseline['model']}, {baseline.get('created', 'earlier')})",
            "",
            f"Pass rate {_signed(diff['pass_rate'] * 100, ' points')} · "
            f"model calls {_signed(diff['model_calls'], ' per run')} · "
            f"tokens {_signed(diff['tokens'], ' per run', 0)} · "
            f"time {_signed(diff['seconds'], ' s per run')}",
        ]
        for label, key in (
            ("Passed before, fail now", "regressed"),
            ("Failed before, pass now", "fixed"),
            ("New tasks", "new"),
            ("Tasks that were dropped", "gone"),
        ):
            if diff[key]:
                lines.append(f"- {label}: {', '.join(diff[key])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("results", type=Path, help="a run's results.json")
    ap.add_argument(
        "--model", default="(settings default)", help="what to call the model on the card"
    )
    ap.add_argument("--via", default="cli")
    ap.add_argument("--baseline", type=Path, help="an earlier scorecard.json to compare with")
    ap.add_argument("--fail-on-regression", action="store_true")
    args = ap.parse_args(argv)
    card = build(
        json.loads(args.results.read_text(encoding="utf-8")), model=args.model, via=args.via
    )
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    sys.stdout.write(markdown(card, baseline))
    if baseline is not None and args.fail_on_regression and (why := regressions(card, baseline)):
        sys.stderr.write("\n".join(why) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
