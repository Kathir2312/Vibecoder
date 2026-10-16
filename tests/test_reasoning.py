"""Splitting Qwen3 <think> reasoning from the visible answer."""

from __future__ import annotations

from cmcoder.models.reasoning import ThinkSplitter, split_think


def test_split_think_tags() -> None:
    assert split_think("<think>plan</think>Answer") == ("Answer", "plan")


def test_split_think_only_closing_tag() -> None:
    # Qwen3 Thinking-2507 templates: the reply starts inside the thought.
    assert split_think("plan</think>\nAnswer") == ("Answer", "plan")


def test_split_think_without_tags() -> None:
    assert split_think("Just an answer") == ("Just an answer", "")


def test_streaming_splitter_handles_tags_cut_across_chunks() -> None:
    s = ThinkSplitter()
    out = []
    for chunk in ["<th", "ink>pl", "an</th", "ink>Ans", "wer"]:
        out += s.feed(chunk)
    out += s.flush()
    text = "".join(t for k, t in out if k == "text")
    reasoning = "".join(t for k, t in out if k == "reasoning")
    assert (text, reasoning) == ("Answer", "plan")
