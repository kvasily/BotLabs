"""Separate native Qt HUD. Click-through, no activation, excluded from capture."""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from config import Settings

STYLE_BITS = 0x20 | 0x80000 | 0x08000000  # TRANSPARENT | LAYERED | NOACTIVATE


def native_overlay(hwnd: int, origin: tuple[int, int]) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ct.WinDLL("user32", use_last_error=True)
    get_style = user32.GetWindowLongPtrW if ct.sizeof(ct.c_void_p) == 8 else user32.GetWindowLongW
    set_style = user32.SetWindowLongPtrW if ct.sizeof(ct.c_void_p) == 8 else user32.SetWindowLongW
    get_style.argtypes, get_style.restype = [wt.HWND, ct.c_int], ct.c_ssize_t
    set_style.argtypes, set_style.restype = [wt.HWND, ct.c_int, ct.c_ssize_t], ct.c_ssize_t
    set_style(hwnd, -20, get_style(hwnd, -20) | STYLE_BITS)
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT]
    if not user32.SetWindowPos(hwnd, wt.HWND(-1), origin[0], origin[1], 3840, 2160, 0x10 | 0x20):
        raise ct.WinError(ct.get_last_error())
    user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
    # Prevent HUD paint from becoming detector pixels in mss screenshots.
    return sys.getwindowsversion().build >= 19041 and bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))


class Overlay(QWidget):
    ACTIVE = {"FIND_TASKMASTER": {"compass", "interact_prompt"}, "READ_QUESTS": {"quest_list"},
              "SELECT_TASK": {"quest_list", "confirm_button"}, "PREP_GEAR": {"hook_slot", "bait_slot", "inventory"},
              "FACE_WATER": {"compass"}, "CAST": {"hit_marker"}, "WAIT_BITE": {"compass"},
              "FIGHT_BASSLE": {"catch_indicator"}, "FIGHT_REDLINE": {"catch_indicator"},
              "RESOLVE_CATCH": {"quest_paper", "quest_tooltip"}, "TURN_IN": {"turn_in_button"}}

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                 Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background: transparent;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("MO2Fish Overlay")
        self.cfg: Settings | None = None
        self.enabled_rois: set[str] = set()
        self.labels = True
        self.telemetry: dict = {}
        self.capture_excluded = False

    def configure(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.enabled_rois = set(cfg.data.get("rois", {}))
        origin = cfg.data.get("game_origin", [0, 0])
        if not isinstance(origin, list) or len(origin) != 2 or not all(type(v) is int for v in origin):
            origin = [0, 0]  # Display-only fallback. The original validator still rejects the profile.
        self.origin = tuple(origin)
        self.setGeometry(*origin, 3840, 2160)
        if self.isVisible():
            self.capture_excluded = native_overlay(int(self.winId()), self.origin)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.cfg:
            self.capture_excluded = native_overlay(int(self.winId()), self.origin)

    def set_snapshot(self, data: dict) -> None:
        self.telemetry = data
        self.update()

    def paintEvent(self, event) -> None:
        if not self.cfg:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.width() / 3840, self.height() / 2160)
        p.setFont(QFont("Segoe UI", 11))
        active = self.ACTIVE.get(self.telemetry.get("state"), set())
        for name in sorted(self.enabled_rois):
            try:
                rect = self.cfg.rect(name)
            except (ValueError, KeyError, TypeError):
                continue
            color = QColor("#65e6b4" if name in active else "#82a1bc")
            color.setAlpha(230 if name in active else 140)
            p.setPen(QPen(color, 3 if name in active else 1.5))
            p.setBrush(QColor(8, 18, 30, 35))
            p.drawRoundedRect(QRectF(rect.x, rect.y, rect.width, rect.height), 3, 3)
            if self.labels:
                p.drawText(rect.x + 5, max(18, rect.y - 5), name)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(10, 17, 27, 205))
        p.drawRoundedRect(QRectF(24, 24, 720, 106), 12, 12)
        p.setPen(QColor("#d8e7f3"))
        data = self.telemetry
        audio = data.get("audio")
        score = audio.scores.get("splash", 0) if audio else 0
        tension = audio.tension if audio else False
        p.drawText(44, 55, f"MO2FISH  /  {data.get('state', 'DISARMED')}  /  {data.get('task') or '—'}  /  {data.get('count', 0)}/3")
        heading = data.get("heading")
        p.drawText(44, 88, f"Heading {heading:.1f}°   ·   Splash {score:.3f}   ·   Tension {'ON' if tension else 'off'}" if heading is not None
                   else "Authoring overlay • live perception starts only from the Control tab")
        p.end()
