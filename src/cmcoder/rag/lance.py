"""LanceDB as the code index's store: a third LangChain `VectorStore`.

LanceDB is a database that runs inside cmcoder (no server, like the built-in
store) and keeps its data on disk in the Lance format, so an index is not
limited by memory the way the built-in one is (which loads every vector into RAM,
fine to about 100k pieces). The database itself applies the path filter, deleting a
file's pieces and updating changed ones are native operations, and (phase 3) it
searches by keywords and by meaning together.

WHY OUR OWN `VectorStore` AND NOT `langchain-lancedb`
That package (0.1.0) is the sync LanceDB wrapper without a metadata filter, a count,
or deleting by path, and the code index needs all of them (and everything here is
async). So this is a `VectorStore` subclass over LanceDB's async client, like
`ChromaVectorStore` is over Chroma's REST API: what LangChain defines (the
interface, `Document`, `Embeddings`, retrievers) is used; what the package doesn't
offer is ours. Plain Python by decision: LanceDB's own client, nothing in
LangChain or LangGraph replaces it.

`lancedb` and `pyarrow` are big, so they are imported when the store is first used,
not when cmcoder starts. Nothing here touches the network: a local folder only.

Scores are cosine similarities (1 is identical), as in the other stores.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Iterable
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
FIELDS = ("id", "path", "start", "end", "language", "symbol", "text")


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


class LanceVectorStore(_CodeStore):
    def __init__(self, folder: Path, embedding: GatewayEmbeddings) -> None:
        super().__init__(embedding)
        self.folder = folder
        private_folder(folder)
        self._db: Any = None
        self._table: Any = None
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
                    self._table = await db.open_table(TABLE)
                elif dim is not None:
                    self._table = await db.create_table(TABLE, schema=_schema(dim))
            except Exception as e:
                raise StoreError(f"LanceDB index {self.folder}: {e}") from e
            return self._table

    async def _table_dim(self, table: Any) -> int:
        schema = await table.schema()
        return int(schema.field("vector").type.list_size)

    # -- writing ------------------------------------------------------------------

    async def aadd_texts(
        self,
        texts: Iterable[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        import pyarrow as pa

        texts = list(texts)
        metas = metadatas or [{} for _ in texts]
        ids = ids or [f"{m.get('path')}:{m.get('start')}" for m in metas]
        if not texts:
            return ids
        vectors = await self._vectors(texts, metas)
        dim = int(vectors.shape[1])
        table = await self._open(dim)
        if await self._table_dim(table) != dim:
            raise StoreError(
                "This index was built with a different embedding size. Rebuild it: /index rebuild."
            )
        rows = [
            {
                "id": i,
                "path": str(m.get("path", "")),
                "start": int(m.get("start", 0)),
                "end": int(m.get("end", 0)),
                "language": str(m.get("language", "text")),
                "symbol": str(m.get("symbol") or ""),
                "text": t,
                "vector": np.asarray(v, dtype=np.float32).tolist(),
            }
            for i, t, m, v in zip(ids, texts, metas, vectors, strict=True)
        ]
        try:
            # Upsert by id: a changed piece replaces the old one, a new one is added.
            await (
                table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(pa.Table.from_pylist(rows, schema=await table.schema()))
            )
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e
        return ids

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

    # -- reading ------------------------------------------------------------------

    async def asimilarity_search_by_vector_with_score(
        self, vector: np.ndarray, k: int, path_prefix: str | None = None
    ) -> list[tuple[Document, float]]:
        table = await self._open()
        if table is None or await table.count_rows() == 0:
            return []
        if await self._table_dim(table) != int(vector.shape[0]):
            return []  # another embedding model's index
        query = (
            table.vector_search(np.asarray(vector, dtype=np.float32).tolist())
            .distance_type("cosine")
            .limit(k)
        )
        if where := prefix_filter(path_prefix):
            query = query.where(where)
        try:
            rows = await query.to_list()
        except Exception as e:
            raise StoreError(f"LanceDB index {self.folder}: {e}") from e
        hits: list[tuple[Document, float]] = []
        for row in rows:
            meta = {key: row[key] for key in ("path", "start", "end", "language", "symbol")}
            hits.append(
                (
                    Document(page_content=row["text"], metadata=meta, id=row["id"]),
                    1.0 - float(row["_distance"]),
                )
            )
        return hits

    async def acount(self) -> int:
        table = await self._open()
        return 0 if table is None else int(await table.count_rows())

    async def aclear(self) -> None:
        async with self._lock:
            db = await self._connection()
            self._table = None
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
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )
