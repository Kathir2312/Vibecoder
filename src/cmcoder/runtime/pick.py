"""`/pick`: point at an element on a web page; it goes to cmcoder with the next message.

The command works out where to pick (VS Code's Integrated Browser when the IDE has one and a way to
run code in it; else cmcoder's own browser window), waits for the click and keeps the element for
the next message (`Session.attachments`, which `Session._run` adds to the note the model gets).
The rules for which addresses cmcoder opens by itself are the Browser tool's: this one only opens
what that tool would open without asking (your own machine, a file in the project, a host you
already approved); any other address is opened by asking cmcoder to, which asks you first.

LANGCHAIN / LANGGRAPH: nothing here; see tools/pick.py.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..tools.base import ToolContext
from ..tools.browser import (
    BrowserInput,
    BrowserSession,
    is_local_host,
    normalize_url,
    url_target,
)
from ..tools.pick import (
    MAX_WAITING,
    PICK_TIMEOUT,
    PickedElement,
    PickError,
    code_tool,
    pick_in_ide,
)
from .attachments import Attachment
from .session import Session

USAGE: tuple[str, ...] = (
    "/pick           point at an element on the page that is open in the browser",
    "/pick <address> open that page in cmcoder's browser window and point at an element there",
    "/pick clear     drop what is waiting for your next message",
    "/pick drop <n>  drop one of them (the number on its chip, from 0)",
)


def _waiting(session: Session) -> str:
    n = len(session.attachments)
    return "It goes" if n == 1 else f"All {n} go"


async def _own_browser(browser: BrowserSession, ctx: ToolContext, address: str) -> PickedElement:
    """Point at an element in cmcoder's own browser, opening `address` first when given."""
    if address:
        url = normalize_url(address)
        target = url_target(url)
        if isinstance(target, str) and target.startswith("!"):
            raise PickError("Only http, https and file addresses can be opened.")
        if isinstance(target, str):
            host = urlparse(url).hostname or ""
            if not (is_local_host(host) or host.lower() in browser.approved):
                raise PickError(
                    f"{host} isn't a page cmcoder opens by itself. Ask cmcoder to open {url} "
                    "(it asks you first), then use /pick."
                )
        elif target is not None and not browser._inside_project(target):  # a file elsewhere
            raise PickError("cmcoder opens only files inside this project.")
        await browser.show()  # a person has to be able to click
        opened = await browser.run(BrowserInput.model_validate({"action": "open", "url": url}), ctx)
        if opened.is_error:
            raise PickError(opened.content)
    elif browser.page is None or browser.page.url in ("", "about:blank"):
        raise PickError(
            "No page is open yet. Use /pick <address of your app>, for example "
            "/pick http://localhost:5080, or ask cmcoder to open the page first."
        )
    elif browser.headless:
        raise PickError(
            "cmcoder's browser is running without a window (browser.headless in the settings), "
            "so there is nothing to click. /pick <address> opens the page in a window."
        )
    return await browser.pick(PICK_TIMEOUT)


async def pick_lines(session: Session, arg: str) -> list[str]:
    """Run `/pick`: what to show the user."""
    if arg == "clear":
        n = len(session.attachments)
        session.attachments.clear()
        return (
            [f"Dropped {n} picked element{'s' if n != 1 else ''}."]
            if n
            else ["Nothing picked is waiting."]
        )
    if arg == "drop" or arg.startswith("drop "):  # one of them, by the number on its chip
        try:
            which = int(arg.split()[1])
            dropped = session.attachments.pop(which)
        except (IndexError, ValueError):
            return ["Usage: /pick drop <number of a picked element>"]
        return [f"Dropped {dropped.label}"]
    if arg in ("help", "?"):
        return list(USAGE)
    a = session.agent
    ide = a.spec.get("ide_browser")
    browser: BrowserSession | None = a.ctx.browser
    try:
        if len(session.attachments) >= MAX_WAITING:
            raise PickError(
                f"{MAX_WAITING} elements are already waiting: send your message, or /pick clear."
            )
        if arg:  # an address always means cmcoder's own window
            if browser is None:
                raise PickError(
                    "cmcoder's browser is off here (browser.enabled), or this build has no Playwright."
                )
            element = await _own_browser(browser, a.ctx, arg)
        elif ide and code_tool(ide) is not None:
            element = await pick_in_ide(ide)
        elif ide:
            raise PickError(
                "VS Code's browser tools here can't run code in the page, which picking needs. "
                "/pick <address> picks in cmcoder's own browser window instead."
            )
        elif browser is not None:
            element = await _own_browser(browser, a.ctx, "")
        else:
            raise PickError(
                "There is no browser to pick in: cmcoder's is off here (browser.enabled)."
            )
    except PickError as e:
        return [str(e)]
    session.attachments.append(Attachment("element", element.label(), element.note()))
    return [
        f"Picked {element.label()}",
        f"{_waiting(session)} with your next message to cmcoder (/pick again for another, /pick clear to drop).",
    ]
