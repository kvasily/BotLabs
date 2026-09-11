"""Capture physical-pixel rectangles; never access game memory or process internals."""
from __future__ import annotations

import ctypes as ct
import sys
import threading

import numpy as np
from numpy.typing import NDArray

from config import Rect, Settings


def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        user32 = ct.WinDLL("user32", use_last_error=True)
        try:
            user32.SetProcessDpiAwarenessContext.argtypes = [ct.c_void_p]
            user32.SetProcessDpiAwarenessContext(ct.c_void_p(-4))  # Per-monitor V2.
        except AttributeError:
            user32.SetProcessDPIAware()


class ScreenCapture:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.origin = tuple(settings["game_origin"])
        self.local = threading.local()

    def grab_rect(self, rect: Rect) -> NDArray[np.uint8]:
        import mss
        # MSS handles belong to the thread which created them.
        if not hasattr(self.local, "capture"):
            self.local.capture = mss.mss()
        screen = self.local.capture.grab({"left": self.origin[0] + rect.x,
                                          "top": self.origin[1] + rect.y,
                                          "width": rect.width, "height": rect.height})
        return np.asarray(screen)[:, :, :3].copy()

    def grab(self, name: str) -> NDArray[np.uint8]:
        return self.grab_rect(self.settings.rect(name))

    def screen_point(self, point: tuple[int, int]) -> tuple[int, int]:
        return point[0] + self.origin[0], point[1] + self.origin[1]

    def close(self) -> None:
        if hasattr(self.local, "capture"):
            self.local.capture.close()
            del self.local.capture
