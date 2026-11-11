"""notes: a plain text file you can read yourself (data/notes.md).

`note_add` is a write tool: the model can only propose a note. FLINS shows you the exact text
and writes it after your yes. Secrets are refused. `notes_read` shows or searches the file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flins.assistant.memory import is_secret, tokens
from flins.assistant.tools.base import Tool, ToolResult, fail, ok

MAX_NOTE_CHARS = 500
SHOWN = 15


def _clean(text: str) -> str:
    return " ".join(text.split()).strip(" \"'")


def make_note_tools(path: Path, tz: ZoneInfo) -> list[Tool]:
    def lines() -> list[str]:
        if not path.is_file():
            return []
        return [
            ln[2:].strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("- ")
        ]

    async def add(arg: str, now: datetime) -> ToolResult:
        note = _clean(arg)
        if not note:
            return fail("There was nothing to write down.")
        if len(note) > MAX_NOTE_CHARS:
            return fail(f"That note is too long (over {MAX_NOTE_CHARS} characters).")
        if is_secret(note):
            return fail("I won't write that down: it looks like a password, PIN or code.")
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"- [{stamp}] {note}\n")
        return ok(f'Noted: "{note}".')

    async def read(arg: str, now: datetime) -> ToolResult:
        notes = lines()
        if not notes:
            return ok("The notes file is empty.")
        asked = tokens(arg.lower())
        if asked:
            hits = [n for n in notes if asked & tokens(n.lower())]
            if not hits:
                return ok(f"No note mentions '{_clean(arg)[:60]}'. There are {len(notes)} notes.")
            shown = hits[-SHOWN:]
            return ok(f"{len(hits)} matching note(s):\n" + "\n".join(shown))
        shown = notes[-SHOWN:]
        return ok(f"{len(notes)} note(s); the latest {len(shown)}:\n" + "\n".join(shown))

    def confirm(arg: str) -> str:
        return f'Add this to your notes: "{_clean(arg)[:MAX_NOTE_CHARS]}"?'

    return [
        Tool(
            name="note_add",
            description=(
                "Write one note into the user's notes file, when they ask you to note or jot "
                "something down. The user is asked to confirm before it is written."
            ),
            usage="the note text, one or two sentences, in their words.",
            risk="write",
            run=add,
            confirm=confirm,
        ),
        Tool(
            name="notes_read",
            description="Read the user's notes file, or find notes that mention something.",
            usage="words to look for, or empty for the latest notes.",
            risk="read",
            run=read,
        ),
    ]
