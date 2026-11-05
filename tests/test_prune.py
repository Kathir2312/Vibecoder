"""packaging/prune.py: what is taken out of the built program's pyarrow, and what stays."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import prune  # noqa: E402


def make(folder: Path, files: dict[str, int]) -> None:
    for name, size in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def tree(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def test_linux_layout(tmp_path: Path) -> None:
    internal = tmp_path / "_internal"
    make(
        internal,
        {
            # what the index needs
            "pyarrow/lib.cpython-313-x86_64-linux-gnu.so": 40,
            "pyarrow/_dataset.cpython-313-x86_64-linux-gnu.so": 30,
            "pyarrow/_compute.cpython-313-x86_64-linux-gnu.so": 30,
            "pyarrow/libarrow.so.2600": 100,
            "pyarrow/libarrow_compute.so.2600": 50,
            "pyarrow/libarrow_python.so.2600": 20,
            "pyarrow/__init__.pyc": 5,
            # linked by every pyarrow module: stays although unused
            "pyarrow/libparquet.so.2600": 60,
            "pyarrow/libarrow_substrait.so.2600": 30,
            # unused
            "pyarrow/libarrow_flight.so.2600": 70,
            "pyarrow/libarrow_python_flight.so.2600": 10,
            "pyarrow/_flight.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/libarrow_s3.so.2600": 40,
            "pyarrow/_s3fs.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/_parquet.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/_dataset_parquet.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/libarrow_python_parquet_encryption.so.2600": 10,
            "pyarrow/_orc.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/_hdfs.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/_substrait.cpython-313-x86_64-linux-gnu.so": 10,
            "pyarrow/lib.pyx": 10,
            "pyarrow/_cuda.pxd": 10,
            "pyarrow/include/arrow/api.h": 10,
            "pyarrow/tests/test_x.py": 10,
            # a second copy of the libraries, next to the program's other libraries
            "libarrow.so.2600": 100,
            "libarrow_compute.so.2600": 50,
            "libparquet.so.2600": 60,
            "libarrow_flight.so.2600": 70,
            "libpython3.13.so.1.0": 200,
            "pyarrow_like_but_not.so": 5,
        },
    )
    removed, freed = prune.prune(tmp_path)
    assert tree(internal) == {
        "pyarrow/lib.cpython-313-x86_64-linux-gnu.so",
        "pyarrow/_dataset.cpython-313-x86_64-linux-gnu.so",
        "pyarrow/_compute.cpython-313-x86_64-linux-gnu.so",
        "pyarrow/libarrow.so.2600",
        "pyarrow/libarrow_compute.so.2600",
        "pyarrow/libarrow_python.so.2600",
        "pyarrow/__init__.pyc",
        "pyarrow/libparquet.so.2600",
        "pyarrow/libarrow_substrait.so.2600",
        "libpython3.13.so.1.0",  # not pyarrow's: untouched
        "pyarrow_like_but_not.so",
    }
    assert freed == 520 and len(removed) == 19  # 15 in pyarrow/, 4 second copies


def test_windows_and_macos_names(tmp_path: Path) -> None:
    make(
        tmp_path / "_internal",
        {
            "pyarrow/arrow.dll": 10,
            "pyarrow/arrow_flight.dll": 10,
            "pyarrow/arrow_substrait.dll": 10,
            "pyarrow/parquet.dll": 10,
            "pyarrow/lib.cp313-win_amd64.pyd": 10,
            "pyarrow/_flight.cp313-win_amd64.pyd": 10,
            "pyarrow/_s3fs.cp313-win_amd64.pyd": 10,
            "pyarrow/libarrow_flight.2600.dylib": 10,
            "pyarrow/libarrow.2600.dylib": 10,
            "pyarrow/libparquet.2600.dylib": 10,
        },
    )
    prune.prune(tmp_path)
    assert tree(tmp_path / "_internal") == {
        "pyarrow/arrow.dll",
        "pyarrow/arrow_substrait.dll",
        "pyarrow/parquet.dll",
        "pyarrow/lib.cp313-win_amd64.pyd",
        "pyarrow/libarrow.2600.dylib",
        "pyarrow/libparquet.2600.dylib",
    }


def test_a_build_without_pyarrow_is_left_alone(tmp_path: Path) -> None:
    make(tmp_path / "_internal", {"libpython3.13.so.1.0": 10, "base_library.zip": 10})
    assert prune.prune(tmp_path) == ([], 0)
    assert len(tree(tmp_path / "_internal")) == 2
    assert prune.prune(tmp_path / "missing") == ([], 0)


def test_running_it_twice_changes_nothing_more(tmp_path: Path) -> None:
    make(
        tmp_path / "_internal",
        {"pyarrow/lib.so": 10, "pyarrow/libarrow_flight.so.2600": 10, "libarrow.so.2600": 10},
    )
    prune.prune(tmp_path)
    assert prune.prune(tmp_path) == ([], 0)
