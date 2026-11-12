"""What waits to go with the user's next message: a page element picked with `/pick`.

Each one is a note for the model (`note`) and a line for the user (`label`), shown as a removable
chip above the message box (the `attachments` event) so nothing travels with a message unseen.
Plain data: LangChain has nothing for "notes that wait for the next message".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol import events as ev


@dataclass
class Attachment:
    kind: str  # "element"
    label: str  # what the chip says
    note: str  # what the model is told, before the user's words


def changed(items: list[Attachment]) -> ev.AttachmentsChanged:
    """The chips as the chat should draw them now."""
    return ev.AttachmentsChanged(
        items=[
            ev.AttachmentInfo(index=i, kind="element", label=a.label) for i, a in enumerate(items)
        ]
    )
