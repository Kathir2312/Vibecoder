"""LanceDB as the code index's store: a third LangChain `VectorStore`.

LanceDB is a database that runs inside cmcoder (no server, like the built-in
store) and keeps its data on disk in the Lance format, so an index is not
limited by memory the way the built-in one is (which loads every vector into RAM,
fine to about 100k pieces). The database itself applies the path filter, and deleting a
file's pieces and updating changed ones are native operations.

WHAT IT ADDS TO THE BUILT-IN STORE
* **Hybrid search.** A question is searched twice: by meaning (the vectors) and by
  words (a full-text index, BM25), and the two rankings are merged with Reciprocal Rank
  Fusion. Meaning finds code described in other words; words find the exact name
  (`parse_config`, `HTTPServerError`) that embeddings tend to blur. The words index
  also holds the pieces of camelCase names (`HTTPServerError` is searchable as
  "http server error"), which LanceDB's tokenizer doesn't split itself.
  Scores stay cosine similarities, whichever search found a piece, so thresholds
  (automatic context) mean the same as with the other stores.
* **An approximate-nearest-neighbour index** (IVF-PQ) for big indexes: created by
  `aoptimize` once there are `ann_min_rows` pieces. Below that, an exact scan is fine
  (measured: at 20,000 pieces the index cuts peak memory from 829 MB to 273 MB).
* **Housekeeping.** Every write makes a new version; `aoptimize` merges small files,
  brings the search indexes up to date and removes versions older than an hour.
* **Moving over from the built-in store** without embedding again: `aimport_local`.

WHY OUR OWN `VectorStore` AND NOT `langchain-lancedb`
That package (0.1.0) is the sync LanceDB wrapper without a metadata filter, a count,
or deleting by path, and the code index needs all of them (and everything here is
async). So this is a `VectorStore` subclass over LanceDB's async client, like
`ChromaVectorStore` is over Chroma's REST API: what LangChain defines (the
interface, `Document`, `Embeddings`, retrievers) is used; what the package doesn't
offer is ours. The merge of the two rankings (`rrf`) is ours too, to keep cosine scores:
LanceDB's own hybrid query returns only a fused relevance score. Plain Python by
decision: LanceDB's own client, nothing in LangChain or LangGraph replaces it.

`lancedb` and `pyarrow` are big, so they are imported when the store is first used,
not when cmcoder starts. Nothing here touches the network: a local folder only.

Scores are cosine similarities (1 is identical), as in the other stores.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sqlite3
import warnings
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
from langchain_core.documents import Document

from .common import StoreError, private_folder
from .embed import GatewayEmbeddings
from .stores import _CodeStore

TABLE = "chunks"
LANCEDB_MISSING = (
    "This cmcoder doesn't include LanceDB (a slim build). Use the built-in index "
    '("store": {"type": "local"}) or a build with LanceDB.'
)
PREDICATE_BATCH = 200  # values in one `IN (...)` filter
ANN_MIN_ROWS = 10_000  # below this an exact scan is fine; above it, build an ANN index
KEEP_VERSIONS = timedelta(hours=1)  # older versions are removed by `aoptimize`
ANN_REFINE = 50  # re-rank the ANN's candidates by exact distance (recall@10 0.14 -> 0.86 at 300k)
RRF_K = 60  # Reciprocal Rank Fusion's constant (the usual value)
POOL_FACTOR = 4  # how many candidates each search brings to the merge, per result wanted
MIN_POOL = 20
IMPORT_BATCH = 1000
SEARCH_COLUMN = "search_text"


def lancedb_available() -> bool:
    """False in a slim build (packaging/build.py --without-lancedb)."""
    return importlib.util.find_spec("lancedb") is not None


def quote(value: str) -> str:
    """A string as a SQL literal for LanceDB's filters: quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


def in_list(column: str, values: list[str]) -> list[str]:
    """Filters deleting `column` in `values`, in batches."""
    return [
        f"{column} IN ({', '.join(quote(v) for v in values[i : i + PREDICATE_BATCH])})"
        for i in range(0, len(values), PREDICATE_BATCH)
    ]


def prefix_filter(prefix: str | None) -> str | None:
    """A filter for a file or a folder: `starts_with`, so `_` and `%` in a name mean
    themselves (a LIKE pattern would read them as wildcards)."""
    if not prefix:
        return None
    prefix = prefix.strip("/")
    if not prefix:
        return None
    return f"(path = {quote(prefix)} OR starts_with(path, {quote(prefix + '/')}))"


# -- words ---------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PIECE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
MAX_EXTRA_WORDS = 400


def split_identifiers(text: str) -> str:
    """The words inside camelCase and PascalCase names, lower-cased: `HTTPServerError`
    gives "http server error". (`snake_case` needs nothing: the tokenizer splits at `_`.)"""
    words: dict[str, None] = {}
    for name in _IDENTIFIER.findall(text):
        if name.islower() or name.isupper():  # one case: nothing to split
            continue
        pieces = [p.lower() for p in _PIECE.findall(name)]
        if len(pieces) < 2:
            continue
        for piece in pieces:
            words.setdefault(piece, None)
        if len(words) >= MAX_EXTRA_WORDS:
            break
    return " ".join(words)


def search_text(text: str) -> str:
    """What the words index holds for a piece: its code, then the words in its names."""
    extra = split_identifiers(text)
    return f"{text}\n{extra}" if extra else text


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: a piece's score is the sum of 1 / (k + rank) over the
    rankings it is in. Returns the keys, best first (ties: the first ranking's order)."""
    score: dict[str, float] = {}
    first: dict[str, int] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
            first.setdefault(key, len(first))
    return sorted(score, key=lambda key: (-score[key], first[key]))


class LanceVectorStore(_CodeStore):
    def __init__(
        self,
        folder: Path,
        embedding: GatewayEmbeddings,
        *,
        hybrid: bool = True,
        ann_min_rows: int = ANN_MIN_ROWS,
        keep_versions: timedelta = KEEP_VERSIONS,
    ) -> None:
        super().__init__(embedding)
        self.folder = folder
        self.hybrid = hybrid
        self.ann_min_rows = ann_min_rows
        self.keep_versions = keep_versions
        private_folder(folder)
        self._db: Any = None
        self._table: Any = None
        self._fts_ready = False
        self._ann: bool | None = None  # whether there is a nearest-neighbour index (cached)
        self._lock = asyncio.Lock()

    def describe(self) -> str:
        return f"LanceDB index ({self.folder})"

    # -- connection ---------------------------------------------------------------

    async def _connection(self) -> Any:
        if self._db is None:
            import lancedb

            try:
                self._db = await lancedb.connect_async(str(self.folder))
            except Exception as e:
                raise StoreError(f"Could not open the LanceDB index at {self.folder}: {e}") from e
        return self._db

    async def _open(self, dim: int | None = None) -> Any:
        """The table, or None when there isn't one (and no `dim` to create it with)."""
        async with self._lock:
            if self._table is not None:
                return self._table
            db = await self._connection()
            try:
                if TABLE in (await db.list_tables()).tables:
                    table = await db.open_table(TABLE)
                    if SEARCH_COLUMN not in (await table.schema()).names:
                        # An index made before the words index existed (phase 1).
                        await table.add_columns({SEARCH_COLUMN: "text"})
                    self._table = table
                elif dim is not None:
                    self._table = await db.create_table(TABLE, schema=_schema(dim))
            except Exception as e:
                raise StoreError(f"LanceDB index {self.folder}: {e}") from e
            return self._table

    async def _table_dim(self, table: Any) -> int:
        schema = await table.schema()
        return int(schema.field("vector").type.list_size)

    async def _indexed_columns(self, table: Any) -> set[str]:
        return {c for index in await table.list_indices() for c in index.columns}

    async def _has_ann(self, table: Any) -> bool:
        if self._ann is None:
            self._ann = "vector" in await self._indexed_columns(table)
        return self._ann

    async def _ensure_fts(self, table: Any) -> None:
        """The words index, made once there is something to index; pieces added later
        are found by scanning until `aoptimize` brings them in."""
        if self._fts_ready:
            return
        if SEARCH_COLUMN not in await self._indexed_columns(table):
            if await table.count_rows() == 0:
                return
            from lancedb.index import FTS

            await table.create_index(SEARCH_COLUMN, config=FTS(), replace=True)
        self._fts_ready = True

    # -- writing ------------------------------------------------------------------

    @staticmethod
    def _row(
        id: str, text: str, meta: dict[str, Any], vector: Iterable[float] | np.ndarray
    ) -> dict[str, Any]:
        return {
            "id": id,
            "path": str(meta.get("path", "")),
            "start": int(meta.get("start", 0)),
            "end": int(meta.get("end", 0)),
            "language": str(meta.get("language", "text")),
            "symbol": str(meta.get("symbol") or ""),
            "text": text,
            SEARCH_COLUMN: search_text(text),
            "vector": np.asarray(vector, dtype=np.float32).tolist(),
        }

    async def _upsert(self, rows: list[dict[str, Any]], dim: int) -> None:
        import pyarrow as pa

        table = await self._open(dim)
        if await self._table_dim(table) != dim:
            raise StoreError(
                "This index was built with a different embedding size. Rebuild it: /index rebuild."
            )
        try:
            # Upsert by id: a changed piece replaces the old one, a new one is added.
            await (
                table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(pa.Table.from_pylist(rows, schema=await table.schema()))
            )
            await self._ensure_fts(table)
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e

    async def aadd_texts(
        self,
        texts: Iterable[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        texts = list(texts)
        metas = metadatas or [{} for _ in texts]
        ids = ids or [f"{m.get('path')}:{m.get('start')}" for m in metas]
        if not texts:
            return ids
        vectors = await self._vectors(texts, metas)
        rows = [
            self._row(i, t, m, v) for i, t, m, v in zip(ids, texts, metas, vectors, strict=True)
        ]
        await self._upsert(rows, int(vectors.shape[1]))
        return ids

    async def aimport_local(self, database: Path) -> int:
        """Copy the pieces and vectors of a built-in store's `index.db` here, so
        changing store doesn't mean embedding the whole project again. Returns how many."""
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, check_same_thread=False)
        total = 0
        try:
            cursor = connection.execute(
                "SELECT id, path, start, end_, language, symbol, text, vector FROM chunks"
            )
            while batch := await asyncio.to_thread(cursor.fetchmany, IMPORT_BATCH):
                vectors = [np.frombuffer(row[7], dtype=np.float32) for row in batch]
                rows = [
                    self._row(
                        row[0],
                        row[6],
                        {
                            "path": row[1],
                            "start": row[2],
                            "end": row[3],
                            "language": row[4],
                            "symbol": row[5],
                        },
                        vector,
                    )
                    for row, vector in zip(batch, vectors, strict=True)
                ]
                await self._upsert(rows, int(vectors[0].shape[0]))
                total += len(rows)
        except sqlite3.Error as e:
            raise StoreError(f"Could not read the built-in index {database}: {e}") from e
        finally:
            connection.close()
        return total

    async def _delete(self, column: str, values: list[str]) -> None:
        table = await self._open()
        if table is None or not values:
            return
        try:
            for where in in_list(column, values):
                await table.delete(where)
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e

    async def adelete(self, ids: list[str] | None = None, **kwargs: Any) -> bool:
        await self._delete("id", list(ids or []))
        return True

    async def adelete_paths(self, paths: list[str]) -> None:
        await self._delete("path", paths)

    async def aoptimize(self) -> None:
        """Housekeeping after an update: the words index, the nearest-neighbour index
        once the index is big, merged files, and old versions removed."""
        table = await self._open()
        if table is None:
            return
        try:
            await self._ensure_fts(table)
            if await table.count_rows() >= self.ann_min_rows and not await self._has_ann(table):
                from lancedb.index import IvfPq

                await table.create_index("vector", config=IvfPq(distance_type="cosine"))
                self._ann = True
            with warnings.catch_warnings():  # "removes every version" when keep_versions is 0
                warnings.simplefilter("ignore", UserWarning)
                await table.optimize(cleanup_older_than=self.keep_versions)
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e

    # -- reading ------------------------------------------------------------------

    async def _keyword_rows(
        self, table: Any, text: str, where: str | None, pool: int
    ) -> list[dict[str, Any]]:
        """The pieces whose words match the question (BM25), best first. Never fails a
        search: if the words index can't answer, the meaning search still does."""
        try:
            await self._ensure_fts(table)
            query = table.query().nearest_to_text(text, columns=SEARCH_COLUMN).limit(pool)
            if where:
                query = query.where(where)
            return list(await query.to_list())
        except Exception:
            return []

    async def asimilarity_search_by_vector_with_score(
        self,
        vector: np.ndarray,
        k: int,
        path_prefix: str | None = None,
        query_text: str | None = None,
    ) -> list[tuple[Document, float]]:
        table = await self._open()
        if table is None or await table.count_rows() == 0:
            return []
        if await self._table_dim(table) != int(vector.shape[0]):
            return []  # another embedding model's index
        query_vector = np.asarray(vector, dtype=np.float32)
        where = prefix_filter(path_prefix)
        use_words = self.hybrid and bool(query_text) and len((query_text or "").strip()) >= 2
        pool = max(k * POOL_FACTOR, MIN_POOL) if use_words else k
        query = table.vector_search(query_vector.tolist()).distance_type("cosine").limit(pool)
        if await self._has_ann(table):
            # The index compresses vectors, so its ranking is rough: re-rank its best
            # candidates by exact distance (measured at 300k pieces: recall@10 0.14 -> 0.86
            # for +7 ms; the piece a query came from was in the top 10 either way).
            query = query.refine_factor(ANN_REFINE)
        if where:
            query = query.where(where)
        try:
            by_meaning = list(await query.to_list())
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e
        rows = {r["id"]: r for r in by_meaning}
        order = [r["id"] for r in by_meaning]
        if use_words:
            by_words = await self._keyword_rows(table, query_text or "", where, pool)
            if by_words:
                for r in by_words:
                    rows.setdefault(r["id"], r)
                order = rrf([order, [r["id"] for r in by_words]])
        hits: list[tuple[Document, float]] = []
        for key in order[:k]:
            row = rows[key]
            if row.get("_distance") is not None:
                score = 1.0 - float(row["_distance"])
            else:  # found by words only: its similarity to the question, from its vector
                score = float(np.dot(query_vector, np.asarray(row["vector"], dtype=np.float32)))
            meta = {name: row[name] for name in ("path", "start", "end", "language", "symbol")}
            hits.append((Document(page_content=row["text"], metadata=meta, id=row["id"]), score))
        return hits

    async def acount(self) -> int:
        table = await self._open()
        return 0 if table is None else int(await table.count_rows())

    async def aclear(self) -> None:
        async with self._lock:
            db = await self._connection()
            self._table = None
            self._fts_ready = False
            self._ann = None
            try:
                if TABLE in (await db.list_tables()).tables:
                    await db.drop_table(TABLE)
            except Exception as e:
                raise StoreError(f"LanceDB index {self.folder}: {e}") from e

    async def aclose(self) -> None:
        async with self._lock:
            for handle in (self._table, self._db):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # already closed
                        pass
            self._table = self._db = None
            self._fts_ready = False
            self._ann = None


def _schema(dim: int) -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("path", pa.string()),
            pa.field("start", pa.int32()),
            pa.field("end", pa.int32()),
            pa.field("language", pa.string()),
            pa.field("symbol", pa.string()),
            pa.field("text", pa.string()),
            pa.field(SEARCH_COLUMN, pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )
