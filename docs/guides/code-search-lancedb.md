# Code search with LanceDB

Code search keeps an index of your project's code so the agent can find it by meaning
(`CodeSearch`, and the matches added to your questions). There are three places the index
can live:

| Store | Good for | Needs |
|---|---|---|
| **Built in** (default) | most projects, up to about 100,000 pieces of code | nothing |
| **LanceDB** | big codebases and monorepos | nothing: it runs inside cmcoder |
| **Chroma** | a server a team shares | a Chroma server |

LanceDB keeps the index on disk instead of loading it all into memory, and applies the path
filter inside the database. For a small project the built-in store is just as good.

## Use it

```json
{ "rag": { "embeddingModel": "corp:bge-m3", "store": { "type": "lancedb" } } }
```

or `cmcoder rag setup --store lancedb`, or pick **On this machine, LanceDB** in VS Code's
code-search setup. Then `cmcoder index` (or `/index`) builds it. The files are under
`~/.cmcoder/index/` and readable by you only. Switching stores means building the index
again (`/index rebuild`), because the vectors live in the store.

LanceDB never uses the network: it only reads and writes that local folder. (The test suite
runs it in an environment with no network at all.)

## Search by words as well as by meaning

With LanceDB a question is searched twice, by meaning (the vectors) and by words (a full-text
index), and the two rankings are merged (Reciprocal Rank Fusion). Meaning finds code described in
other words; words find the exact name that embeddings tend to blur: `parse_config`, `crc32`,
`HTTPServerError`. The words index also knows the parts of camelCase names, so "server error"
finds `HTTPServerError`. Each result still carries a cosine similarity, so the automatic-context
threshold means the same as with the other stores.

It is on by default for LanceDB. To use meaning only: `"store": {"type": "lancedb", "hybrid": false}`.
If the words index can't answer for some reason, the meaning search still does.

## Big indexes, and keeping it tidy

- Up to 10,000 pieces LanceDB scans exactly. Past that, `/index` (and the session's automatic
  update) builds an approximate-nearest-neighbour index (IVF-PQ), whose best candidates are then
  re-ranked by exact distance. At 20,000 pieces the index cuts the search's memory from 829 MB to
  273 MB.
- Every change makes a new version on disk. After an update cmcoder merges small files, brings the
  search indexes up to date and removes versions older than an hour, so the folder doesn't grow
  with every edit.

## Switching from the built-in store

If you change `store.type` from `local` to `lancedb` and the project has a built-in index for the
same embedding model, the next update **moves it over**: the pieces and vectors are copied, nothing
is sent to your gateway to be embedded again, and only files that changed since are indexed.
`/index rebuild` starts from nothing instead. (The other way, back to the built-in store, builds
again.)

## How it compares (synthetic benchmark)

`uv run python evals/rag_stores.py` indexes random vectors with code-like text in a fresh process
per store and size, and measures speed, memory, disk and the ANN's recall. It cannot tell how well
your embedding model captures meaning: the stores only keep and search the vectors they are given.
Linux x86-64, 4 cores, 16 GB, 384-dimension vectors in clusters, 100 queries; each store was built
in one process and searched in another (the memory is the search process's peak, what a session
pays). Times in milliseconds (median / 95th percentile).

| Pieces | Store | Build | Search | With a path filter | Memory | On disk |
|---:|---|---:|---:|---:|---:|---:|
| 5,000 | built in | 0.1 s | 0.3 / 0.6 | 0.5 / 0.7 | 99 MB | 10 MB |
| | LanceDB | 1.8 s | 8.1 / 13.6 | 9.0 / 12.3 | 421 MB | 8 MB |
| 20,000 | built in | 0.3 s | 1.2 / 1.4 | 1.2 / 1.4 | 154 MB | 40 MB |
| | LanceDB | 6.2 s | 13.6 / 17.5 | 13.6 / 17.4 | 272 MB | 59 MB |
| 100,000 | built in | 1.5 s | 4.5 / 6.1 | 6.0 / 7.6 | 452 MB | 200 MB |
| | LanceDB | 21 s | 17 / 23 | 23 / 30 | 285 MB | 317 MB |
| 300,000 | built in | 6.5 s | 20 / 28 | 19 / 24 | 1,190 MB | 600 MB |
| | LanceDB | 38 s | 20 / 27 | 43 / 49 | 308 MB | 959 MB |

(LanceDB rows are with hybrid search on; switching it off changes the times by under a
millisecond.) What this says, plainly:

- **The built-in store is faster** at every size up to 300,000 pieces, and uses less memory up to
  roughly 50,000 pieces. LanceDB's search is 8 to 20 ms, which is far below the time a model takes
  to answer, but it isn't a speed-up.
- **LanceDB's memory stays flat** (about 270 to 310 MB) while the built-in store's grows with the
  index: 452 MB at 100,000 pieces, 1.2 GB at 300,000. That is the reason to choose it for a very
  large codebase.
- **Accuracy:** the piece a query came from was in the top 10 every time, for every store and size.
  Past 100,000 pieces LanceDB's approximate index returns 85% of the exact top 10 at 300,000 pieces
  (100% at 100,000); the built-in store is always exact.
- **Words:** asked by words inside a camelCase name with a query vector that says nothing, hybrid
  search found 100% and meaning-only search 0 to 3%. That shows the words index works. Whether it
  helps depends on your embedding model, which this benchmark can't tell you.
- **Costs:** building a LanceDB index takes 5 to 20 times longer (indexing time is mostly your
  gateway embedding the code, so you'd rarely notice), and the folder is 1.2 to 1.6 times bigger
  (the words index).

So: keep the built-in store unless the project has more than about 100,000 pieces of code (memory),
or you want exact-name search through hybrid mode.


## For IT: what the standalone program contains

- **LanceDB and the part of `pyarrow` it needs are inside the program.** A PC needs no Python,
  no database and no download.
- **Trimmed:** `packaging/prune.py` removes what the code index never uses from `pyarrow`
  (Flight, the S3 and HDFS file systems, ORC, C++ headers, Cython sources, and a second copy of
  the arrow libraries that PyInstaller makes on Linux). The Parquet and Substrait libraries stay:
  every `pyarrow` module is linked against them, and the program does not start without them.
- **Size** (measured on a Linux build, with the Browser tool): unpacked 300 MB without LanceDB,
  780 MB with it untrimmed, **586 MB** trimmed; the release zip goes from 112 MB to **218 MB**
  (+106 MB). Startup is unchanged (about 0.6 s): `lancedb` and `pyarrow` are loaded only when a
  LanceDB index is first used. LanceDB's own engine is a single 180 MB native library that can't
  be made smaller.
- **A smaller build:** `build.py --without-lancedb` leaves LanceDB and `pyarrow` out (the
  built-in and Chroma stores still work, and choosing LanceDB says "This cmcoder doesn't include
  LanceDB"); `--without-browser` leaves out Playwright; `--slim` is both.
- **Checked in the release build:** `packaging/smoke_rag.py <program>` runs the built program with
  Python and uv hidden: it builds an index of a small project, finds nothing to redo on the next
  run, searches through a scripted model, and checks the store's folder is private. It runs for
  LanceDB and for the built-in store.
