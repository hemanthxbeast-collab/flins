"""Runs ONE skill inside a fresh child process (SPEC §9.3). Standard library only.

Started by `runtime.py` as `python -I -B -m flins.skills.worker_main` with an empty environment
and the run folder as working directory. The protocol is JSON lines on stdin/stdout:

    core -> worker  {"type": "run", "skill_dir", "module", "function", "run_dir", "input"}
    worker -> core  {"type": "call", "id", "method", "params"}      (a ctx.* request)
    core -> worker  {"type": "result", "id", "ok", "value" | "error"}
    worker -> core  {"type": "done", "output"} | {"type": "error", "error"}

The core checks every ctx call against the manifest and validates the output, so it doesn't
trust this process. The audit hook below is a second wall: no sockets, no subprocesses, no
ctypes, no writes outside the run folder, no reads outside the skill and Python's own files.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import traceback
from collections.abc import Callable
from typing import Any, BinaryIO

BANNED_MODULES = frozenset(
    {
        "socket",
        "_socket",
        "ssl",
        "_ssl",
        "select",
        "selectors",
        "subprocess",
        "_posixsubprocess",
        "multiprocessing",
        "_multiprocessing",
        "ctypes",
        "_ctypes",
        "_winapi",
        "winreg",
        "_winreg",
        "msvcrt",
        "asyncio",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "telnetlib",
        "requests",
        "httpx",
        "aiohttp",
        "sqlite3",
        "_sqlite3",
        "flins",
    }
)
_BLOCKED_EVENTS = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.startfile",
    "os.fork",
    "os.kill",
    "os.chdir",
    "ctypes.",
    "winreg.",
    "_winapi.",
    "builtins.input",
    "webbrowser.",
    "sqlite3.",
)
_PATH_EVENTS = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "shutil.rmtree",
        "shutil.copyfile",
        "shutil.move",
    }
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class SandboxViolation(PermissionError):
    pass


class CapabilityError(RuntimeError):
    """A ctx call was denied or failed. Skills may catch it and carry on."""


def _norm(path: object) -> str | None:
    if isinstance(path, int) or path is None:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.fsdecode(path)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r.rstrip("\\/") + os.sep) for r in roots)


def install_sandbox(read_roots: list[str], write_roots: list[str]) -> None:
    reads = tuple(n for n in (_norm(r) for r in read_roots + write_roots) if n)
    writes = tuple(n for n in (_norm(r) for r in write_roots) if n)
    # Already-loaded modules wouldn't trigger the import check: unload the banned ones.
    for name in list(sys.modules):
        if name.split(".")[0] in BANNED_MODULES:
            del sys.modules[name]

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "import":
            top = str(args[0]).split(".")[0]
            if top in BANNED_MODULES:
                raise SandboxViolation(f"import of {args[0]!r} is not allowed in a skill")
            return
        if event.startswith(_BLOCKED_EVENTS):
            raise SandboxViolation(f"{event} is not allowed in a skill")
        if event == "open":
            path = _norm(args[0])
            if path is None:
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = (
                any(c in mode for c in "wax+")
                if isinstance(mode, str)
                else bool(flags & _WRITE_FLAGS)
            )
            if writing and not _under(path, writes):
                raise SandboxViolation(f"writing {args[0]!r} is not allowed in a skill")
            if not _under(path, reads):
                raise SandboxViolation(f"reading {args[0]!r} is not allowed in a skill")
            return
        if event in ("os.listdir", "os.scandir"):
            path = _norm(args[0]) if args and args[0] is not None else _norm(".")
            if path is not None and not _under(path, reads):
                raise SandboxViolation(f"listing {args[0]!r} is not allowed in a skill")
            return
        if event in _PATH_EVENTS:
            for a in args[:2]:
                p = _norm(a)
                if p is not None and not _under(p, writes):
                    raise SandboxViolation(f"{event} on {a!r} is not allowed in a skill")

    sys.addaudithook(hook)


class _Channel:
    def __init__(self, inp: BinaryIO, out: BinaryIO) -> None:
        self._in = inp
        self._out = out
        self._next = 0

    def send(self, msg: dict[str, Any]) -> None:
        self._out.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        self._out.flush()

    def receive(self) -> dict[str, Any]:
        line = self._in.readline()
        if not line:
            raise SystemExit(3)
        msg: dict[str, Any] = json.loads(line)
        return msg

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self._next += 1
        self.send({"type": "call", "id": self._next, "method": method, "params": params})
        reply = self.receive()
        if reply.get("type") != "result" or reply.get("id") != self._next:
            raise CapabilityError("protocol error")
        if not reply.get("ok"):
            raise CapabilityError(str(reply.get("error", "denied")))
        return reply.get("value")


class _Group:
    def __init__(self, call: Callable[..., Any], **methods: str) -> None:
        for attr, method in methods.items():
            setattr(self, attr, self._bind(call, method))

    @staticmethod
    def _bind(call: Callable[..., Any], method: str) -> Callable[..., Any]:
        def fn(**params: Any) -> Any:
            return call(method, params)

        return fn


class Ctx:
    """What a skill can ask the core for. Every call is checked against its manifest."""

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel
        call = channel.call
        self.calendar = _Group(call, free_busy="calendar.free_busy")
        self.messages = _Group(call, recent="messages.recent")
        self.http = _Group(call, get="http.get")

    def thread(self, n: int = 10) -> Any:
        return self._channel.call("thread", {"n": n})

    def faq(self) -> Any:
        return self._channel.call("faq", {})

    def log(self, message: str) -> None:
        self._channel.call("log", {"message": str(message)[:500]})


def main() -> int:
    proto_in = sys.stdin.buffer
    proto_out = sys.stdout.buffer
    # Anything the skill prints goes to the log, never into the protocol.
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr
    sys.stdin = io.StringIO("")
    channel = _Channel(proto_in, proto_out)
    msg = channel.receive()
    if msg.get("type") != "run":
        channel.send({"type": "error", "error": "expected a run message"})
        return 2
    skill_dir = str(msg["skill_dir"])
    run_dir = str(msg["run_dir"])
    stdlib = os.path.dirname(os.__file__)
    install_sandbox([skill_dir, stdlib, sys.base_prefix, sys.prefix, sys.exec_prefix], [run_dir])
    # Only the skill's own folder and the standard library: no site-packages.
    sys.path[:] = [skill_dir, *(p for p in sys.path if "site-packages" not in p)]
    try:
        module = importlib.import_module(str(msg["module"]))
        fn = getattr(module, str(msg["function"]))
        result = fn(msg["input"], Ctx(channel))
        if not isinstance(result, dict):
            raise TypeError(f"{msg['function']}() must return a dict, got {type(result).__name__}")
        channel.send({"type": "done", "output": result})
        return 0
    except SystemExit:
        raise
    except BaseException as exc:
        traceback.print_exc()
        channel.send({"type": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
