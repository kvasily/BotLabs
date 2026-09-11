"""Windows entry point. Preflight completes before live input or hotkey startup."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable

from config import ConfigError, Settings, load_config

if TYPE_CHECKING:
    from audio.loopback import LoopbackAudio
    from fsm.states import AssistantFSM
    from input.keys import Backend, InputController, SafetyGate


@dataclass
class AssistantRuntime:
    """Shared CLI/GUI construction result. Hotkeys and UI belong to the caller."""
    gate: SafetyGate
    audio: LoopbackAudio
    io: InputController
    fsm: AssistantFSM
    worker: threading.Thread

    def close(self, reason: str = "Assistant stopped") -> None:
        self.gate.pause(reason, exit_=True)
        self.worker.join(timeout=1)
        self.io.release_all()
        self.audio.close()


def start_assistant(cfg: Settings, gate: SafetyGate, *, backend: Backend | None = None,
                    audio_factory: Callable | None = None) -> AssistantRuntime:
    """Construct the existing brain and start its worker PAUSED. Caller validates/resumes.

    This helper never creates Hotkeys or a debug window. GUI callers run it off the
    UI thread and may inject a permission-enforcing wrapper around Win32Backend.
    """
    from audio.detectors import DetectorBank
    from audio.loopback import LoopbackAudio
    from fsm.states import AssistantFSM
    from input.human_mouse import HumanMouse
    from input.keys import InputController, Win32Backend
    from vision.capture import ScreenCapture
    from vision.compass import Compass
    from vision.ocr import OCR
    from vision.perception import Perception
    from vision.templates import TemplateStore
    audio = (audio_factory or LoopbackAudio)(cfg, DetectorBank(cfg))
    try:
        audio.start()
        io = InputController(gate, backend if backend is not None else Win32Backend(), cfg["window_title_contains"])
        screen, store = ScreenCapture(cfg), TemplateStore(cfg)
        ocr = OCR(cfg["vision"]["ocr_enabled"], cfg["vision"]["tesseract_cmd"])
        fsm = AssistantFSM(cfg, gate, io, HumanMouse(io, cfg["counts_per_degree"]), screen,
                           Perception(cfg, screen, store, ocr), Compass(cfg, screen, store), audio)
        worker = threading.Thread(target=fsm.run, daemon=True, name="fishing-fsm")
        result = AssistantRuntime(gate, audio, io, fsm, worker)
        worker.start()
        return result
    except Exception:
        gate.pause("Assistant startup failed", exit_=True)
        audio.close()
        raise


def check_assets(cfg: Settings) -> None:
    from audio.detectors import DetectorBank
    from vision.templates import TemplateStore
    cfg.validate()
    store = TemplateStore(cfg)
    for path in cfg.required_assets():
        if path.suffix.lower() == ".png":
            store.load(str(path))
    DetectorBank(cfg)
    bindings = {"hit_marker": "hit_marker", "taskmaster": "interact_prompt", "quest_gui": "quest_list",
                "confirm": "confirm_button", "turn_in": "turn_in_button", "catch": "catch_indicator",
                "bait_empty": "bait_slot", "bait_zero": "bait_count"}
    bindings.update({f"progress_{i}": "quest_tooltip" for i in range(4)})
    for name, roi in bindings.items():
        path = cfg["templates"].get(name)
        if path:
            height, width = store.load(path).shape
            rect = cfg.rect(roi)
            if width > rect.width or height > rect.height:
                raise ConfigError(f"{path}: template exceeds rois.{roi}")
    for name, task in cfg["tasks"].items():
        for kind in ("hook", "bait"):
            height, width = store.load(task[f"{kind}_template"]).shape
            rect = cfg.rect(f"{kind}_slot")
            if width > rect.width or height > rect.height:
                raise ConfigError(f"{task[f'{kind}_template']}: template exceeds rois.{kind}_slot")
        height, width = store.load(cfg["templates"][name]).shape
        for roi in ("quest_list", "quest_tooltip"):
            rect = cfg.rect(roi)
            if width > rect.width or height > rect.height:
                raise ConfigError(f"{name}: fish-name template exceeds {roi}")
    from vision.ocr import OCR
    OCR(cfg["vision"]["ocr_enabled"], cfg["vision"]["tesseract_cmd"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Validate YAML/assets without opening devices or sending input")
    mode.add_argument("--list-devices", action="store_true", help="Print WASAPI loopback endpoints and exit")
    mode.add_argument("--audio-meter", action="store_true", help="Audio-only calibration; no game input")
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--hotkeys", action="store_true", help="Register F8/F9/F10 for the legacy CLI only")
    args = parser.parse_args()
    audio = None
    gate = None
    debug = None
    worker = None
    try:
        if args.list_devices:
            from audio.loopback import devices
            for device in devices():
                print(f"{device.name}\n  id: {device.id}")
            return 0
        cfg = load_config(args.config)
        if not args.audio_meter:
            check_assets(cfg)
        if args.check:
            print("Preflight passed: configuration, images, WAVs, and optional OCR. No input sent.")
            return 0
        if not args.audio_meter and not args.hotkeys:
            raise ConfigError("Use the desktop GUI, or pass --hotkeys for the legacy CLI controls")
        if sys.platform != "win32":
            raise ConfigError("Live operation requires Windows 10/11 and Python 3.11+")
        from audio.detectors import DetectorBank
        from audio.loopback import LoopbackAudio
        from debug import DebugWindow
        from input.keys import Hotkeys, InputController, SafetyGate, Win32Backend
        from vision.capture import enable_dpi_awareness
        enable_dpi_awareness()
        log_path = cfg.asset(cfg["debug"]["log_file"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S",
                            handlers=[logging.StreamHandler(), RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")])
        gate = SafetyGate()
        if args.hotkeys:
            Hotkeys(gate).start()
        if args.audio_meter or (cfg["debug"]["window"] and not args.no_debug):
            debug = DebugWindow(lambda: gate.pause("Debug window closed", exit_=True))
        fsm = None
        if args.audio_meter:
            audio = LoopbackAudio(cfg, DetectorBank(cfg))
            audio.start()
        else:
            runtime = start_assistant(cfg, gate)
            audio, fsm, worker = runtime.audio, runtime.fsm, runtime.worker
        print("Audio meter only; no actuation." if args.audio_meter else "Ready, PAUSED. Normalize UI and focus the game, then F8. F9 panic; F10 hard exit.")
        while not gate.shutdown.wait(0.05):
            snap = audio.snapshot()
            if time.monotonic() - snap.timestamp > cfg["audio"]["stale_seconds"]:
                raise RuntimeError("WASAPI audio stalled")
            if debug:
                status = f"{fsm.state.name} · {fsm.task or 'no task'} · verified {fsm.count}/3" if fsm else "Audio calibration — F10 exit"
                detail = (f"yaw {fsm.compass.last_heading:.1f}° ({fsm.compass.last_score:.2f}) · {fsm.message}" if fsm and gate.active else gate.reason)
                debug.update(snap, status, detail)
        return 1 if fsm and fsm.state.name == "FATAL" else 0
    except KeyboardInterrupt:
        return 0
    except (ConfigError, KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if gate:
            gate.pause("Application exit; release all inputs", exit_=True)
        if worker:
            worker.join(timeout=1)
        if audio:
            audio.close()
        if debug:
            debug.close()


if __name__ == "__main__":
    raise SystemExit(main())
