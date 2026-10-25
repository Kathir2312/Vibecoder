"""Embeddings for code search, from the gateway's embedding model.

The same request works with LiteLLM (`/v1/embeddings`) and Open WebUI
(`/api/embeddings`, which passes Ollama models on to Ollama).

LANGCHAIN CONCEPTS IN THIS FILE

* **`Embeddings`** is LangChain's interface for "text in, vector out":
  `embed_documents(texts)` for what goes into an index, `embed_query(text)`
  for what is searched with (some models embed the two differently), and
  their async twins `aembed_documents` / `aembed_query`. Any vector store
  takes an `Embeddings`; ours (rag/stores.py) calls it to fill the index and
  to search it.
* Why our own class instead of `OpenAIEmbeddings`? That one counts tokens
  with tiktoken to split long texts (OpenAI's tokenizer, wrong for a bge or
  nomic model), and only knows OpenAI's error format. Ours sends plain
  strings in batches, through the same HTTP client as the chat model (TLS,
  proxy, key), returns unit-length vectors, and explains a wrong model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import numpy as np
from langchain_core.embeddings import Embeddings

from ..models.gateway import Gateway
from .common import EmbeddingError

__all__ = ["EmbeddingError"]

# Texts per request, and the longest text sent (characters): embedding
# models take a few thousand tokens at most; a chunk is far smaller anyway.
BATCH_SIZE = 32
MAX_CHARS = 6000


class GatewayEmbeddings(Embeddings):
    def __init__(self, gateway: Gateway, model: str) -> None:
        self.gateway = gateway
        self.model = model
        self.dim: int | None = None

    @property
    def gateway_kind(self) -> str:
        """For messages: which gateway answers."""
        return "Open WebUI" if self.gateway.kind == "openwebui" else "OpenAI-compatible"

    # --- LangChain's interface -------------------------------------------------

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return (await self.embed(texts)).tolist()

    async def aembed_query(self, text: str) -> list[float]:
        return (await self.embed([text]))[0].tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return asyncio.run(self.aembed_documents(texts))  # cmcoder itself is async

    def embed_query(self, text: str) -> list[float]:
        return asyncio.run(self.aembed_query(text))

    # --- the work ----------------------------------------------------------------

    async def embed(self, texts: list[str]) -> np.ndarray:
        """Unit-length float32 vectors, one row per text (for matrix search)."""
        rows: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = [t[:MAX_CHARS] or " " for t in texts[i : i + BATCH_SIZE]]
            rows += await self._request(batch)
        if not rows:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        matrix = np.asarray(rows, dtype=np.float32)
        if self.dim is None:
            self.dim = int(matrix.shape[1])
        elif matrix.shape[1] != self.dim:
            raise EmbeddingError(
                f"{self.model} returned vectors of size {matrix.shape[1]}, earlier {self.dim}"
            )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return matrix / norms

    async def _request(self, batch: list[str]) -> list[list[float]]:
        url = f"{self.gateway.base_url}/embeddings"
        attempt = 0
        while True:
            try:
                resp = await self.gateway.client.post(
                    url, json={"model": self.model, "input": batch}
                )
            except httpx.HTTPError as e:
                if attempt < self.gateway.cfg.max_retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise EmbeddingError(
                    f"Can't reach the gateway for embeddings: {type(e).__name__}: {e}",
                    "Check the VPN, the URL and NO_PROXY; run `cmcoder doctor`.",
                ) from e
            if resp.status_code == 200:
                return _vectors(resp.json(), self.model, len(batch))
            if resp.status_code >= 500 and self.gateway.kind == "openwebui":
                await self._explain_openwebui(resp)
            if resp.status_code >= 500 and attempt < self.gateway.cfg.max_retries:
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
                continue
            raise EmbeddingError(_error_message(resp))

    async def _explain_openwebui(self, resp: httpx.Response) -> None:
        """Open WebUI answers a bare 500 for a model it doesn't know: say so."""
        try:
            known = self.model in await self.gateway.list_models()
        except Exception:
            return
        if not known:
            raise EmbeddingError(
                f"Open WebUI has no model {self.model!r} (it answered: HTTP {resp.status_code})",
                hint="Embedding models it offers are among "
                f"`cmcoder models --provider {self.gateway.name}`.",
            )


def _vectors(data: Any, model: str, count: int) -> list[list[float]]:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != count:
        raise EmbeddingError(f"{model} returned no embeddings for {count} texts")
    items = sorted(items, key=lambda d: d.get("index", 0))
    return [list(map(float, d["embedding"])) for d in items]


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    err = data.get("error") or data.get("detail") or data if isinstance(data, dict) else data
    if isinstance(err, dict):
        err = err.get("message") or err
    return f"HTTP {resp.status_code}: {err}"
