"""Start, stop and pair the Node sidecar process."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import IO


class SidecarSetupError(RuntimeError):
    """Something the user has to fix before the sidecar can run."""


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


class Sidecar:
    def __init__(self, root: Path, data_dir: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.auth_dir = data_dir / "wa-auth"
        self.log_path = data_dir / "logs" / "wa-sidecar.log"
        self.script = root / "wa-sidecar" / "dist" / "index.js"
        self._proc: asyncio.subprocess.Process | None = None
        self._log_file: IO[bytes] | None = None

    def _node(self) -> str:
        node = shutil.which("node")
        if not node:
            raise SidecarSetupError("Node.js not found. Install Node 20+.")
        if not self.script.is_file():
            raise SidecarSetupError(
                "sidecar not built. Run:\n"
                "  npm --prefix wa-sidecar install\n"
                "  npm --prefix wa-sidecar run build"
            )
        return node

    def is_paired(self) -> bool:
        return (self.auth_dir / "creds.json").is_file()

    def pair(self, *, force: bool = False) -> int:
        """Run pairing in the foreground so the QR shows in this terminal."""
        node = self._node()
        if force and self.auth_dir.exists():
            aside = self.auth_dir.with_name(f"wa-auth.replaced-{int(time.time())}")
            self.auth_dir.rename(aside)
            print(f"moved old session to {aside.name}")
        self.auth_dir.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.call([node, str(self.script), "--pair", "--auth-dir", str(self.auth_dir)])

    async def start(self, secret: str) -> None:
        node = self._node()
        if port_open(self.port):
            raise SidecarSetupError(f"port {self.port} is already in use")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_path.open("ab")
        env = {**os.environ, "FLINS_WA_SECRET": secret}
        self._proc = await asyncio.create_subprocess_exec(
            node,
            str(self.script),
            "--port",
            str(self.port),
            "--auth-dir",
            str(self.auth_dir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=self._log_file,
        )
        for _ in range(50):  # wait up to ~10 s for it to listen
            if self._proc.returncode is not None:
                raise SidecarSetupError(f"sidecar exited early; see {self.log_path}")
            if port_open(self.port):
                return
            await asyncio.sleep(0.2)
        raise SidecarSetupError(f"sidecar did not start listening; see {self.log_path}")

    async def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                proc.terminate()
                await proc.wait()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self._proc = None
