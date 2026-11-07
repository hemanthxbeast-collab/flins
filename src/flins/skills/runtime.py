"""Run one skill in a fresh, locked-down Python process (SPEC §9.3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flins.skills.ctx_api import Capabilities, CapabilityDenied, CapabilityFailed
from flins.skills.manifest import Manifest
from flins.skills.models import SkillInput, SkillOutput

MAX_LINE = 1 << 20  # one protocol message, bytes
MAX_STDERR = 20_000


@dataclass
class RunResult:
    output: SkillOutput | None
    error: str | None
    ms: int
    denied: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.output is not None


def _env() -> dict[str, str]:
    """Nearly empty: no API keys, no PATH. Windows needs SYSTEMROOT to start Python."""
    env = {"PYTHONIOENCODING": "utf-8"}
    for k in ("SYSTEMROOT", "SystemRoot"):
        if k in os.environ:
            env["SYSTEMROOT"] = os.environ[k]
    return env


async def run_skill(
    skill_dir: Path,
    manifest: Manifest,
    inp: SkillInput,
    caps: Capabilities,
    runs_dir: Path,
    *,
    keep_run_dir: bool = False,
) -> RunResult:
    run_id = caps.run or uuid.uuid4().hex[:12]
    caps.run = run_id
    run_dir = (runs_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-m",
        "flins.skills.worker_main",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=run_dir,
        env=_env(),
        limit=MAX_LINE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stderr_task = asyncio.create_task(_drain(proc.stderr))
    output: SkillOutput | None = None
    error: str | None = None
    try:
        await asyncio.wait_for(
            _converse(proc, skill_dir.resolve(), run_dir, manifest, inp, caps),
            timeout=manifest.timeout_s,
        )
    except TimeoutError:
        error = f"timed out after {manifest.timeout_s:g} s"
    except _Finished as done:
        output, error = done.output, done.error
    except (ValueError, json.JSONDecodeError, asyncio.LimitOverrunError) as exc:
        error = f"protocol error: {type(exc).__name__}"
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        stderr = await _finish(stderr_task)
        if not keep_run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
    if output is None and error is None:
        error = "the skill exited without an answer"
    ms = int((time.perf_counter() - started) * 1000)
    return RunResult(output, error, ms, list(caps.denied), list(caps.logs), stderr)


class _Finished(Exception):
    def __init__(self, output: SkillOutput | None, error: str | None) -> None:
        self.output = output
        self.error = error


async def _converse(
    proc: asyncio.subprocess.Process,
    skill_dir: Path,
    run_dir: Path,
    manifest: Manifest,
    inp: SkillInput,
    caps: Capabilities,
) -> None:
    assert proc.stdin and proc.stdout

    async def send(msg: dict[str, Any]) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        await proc.stdin.drain()

    await send(
        {
            "type": "run",
            "skill_dir": str(skill_dir),
            "run_dir": str(run_dir),
            "module": manifest.module,
            "function": manifest.function,
            "input": inp.model_dump(mode="json"),
        }
    )
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise _Finished(None, None)
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("not an object")
        kind = msg.get("type")
        if kind == "call":
            call_id = msg.get("id")
            raw = msg.get("params")
            params: dict[str, Any] = raw if isinstance(raw, dict) else {}
            try:
                value = await caps.call(str(msg.get("method")), params)
                reply = {"type": "result", "id": call_id, "ok": True, "value": value}
            except (CapabilityDenied, CapabilityFailed) as exc:
                reply = {"type": "result", "id": call_id, "ok": False, "error": str(exc)}
            await send(reply)
        elif kind == "done":
            try:
                out = SkillOutput.model_validate(msg.get("output"))
            except ValidationError as exc:
                raise _Finished(None, f"invalid output: {_short(exc)}") from None
            raise _Finished(out, None)
        elif kind == "error":
            raise _Finished(None, str(msg.get("error", "error"))[:500])
        else:
            raise ValueError(f"unknown message type {kind!r}")


def _short(exc: ValidationError) -> str:
    e = exc.errors()[0]
    return f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"


async def _drain(stream: asyncio.StreamReader) -> str:
    chunks: list[bytes] = []
    size = 0
    while True:
        data = await stream.read(4096)
        if not data:
            break
        if size < MAX_STDERR:
            chunks.append(data)
            size += len(data)
    return b"".join(chunks)[:MAX_STDERR].decode("utf-8", errors="replace")


async def _finish(task: asyncio.Task[str]) -> str:
    try:
        return await asyncio.wait_for(task, timeout=2)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
        return ""
