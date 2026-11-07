"""Global push-to-talk hotkey (default Ctrl+Alt+Space) via pynput."""

from __future__ import annotations

import asyncio
from typing import Any

DEFAULT_HOTKEY = "<ctrl>+<alt>+<space>"


class Hotkey:
    """Each press puts an item on `presses`. Works while any window has focus."""

    def __init__(self, combo: str = DEFAULT_HOTKEY) -> None:
        self.combo = combo
        self.presses: asyncio.Queue[None] = asyncio.Queue()
        self._listener: Any = None

    def start(self) -> None:
        from pynput import keyboard

        loop = asyncio.get_running_loop()

        def pressed() -> None:
            loop.call_soon_threadsafe(self.presses.put_nowait, None)

        self._listener = keyboard.GlobalHotKeys({self.combo: pressed})
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
