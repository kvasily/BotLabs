"""SendInput and RegisterHotKey only. No game handles, injection, or process hooks."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as ct
from ctypes import wintypes as wt
import logging
import sys
import threading
import time
from typing import Callable, Iterator, Protocol


class Interrupted(RuntimeError):
    """A paused/stale action must unwind instead of resuming halfway through."""


class Backend(Protocol):
    def mouse(self, flags: int, dx: int = 0, dy: int = 0) -> None: ...
    def key(self, name: str, down: bool) -> None: ...
    def cursor(self) -> tuple[int, int]: ...
    def foreground_title(self) -> str: ...
    def desktop(self) -> tuple[int, int, int, int]: ...


class SafetyGate:
    """Epoch leases prevent an F8 resume from resurrecting a cancelled operation."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active = False
        self.epoch = 0
        self.shutdown = threading.Event()
        self.local = threading.local()
        self.reason = "Press F8 to start"
        self.release: Callable[[], None] = lambda: None

    def pause(self, reason: str, exit_: bool = False) -> None:
        with self.lock:
            self.active = False
            self.epoch += 1
            self.reason = reason
            if exit_:
                self.shutdown.set()
            self.release()
        logging.warning("%s", reason)

    def resume(self) -> None:
        with self.lock:
            if not self.shutdown.is_set():
                self.epoch += 1
                self.active = True
                self.reason = "Running"

    @contextmanager
    def session(self) -> Iterator[None]:
        self.local.epoch = self.epoch
        self.check()
        try:
            yield
        finally:
            self.local.epoch = None

    def check(self) -> None:
        if not self.active or self.shutdown.is_set() or getattr(self.local, "epoch", None) != self.epoch:
            raise Interrupted(self.reason)

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0, seconds)
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self.shutdown.wait(min(0.01, remaining))


class Win32Backend:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Live input requires Windows")
        self.user32 = ct.WinDLL("user32", use_last_error=True)
        ulong_ptr = ct.c_size_t

        class Mouse(ct.Structure):
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ulong_ptr)]

        class Keyboard(ct.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ulong_ptr)]

        class Hardware(ct.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class Union(ct.Union):
            _fields_ = [("mi", Mouse), ("ki", Keyboard), ("hi", Hardware)]

        class Input(ct.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wt.DWORD), ("u", Union)]

        self.Mouse, self.Keyboard, self.Input = Mouse, Keyboard, Input
        self.user32.SendInput.argtypes = [wt.UINT, ct.POINTER(Input), ct.c_int]
        self.user32.SendInput.restype = wt.UINT
        self.user32.GetForegroundWindow.restype = wt.HWND
        self.user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ct.c_int]
        self.user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]

    def _send(self, item: ct.Structure) -> None:
        if self.user32.SendInput(1, ct.byref(item), ct.sizeof(self.Input)) != 1:
            raise RuntimeError(f"SendInput failed ({ct.get_last_error()}); check foreground and privilege level")

    def mouse(self, flags: int, dx: int = 0, dy: int = 0) -> None:
        item = self.Input(type=0)
        item.mi = self.Mouse(dx, dy, 0, flags, 0, 0)
        self._send(item)

    def key(self, name: str, down: bool) -> None:
        named = {"esc": 0x1B, "tab": 9, "enter": 13, "space": 32, "shift": 16, "ctrl": 17, "alt": 18}
        name = name.lower()
        if name in named:
            vk = named[name]
        elif len(name) == 1 and name.isascii() and name.isalnum():
            vk = ord(name.upper())
        elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
            vk = 0x6F + int(name[1:])
        else:
            raise ValueError(f"Unsupported key: {name!r}")
        item = self.Input(type=1)
        # Physical scan codes suit game input; this is ordinary SendInput.
        item.ki = self.Keyboard(0, self.user32.MapVirtualKeyW(vk, 0), 0x0008 | (0 if down else 0x0002), 0, 0)
        self._send(item)

    def cursor(self) -> tuple[int, int]:
        point = wt.POINT()
        if not self.user32.GetCursorPos(ct.byref(point)):
            raise ct.WinError(ct.get_last_error())
        return point.x, point.y

    def foreground_title(self) -> str:
        buf = ct.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(self.user32.GetForegroundWindow(), buf, len(buf))
        return buf.value

    def desktop(self) -> tuple[int, int, int, int]:
        return tuple(self.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # type: ignore[return-value]


class InputController:
    BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010)}

    def __init__(self, gate: SafetyGate, backend: Backend, title: str) -> None:
        self.gate, self.backend, self.title = gate, backend, title.lower()
        self.buttons: set[str] = set()
        self.keys: set[str] = set()
        gate.release = self.release_all

    def _guard(self) -> None:
        self.gate.check()
        if self.title not in self.backend.foreground_title().lower():
            self.gate.pause("PAUSE: game lost foreground focus")
            raise Interrupted(self.gate.reason)

    def button(self, name: str, down: bool) -> None:
        with self.gate.lock:
            self._guard()
            if down == (name in self.buttons):
                return
            self.backend.mouse(self.BUTTONS[name][0 if down else 1])
            (self.buttons.add if down else self.buttons.discard)(name)

    def key(self, name: str, down: bool) -> None:
        with self.gate.lock:
            self._guard()
            if down == (name in self.keys):
                return
            self.backend.key(name, down)
            (self.keys.add if down else self.keys.discard)(name)

    def relative(self, dx: int, dy: int = 0) -> None:
        with self.gate.lock:
            self._guard()
            if dx or dy:
                self.backend.mouse(0x0001, dx, dy)

    def gui_position(self, x: int, y: int) -> None:
        """Absolute points are ONLY used along visible UI cursor/drag paths."""
        with self.gate.lock:
            self._guard()
            left, top, width, height = self.backend.desktop()
            self.backend.mouse(0x0001 | 0x8000 | 0x4000,
                               round((x - left) * 65535 / (width - 1)),
                               round((y - top) * 65535 / (height - 1)))

    def release_all(self) -> None:
        """Release even when paused/out of focus. Try every release after any failure."""
        with self.gate.lock:
            failures: list[str] = []
            for name in self.BUTTONS:
                try:
                    self.backend.mouse(self.BUTTONS[name][1])
                except Exception as exc:
                    failures.append(str(exc))
            for name in tuple(self.keys):
                try:
                    self.backend.key(name, False)
                except Exception as exc:
                    failures.append(str(exc))
            self.buttons.clear()
            self.keys.clear()
            if failures:
                logging.error("Input release failed: %s", "; ".join(failures))

    def tap(self, key: str, seconds: float = 0.08) -> None:
        self.key(key, True)
        try:
            self.gate.sleep(seconds)
        finally:
            # All operations are interruptible; unconditional cleanup also handles panic.
            self.release_all()


class Hotkeys:
    """OS hotkeys on a dedicated thread; no keyboard/game process hook."""
    def __init__(self, gate: SafetyGate) -> None:
        self.gate = gate
        self.ready = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError("Hotkey registration timed out")
        if self.error:
            raise self.error

    def _run(self) -> None:
        user32 = ct.WinDLL("user32", use_last_error=True)
        registered: list[int] = []
        try:
            for identifier, vk in ((8, 0x77), (9, 0x78), (10, 0x79)):
                if not user32.RegisterHotKey(None, identifier, 0x4000, vk):
                    raise RuntimeError(f"F{identifier} is unavailable; close the app using that hotkey")
                registered.append(identifier)
            self.ready.set()
            msg = wt.MSG()
            while not self.gate.shutdown.wait(0.005):
                while user32.PeekMessageW(ct.byref(msg), None, 0x0312, 0x0312, 1):
                    if msg.wParam == 8:
                        if self.gate.active:
                            self.gate.pause("PAUSE: F8")
                        else:
                            self.gate.resume()
                    elif msg.wParam == 9:
                        self.gate.pause("PANIC: F9; all inputs released")
                    elif msg.wParam == 10:
                        self.gate.pause("EXIT: F10", exit_=True)
        except Exception as exc:
            self.error = exc
            self.gate.pause(f"FATAL: hotkey service: {exc}", exit_=True)
            self.ready.set()
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)
