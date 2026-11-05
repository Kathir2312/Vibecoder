"""Smoke test of code search with LanceDB in a built program (no Python on the PATH).

    uv run python packaging/smoke_rag.py dist/cmcoder/cmcoder [--store lancedb|local]

Runs the built `cmcoder` with Python and uv hidden (packaging/no_python.py), against
the mock gateway (fake embeddings): `rag setup --index` builds the index of a small
project, `index` finds nothing to redo, `index --status` describes the store, and a
`-p` run with a scripted model calls the CodeSearch tool and gets the right file back.
It also checks that the store's files are where they should be and that the folder is
private. The default store is LanceDB, the point of the check: the bundled engine and
its native library working on a PC with no Python.

Exits 0 when everything passes, 1 when something fails.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "src"))

import no_python  # noqa: E402

from cmcoder.testing.mock_server import MockServer, MockState  # noqa: E402

API_KEY = "sk-smoke-key"
AUTH = '''"""Authentication helpers."""


def refresh_auth_token(session):
    """Refresh the expired auth token with the identity provider."""
    session.token = session.provider.renew(session.refresh_token)
    return session.token
'''
ERRORS = "class HTTPServerError(Exception): pass\n"
BILLING = '''"""Billing."""


def parse_invoice_total(lines):
    """Sum the amounts of an invoice."""
    return sum(float(x.split(";")[1]) for x in lines)
'''


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("exe")
    ap.add_argument("--store", choices=("lancedb", "local"), default="lancedb")
    args = ap.parse_args(argv)
    exe = Path(args.exe).resolve()
    if not exe.is_file():
        print(f"not found: {exe}", file=sys.stderr)
        return 2

    def search_call(query: str) -> dict[str, list[dict[str, object]]]:
        return {"tool_calls": [{"name": "CodeSearch", "arguments": {"query": query}}]}

    # The second search is by words inside a camelCase name: the made-up embeddings can't
    # match it, so finding errors.py proves the words index (LanceDB's hybrid search) runs.
    script = [
        search_call("refresh the auth token"),
        search_call("server error"),
        {"content": "Found it."},
    ]
    server = MockServer(MockState(script, api_key=API_KEY))
    server.thread.start()
    with tempfile.TemporaryDirectory(prefix="cmcoder-smoke-rag-") as tmp:
        tmp_path = Path(tmp)
        config = tmp_path / "config"
        config.mkdir()
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        (project / "auth.py").write_text(AUTH)
        (project / "billing.py").write_text(BILLING)
        (project / "errors.py").write_text(ERRORS)
        log = tmp_path / "started.log"
        env = no_python.environment(os.environ, no_python.trap(tmp_path / "trap"), log)
        env = {k: v for k, v in env.items() if not k.lower().endswith("_proxy")}
        env.update(
            CMCODER_CONFIG_DIR=str(config),
            CMCODER_BASE_URL=server.base_url,
            CMCODER_API_KEY=API_KEY,
            CMCODER_MODEL="qwen3-27b",
            CMCODER_SANDBOX="off",
            NO_COLOR="1",
        )

        def run(*a: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(exe), *a],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                timeout=240,
            )

        setup = run(
            "rag", "setup", "-m", "default:text-embedding-3-small", "--store", args.store,
            "--index", "--yes",
        )  # fmt: skip
        again = run("index")
        status = run("index", "--status")
        search = run("-p", "where is the token refreshed?")
        python_started = no_python.started(log)
        store_dirs = sorted(p for p in (config / "index").rglob(args.store) if p.is_dir())
        private = bool(store_dirs) and all(
            p.stat().st_mode & 0o077 == 0 for p in store_dirs if sys.platform != "win32"
        )
    server.httpd.shutdown()
    label = "LanceDB" if args.store == "lancedb" else "the built-in store"
    seen = str(server.requests)
    results = str(server.requests[-1]["messages"][-1])  # the second search's answer
    checks = {
        "setup and the first index worked": setup.returncode == 0
        and "Indexed 3 files" in setup.stdout,
        f"it uses {label}": ("LanceDB inside cmcoder" in setup.stdout) == (args.store == "lancedb"),
        "an unchanged project is not indexed again": "Indexed 0 files" in again.stdout
        and "3 unchanged" in again.stdout,
        "status describes the store": status.returncode == 0
        and ("LanceDB index" if args.store == "lancedb" else "local index") in status.stdout,
        "the store's folder exists and is private": private,
        "code search finds the right file": search.returncode == 0
        and "Found it." in search.stdout
        and "auth.py" in seen
        and "refresh_auth_token" in seen,
        "no Python was started": not python_started,
    }
    if args.store == "lancedb":
        checks["the words index finds a camelCase name by its words"] = "errors.py" in results
    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not all(checks.values()):
        for title, done in (
            ("setup", setup),
            ("index", again),
            ("status", status),
            ("search", search),
        ):
            print(
                f"--- {title}: exit {done.returncode} ---\n{done.stdout[-1200:]}", file=sys.stderr
            )
            print(done.stderr[-1200:], file=sys.stderr)
        if python_started:
            print("Python started: " + ", ".join(python_started), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
