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
