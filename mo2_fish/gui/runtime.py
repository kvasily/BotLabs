"""Permission wrapper and Qt signal bridge around the unchanged AssistantFSM."""
from __future__ import annotations

import logging
from pathlib import Path
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal

from config import load_config
from gui.profiles import fingerprint
from input.keys import Backend, InputController, Interrupted, SafetyGate, Win32Backend
from main import AssistantRuntime, check_assets, start_assistant


def gui_reason(reason: str) -> str:
    return reason.replace("Press F8 to start", "Disarmed • Validate setup, Arm, then Start").replace("before F8", "before clicking Start").replace("F8", "Start")


class PermissionBackend:
    """Wrap SendInput, never replace it. Unowned button-up events are suppressed.

    InputController always releases both buttons in finally blocks, even if never
    held. Tracking ownership here guarantees zero SendInput while disarmed/idle.
    Release owned inputs before marking disarmed; later cleanup becomes a no-op.
    """
    def __init__(self, backend: Backend, allowed: Callable[[], bool]) -> None:
        self.backend, self.allowed = backend, allowed
        self.buttons: set[int] = set()
        self.keys: set[str] = set()
        self.lock = threading.RLock()

    def mouse(self, flags: int, dx: int = 0, dy: int = 0) -> None:
        with self.lock:
            releases = {4: 2, 16: 8}
            if flags in releases:
                down = releases[flags]
                if down in self.buttons:
                    self.backend.mouse(flags, dx, dy)
                    self.buttons.discard(down)
                return
            if not self.allowed():
                raise Interrupted("Actuation blocked by GUI Arm / Start / focus / preflight controls")
            self.backend.mouse(flags, dx, dy)
            if flags in (2, 8):
                self.buttons.add(flags)

    def key(self, name: str, down: bool) -> None:
        with self.lock:
            if not down:
                if name in self.keys:
                    self.backend.key(name, False)
                    self.keys.discard(name)
                return
            if not self.allowed():
                raise Interrupted("Keyboard actuation blocked by GUI controls")
            self.backend.key(name, True)
            self.keys.add(name)

    def foreground_title(self) -> str:
        return self.backend.foreground_title()

    def cursor(self) -> tuple[int, int]:
        return self.backend.cursor()

    def desktop(self) -> tuple[int, int, int, int]:
        return self.backend.desktop()


class RuntimeController(QObject):
    snapshot = Signal(object)
    notice = Signal(str)
    changed = Signal()
    calibration_finished = Signal(bool)

    def __init__(self, path: Path, backend: Backend | None = None, observe: bool = True) -> None:
        super().__init__()
        self.path = path
        self.backend = backend or Win32Backend()
        self.armed = False
        self.started = False
        self.starting = False
        self.calibrating = False
        self.validated: str | None = None
        self.title = str(load_config(path).data.get("window_title_contains", ""))
        self.gate = SafetyGate()
        self.gate.reason = "Disarmed • Validate setup, Arm, then Start"
        self.runtime: AssistantRuntime | None = None
        self.serial = 0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.observer = threading.Thread(target=self._observe, daemon=True, name="gui-observer")
        if observe:
            self.observer.start()

    def focused(self) -> bool:
        return bool(self.title) and self.title.casefold() in self.backend.foreground_title().casefold()

    def allowed(self) -> bool:
        return bool(self.armed and self.started and self.validated and self.gate.active and self.focused())

    def accept_validation(self, token: str) -> None:
        self.validated = token
        self.title = str(load_config(self.path)["window_title_contains"])
        self.changed.emit()

    def set_armed(self, armed: bool) -> None:
        if not armed:
            # Release while permission is still owned, then make future input impossible.
            self.pause("Disarmed from GUI")
            self.armed = False
            self.dispose_runtime()
        else:
            self.armed = True  # Arming alone does not resume the gate.
            self.gate.reason = "Armed • click Start while MO2 is focused"
        self.changed.emit()

    def pause(self, reason: str = "Paused from GUI") -> None:
        with self.lock:
            self.serial += 1
            self.gate.pause(reason)
            self.started = False
        self.changed.emit()

    def invalidate(self, reason: str = "Setup changed • Validate again") -> None:
        self.pause(reason)
        self.validated = None
        self.dispose_runtime()
        self.changed.emit()

    def dispose_runtime(self) -> None:
        self.serial += 1
        old = self.runtime
        self.runtime = None
        self.gate.pause(self.gate.reason, exit_=True)
        if old:
            threading.Thread(target=lambda: old.close("Runtime closed"), daemon=True, name="runtime-cleanup").start()

    def set_profile(self, path: Path) -> None:
        self.set_armed(False)
        self.validated = None
        self.path = path
        self.title = str(load_config(path).data.get("window_title_contains", ""))
        self.gate = SafetyGate()
        self.gate.reason = "Profile loaded • Disarmed • Validate setup"
        self.changed.emit()

    def start(self, override: bool = False, calibration_counts: int | None = None) -> bool:
        if self.starting or self.started or self.stop_event.is_set():
            return False
        if not self.armed:
            self.notice.emit("Arm the assistant first. Arming itself sends no input.")
            return False
        if not self.focused():
            self.notice.emit("MO2 is not focused. Focus the game, then click the visible Start button; the button preserves focus.")
            return False
        if self.validated is None and not override:
            self.notice.emit("Validate the saved profile before starting.")
            return False
        if calibration_counts is not None and self.runtime is not None:
            self.notice.emit("Disarm the fishing session, then Arm again before a heading calibration move.")
            return False
        self.serial += 1
        request = self.serial
        self.starting = True
        self.gate.reason = "Checking saved profile before start…"
        path = self.path
        def launch() -> None:
            created: AssistantRuntime | None = None
            try:
                token = fingerprint(path)
                if self.validated is not None and token != self.validated:
                    raise RuntimeError("Saved YAML or assets changed • Validate again")
                cfg = load_config(path)
                # OVERRIDE unlocks this fresh check; it NEVER bypasses check_assets.
                check_assets(cfg)
                if fingerprint(path) != token:
                    raise RuntimeError("Profile changed during preflight • Validate again")
                if request != self.serial or not self.armed or not self.focused() or self.stop_event.is_set():
                    raise Interrupted("Start cancelled or game focus changed")
                self.validated = token
                if self.runtime is None:
                    self.gate = SafetyGate()
                    self.gate.reason = "Starting from GUI"
                    guarded = PermissionBackend(self.backend, self.allowed)
                    if calibration_counts is None:
                        from gui.com_audio import GuiLoopbackAudio
                        created = start_assistant(cfg, self.gate, backend=guarded, audio_factory=GuiLoopbackAudio)
                    else:
                        io = InputController(self.gate, guarded, self.title)
                # Commit and resume atomically with respect to Pause/Panic/Disarm.
                with self.lock:
                    if request != self.serial or not self.armed or not self.focused() or self.stop_event.is_set():
                        if created:
                            created.close("Start cancelled before actuation")
                        raise Interrupted("Start cancelled; click Start again while MO2 is focused")
                    if created:
                        self.runtime = created
                    self.started = True
                    self.gate.resume()
                if calibration_counts is not None:
                    self.calibrating = True
                    from tools.heading_calibrator import send_calibration_move
                    with self.gate.session():
                        send_calibration_move(io, calibration_counts)
                    self.pause("Calibration movement finished • enter the observed heading")
                    self.calibration_finished.emit(True)
            except Exception as exc:
                if request == self.serial:
                    self.pause(gui_reason(str(exc)))
                self.notice.emit(gui_reason(str(exc)))
                if calibration_counts is not None:
                    self.calibration_finished.emit(False)
                if created and created is not self.runtime:
                    created.close("Startup failed")
            finally:
                self.starting = False
                self.calibrating = False
                self.changed.emit()
        threading.Thread(target=launch, daemon=True, name="gui-start").start()
        self.changed.emit()
        return True

    def _observe(self) -> None:
        next_hash = 0.0
        while not self.stop_event.wait(0.08):
            try:
                focus = self.focused()
                if (self.started or self.starting) and not focus:
                    self.pause("MO2 lost focus • Paused; click Start to resume")
                now = time.monotonic()
                if self.validated and now >= next_hash:
                    next_hash = now + 1.0
                    if fingerprint(self.path) != self.validated:
                        self.invalidate("Profile files changed • Validate again")
                runtime = self.runtime
                audio = runtime.audio.snapshot() if runtime else None
                if audio and now - audio.timestamp > load_config(self.path)["audio"]["stale_seconds"]:
                    self.pause("Audio stale • Paused")
                if self.started and not self.gate.active:
                    self.started = False
                    self.changed.emit()
                fsm = runtime.fsm if runtime else None
                self.snapshot.emit({"armed": self.armed, "started": self.started, "starting": self.starting,
                                    "focused": focus, "state": fsm.state.name if fsm else "READY",
                                    "task": fsm.task if fsm else None, "count": fsm.count if fsm else 0,
                                    "heading": fsm.compass.last_heading if fsm else None,
                                    "message": gui_reason(fsm.message) if fsm else "",
                                    "reason": gui_reason(self.gate.reason), "audio": audio})
            except Exception as exc:
                if self.started:
                    self.pause(f"Observation failed • {exc}")
                self.notice.emit(str(exc))
                self.stop_event.wait(0.5)

    def close(self) -> None:
        self.stop_event.set()
        self.pause("Quit from GUI")
        self.armed = False
        self.gate.pause("Quit from GUI", exit_=True)
        if self.runtime:
            self.runtime.close("Quit from GUI")
            self.runtime = None
        if self.observer.is_alive():
            self.observer.join(timeout=1)
