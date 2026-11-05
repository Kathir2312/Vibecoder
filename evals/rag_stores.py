"""Compare the code index's stores at different sizes: built-in, LanceDB, LanceDB hybrid.

    uv run python evals/rag_stores.py                       # 5k, 20k, 100k, 300k pieces
    uv run python evals/rag_stores.py --sizes 1000 50000 --dim 768

For each store and size, two fresh processes: one BUILDS the index (time to add the
pieces, folder size on disk) and one SEARCHES it (a session that opens an existing index:
the median and 95th percentile of a search with and without a path filter, and the
process's peak memory, which is what a session pays for the store). The pieces are
vectors in clusters, like real embeddings (random vectors have no neighbours worth
finding); a query is a stored vector plus noise. Two accuracy numbers: `top10` is how
often the piece the query came from is in the 10 results, `recall` how many of the 10
exact nearest neighbours come back.

What it can NOT measure: how well the vectors capture meaning. That depends on your
embedding model; the stores only keep and search what they are given. The one quality
check here is `words`: pieces named in camelCase are asked for by words inside the name
with a meaningless query vector, so only the words index can find them. It shows the
words index works, not how real embeddings behave.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VOCAB = [
    "load",
    "save",
    "parse",
    "render",
    "build",
    "index",
    "token",
    "user",
    "order",
    "invoice",
    "account",
    "stream",
    "buffer",
    "config",
    "handler",
    "request",
    "response",
    "session",
    "cache",
    "queue",
    "worker",
    "retry",
    "timeout",
    "store",
    "fetch",
    "update",
    "delete",
    "create",
    "search",
    "filter",
    "merge",
    "split",
    "encode",
    "decode",
    "verify",
    "sign",
]
QUERIES = 100
CLUSTERS = 300


def clustered(n: int, dim: int) -> np.ndarray:
    rng = np.random.default_rng(3)
    centers = rng.normal(size=(CLUSTERS, dim)).astype(np.float32)
    noise = 0.8 * rng.normal(size=(n, dim)).astype(np.float32)
    v = centers[rng.integers(0, CLUSTERS, size=n)] + noise
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def piece_text(i: int, rng: np.random.Generator) -> tuple[str, str]:
    w = rng.choice(VOCAB, size=6)
    name = f"{w[0]}{w[1].title()}{w[2].title()}"
    return f"def {name}_{i}(x): return {w[3]}({w[4]}, {w[5]})  #{i}", f"{w[1]} {w[2]} {i}"


class Vectors:
    """Embeddings from a table: the text carries its row number."""

    def __init__(self, vectors: np.ndarray) -> None:
        self.vectors = vectors

    async def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([self.vectors[int(t.rsplit("#", 1)[1].split()[0])] for t in texts])


def folder_bytes(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def make_store(kind: str, folder: Path, embedding: object) -> Any:
    from cmcoder.rag.lance import ANN_MIN_ROWS, LanceVectorStore
    from cmcoder.rag.stores import LocalVectorStore

    if kind == "local":
        return LocalVectorStore(folder / "local", embedding)  # type: ignore[arg-type]
    hybrid = kind == "lancedb-hybrid"
    ann = int(os.environ.get("CMCODER_BENCH_ANN_MIN_ROWS", ANN_MIN_ROWS))
    return LanceVectorStore(
        folder / "lancedb",
        embedding,  # type: ignore[arg-type]
        hybrid=hybrid,
        ann_min_rows=ann,
    )


async def build(kind: str, n: int, dim: int, folder: Path) -> dict[str, float]:
    rng = np.random.default_rng(7)
    vectors = clustered(n, dim)
    pairs = [piece_text(i, rng) for i in range(n)]
    texts = [p[0] for p in pairs]
    metas = [
        {"path": f"pkg{i % 50}/mod{i % 400}.py", "start": i, "end": i + 5, "language": "python"}
        for i in range(n)
    ]
    # What the search process needs: queries, their truth, and word probes.
    q = rng.choice(n, size=QUERIES, replace=False)
    noisy = vectors[q] + 0.02 * rng.normal(size=(QUERIES, dim)).astype(np.float32)
    noisy /= np.linalg.norm(noisy, axis=1, keepdims=True)
    np.save(folder / "queries.npy", noisy)
    np.save(folder / "needles.npy", q)
    np.save(folder / "truth.npy", np.argsort(-(vectors @ noisy.T), axis=0)[:10].T)
    probes = rng.choice(n, size=30, replace=False)
    (folder / "probes.json").write_text(json.dumps([[int(i), pairs[i][1]] for i in probes]))
    store = make_store(kind, folder, Vectors(vectors))
    started = time.perf_counter()
    for i in range(0, n, 5000):
        ids = [str(j) for j in range(i, min(i + 5000, n))]
        await store.aadd_texts(texts[i : i + 5000], metas[i : i + 5000], ids=ids)
    await store.aoptimize()
    seconds = time.perf_counter() - started
    await store.aclose()
    return {"add_s": seconds}


async def search(kind: str, n: int, dim: int, folder: Path) -> dict[str, float]:
    store = make_store(kind, folder, None)
    queries = np.load(folder / "queries.npy")
    needles = np.load(folder / "needles.npy")
    truth = np.load(folder / "truth.npy")
    out: dict[str, float] = {}
    for label, prefix in (("search", None), ("filtered", "pkg7")):
        times: list[float] = []
        for query in queries:
            t0 = time.perf_counter()
            await store.asimilarity_search_by_vector_with_score(query, 10, prefix)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        out[f"{label}_p50"] = statistics.median(times)
        out[f"{label}_p95"] = times[int(len(times) * 0.95) - 1]
    top10 = recall = 0.0
    for query, needle, true in zip(queries, needles, truth, strict=True):
        got = {d.id for d, _ in await store.asimilarity_search_by_vector_with_score(query, 10)}
        top10 += str(needle) in got
        recall += len(got & {str(t) for t in true}) / 10
    out["top10"], out["recall"] = top10 / QUERIES, recall / QUERIES
    if kind != "local":
        found = 0
        rng = np.random.default_rng(5)
        probes = json.loads((folder / "probes.json").read_text())
        for i, words in probes:
            noise = rng.normal(size=dim).astype(np.float32)  # a vector that says nothing
            hits = await store.asimilarity_search_by_vector_with_score(
                noise / np.linalg.norm(noise), 10, None, query_text=words
            )
            found += any(d.id == str(i) for d, _ in hits)
        out["words"] = found / len(probes)
    out["disk_mb"] = folder_bytes(folder) / 1_048_576
    out["rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    await store.aclose()
    return out


def run_child(phase: str, kind: str, n: int, dim: int, folder: Path) -> dict[str, float]:
    done = subprocess.run(
        [sys.executable, __file__, "--child", phase, kind, str(n), str(dim), str(folder)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if done.returncode != 0:
        raise RuntimeError(f"{phase} {kind} {n}: {done.stderr[-600:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=[5_000, 20_000, 100_000, 300_000])
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--child", nargs=5, metavar="X", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        phase, kind, n, dim, folder = args.child
        work = build if phase == "build" else search
        print(json.dumps(asyncio.run(work(kind, int(n), int(dim), Path(folder)))))
        return 0
    print(f"dimension {args.dim}, clustered vectors, {QUERIES} queries")
    print(
        f"{'pieces':>7} {'store':<15} {'add s':>6} {'search p50/p95 ms':>18} "
        f"{'filtered p50/p95':>17} {'top10':>6} {'recall':>7} {'words':>6} "
        f"{'disk MB':>8} {'RAM MB':>7}"
    )
    for n in args.sizes:
        for kind in ("local", "lancedb", "lancedb-hybrid"):
            folder = Path(tempfile.mkdtemp(prefix="cmcoder-bench-"))
            try:
                built = run_child("build", kind, n, args.dim, folder)
                r = run_child("search", kind, n, args.dim, folder)
            except RuntimeError as e:
                print(f"{n:>7} {kind:<15} failed: {e}", file=sys.stderr)
                continue
            words = f"{r['words']:.0%}" if "words" in r else "-"
            print(
                f"{n:>7} {kind:<15} {built['add_s']:>6.1f} "
                f"{r['search_p50']:>8.1f}/{r['search_p95']:<8.1f} "
                f"{r['filtered_p50']:>8.1f}/{r['filtered_p95']:<8.1f} {r['top10']:>6.2f} "
                f"{r['recall']:>7.2f} {words:>6} {r['disk_mb']:>8.0f} {r['rss_mb']:>7.0f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
