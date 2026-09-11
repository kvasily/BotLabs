"""Named WASAPI render-endpoint loopback. Capture all channels, then mono downmix."""
from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from audio.detectors import AudioSnapshot, DetectorBank
from config import Settings


def devices() -> list[Any]:
    import soundcard as sc
    return [d for d in sc.all_microphones(include_loopback=True) if d.isloopback]


def select_device(name: str) -> Any:
    available = devices()
    exact = [d for d in available if d.name.casefold() == name.casefold() or d.id == name]
    matches = exact or [d for d in available if name.casefold() in d.name.casefold()]
    if len(matches) != 1:
        labels = "\n  ".join(f"{d.name} [id={d.id}]" for d in available)
        raise RuntimeError(f"Loopback device {name!r}: expected one match, got {len(matches)}.\nAvailable:\n  {labels}")
    return matches[0]


class LoopbackAudio:
    def __init__(self, settings: Settings, bank: DetectorBank) -> None:
        self.settings, self.bank = settings, bank
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.latest = AudioSnapshot(0, 0, {}, False, 0, ())
        self.thread = threading.Thread(target=self._run, daemon=True, name="wasapi-loopback")

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("WASAPI startup timed out; verify the named playback endpoint")
        if self.error:
            raise RuntimeError(f"WASAPI: {self.error}") from self.error

    def _run(self) -> None:
        cfg = self.settings["audio"]
        rate = cfg["sample_rate"]
        hop_size = round(rate * cfg["hop_ms"] / 1000)
        ring_size = round(rate * cfg["ring_seconds"])
        ring = np.empty(0, dtype=np.float32)
        try:
            # Instantiate SoundCard objects inside their owning capture thread.
            mic = select_device(self.settings["audio_device_name"])
            with mic.recorder(samplerate=rate, channels=cfg["channels"], blocksize=hop_size) as recorder:
                while not self.stop_event.is_set():
                    frames = recorder.record(numframes=hop_size)
                    hop = np.asarray(frames, dtype=np.float32).mean(axis=1)
                    if not np.isfinite(hop).all():
                        raise RuntimeError("Audio contains non-finite samples")
                    ring = np.concatenate((ring, hop))[-ring_size:]
                    snapshot = self.bank.process(ring, hop, time.monotonic())
                    with self.lock:
                        self.latest = snapshot
                    self.ready.set()
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def snapshot(self) -> AudioSnapshot:
        if self.error:
            raise RuntimeError(f"WASAPI stream failed: {self.error}") from self.error
        with self.lock:
            return self.latest

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
