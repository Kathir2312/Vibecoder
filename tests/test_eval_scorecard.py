"""The eval scorecard (evals/scorecard.py): the numbers of a run, and what changed since an earlier one."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cmcoder.compat import find_shell

sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))
try:
    import scorecard  # evals/scorecard.py
finally:
    sys.path.pop(0)


def result(
    task: str,
    passed: bool,
    turns: int | None = 4,
    prompt: int | None = 1000,
    completion: int | None = 200,
    seconds: float = 10.0,
    **more: Any,
) -> dict[str, Any]:
    return {
        "task": task, "passed": passed, "agent_status": "success" if passed else "error", "turns": turns,
        "prompt_tokens": prompt, "completion_tokens": completion, "seconds": seconds,
        "tools": {}, "file_tool_calls": 3, "bash_file_work": 0, **more,
    }  # fmt: skip


RUN = [
    result("a", True, 2),
    result("b", True, 6, seconds=20.0),
    result("c", False, None, None, None, 30.0),
]


def test_the_card_holds_the_numbers_of_the_run() -> None:
    card = scorecard.build(RUN, model="qwen3-27b", via="cli")
    assert (card["runs"], card["passed"], card["pass_rate"]) == (3, 2, 0.6667)
    assert card["model_calls"] == {"mean": 4.0, "median": 4.0, "max": 6.0, "total": 8.0}
    assert card["tokens"]["total"] == 2400.0  # a run with no numbers counts as none, not as a gap
    assert card["seconds"]["mean"] == 20.0
    assert [t["task"] for t in card["per_task"]] == ["a", "b", "c"]
    assert card["per_task"][2] == {
        "task": "c",
        "via": "cli",
        "passed": False,
        "status": "error",
        "model_calls": None,
        "tokens": 0,
        "seconds": 30.0,
    }
    assert card["file_tool_share"] == 1.0


def test_file_work_through_bash_lowers_the_share_of_file_tools() -> None:
    card = scorecard.build([result("a", True, file_tool_calls=3, bash_file_work=1)], model="m")
    assert card["file_tool_share"] == 0.75


def test_an_empty_run_is_a_card_too() -> None:
    card = scorecard.build([], model="m")
    assert (card["runs"], card["pass_rate"], card["model_calls"]["mean"]) == (0, 0.0, 0.0)
    assert "0 of 0 runs passed" in scorecard.markdown(card)


def test_markdown_says_the_result_in_a_line_and_lists_every_task() -> None:
    text = scorecard.markdown(scorecard.build(RUN, model="qwen3-27b"))
    assert text.startswith("# Eval scorecard: qwen3-27b (cli)")
    assert (
        "**2 of 3 runs passed (66.7%)**" in text
        and "4.0 model calls per run" in text
        and "800 tokens per run" in text
    )
    assert "| a | pass | 2 | 1,200 | 10.0 |" in text
    assert "| c | **FAIL** (error) | - | 0 | 30.0 |" in text
    assert "Since the baseline" not in text
    assert "scripted replies" in scorecard.markdown(scorecard.build(RUN, model="m", mock=True))


def test_what_changed_since_the_baseline() -> None:
    before = scorecard.build(
        [result("a", True, 2), result("b", False), result("gone", True)], model="old"
    )
    now = scorecard.build(
        [result("a", False, 5), result("b", True, 3, 800, 100), result("fresh", True)], model="new"
    )
    diff = scorecard.compare(now, before)
    assert diff["regressed"] == ["a [cli]"] and diff["fixed"] == ["b [cli]"]
    assert diff["new"] == ["fresh [cli]"] and diff["gone"] == ["gone [cli]"]
    assert diff["pass_rate"] == 0.0 and diff["model_calls"] == pytest.approx(
        0.67, abs=0.01
    )  # (5+3+4)/3 - (2+4+4)/3
    text = scorecard.markdown(now, before)
    assert "Passed before, fail now: a [cli]" in text and "Failed before, pass now: b [cli]" in text
    assert "New tasks: fresh [cli]" in text and "Tasks that were dropped: gone [cli]" in text


def test_a_regression_is_a_task_that_passed_before_and_fails_now() -> None:
    good = scorecard.build([result("a", True), result("b", True)], model="m")
    worse = scorecard.build([result("a", True), result("b", False)], model="m")
    assert scorecard.regressions(worse, good) == ["passed before, fails now: b [cli]"]
    assert scorecard.regressions(good, worse) == [], "getting better is not a regression"
    assert scorecard.regressions(good, good) == []
    # A different set of tasks that lost ground still counts, even with no name to point at.
    other = scorecard.build([result("x", False)], model="m")
    assert scorecard.regressions(other, good)[0].startswith("the pass rate fell by 100.0 points")


def test_both_ways_of_running_a_task_are_kept_apart() -> None:
    both = [result("a", True, via="cli"), result("a", False, via="stdio")]
    card = scorecard.build(both, model="m", via="both")
    assert [(t["task"], t["via"]) for t in card["per_task"]] == [("a", "cli"), ("a", "stdio")]
    assert "| a [stdio] | **FAIL** (error)" in scorecard.markdown(card)


def test_the_command_line_prints_the_card_and_can_fail_on_a_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.json"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(scorecard.build([result("a", True), result("b", True)], model="old"))
    )
    results.write_text(json.dumps([result("a", True), result("b", False)]))
    assert scorecard.main([str(results), "--model", "new", "--baseline", str(baseline)]) == 0
    assert "# Eval scorecard: new" in capsys.readouterr().out
    assert scorecard.main([str(results), "--baseline", str(baseline), "--fail-on-regression"]) == 1
    assert "passed before, fails now: b [cli]" in capsys.readouterr().err
    results.write_text(json.dumps([result("a", True), result("b", True)]))
    assert scorecard.main([str(results), "--baseline", str(baseline), "--fail-on-regression"]) == 0


def test_every_run_of_the_runner_writes_a_scorecard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if find_shell() is None:
        pytest.skip("no bash")
    sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))
    try:
        import run  # evals/run.py
    finally:
        sys.path.pop(0)
    earlier = tmp_path / "earlier.json"  # a run in which the task failed
    earlier.write_text(
        json.dumps(scorecard.build([result("fix-off-by-one", False)], model="earlier"))
    )
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--mock",
            "--task",
            "fix-off-by-one",
            "--label",
            "scripted",
            "--baseline",
            str(earlier),
            "--fail-on-regression",
        ],
    )
    assert run.main() == 0
    (folder,) = (tmp_path / ".runs").iterdir()
    card = json.loads((folder / "scorecard.json").read_text())
    assert (card["model"], card["runs"], card["passed"], card["mock"]) == ("scripted", 1, 1, True)
    text = (folder / "scorecard.md").read_text()
    assert (
        "# Eval scorecard: scripted (cli, scripted replies)" in text
        and "Failed before, pass now: fix-off-by-one [cli]" in text
    )
    out = capsys.readouterr().out
    assert "Scorecard:" in out and "Since the baseline: pass rate +100.0 points" in out
