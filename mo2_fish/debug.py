"""Small, local Tk calibration meter; no screen overlay injected into the game."""
from __future__ import annotations

from typing import Callable

from audio.detectors import AudioSnapshot


class DebugWindow:
    def __init__(self, stop: Callable[[], None]) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title("MO2 fishing · calibration")
        self.root.geometry("410x440")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", stop)
        self.status = tk.StringVar(value="Paused — F8 start, F9 panic, F10 exit")
        self.detail = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status, wraplength=390).pack(anchor="w", padx=10, pady=8)
        ttk.Label(self.root, textvariable=self.detail, wraplength=390).pack(anchor="w", padx=10)
        self.rows: dict[str, tuple[object, object]] = {}
        self.closed = False
        self.root.update()

    def update(self, snapshot: AudioSnapshot, status: str, detail: str) -> None:
        if self.closed:
            return
        self.status.set(status)
        self.detail.set(detail)
        metrics = {"RMS": snapshot.rms, **snapshot.scores, "spectral flux": snapshot.flux,
                   "tension held": float(snapshot.tension)}
        for name, value in metrics.items():
            if name not in self.rows:
                frame = self.ttk.Frame(self.root)
                frame.pack(fill="x", padx=10, pady=3)
                label = self.ttk.Label(frame, width=24)
                label.pack(side="left")
                bar = self.ttk.Progressbar(frame, maximum=1, length=170)
                bar.pack(side="left")
                self.rows[name] = label, bar
            label, bar = self.rows[name]
            label.configure(text=f"{name}: {value:.4f}")
            bar.configure(value=min(1, max(0, value)))
        self.root.update_idletasks()
        self.root.update()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.root.destroy()
