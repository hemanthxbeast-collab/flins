"""Draw the pairing QR (a grid of 0/1 from the sidecar) as inline SVG. No image library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_MODULES = 177  # the largest QR version


def qr_svg(modules: list[list[int]]) -> str:
    n = len(modules)
    if not 21 <= n <= MAX_MODULES or any(len(r) != n for r in modules):
        raise ValueError("not a QR grid")
    quiet = 4
    size = n + 2 * quiet
    path = "".join(
        f"M{c + quiet} {r + quiet}h1v1h-1z"
        for r, row in enumerate(modules)
        for c, dark in enumerate(row)
        if dark == 1
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="300" height="300" shape-rendering="crispEdges" role="img" '
        f'aria-label="WhatsApp pairing QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )


def read_pair_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
