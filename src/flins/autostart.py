"""Start FLINS when you log in to Windows (SPEC §14 M9).

Uses Task Scheduler: a task named "FLINS" that runs at *your* logon, as you, not elevated, 30 s
after logon, in a minimised window. If Windows refuses to create it (some setups need admin for
logon tasks), a small FLINS.cmd in your Startup folder does the same job.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

TASK = "FLINS"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)


def flins_command(root: Path, voice: bool) -> list[str]:
    exe = Path(sys.executable).with_name("flins.exe")
    base = [str(exe)] if exe.is_file() else [sys.executable, "-m", "flins"]
    return [*base, "--root", str(root), "run", *(["--voice"] if voice else [])]


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def launch_line(root: Path, voice: bool) -> str:
    """cmd.exe line that opens FLINS in its own minimised window."""
    return 'cmd.exe /c start "FLINS" /min ' + " ".join(
        _quote(a) for a in flins_command(root, voice)
    )


def startup_file() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / ("FLINS.cmd")
    )


@dataclass
class Status:
    task: bool
    startup: bool

    def text(self) -> str:
        if self.task:
            return "on (Task Scheduler task 'FLINS', at your logon)"
        if self.startup:
            return f"on (Startup folder: {startup_file()})"
        return "off"


def status(run: Runner = _run) -> Status:
    task = run(["schtasks", "/Query", "/TN", TASK]).returncode == 0
    return Status(task, startup_file().is_file())


def enable(root: Path, voice: bool, run: Runner = _run) -> str:
    user = os.environ.get("USERNAME", "")
    cmd = [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        TASK,
        "/SC",
        "ONLOGON",
        "/DELAY",
        "0000:30",
        "/RL",
        "LIMITED",
        "/TR",
        launch_line(root, voice),
    ]
    if user:
        cmd += ["/RU", user, "/IT"]
    r = run(cmd)
    if r.returncode == 0:
        return "Autostart is on: FLINS starts 30 s after you log in (Task Scheduler task 'FLINS')."
    f = startup_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "@echo off\r\nrem Starts FLINS at logon. Remove with: flins autostart off\r\n"
        "timeout /t 30 /nobreak >nul\r\n" + launch_line(root, voice) + "\r\n",
        encoding="utf-8",
    )
    why = (r.stderr or r.stdout).strip().splitlines()
    return (
        f"Task Scheduler said: {why[0] if why else 'failed'}. "
        f"Used your Startup folder instead ({f}); FLINS starts when you log in."
    )


def disable(run: Runner = _run) -> str:
    removed = []
    if run(["schtasks", "/Query", "/TN", TASK]).returncode == 0:
        r = run(["schtasks", "/Delete", "/F", "/TN", TASK])
        if r.returncode != 0:
            return f"Couldn't remove the task: {(r.stderr or r.stdout).strip()}"
        removed.append("Task Scheduler task")
    f = startup_file()
    if f.is_file():
        f.unlink()
        removed.append("Startup folder entry")
    return f"Autostart is off (removed: {', '.join(removed)})." if removed else "Autostart was off."
