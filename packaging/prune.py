"""Trim what the code index doesn't use from a built program's folder.

LanceDB needs `pyarrow`, and PyInstaller brings all of it: Flight (an RPC system),
the S3 and HDFS file systems, Parquet, ORC, Substrait, Gandiva and CUDA, each with a
big native library, plus Cython sources and C++ headers. The code index uses none of
them (LanceDB has its own file format and engine). On Linux PyInstaller also puts a
second copy of every arrow library next to the executable's other libraries.

    python packaging/prune.py dist/cmcoder

removes them (build.py does it after the build). It is safe because every one of those
imports in pyarrow is optional (`try: ... except ImportError`), and
`packaging/smoke_rag.py` runs the pruned program to prove it.

Two libraries stay although the index doesn't use them: pyarrow's own modules are
linked against Parquet's and Substrait's libraries, so the program wouldn't start
without them (found by trying: "libarrow_substrait.so: cannot open shared object file").
Only the Python modules that use them are removed.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# Words in a file's name (split at "_", "." and "-") that mark a part the index doesn't use.
UNUSED = {"flight", "s3", "s3fs", "parquet", "substrait", "orc", "hdfs", "gandiva", "cuda"}
NATIVE = (".so", ".pyd", ".dll", ".dylib")
# Libraries every pyarrow module is linked against: never removed.
LINKED = re.compile(r"^(lib)?(parquet|arrow_substrait)([._]|$)", re.IGNORECASE)
SOURCES = (".pyx", ".pxd", ".pxi")
DROP_FOLDERS = ("include", "tests")


def _words(name: str) -> set[str]:
    stem = re.sub(r"^lib", "", name.lower())
    return {w for w in re.split(r"[_.\-]", stem) if w}


def _unused_native(path: Path) -> bool:
    if not any(s in path.name for s in NATIVE) or LINKED.match(path.name):
        return False
    return bool(_words(path.name) & UNUSED)


def prune(folder: Path) -> tuple[list[Path], int]:
    """Remove the unused parts from a built program's folder (the one with `_internal`);
    returns what was removed and the bytes freed. A folder without pyarrow: nothing."""
    internal = folder / "_internal"
    root = internal if internal.is_dir() else folder
    arrow = root / "pyarrow"
    if not arrow.is_dir():
        return [], 0
    removed: list[Path] = []
    freed = 0

    def remove(path: Path) -> None:
        nonlocal freed
        if path.is_dir() and not path.is_symlink():
            freed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path)
        else:
            freed += path.stat().st_size if path.exists() else 0
            path.unlink()
        removed.append(path)

    for path in sorted(arrow.iterdir()):
        if (
            path.is_dir()
            and path.name in DROP_FOLDERS
            or path.is_file()
            and (_unused_native(path) or path.suffix in SOURCES)
        ):
            remove(path)
    # PyInstaller on Linux copies pyarrow's libraries to the folder above as well:
    # keep only the copy next to the extension modules, which find it by their RPATH.
    for path in sorted(root.iterdir()):
        if not path.is_file() or not any(s in path.name for s in NATIVE):
            continue
        twin = arrow / path.name
        same = twin.exists() and twin.stat().st_size == path.stat().st_size
        if (
            _unused_native(path)
            and re.match(r"^(lib)?(arrow|parquet)", path.name)
            or same
            and re.match(r"^(lib)?(arrow|parquet)", path.name)
        ):
            remove(path)
    return removed, freed


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    removed, freed = prune(Path(argv[0]))
    print(f"removed {len(removed)} files and folders, {freed / 1_048_576:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
