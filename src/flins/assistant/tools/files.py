"""files: read-only access to folders you approved (`assistant.tools.folders`).

Rules, all enforced here in code:
- A path is resolved first (following `..` and links), then must be inside an approved folder.
- Hidden files and folders, key / credential files and anything that isn't a known text type
  are refused. So is a file whose content looks like it holds credentials.
- Nothing is ever written, moved or deleted.
- What a file says is outside content: the core marks the turn as tainted after a read.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

from flins.assistant.tools.base import Tool, ToolResult, fail, ok

TEXT_TYPES = frozenset(
    [
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".tex",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".log",
        ".py",
        ".ipynb",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".htm",
        ".css",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
        ".xml",
        ".srt",
    ]
)
_BLOCKED_NAME = re.compile(
    r"passw|secret|credential|token|id_rsa|id_ed25519|private[_-]?key|\.env\b|wallet|keystore"
    r"|recovery|seed[_-]?phrase|\b2fa\b|backup[_-]?codes?",
    re.IGNORECASE,
)
_BLOCKED_TYPES = frozenset(
    [".pem", ".key", ".pfx", ".p12", ".kdbx", ".jks", ".keystore", ".ppk", ".asc", ".gpg"]
)
_CREDENTIALS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b"
    r"\s*[:=]\s*['\"]?[^\s'\"]{6,}"
    r"|\b(?:sk|gsk|ghp|gho|xox[baprs]|nvapi|AKIA)[-_][A-Za-z0-9_\-]{16,}",
    re.IGNORECASE,
)
MAX_ENTRIES = 60
MAX_HITS = 10
MAX_SCAN_FILES = 3000
MAX_READ_CHARS = 3600
SEARCH_BUDGET_S = 3.0


class FileError(ValueError):
    pass


class Folders:
    """The approved folders, and every check a path must pass."""

    def __init__(self, folders: list[Path], *, max_kb: int = 200, deny: list[Path] | None = None):
        self.roots = [f.resolve() for f in folders if f.is_dir()]
        self._max_bytes = max_kb * 1024
        # Never readable, even if an approved folder contains them (FLINS's own data, .env).
        self._deny = [d.resolve() for d in (deny or [])]

    def _inside(self, p: Path) -> bool:
        if any(p == d or d in p.parents for d in self._deny):
            return False
        return any(p == r or r in p.parents for r in self.roots)

    def _hidden(self, p: Path) -> bool:
        for root in self.roots:
            if p == root or root in p.parents:
                return any(part.startswith(".") for part in p.relative_to(root).parts)
        return True

    def resolve(self, raw: str) -> Path:
        """A path the user (or the model) gave -> a real path inside an approved folder."""
        text = raw.strip().strip("\"'")
        if not text:
            raise FileError("no path was given")
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            options = [candidate]
        else:
            options = [r / candidate for r in self.roots]
            # Paths are shown back as "<folder name>/<path inside it>": accept that form too.
            parts = candidate.parts
            options += [r.joinpath(*parts[1:]) for r in self.roots if parts[0] == r.name]
        for option in options:
            try:
                real = option.resolve()
            except OSError:
                continue
            if self._inside(real) and real.exists():
                if self._hidden(real):
                    raise FileError("hidden files and folders are off limits")
                return real
        if candidate.is_absolute():
            raise FileError("that path is outside the folders you approved, or doesn't exist")
        raise FileError(f"I can't find '{text}' in the approved folders")

    def readable(self, p: Path) -> None:
        """Raises FileError unless `p` is a text file FLINS may read."""
        if not p.is_file():
            raise FileError("that is not a file")
        if p.suffix.lower() in _BLOCKED_TYPES or _BLOCKED_NAME.search(p.name):
            raise FileError("that looks like a key or credentials file, so I won't open it")
        if p.suffix.lower() not in TEXT_TYPES:
            raise FileError(f"I can only read text files, not '{p.suffix or 'this type'}'")
        if p.stat().st_size > self._max_bytes:
            raise FileError(f"that file is larger than {self._max_bytes // 1024} KB")

    def read(self, p: Path) -> str:
        self.readable(p)
        text = p.read_bytes().decode("utf-8", errors="replace")
        if "\x00" in text[:2000]:
            raise FileError("that file isn't plain text")
        if _CREDENTIALS.search(text):
            raise FileError("that file seems to contain credentials, so I won't read it out")
        return text

    def shown(self, p: Path) -> str:
        """The path as shown back: relative to its approved folder, prefixed by its name."""
        for root in self.roots:
            if p == root:
                return root.name
            if root in p.parents:
                return f"{root.name}/{p.relative_to(root).as_posix()}"
        return p.name

    def walk(self) -> list[Path]:
        """Visible files under the approved folders (bounded)."""
        out: list[Path] = []
        for root in self.roots:
            stack = [root]
            while stack and len(out) < MAX_SCAN_FILES:
                folder = stack.pop()
                try:
                    entries = sorted(folder.iterdir(), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for e in entries:
                    if e.name.startswith(".") or e.name in ("node_modules", "__pycache__"):
                        continue
                    # Links are not followed (no loops, no way out of the folder), and
                    # whatever a path really points at must still be inside.
                    if e.is_symlink() or not self._inside(e.resolve()):
                        continue
                    if e.is_dir():
                        stack.append(e)
                    elif e.is_file():
                        out.append(e)
        return out


def _list(folders: Folders, arg: str) -> ToolResult:
    if not arg.strip():
        roots = "\n".join(f"{r.name}/  ({r})" for r in folders.roots)
        return ok("Approved folders:\n" + roots)
    try:
        target = folders.resolve(arg)
    except FileError as exc:
        return fail(f"Can't list that: {exc}.")
    if not target.is_dir():
        return fail("That is a file, not a folder. Use files_read for it.")
    entries = [e for e in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))]
    entries = [e for e in entries if not e.name.startswith(".")]
    lines = [f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size // 1024 + 1} KB)"
             for e in entries[:MAX_ENTRIES]]  # fmt: skip
    more = f"\n… and {len(entries) - MAX_ENTRIES} more" if len(entries) > MAX_ENTRIES else ""
    return ok(f"{folders.shown(target)} has {len(entries)} item(s):\n" + "\n".join(lines) + more)


def _search(folders: Folders, arg: str) -> ToolResult:
    words = [w for w in re.findall(r"[\w.\-]+", arg.lower()) if len(w) >= 2]
    if not words:
        return fail("Say what to look for.")
    started = time.monotonic()
    by_name: list[str] = []
    by_text: list[str] = []
    cut = False
    for f in folders.walk():
        if time.monotonic() - started > SEARCH_BUDGET_S:
            cut = True
            break
        name = f.name.lower()
        if all(w in name for w in words):
            by_name.append(folders.shown(f))
            continue
        if len(by_text) >= MAX_HITS:
            continue
        try:
            text = folders.read(f)
        except (FileError, OSError):
            continue
        low = text.lower()
        if all(w in low for w in words):
            at = low.find(words[0])
            line = text[max(0, text.rfind("\n", 0, at) + 1) :].splitlines()[0].strip()
            by_text.append(f"{folders.shown(f)}: {line[:140]}")
    if not by_name and not by_text:
        note = " (I ran out of time before checking everything.)" if cut else ""
        return ok(f"Nothing in the approved folders matches '{arg.strip()[:60]}'.{note}")
    out = []
    if by_name:
        out.append("Files with that name:\n" + "\n".join(by_name[:MAX_HITS]))
    if by_text:
        out.append("Files that mention it:\n" + "\n".join(by_text[:MAX_HITS]))
    if cut:
        out.append("(Stopped early: the folders are large.)")
    return ok("\n".join(out))


def _read(folders: Folders, arg: str) -> ToolResult:
    try:
        text = folders.read(folders.resolve(arg))
    except FileError as exc:
        return fail(f"Can't read that: {exc}.")
    except OSError:
        return fail("Can't read that: the file couldn't be opened.")
    name = folders.shown(folders.resolve(arg))
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS].rstrip() + f"\n… (the first {MAX_READ_CHARS} characters)"
    return ok(f"Content of {name}:\n{text}")


def make_file_tools(folders: Folders) -> list[Tool]:
    """Empty when no folder is approved: the tools then don't exist at all."""
    if not folders.roots:
        return []

    async def run_list(arg: str, now: datetime) -> ToolResult:
        return await asyncio.to_thread(_list, folders, arg)

    async def run_search(arg: str, now: datetime) -> ToolResult:
        return await asyncio.to_thread(_search, folders, arg)

    async def run_read(arg: str, now: datetime) -> ToolResult:
        return await asyncio.to_thread(_read, folders, arg)

    names = ", ".join(r.name for r in folders.roots)
    return [
        Tool(
            name="files_list",
            description=f"List what is in one of the user's approved folders ({names}).",
            usage="a folder path like 'Notes/physics', or empty to see the approved folders.",
            risk="private",
            run=run_list,
            outside_content=True,
        ),
        Tool(
            name="files_search",
            description="Find files in the approved folders by name or by words they contain.",
            usage="the words to look for, e.g. 'thermodynamics formula'.",
            risk="private",
            run=run_search,
            outside_content=True,
        ),
        Tool(
            name="files_read",
            description="Read one text file from the approved folders.",
            usage="the file path as files_list or files_search showed it, e.g. 'Notes/todo.md'.",
            risk="private",
            run=run_read,
            outside_content=True,
        ),
    ]
