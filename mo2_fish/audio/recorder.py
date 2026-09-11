"""Hold a key to record a bounded, 48 kHz mono sound template."""
from __future__ import annotations

import ctypes as ct
from pathlib import Path
import time

import numpy as np
import soundfile as sf

from audio.detectors import rms
from audio.loopback import select_device


def record_clip(device_name: str, output: Path, virtual_key: int = 0x75,
                max_seconds: float = 0.9, hop_ms: int = 30) -> None:
    if not 0.02 <= max_seconds <= 0.95:
        raise ValueError("max_seconds must be 0.02–0.95 to fit the one-second detector ring")
    output.parent.mkdir(parents=True, exist_ok=True)
    mic = select_device(device_name)
    user32 = ct.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [ct.c_int]
    user32.GetAsyncKeyState.restype = ct.c_short
    count = round(48000 * hop_ms / 1000)
    chunks: list[np.ndarray] = []
    recording = False
    limit = round(48000 * max_seconds)
    print(f"Hold F{virtual_key - 0x6F} for the sound; release to save. Esc cancels. Maximum {max_seconds:.2f}s.")
    with mic.recorder(samplerate=48000, channels=2, blocksize=count) as recorder:
        while True:
            frames = recorder.record(numframes=count)
            if user32.GetAsyncKeyState(0x1B) & 0x8000:
                print("Cancelled; no file written.")
                return
            held = bool(user32.GetAsyncKeyState(virtual_key) & 0x8000)
            if held:
                recording = True
                chunks.append(np.asarray(frames, dtype=np.float32).mean(axis=1))
            if recording and (not held or sum(len(c) for c in chunks) >= limit):
                break
    clip = np.concatenate(chunks)[:limit]
    if len(clip) < 960 or rms(clip - clip.mean()) < 1e-6:
        raise ValueError("Recording was too short or silent; try again")
    sf.write(str(output), clip, 48000, subtype="PCM_16")
    peak_rms = max(rms(clip[i:i + count]) for i in range(0, len(clip), count))
    print(f"Saved {output.resolve()} ({len(clip) / 48000:.3f}s), peak RMS={peak_rms:.5f}")
    print(f"Suggested min_rms starting point: {peak_rms * 0.15:.5f}; NCC threshold: 0.75.")
    print("NCC threshold is only a starting point: validate positive clips and background negatives in the live meters.")
