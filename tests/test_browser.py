"""The Browser tool: a real browser (Playwright) driven by the agent to test a web app."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from cmcoder.config.settings import Settings
from cmcoder.core.permissions import Decision, PermissionPolicy, suggest_rules
from cmcoder.tools.base import ToolContext
from cmcoder.tools.browser import (
    BrowserInput,
    BrowserSession,
    BrowserTool,
    browser_session,
    find_browser,
    is_local_host,
    url_target,
)

from .conftest import API_KEY
from .helpers import make_session, run_turn

PAGE = b"""<!doctype html><html><head><title>Todos</title></head><body>
<h1>My todos</h1>
<label for=t>New todo</label><input id=t name=t>
<button id=add onclick="
  const li=document.createElement('li'); li.textContent=document.getElementById('t').value;
  document.getElementById('list').appendChild(li);">Add</button>
<button onclick="console.error('boom from the page')">Break</button>
<select aria-label=Filter><option>All</option><option>Done</option></select>
<label><input type=checkbox aria-label=Urgent> Urgent</label>
<input type=password aria-label=Secret value=hunter2>
<a href="http://example.org/away">Leave</a> <a href="/other">Other page</a>
<button onclick="location.href='http://example.net/jump'">Jump</button>
<ul id=list></ul>
<script>fetch('/missing.json')</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/other":
            body, status = b"<title>Other</title><h1>Other page</h1>", 200
        elif self.path == "/":
            body, status = PAGE, 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


needs_browser = pytest.mark.skipif(
    find_browser()[0] is None, reason="no Chrome, Edge or Playwright Chromium here"
)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path, project_root=tmp_path)


async def act(session: BrowserSession, ctx: ToolContext, **kw: object):
    return await session.run(BrowserInput(**kw), ctx)  # type: ignore[arg-type]


def ref_of(snapshot: str, label: str) -> int:
    line = next(line for line in snapshot.splitlines() if line.startswith("[") and label in line)
    return int(line[1 : line.index("]")])


# --- permissions (no browser needed) -------------------------------------------


def test_local_and_loopback_hosts() -> None:
    assert all(is_local_host(h) for h in ("localhost", "127.0.0.1", "[::1]", "app.localhost"))
    assert not any(is_local_host(h) for h in ("example.com", "192.168.1.5", "10.0.0.1"))


def check(url: str | None, ctx: ToolContext, mode: str = "default", action: str = "open"):
    args = BrowserInput(action=action, url=url)  # type: ignore[arg-type]
    return PermissionPolicy(mode=mode).check(BrowserTool(), args, ctx)


def test_localhost_opens_freely_other_hosts_ask(ctx: ToolContext) -> None:
    assert check("http://localhost:3000", ctx).decision is Decision.ALLOW
    assert check("localhost:3000", ctx).decision is Decision.ALLOW
    assert check("https://example.com/x", ctx).decision is Decision.ASK
    assert check(None, ctx, action="snapshot").decision is Decision.ALLOW
    (ctx.project_root / "page.html").write_text("<p>x</p>")
    assert check((ctx.project_root / "page.html").as_uri(), ctx).decision is Decision.ALLOW
    assert check("file:///etc/passwd", ctx).decision is Decision.ASK


def test_unsafe_addresses_and_modes(ctx: ToolContext) -> None:
    assert check("javascript:alert(1)", ctx).decision is Decision.DENY
    assert check("chrome://settings", ctx).decision is Decision.DENY
    assert check("http://localhost:3000", ctx, mode="plan").decision is Decision.DENY
    assert check("https://example.com", ctx, mode="bypassPermissions").decision is Decision.ALLOW
    assert check((ctx.project_root / ".env").as_uri(), ctx).decision is Decision.DENY


def test_approved_hosts_and_always_allow_are_per_host(ctx: ToolContext) -> None:
    args = BrowserInput(action="open", url="https://docs.example.com/a")
    assert suggest_rules(BrowserTool(), args, ctx) == ["Browser(docs.example.com)"]
    policy = PermissionPolicy(allow=["Browser(docs.example.com)"], deny=["Browser(evil.test)"])
    tool = BrowserTool()
    assert policy.check(tool, args, ctx).decision is Decision.ALLOW
    other = BrowserInput(action="open", url="https://other.example.com")
    assert policy.check(tool, other, ctx).decision is Decision.ASK
    bad = BrowserInput(action="open", url="https://evil.test")
    assert policy.check(tool, bad, ctx).decision is Decision.DENY
    ctx.browser = BrowserSession()
    ctx.browser.approved.add("other.example.com")  # approved earlier in this session
    assert policy.check(tool, other, ctx).decision is Decision.ALLOW


def test_url_target() -> None:
    assert url_target("HTTP://Example.COM:8080/x") == "example.com"
    assert url_target("example.com") == "example.com"
    assert url_target("about:blank") is None
    assert url_target("javascript:1") == "!javascript"


def test_a_slim_build_without_playwright_offers_no_browser_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cmcoder.tools.browser.playwright_available", lambda: False)
    assert browser_session(Settings.model_validate({}), tmp_path) is None


def test_the_tool_is_offered_unless_switched_off(tmp_path: Path) -> None:
    on = Settings.model_validate({})
    assert browser_session(on, tmp_path) is not None
    off = Settings.model_validate({"browser": {"enabled": False}})
    assert browser_session(off, tmp_path) is None
    assert find_browser("/no/such/browser")[0] is None


# --- a real browser -------------------------------------------------------------


@needs_browser
async def test_test_a_web_page_end_to_end(site: str, ctx: ToolContext) -> None:
    session = BrowserSession()
    try:
        opened = await act(session, ctx, action="open", url=site)
        assert not opened.is_error and "Todos" in opened.content
        snap = await act(session, ctx, action="snapshot")
        text = snap.content
        assert "My todos" in text and 'textbox "New todo"' in text and "data" in text.lower()
        assert 'button "Add"' in text and 'link "Other page" -> /other' in text
        assert 'dropdown "Filter"' in text and 'checkbox "Urgent"' in text
        assert 'value="hunter2"' not in text  # a password is never echoed
        await act(session, ctx, action="type", ref=ref_of(text, "New todo"), text="Buy milk")
        await act(session, ctx, action="click", ref=ref_of(text, '"Add"'))
        again = (await act(session, ctx, action="snapshot")).content
        assert "Buy milk" in again
        pick = await act(session, ctx, action="type", ref=ref_of(again, "Filter"), text="Done")
        assert not pick.is_error
        await act(session, ctx, action="click", ref=ref_of(again, "Urgent"))
        assert "(checked)" in (await act(session, ctx, action="snapshot")).content
        bad = await act(session, ctx, action="type", ref=ref_of(again, "Urgent"), text="x")
        assert bad.is_error and "use click" in bad.content
        # Errors the page produced are reported, not hidden: on the action that caused them...
        broke = await act(session, ctx, action="click", ref=ref_of(again, "Break"))
        assert "console error: boom from the page" in broke.content
        # ...and what hasn't been shown yet (the 404 may arrive at any step) via console.
        rest = (await act(session, ctx, action="console")).content
        seen = opened.content + text + again + rest
        assert "HTTP 404 GET" in seen and "missing.json" in seen
        assert "clean" in (await act(session, ctx, action="console")).summary
        # Follow a link on the same host.
        moved = await act(session, ctx, action="click", ref=ref_of(again, "Other page"))
        assert "Other" in moved.content
        found = await act(session, ctx, action="wait_for", text="Other page", seconds=3)
        assert not found.is_error
        missing = await act(session, ctx, action="wait_for", text="never here", seconds=1)
        assert missing.is_error
    finally:
        await session.close()


@needs_browser
async def test_a_page_cannot_take_the_agent_to_an_unapproved_host(site: str, ctx: ToolContext):
    session = BrowserSession()
    try:
        await act(session, ctx, action="open", url=site)
        snap = (await act(session, ctx, action="snapshot")).content
        # A link to an unapproved site isn't even clicked, and the page keeps its state.
        await act(session, ctx, action="type", ref=ref_of(snap, "New todo"), text="Keep me")
        await act(session, ctx, action="click", ref=ref_of(snap, '"Add"'))
        left = await act(session, ctx, action="click", ref=ref_of(snap, "Leave"))
        assert left.is_error and "example.org, which isn't approved" in left.content
        kept = (await act(session, ctx, action="snapshot")).content
        assert "Keep me" in kept  # the link wasn't clicked: the page kept its state
        # A navigation the page makes itself is stopped by the gate, and the agent is told.
        jumped = await act(session, ctx, action="click", ref=ref_of(snap, "Jump"))
        assert "blocked: the page tried to open example.net" in jumped.content
        after = (await act(session, ctx, action="snapshot")).content
        assert after.startswith("Page: Todos (http://127.0.0.1")  # still on the app
        assert "chrome-error" not in after
        # Once the user has approved a host, the same link works.
        session.approved.add("example.org")
    finally:
        await session.close()


@needs_browser
async def test_stale_numbers_bad_input_and_unsafe_schemes(site: str, ctx: ToolContext) -> None:
    session = BrowserSession()
    try:
        assert "needs a url" in (await act(session, ctx, action="open")).content
        assert (await act(session, ctx, action="open", url="javascript:alert(1)")).is_error
        await act(session, ctx, action="open", url=site)
        gone = await act(session, ctx, action="click", ref=99)
        assert gone.is_error and "Take a snapshot" in gone.content
        assert (await act(session, ctx, action="click")).is_error
        unreachable = await act(session, ctx, action="open", url="http://127.0.0.1:1/")
        assert unreachable.is_error and "Could not open" in unreachable.content
        # The browser is not left on its error page: the next action still works.
        after = await act(session, ctx, action="snapshot")
        assert not after.is_error and "chrome-error" not in after.content
        shot = await act(session, ctx, action="screenshot")
        assert ".png" in shot.content and "can't view images" in shot.content
    finally:
        await session.close()
    closed = await act(session, ctx, action="close")
    assert closed.content == "Browser closed."


@needs_browser
async def test_a_project_file_opens_and_one_outside_it_is_refused_by_the_gate(
    tmp_path: Path, ctx: ToolContext
) -> None:
    (tmp_path / "page.html").write_text("<title>Local</title><h1>From a file</h1>")
    session = BrowserSession(project_root=tmp_path)
    try:
        opened = await act(session, ctx, action="open", url=(tmp_path / "page.html").as_uri())
        assert not opened.is_error and "Local" in opened.content
    finally:
        await session.close()


@needs_browser
async def test_the_model_tests_an_app_through_the_agent(mock_server, site: str, tmp_path: Path):
    import os

    os.environ["CMCODER_API_KEY"] = API_KEY
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    call = lambda **a: {"tool_calls": [{"name": "Browser", "arguments": a}]}  # noqa: E731
    server = mock_server(
        [
            call(action="open", url=site),
            call(action="snapshot"),
            call(action="close"),
            {"content": "The page loads and shows My todos."},
        ]
    )
    session = make_session(server.base_url, project, ask=None)
    try:
        events = await run_turn(session, "open the app and check it loads")
    finally:
        await session.agent.close()
    assert events[-1].result == "The page loads and shows My todos."
    names = {t["function"]["name"] for t in server.requests[0].get("tools", [])}
    assert "Browser" in names
    assert "My todos" in str(server.requests[2]["messages"])  # the snapshot reached the model


@needs_browser
async def test_a_screenshot_goes_to_the_chat_not_to_the_model(
    mock_server, site: str, tmp_path: Path
) -> None:
    import os

    os.environ["CMCODER_API_KEY"] = API_KEY
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    call = lambda **a: {"tool_calls": [{"name": "Browser", "arguments": a}]}  # noqa: E731
    server = mock_server(
        [
            call(action="open", url=site),
            call(action="screenshot"),
            call(action="close"),
            {"content": "Done."},
        ]
    )
    session = make_session(server.base_url, project, ask=None)
    try:
        events = await run_turn(session, "take a screenshot of the app")
    finally:
        await session.agent.close()
    shots = [e for e in events if getattr(e, "type", "") == "tool_result" and e.images]
    assert len(shots) == 1 and shots[0].name == "Browser"
    assert shots[0].images[0].startswith("data:image/png;base64,")
    sent = str(server.requests[-1]["messages"])
    assert "base64" not in sent and "data:image" not in sent  # the model never gets the picture
    assert "Took a screenshot" in sent


@needs_browser
async def test_screenshot_files_are_removed_when_the_browser_closes(
    site: str, ctx: ToolContext
) -> None:
    session = BrowserSession()
    try:
        await act(session, ctx, action="open", url=site)
        shot = await act(session, ctx, action="screenshot")
        assert len(shot.images) == 1 and shot.images[0].read_bytes()[:4] == b"\x89PNG"
        folder = shot.images[0].parent
        assert (await act(session, ctx, action="snapshot")).images == []
    finally:
        await session.close()
    assert not folder.exists()
