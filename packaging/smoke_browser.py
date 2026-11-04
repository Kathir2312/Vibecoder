"""Smoke test of the Browser tool in a built program (no Python on the PATH).

    uv run python packaging/smoke_browser.py dist/cmcoder/cmcoder

It serves a small page on localhost and a scripted model (the mock server), then runs
the built `cmcoder -p` with Python and uv hidden (packaging/no_python.py), so what
runs is what a developer's PC would run: the bundled Playwright and its Node driver,
and the PC's own Chrome or Edge. The "model" opens the page, takes a snapshot and
clicks a button; the test checks that the page's text reached the model, that the
click worked, and that nothing started Python.

Needs Chrome, Edge or a Playwright Chromium on this machine; exits 0 with a note when
there is none (a machine like that can't run the tool at all), 1 when something fails.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "src"))

import no_python  # noqa: E402

from cmcoder.testing.mock_server import MockServer, MockState  # noqa: E402
from cmcoder.tools.browser import find_browser  # noqa: E402

API_KEY = "sk-smoke-key"
PAGE = (
    b"<!doctype html><title>Smoke page</title><h1>Smoke heading</h1>"
    b"<button onclick=\"document.getElementById('r').textContent='clicked ok'\">Press me</button>"
    b"<p id=r></p>"
)


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args: object) -> None:
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    exe = Path(argv[0]).resolve()
    if not exe.is_file():
        print(f"not found: {exe}", file=sys.stderr)
        return 2
    if find_browser()[0] is None:
        print("No Chrome, Edge or Playwright Chromium here: nothing to test. Skipped.")
        return 0
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{site.server_address[1]}"
    call = lambda **a: {"tool_calls": [{"name": "Browser", "arguments": a}]}  # noqa: E731
    script = [
        call(action="open", url=url),
        call(action="snapshot"),
        call(action="click", ref=1),
        call(action="snapshot"),
        call(action="close"),
        {"content": "Smoke test done."},
    ]
    server = MockServer(MockState(script, api_key=API_KEY))
    server.thread.start()
    with tempfile.TemporaryDirectory(prefix="cmcoder-smoke-") as tmp:
        tmp_path = Path(tmp)
        config = tmp_path / "config"
        config.mkdir()
        settings = {"providers": {"mock": {"baseUrl": server.base_url}}, "model": "mock:qwen3-27b"}
        (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        log = tmp_path / "started.log"
        env = no_python.environment(os.environ, no_python.trap(tmp_path / "trap"), log)
        env.update(
            CMCODER_CONFIG_DIR=str(config),
            CMCODER_API_KEY=API_KEY,
            CMCODER_SANDBOX="off",
        )
        done = subprocess.run(
            [str(exe), "-p", "open the page and press the button"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        python_started = no_python.started(log)
    server.httpd.shutdown()
    site.shutdown()
    seen = str(server.requests)
    offered = {t["function"]["name"] for t in server.requests[0].get("tools", [])}
    checks = {
        "the program exited normally": done.returncode == 0,
        "its answer came back": "Smoke test done." in done.stdout,
        "the Browser tool was offered to the model": "Browser" in offered,
        "the page's text reached the model": "Smoke heading" in seen,
        "the click worked": "clicked ok" in seen,
        "no Python was started": not python_started,
    }
    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not all(checks.values()):
        print("--- stdout ---\n" + done.stdout[-2000:], file=sys.stderr)
        print("--- stderr ---\n" + done.stderr[-2000:], file=sys.stderr)
        if python_started:
            print("Python started: " + ", ".join(python_started), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
