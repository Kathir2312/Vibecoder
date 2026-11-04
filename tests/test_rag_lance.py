"""The LanceDB store of the code index: the operations the index needs, on a real
LanceDB in a temporary folder, with vectors made up by a fake embedding model."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cmcoder.rag.lance import LanceVectorStore, in_list, prefix_filter, quote
from cmcoder.rag.stores import StoreError


class FakeEmbedding:
    """Unit vectors from the words of a text: texts sharing words are close."""

    def __init__(self, dim: int = 32) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim
                out[row, slot] += 1.0
            norm = np.linalg.norm(out[row])
            out[row] /= norm or 1.0
        return out


def meta(path: str, start: int = 1, symbol: str = "") -> dict[str, Any]:
    return {"path": path, "start": start, "end": start + 5, "language": "python", "symbol": symbol}


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = LanceVectorStore(tmp_path / "lancedb", FakeEmbedding())  # type: ignore[arg-type]
    yield s
    await s.aclose()


async def fill(store: LanceVectorStore) -> None:
    await store.aadd_texts(
        ["refresh token session", "parse invoice total", "send monthly email"],
        [meta("auth.py", 1), meta("billing.py", 1), meta("mail/send.py", 1)],
    )


def test_sql_literals_and_filters() -> None:
    assert quote("it's") == "'it''s'"
    assert in_list("path", ["a", "b"]) == ["path IN ('a', 'b')"]
    assert len(in_list("id", [str(i) for i in range(450)])) == 3
    assert prefix_filter(None) is None and prefix_filter("/") is None
    assert prefix_filter("src/") == "(path = 'src' OR starts_with(path, 'src/'))"


async def test_add_search_count_clear(store: LanceVectorStore) -> None:
    assert await store.acount() == 0
    assert await store.asimilarity_search_by_vector_with_score(np.ones(32, np.float32), 3) == []
    await fill(store)
    assert await store.acount() == 3
    hits = await store.asimilarity_search_with_score("refresh the token", k=2)
    assert hits[0][0].metadata["path"] == "auth.py" and hits[0][0].page_content.startswith(
        "refresh"
    )
    assert 0 < hits[0][1] <= 1.0001 and hits[0][1] >= hits[1][1]
    assert set(hits[0][0].metadata) == {"path", "start", "end", "language", "symbol"}
    await store.aclear()
    assert await store.acount() == 0
    await fill(store)  # usable again after a clear
    assert await store.acount() == 3


async def test_same_id_replaces_the_piece(store: LanceVectorStore) -> None:
    await store.aadd_texts(["old text about tokens"], [meta("a.py")], ids=["a.py:1"])
    await store.aadd_texts(["new text about invoices"], [meta("a.py")], ids=["a.py:1"])
    assert await store.acount() == 1
    (doc, _), *_ = await store.asimilarity_search_with_score("invoices", k=1)
    assert doc.page_content == "new text about invoices"


async def test_delete_by_path_and_by_id_with_awkward_names(store: LanceVectorStore) -> None:
    names = ["it's.py", "100%_done.py", 'quo"te.py', "plain.py"]
    await store.aadd_texts([f"text {n}" for n in names], [meta(n) for n in names])
    assert await store.acount() == 4
    await store.adelete_paths(["it's.py", "100%_done.py", 'quo"te.py'])
    assert await store.acount() == 1
    await store.adelete(["plain.py:1"])
    assert await store.acount() == 0
    await store.adelete_paths([])  # nothing to do
    await store.adelete_paths(["never-indexed.py"])


async def test_path_prefix_means_a_file_or_a_folder_not_a_pattern(store: LanceVectorStore) -> None:
    paths = ["src/a.py", "src_extra/b.py", "src/deep/c.py", "srcX.py", "100%/d.py"]
    await store.aadd_texts([f"code {p}" for p in paths], [meta(p) for p in paths])
    vec = (await store.embedding.embed(["code"]))[0]  # type: ignore[attr-defined]

    async def found(prefix: str | None) -> set[str]:
        hits = await store.asimilarity_search_by_vector_with_score(vec, 10, prefix)
        return {d.metadata["path"] for d, _ in hits}

    assert await found("src") == {"src/a.py", "src/deep/c.py"}
    assert await found("src/") == {"src/a.py", "src/deep/c.py"}
    assert await found("src/a.py") == {"src/a.py"}
    assert await found("100%") == {"100%/d.py"}
    assert await found("s%") == set()
    assert len(await found(None)) == 5


async def test_the_index_survives_reopening_and_is_private(tmp_path: Path) -> None:
    folder = tmp_path / "idx" / "lancedb"
    first = LanceVectorStore(folder, FakeEmbedding())  # type: ignore[arg-type]
    await fill(first)
    await first.aclose()
    assert folder.stat().st_mode & 0o077 == 0
    again = LanceVectorStore(folder, FakeEmbedding())  # type: ignore[arg-type]
    try:
        assert await again.acount() == 3
        (doc, _), *_ = await again.asimilarity_search_with_score("monthly email", k=1)
        assert doc.metadata["path"] == "mail/send.py"
    finally:
        await again.aclose()


async def test_another_embedding_size_is_not_mixed_in(tmp_path: Path) -> None:
    folder = tmp_path / "lancedb"
    small = LanceVectorStore(folder, FakeEmbedding(16))  # type: ignore[arg-type]
    await fill(small)
    await small.aclose()
    big = LanceVectorStore(folder, FakeEmbedding(48))  # type: ignore[arg-type]
    try:
        # Searching with another model's vectors finds nothing instead of failing...
        assert await big.asimilarity_search_by_vector_with_score(np.ones(48, np.float32), 3) == []
        # ...and adding to it says what to do.
        with pytest.raises(StoreError, match="/index rebuild"):
            await big.aadd_texts(["x"], [meta("x.py")])
    finally:
        await big.aclose()


OFFLINE_SCRIPT = """
import asyncio, sys
import numpy as np
from pathlib import Path
from cmcoder.rag.lance import LanceVectorStore

class Fake:
    async def embed(self, texts):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, len(t) % 8] = 1.0
        return out

async def main():
    store = LanceVectorStore(Path(sys.argv[1]) / "lancedb", Fake())
    await store.aadd_texts(["alpha", "beta beta"], [{"path": "a.py", "start": 1}, {"path": "b.py", "start": 1}])
    assert await store.acount() == 2
    assert await store.asimilarity_search_with_score("alpha", k=1)
    await store.adelete_paths(["a.py"])
    await store.aclear()
    await store.aclose()
    print("offline ok")

asyncio.run(main())
"""


@pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux network namespace")
def test_it_works_with_no_network_at_all(tmp_path: Path) -> None:
    """LanceDB's engine is native code, so Python-level socket patches would prove
    nothing. Run it in a process whose network namespace has no network, not even a
    route out: every operation still works, so none of them needs the network."""
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("cannot create a network namespace here")
    done = subprocess.run(
        ["unshare", "-rn", sys.executable, "-I", "-c", OFFLINE_SCRIPT, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert "offline ok" in done.stdout


async def test_a_store_that_cannot_open_says_so(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    with pytest.raises(OSError):
        LanceVectorStore(blocker / "lancedb", FakeEmbedding())  # type: ignore[arg-type]


def test_a_build_without_lancedb_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cmcoder.config.settings import Settings
    from cmcoder.rag import lance
    from cmcoder.rag.index import open_index

    monkeypatch.setattr(lance, "lancedb_available", lambda: False)
    settings = Settings.model_validate(
        {
            "providers": {"corp": {"baseUrl": "http://127.0.0.1:1/v1"}},
            "model": "corp:qwen3-27b",
            "rag": {"embeddingModel": "corp:embed", "store": {"type": "lancedb"}},
        }
    )
    with pytest.raises(StoreError, match="doesn't include LanceDB"):
        open_index(settings, tmp_path)
