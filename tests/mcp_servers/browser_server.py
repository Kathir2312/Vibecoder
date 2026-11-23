"""A fake browser MCP server for the tests: tools named like Playwright's, a screenshot (text and
an image block), a very large page snapshot, and (FAKE_FILLER=N) N more tools.

Run: python browser_server.py
"""

from __future__ import annotations

import io
import os

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PilImage

server = FastMCP("fake-browser", instructions="A fake browser for cmcoder's tests.")


@server.tool()
def browser_navigate(url: str) -> str:
    """Go to an address."""
    return f"### Page\n- Page URL: {url}"


@server.tool()
def browser_snapshot() -> str:
    """Read the page as text."""
    return '- heading "Orders" [ref=e2]\n- button "Add" [ref=e3]'


@server.tool()
def browser_big_snapshot() -> str:
    """Read a page that has far too much on it."""
    return "- row " * 40_000  # 240,000 characters


@server.tool()
def browser_cookie_list() -> str:
    """List the cookies."""
    return "session=abc"


@server.tool()
def browser_take_screenshot():  # no return type: FastMCP then sends the blocks as they are
    """Take a picture of the page: some text, and the picture."""
    out = io.BytesIO()
    PilImage.new("RGB", (40, 20), (200, 30, 30)).save(out, format="PNG")
    return ["### Result\n- Screenshot of viewport", Image(data=out.getvalue(), format="png")]


for i in range(int(os.environ.get("FAKE_FILLER", "0"))):

    def filler(text: str = "") -> str:
        """A tool that does nothing useful, with a description long enough to cost tokens."""
        return text + " " + "filler " * 40

    server.tool(name=f"filler_tool_{i}", description=f"Filler tool number {i}. " + "Words. " * 60)(
        filler
    )


if __name__ == "__main__":
    server.run("stdio")
