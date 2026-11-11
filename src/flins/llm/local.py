"""Local models through Ollama (JARVIS phase L).

The laptop has one GPU with limited video memory, so only one language model is kept loaded.
Before a local model is used, any *other* loaded model is unloaded. Whisper (listening) and
Piper (speaking) run on the CPU and aren't managed here.

Uses Ollama's own API next to its OpenAI-compatible one:
  GET  /api/version, /api/tags (installed), /api/ps (loaded)
  POST /api/generate {"model": m, "keep_alive": 0}   unload m
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class LocalModel:
    name: str
    size_gb: float  # on disk (installed) or in memory (loaded)
    vram_gb: float = 0.0  # loaded: how much of it is in video memory


def native_base(openai_base_url: str) -> str:
    """'http://localhost:11434/v1' -> 'http://localhost:11434'."""
    base = openai_base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


class LocalModels:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=native_base(base_url), timeout=timeout_s, transport=transport
        )
        self._current: str | None = None  # the model this process last asked for

    async def _get(self, path: str) -> dict[str, Any]:
        resp = await self._client.get(path)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def version(self) -> str | None:
        """Ollama's version, or None if it isn't running."""
        try:
            return str((await self._get("/api/version")).get("version"))
        except (httpx.HTTPError, ValueError):
            return None

    async def installed(self) -> list[LocalModel]:
        models = (await self._get("/api/tags")).get("models", [])
        return [LocalModel(str(m.get("name")), int(m.get("size") or 0) / 1e9) for m in models]

    async def loaded(self) -> list[LocalModel]:
        models = (await self._get("/api/ps")).get("models", [])
        return [
            LocalModel(
                str(m.get("name")),
                int(m.get("size") or 0) / 1e9,
                int(m.get("size_vram") or 0) / 1e9,
            )
            for m in models
        ]

    async def unload(self, name: str) -> None:
        await self._client.post("/api/generate", json={"model": name, "keep_alive": 0})

    async def use(self, model: str) -> list[str]:
        """Make room for `model`: unload every other loaded model. Returns what was unloaded.
        Never raises: if Ollama can't be asked, the request itself will report the problem."""
        if self._current == model:
            return []  # nothing else was loaded by us since; skip the extra round trip
        unloaded: list[str] = []
        try:
            for m in await self.loaded():
                if m.name != model:
                    await self.unload(m.name)
                    unloaded.append(m.name)
        except (httpx.HTTPError, ValueError):
            return unloaded
        self._current = model
        return unloaded

    async def unload_all(self) -> list[str]:
        names = [m.name for m in await self.loaded()]
        for name in names:
            await self.unload(name)
        self._current = None
        return names
