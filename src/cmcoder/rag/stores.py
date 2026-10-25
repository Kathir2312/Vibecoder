"""Vector stores for the code index: two LangChain `VectorStore`s.

- `LocalVectorStore` (default): built in, a SQLite file under
  ~/.cmcoder/index/ with the vectors; search is a matrix product in memory.
  Nothing to install, so it works in the standalone build. Fine up to about
  100k pieces.
- `ChromaVectorStore`: Chroma at a URL, running on this PC
  (http://localhost:8000) or a server shared by a team, through its REST API
  (v2) with cmcoder's own HTTP client: TLS, proxy and company CA as for the
  gateway. (`langchain-chroma` needs the whole `chromadb` package, which the
  standalone program can't include; the REST API needs nothing.)

Scores are cosine similarities (vectors are unit length): 1 is identical.

LANGCHAIN CONCEPTS IN THIS FILE

* **`VectorStore`**: documents in (`aadd_documents` / `aadd_texts`, embedded
  with the store's `Embeddings`), documents out
  (`asimilarity_search_with_score(query, k)`), `adelete(ids)`. Implementing
  it gives the rest for free, e.g. `store.as_retriever()` (a `Retriever`,
  LangChain's "query in, documents out" runnable).
* A store embeds `page_content` by default. Code search embeds a little more
  (the file's path and the definition's name, which help find it by
  meaning: `embedding_text`) and stores the plain code.
* cmcoder is async throughout, so these stores implement the `a...` methods;
  the sync ones say so.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore

from ..mcp.manager import expand
from ..providers.auth import load_stored_api_key
from ..providers.transport import TransportOptions, build_client
from .common import StoreError, chroma_key_name, chroma_needs_url, private_folder
from .embed import GatewayEmbeddings

__all__ = ["StoreError", "chroma_key_name", "chroma_needs_url", "private_folder"]

BATCH = 100  # records per request to a Chroma server
ASYNC_ONLY = "cmcoder's vector stores are async: use the a... methods"


def embedding_text(text: str, metadata: dict[str, Any]) -> str:
    """What is embedded: the path and name help find the piece by meaning."""
    head = f"File: {metadata.get('path', '?')}"
    if metadata.get("symbol"):
        head += f"\nDefines: {metadata['symbol']}"
    return f"{head}\n\n{text}"


def prefix_ok(path: str, prefix: str | None) -> bool:
    if not prefix:
        return True
    prefix = prefix.strip("/")
    return path == prefix or path.startswith(prefix + "/")


class _CodeStore(VectorStore):
    """What both stores share: our embeddings and the extra operations."""

    def __init__(self, embedding: GatewayEmbeddings) -> None:
        self.embedding = embedding

    @property
    def embeddings(self) -> Embeddings:
        return self.embedding

    def describe(self) -> str:
        raise NotImplementedError

    async def _vectors(self, texts: list[str], metadatas: list[dict[str, Any]]) -> np.ndarray:
        return await self.embedding.embed(
            [embedding_text(t, m) for t, m in zip(texts, metadatas, strict=True)]
        )

    async def asimilarity_search_with_score(
        self, query: str, k: int = 4, path_prefix: str | None = None, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        vector = (await self.embedding.embed([query]))[0]
        return await self.asimilarity_search_by_vector_with_score(vector, k, path_prefix)

    async def asimilarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        return [d for d, _ in await self.asimilarity_search_with_score(query, k, **kwargs)]

    async def asimilarity_search_by_vector_with_score(
        self, vector: np.ndarray, k: int, path_prefix: str | None = None
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError

    async def adelete_paths(self, paths: list[str]) -> None:
        raise NotImplementedError

    async def acount(self) -> int:
        raise NotImplementedError

    async def aclear(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass

    # Sync API: not used by cmcoder (everything is async).
    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        raise NotImplementedError(ASYNC_ONLY)

    def similarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        raise NotImplementedError(ASYNC_ONLY)

    @classmethod
    def from_texts(
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict[str, Any]] | None = None,
        *,
        ids: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError(ASYNC_ONLY)


# --- built in ------------------------------------------------------------------


class LocalVectorStore(_CodeStore):
    def __init__(self, folder: Path, embedding: GatewayEmbeddings) -> None:
        super().__init__(embedding)
        self.folder = folder
        private_folder(folder)
        self._db = sqlite3.connect(folder / "index.db", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, path TEXT, "
                "start INTEGER, end_ INTEGER, language TEXT, symbol TEXT, text TEXT, vector BLOB)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS by_path ON chunks(path)")
            self._db.commit()
        self._matrix: np.ndarray | None = None  # loaded on first search
        self._rows: list[tuple[Any, ...]] = []

    def describe(self) -> str:
        return f"local index ({self.folder})"

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
        vectors = await self._vectors(texts, metas)
        rows = [
            (
                i,
                str(m.get("path", "")),
                int(m.get("start", 0)),
                int(m.get("end", 0)),
                str(m.get("language", "text")),
                str(m.get("symbol") or ""),
                t,
                np.asarray(v, dtype=np.float32).tobytes(),
            )
            for i, t, m, v in zip(ids, texts, metas, vectors, strict=True)
        ]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
            self._matrix = None
        return ids

    async def adelete(self, ids: list[str] | None = None, **kwargs: Any) -> bool:
        with self._lock:
            self._db.executemany("DELETE FROM chunks WHERE id = ?", [(i,) for i in ids or []])
            self._db.commit()
            self._matrix = None
        return True

    async def adelete_paths(self, paths: list[str]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM chunks WHERE path = ?", [(p,) for p in paths])
            self._db.commit()
            self._matrix = None

    def _load(self) -> np.ndarray:
        with self._lock:
            if self._matrix is None:
                cur = self._db.execute(
                    "SELECT id, path, start, end_, language, symbol, text, vector FROM chunks"
                )
                rows, vectors = [], []
                for row in cur:
                    rows.append(row[:7])
                    vectors.append(np.frombuffer(row[7], dtype=np.float32))
                self._rows = rows
                self._matrix = np.vstack(vectors) if vectors else np.zeros((0, 1), np.float32)
            return self._matrix

    async def asimilarity_search_by_vector_with_score(
        self, vector: np.ndarray, k: int, path_prefix: str | None = None
    ) -> list[tuple[Document, float]]:
        matrix = await asyncio.to_thread(self._load)
        if not len(self._rows) or matrix.shape[1] != vector.shape[0]:
            return []
        scores = matrix @ vector.astype(np.float32)
        hits: list[tuple[Document, float]] = []
        for i in np.argsort(-scores):
            cid, path, start, end, language, symbol, text = self._rows[int(i)]
            if not prefix_ok(path, path_prefix):
                continue
            meta = {
                "path": path,
                "start": start,
                "end": end,
                "language": language,
                "symbol": symbol,
            }
            hits.append((Document(page_content=text, metadata=meta, id=cid), float(scores[int(i)])))
            if len(hits) >= k:
                break
        return hits

    async def acount(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    async def aclear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chunks")
            self._db.commit()
            self._matrix = None

    async def aclose(self) -> None:
        with self._lock:
            self._db.close()


# --- a Chroma server -----------------------------------------------------------


class ChromaVectorStore(_CodeStore):
    def __init__(
        self,
        url: str,
        collection: str,
        embedding: GatewayEmbeddings,
        *,
        headers: dict[str, str] | None = None,
        ca_cert_path: str | None = None,
        tenant: str = "default_tenant",
        database: str = "default_database",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(embedding)
        self.url = url.rstrip("/")
        self.collection = collection
        self.collection_metadata = metadata or {}
        sent = {k: expand(v) for k, v in (headers or {}).items()}
        key = os.environ.get("CMCODER_CHROMA_API_KEY") or load_stored_api_key(
            chroma_key_name(self.url)
        )
        if key and not any(h.lower() in ("authorization", "x-chroma-token") for h in sent):
            sent["Authorization"] = f"Bearer {key}"
        self.client = build_client(
            TransportOptions(ca_cert_path=ca_cert_path, read_timeout=60), headers=sent
        )
        self.base = f"{self.url}/api/v2/tenants/{tenant}/databases/{database}/collections"
        self._id: str | None = None

    def describe(self) -> str:
        return f"Chroma server {self.url} (collection {self.collection})"

    async def _call(self, method: str, url: str, body: Any = None) -> Any:
        try:
            resp = await self.client.request(method, url, json=body)
        except httpx.HTTPError as e:
            hint = ""
            if httpx.URL(self.url).host in ("localhost", "127.0.0.1", "::1"):
                hint = (
                    " Is Chroma running on this PC? Start it first, e.g. "
                    "`docker run -d -p 127.0.0.1:8000:8000 -v chroma-data:/data chromadb/chroma`."
                )
            raise StoreError(f"Can't reach the Chroma server {self.url}: {e}.{hint}") from e
        if resp.status_code in (401, 403):
            raise StoreError(
                f"The Chroma server {self.url} refused the request ({resp.status_code}). "
                "Check its API key: `cmcoder rag setup`, or CMCODER_CHROMA_API_KEY."
            )
        if resp.status_code >= 400:
            raise StoreError(f"Chroma server error {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    async def _collection(self) -> str:
        if self._id is None:
            body = {
                "name": self.collection,
                "get_or_create": True,
                "configuration": {"hnsw": {"space": "cosine"}},
                "metadata": self.collection_metadata or None,
            }
            data = await self._call("POST", self.base, body)
            self._id = str(data["id"])
        return self._id

    async def check(self) -> None:
        """Reachable, and the key is accepted (listing needs it; the heartbeat doesn't)."""
        await self._call("GET", f"{self.url}/api/v2/heartbeat")
        await self._call("GET", f"{self.base}?limit=1")

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
        vectors = await self._vectors(texts, metas)
        cid = await self._collection()
        for i in range(0, len(texts), BATCH):
            body = {
                "ids": ids[i : i + BATCH],
                "embeddings": [[float(x) for x in v] for v in vectors[i : i + BATCH]],
                "documents": texts[i : i + BATCH],
                "metadatas": metas[i : i + BATCH],
            }
            await self._call("POST", f"{self.base}/{cid}/upsert", body)
        return ids

    async def adelete(self, ids: list[str] | None = None, **kwargs: Any) -> bool:
        if ids:
            cid = await self._collection()
            await self._call("POST", f"{self.base}/{cid}/delete", {"ids": ids})
        return True

    async def adelete_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        cid = await self._collection()
        for i in range(0, len(paths), BATCH):
            where = {"path": {"$in": paths[i : i + BATCH]}}
            await self._call("POST", f"{self.base}/{cid}/delete", {"where": where})

    async def asimilarity_search_by_vector_with_score(
        self, vector: np.ndarray, k: int, path_prefix: str | None = None
    ) -> list[tuple[Document, float]]:
        cid = await self._collection()
        body = {
            "query_embeddings": [[float(x) for x in vector]],
            "n_results": k * 4 if path_prefix else k,
            "include": ["documents", "metadatas", "distances"],
        }
        data = await self._call("POST", f"{self.base}/{cid}/query", body) or {}
        ids = (data.get("ids") or [[]])[0]
        docs = (data.get("documents") or [[None] * len(ids)])[0]
        metas = (data.get("metadatas") or [[None] * len(ids)])[0]
        dists = (data.get("distances") or [[1.0] * len(ids)])[0]
        hits: list[tuple[Document, float]] = []
        for cid_, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            meta = meta or {}
            if prefix_ok(str(meta.get("path", "")), path_prefix):
                score = 1.0 - float(dist if dist is not None else 1.0)
                hits.append((Document(page_content=doc or "", metadata=meta, id=cid_), score))
        return hits[:k]

    async def acount(self) -> int:
        cid = await self._collection()
        return int(await self._call("GET", f"{self.base}/{cid}/count") or 0)

    async def aclear(self) -> None:
        try:
            await self._call("DELETE", f"{self.base}/{self.collection}")
        except StoreError as e:
            if "404" not in str(e):  # no collection yet: nothing to clear
                raise
        self._id = None

    async def aclose(self) -> None:
        await self.client.aclose()


def documents_of(hits: Sequence[tuple[Document, float]]) -> list[Document]:
    return [d for d, _ in hits]
