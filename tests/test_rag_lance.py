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

from cmcoder.rag.lance import (
    LanceVectorStore,
    in_list,
    prefix_filter,
    quote,
    rrf,
    search_text,
    split_identifiers,
)
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


# --- phase 3: hybrid search, the nearest-neighbour index, housekeeping -----------------


def test_the_words_inside_camel_case_names() -> None:
    assert split_identifiers("class HTTPServerError(Exception):") == "http server error"
    assert split_identifiers("parseConfigFile(path)") == "parse config file"
    assert split_identifiers("getUserByID and getUserById") == "get user by id"
    assert split_identifiers("snake_case_name MAX_SIZE lower UPPER") == ""
    assert split_identifiers("Config") == ""  # one word: nothing to add
    assert search_text("def f(): pass") == "def f(): pass"
    assert search_text("fooBarBaz()").endswith("\nfoo bar baz")


def test_reciprocal_rank_fusion() -> None:
    assert rrf([["a", "b", "c"], ["c", "b", "d"]]) == ["c", "b", "a", "d"]  # 1/63 + 1/61 beats 2/62
    assert rrf([["a", "b"]]) == ["a", "b"]
    assert rrf([]) == []
    # present in both rankings beats best in only one
    assert rrf([["x", "y"], ["z", "y"]])[0] == "y"


async def hybrid_store(tmp_path: Path, hybrid: bool = True) -> LanceVectorStore:
    store = LanceVectorStore(tmp_path / f"h{hybrid}", FakeEmbedding(), hybrid=hybrid)  # type: ignore[arg-type]
    texts = [
        "class HTTPServerError(Exception): pass",
        "def parse_config(path): return json.load(open(path))",
        "send monthly invoices by email to customers",
        "refresh the expired auth token with the provider",
        "def compute_checksum(data): return zlib.crc32(data)",
    ]
    metas = [meta("srv/errors.py"), meta("cfg/load.py"), meta("mail/send.py"), meta("auth.py")]
    await store.aadd_texts(texts[:4], metas)
    await store.aadd_texts(texts[4:], [meta("util/sum.py")])
    return store


async def test_words_find_what_the_meaning_search_misses(tmp_path: Path) -> None:
    both = await hybrid_store(tmp_path, hybrid=True)
    vector_only = await hybrid_store(tmp_path, hybrid=False)
    try:
        for query, path in [
            ("server error", "srv/errors.py"),  # words inside a camelCase name
            ("parse_config", "cfg/load.py"),  # an exact name
            ("crc32", "util/sum.py"),
        ]:
            top = (await both.asimilarity_search_with_score(query, k=1))[0][0]
            assert top.metadata["path"] == path, query
        miss = (await vector_only.asimilarity_search_with_score("server error", k=1))[0][0]
        assert miss.metadata["path"] != "srv/errors.py"  # the made-up embedding blurs names
    finally:
        await both.aclose()
        await vector_only.aclose()


async def test_scores_stay_cosine_whichever_search_found_the_piece(tmp_path: Path) -> None:
    store = await hybrid_store(tmp_path)
    try:
        embed = FakeEmbedding()
        query = "server error"
        qv = (await embed.embed([query]))[0]
        (doc, score), *_ = await store.asimilarity_search_with_score(query, k=1)
        assert doc.metadata["path"] == "srv/errors.py"  # found by its words
        stored = (await embed.embed([embedding_text_of(doc)]))[0]
        assert score == pytest.approx(float(np.dot(qv, stored)), abs=1e-5)
        assert -1.0001 <= score <= 1.0001
    finally:
        await store.aclose()


def embedding_text_of(doc: Any) -> str:
    from cmcoder.rag.stores import embedding_text

    return embedding_text(doc.page_content, doc.metadata)


async def test_the_path_filter_applies_to_the_words_too(tmp_path: Path) -> None:
    store = await hybrid_store(tmp_path)
    try:
        everywhere = await store.asimilarity_search_with_score("parse_config", k=3)
        assert everywhere[0][0].metadata["path"] == "cfg/load.py"
        narrowed = await store.asimilarity_search_with_score(
            "parse_config", k=3, path_prefix="mail"
        )
        assert {d.metadata["path"] for d, _ in narrowed} == {"mail/send.py"}
    finally:
        await store.aclose()


async def test_pieces_added_later_are_found_by_words_before_housekeeping(tmp_path: Path) -> None:
    store = await hybrid_store(tmp_path)
    try:
        await store.aadd_texts(["def quarantine_tarball(blob): pass"], [meta("new/late.py")])
        top = (await store.asimilarity_search_with_score("quarantine tarball", k=1))[0][0]
        assert top.metadata["path"] == "new/late.py"
    finally:
        await store.aclose()


async def test_a_words_index_that_fails_never_fails_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await hybrid_store(tmp_path)
    try:

        async def broken(table: Any) -> None:
            raise RuntimeError("words index unavailable")

        monkeypatch.setattr(store, "_ensure_fts", broken)
        hits = await store.asimilarity_search_with_score("refresh the token", k=2)
        assert hits and hits[0][0].metadata["path"] == "auth.py"
    finally:
        await store.aclose()


async def test_odd_questions_do_not_break_the_words_search(tmp_path: Path) -> None:
    store = await hybrid_store(tmp_path)
    try:
        for query in [
            "foo(bar)",
            "a:b",
            '"quoted',
            "it's",
            "x AND y",
            "*",
            "~",
            "a",
            "  ",
            "def f(self, path):",
        ]:
            assert await store.asimilarity_search_with_score(query, k=2) is not None
    finally:
        await store.aclose()


async def count_vector_indexes(store: LanceVectorStore) -> int:
    table = await store._open()
    return sum("vector" in i.columns for i in await table.list_indices())


async def test_big_indexes_get_a_nearest_neighbour_index(tmp_path: Path) -> None:
    store = LanceVectorStore(tmp_path / "big", FakeEmbedding(16), ann_min_rows=300)  # type: ignore[arg-type]
    small = LanceVectorStore(tmp_path / "small", FakeEmbedding(16))  # type: ignore[arg-type]
    try:
        texts = [f"unique{i} filler{i % 7} body{i % 13} token{i}" for i in range(400)]
        metas = [meta(f"f{i}.py") for i in range(400)]
        await store.aadd_texts(texts, metas)
        await small.aadd_texts(texts[:20], metas[:20])
        assert await count_vector_indexes(store) == 0
        await store.aoptimize()
        await small.aoptimize()
        assert await count_vector_indexes(store) == 1  # past the threshold
        assert await count_vector_indexes(small) == 0  # exact scan is fine for a small one
        # Searching through the approximate index still finds the piece.
        top = (await store.asimilarity_search_with_score("unique123 token123", k=3))[0][0]
        assert top.metadata["path"] == "f123.py"
        await store.aoptimize()  # again: nothing to redo, nothing breaks
        assert await count_vector_indexes(store) == 1
    finally:
        await store.aclose()
        await small.aclose()


def folder_bytes(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


async def test_housekeeping_removes_old_versions(tmp_path: Path) -> None:
    from datetime import timedelta

    store = LanceVectorStore(tmp_path / "v", FakeEmbedding(), keep_versions=timedelta(0))  # type: ignore[arg-type]
    try:
        for round_ in range(15):  # a file edited again and again
            await store.aadd_texts(
                [f"edit {round_} of the same file", "another piece"],
                [meta("a.py", 1), meta("a.py", 10)],
            )
        before = folder_bytes(tmp_path / "v")
        await store.aoptimize()
        assert folder_bytes(tmp_path / "v") < before
        assert await store.acount() == 2
    finally:
        await store.aclose()


async def test_an_index_made_before_the_words_index_is_upgraded(tmp_path: Path) -> None:
    import lancedb
    import pyarrow as pa

    folder = tmp_path / "old" / "lancedb"
    folder.mkdir(parents=True)
    db = await lancedb.connect_async(str(folder))
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("path", pa.string()),
            pa.field("start", pa.int32()),
            pa.field("end", pa.int32()),
            pa.field("language", pa.string()),
            pa.field("symbol", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 32)),
        ]
    )
    table = await db.create_table("chunks", schema=schema)
    vec = (await FakeEmbedding().embed(["legacy marker word"]))[0].tolist()
    await table.add(
        pa.Table.from_pylist(
            [
                {
                    "id": "x:1",
                    "path": "x.py",
                    "start": 1,
                    "end": 2,
                    "language": "python",
                    "symbol": "",
                    "text": "legacy marker word",
                    "vector": vec,
                }
            ],
            schema=schema,
        )
    )
    db.close()
    store = LanceVectorStore(folder, FakeEmbedding())  # type: ignore[arg-type]
    try:
        assert await store.acount() == 1
        await store.aadd_texts(["a newer piece"], [meta("y.py")])
        (doc, _), *_ = await store.asimilarity_search_with_score("legacy marker", k=1)
        assert doc.metadata["path"] == "x.py"
    finally:
        await store.aclose()


class CountingEmbedding(FakeEmbedding):
    def __init__(self, dim: int = 32) -> None:
        super().__init__(dim)
        self.calls = 0

    async def embed(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        return await super().embed(texts)


async def test_a_builtin_index_moves_over_without_embedding_again(tmp_path: Path) -> None:
    from cmcoder.rag.stores import LocalVectorStore

    embedding = CountingEmbedding()
    local = LocalVectorStore(tmp_path / "local", embedding)  # type: ignore[arg-type]
    await fill(local)
    local_hits = await local.asimilarity_search_with_score("monthly email", k=2)
    await local.aclose()
    calls = embedding.calls

    lance = LanceVectorStore(tmp_path / "lancedb", embedding)  # type: ignore[arg-type]
    try:
        assert await lance.aimport_local(tmp_path / "local" / "index.db") == 3
        assert await lance.aimport_local(tmp_path / "local" / "index.db") == 3  # again: same rows
        assert await lance.acount() == 3
        assert embedding.calls == calls  # nothing was embedded for the move
        moved = await lance.asimilarity_search_with_score("monthly email", k=2)
        assert [d.metadata["path"] for d, _ in moved] == [d.metadata["path"] for d, _ in local_hits]
        assert moved[0][1] == pytest.approx(local_hits[0][1], abs=1e-4)
        assert moved[0][0].page_content == local_hits[0][0].page_content
    finally:
        await lance.aclose()


async def test_an_unreadable_builtin_index_is_an_error_not_a_crash(tmp_path: Path) -> None:
    bad = tmp_path / "index.db"
    bad.write_text("not a database")
    lance = LanceVectorStore(tmp_path / "lancedb", FakeEmbedding())  # type: ignore[arg-type]
    try:
        with pytest.raises(StoreError, match="built-in index"):
            await lance.aimport_local(bad)
    finally:
        await lance.aclose()
