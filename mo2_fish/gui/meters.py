"""Qt audio meters plus explicitly requested loopback probes/recordings; no FSM."""
from __future__ import annotations

import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QProgressBar, QWidget

from audio.detectors import AudioSnapshot, rms
from audio.loopback import select_device
from gui.com_audio import audio_apartment


class Meters(QWidget):
    def __init__(self, names: tuple[str, ...] = ("RMS", "bubble", "splash", "tension", "quest_complete", "catch")) -> None:
        super().__init__()
        self.layout_ = QFormLayout(self)
        self.rows: dict[str, QProgressBar] = {}
        self.state = QLabel("Audio idle • no device opened")
        self.layout_.addRow(self.state)
        for name in names:
            self._row(name)

    def _row(self, name: str) -> QProgressBar:
        if name not in self.rows:
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setFormat("0.000")
            self.rows[name] = bar
            self.layout_.addRow(name, bar)
        return self.rows[name]

    def set_level(self, name: str, value: float) -> None:
        bar = self._row(name)
        bar.setValue(round(min(1, max(0, value)) * 1000))
        bar.setFormat(f"{value:.4f}")

    def update_snapshot(self, snapshot: AudioSnapshot | None, stale_seconds: float = 1.5) -> None:
        if snapshot is None:
            self.state.setText("Audio idle • no device opened")
            for name in self.rows:
                self.set_level(name, 0)
            return
        stale = time.monotonic() - snapshot.timestamp > stale_seconds
        self.state.setText(f"{'STALE AUDIO' if stale else 'LIVE 48 kHz'}   •   Tension {'ON' if snapshot.tension else 'off'}")
        self.state.setStyleSheet("color: #ffc36a" if stale else "color: #5ce0b3")
        for name, value in {"RMS": snapshot.rms, **snapshot.scores}.items():
            self.set_level(name, value)


class LoopbackProbe(QObject):
    level = Signal(float)
    clip = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, device: str, record: bool = False, max_seconds: float = 30) -> None:
        if self.running:
            raise RuntimeError("The previous audio probe is still closing")
        self.stop_event.clear()
        def run() -> None:
            chunks: list[np.ndarray] = []
            try:
                with audio_apartment():
                    self._capture(device, record, max_seconds, chunks)
                if chunks:
                    self.clip.emit(np.concatenate(chunks))
            except Exception as exc:
                self.failed.emit(str(exc))
            finally:
                self.finished.emit()
        self.thread = threading.Thread(target=run, daemon=True, name="authoring-loopback")
        self.thread.start()

    def _capture(self, device: str, record: bool, max_seconds: float, chunks: list[np.ndarray]) -> None:
        mic = select_device(device)
        with mic.recorder(samplerate=48000, channels=2, blocksize=1440) as recorder:
            while not self.stop_event.is_set():
                frames = recorder.record(numframes=1440)
                mono = np.asarray(frames, dtype=np.float32).mean(axis=1)
                self.level.emit(rms(mono))
                if record:
                    chunks.append(mono)
                    if len(chunks) * 1440 >= 48000 * max_seconds:
                        break

    def stop(self) -> None:
        self.stop_event.set()
