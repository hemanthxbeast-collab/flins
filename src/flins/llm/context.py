"""Turn stored messages into prompt text, treating every message as untrusted data.

- People appear by name only. Phone numbers and JIDs never reach the LLM.
- `<` and `>` in message text are replaced, so a message can't close or open our tags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_ANGLE = str.maketrans({"<": "‹", ">": "›"})  # ‹ ›


def neutralise(text: str | None) -> str:
    return (text or "").translate(_ANGLE).strip()


@dataclass(frozen=True)
class Line:
    when: datetime
    from_me: bool
    text: str | None
    msg_type: str = "text"


def _render(lines: list[Line], contact: str, tz: ZoneInfo) -> str:
    out = []
    for line in lines:
        who = "Me" if line.from_me else contact
        body = neutralise(line.text)
        if line.msg_type != "text":
            body = f"[{line.msg_type}] {body}".strip()
        out.append(f"[{line.when.astimezone(tz):%H:%M}] {who}: {body}")
    return "\n".join(out) or "(nothing)"


def render_conversation(
    *, earlier: list[Line], new: list[Line], contact_name: str, tz: ZoneInfo
) -> str:
    name = neutralise(contact_name) or "Contact"
    return (
        "<untrusted_thread>\n"
        f"{_render(earlier, name, tz)}\n"
        "<new_messages>\n"
        f"{_render(new, name, tz)}\n"
        "</new_messages>\n"
        "</untrusted_thread>"
    )
