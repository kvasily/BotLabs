"""Measure signed relative counts/degree from a manual North reference."""
from __future__ import annotations

import argparse
from pathlib import Path
import queue
import re
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from input.human_mouse import HumanMouse, heading_error
from input.keys import Hotkeys, InputController, SafetyGate, Win32Backend
from vision.capture import enable_dpi_awareness


def send_calibration_move(io: InputController, counts: int) -> None:
    """Shared exact-count motion. Caller owns the gate, focus checks, and UI controls."""
    if counts <= 0:
        raise ValueError("Counts must be positive")
    HumanMouse(io, 1)._segment(counts, 1.0)


def save_calibration(path: Path, counts: int, heading: float) -> float:
    """Shared measurement/write logic, usable without registering any hotkeys."""
    if counts <= 0 or not 0 <= heading < 360:
        raise ValueError("Positive counts and a heading >= 0 and < 360 are required")
    angle = heading_error(heading, 0)
    if not 2 <= abs(angle) <= 150:
        raise ValueError("Move should be 2–150 degrees without wrapping; change counts and retry")
    scale = counts / angle
    original = path.read_text(encoding="utf-8")
    updated, replacements = re.subn(r"(?m)^counts_per_degree:.*$", f"counts_per_degree: {scale:.8f}  # Calibrated signed counts/degree", original)
    if replacements != 1:
        raise ValueError("Expected exactly one counts_per_degree field; config left unchanged")
    path.write_text(updated, encoding="utf-8")
    return scale


def prompt(text: str, gate: SafetyGate) -> str:
    answers: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: answers.put(input(text)), daemon=True).start()
    while not gate.shutdown.wait(0.05):
        try:
            return answers.get_nowait()
        except queue.Empty:
            continue
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--counts", type=int, default=300, help="Positive counts; choose a move below 180 degrees")
    args = parser.parse_args()
    if args.counts <= 0:
        parser.error("--counts must be positive")
    cfg = load_config(args.config)
    enable_dpi_awareness()
    gate = SafetyGate()
    io = InputController(gate, Win32Backend(), cfg["window_title_contains"])
    Hotkeys(gate).start()
    try:
        prompt("Face exactly N, restore look mode. Press Enter here, then focus game within 5 seconds. ", gate)
        gate.resume()
        with gate.session():
            gate.sleep(5)
            send_calibration_move(io, args.counts)
        answer = prompt("Read new compass heading (0–360) and type it here: ", gate)
        heading = float(answer)
        scale = save_calibration(args.config, args.counts, heading)
        print(f"Stored counts_per_degree={scale:.8f} in {args.config.resolve()}. Verify several headings before fishing.")
        return 0
    finally:
        gate.pause("Calibration stopped", exit_=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled")
