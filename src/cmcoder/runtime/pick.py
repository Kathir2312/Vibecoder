"""`/pick`: point at an element on a web page; it goes to cmcoder with the next message.

The command picks in VS Code's Integrated Browser (the IDE's own browser tools, which must include
one that runs code in the page), waits for the click and keeps the element for the next message
(`Session.attachments`, which `Session._run` adds to the note the model gets). cmcoder has no
browser of its own, so without those tools there is nothing to pick in.

LANGCHAIN / LANGGRAPH: nothing here; see tools/pick.py.
"""

from __future__ import annotations

from ..tools.pick import MAX_WAITING, PickError, code_tool, pick_in_ide
from .attachments import Attachment
from .session import Session

USAGE: tuple[str, ...] = (
    "/pick           point at an element on the page that is open in VS Code's browser",
    "/pick clear     drop what is waiting for your next message",
    "/pick drop <n>  drop one of them (the number on its chip, from 0)",
)


def _waiting(session: Session) -> str:
    n = len(session.attachments)
    return "It goes" if n == 1 else f"All {n} go"


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
    ide = session.agent.spec.get("ide_browser")
    try:
        if arg:
            raise PickError(
                "/pick takes no address: open the page in VS Code's browser, then use /pick."
            )
        if len(session.attachments) >= MAX_WAITING:
            raise PickError(
                f"{MAX_WAITING} elements are already waiting: send your message, or /pick clear."
            )
        if not ide:
            raise PickError(
                "/pick works in VS Code's Integrated Browser, and this chat has no IDE browser "
                "(it needs VS Code 1.110 or newer with workbench.browser.enableChatTools on)."
            )
        if code_tool(ide) is None:
            raise PickError(
                "VS Code's browser tools here can't run code in the page, which picking needs."
            )
        element = await pick_in_ide(ide)
    except PickError as e:
        return [str(e)]
    session.attachments.append(Attachment("element", element.label(), element.note()))
    return [
        f"Picked {element.label()}",
        f"{_waiting(session)} with your next message to cmcoder (/pick again for another, /pick clear to drop).",
    ]
