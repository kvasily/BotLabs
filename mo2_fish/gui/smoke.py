"""Packaged smoke checks. Temporary test media never enters user profiles or bundles."""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
from pathlib import Path
import tempfile

import cv2
import numpy as np
import soundfile as sf

from gui.overlay import STYLE_BITS
from gui.video_teacher import extract_audio


def exercise_package(window, app) -> list[str]:
    report: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mo2fish-package-check-") as directory:
        root = Path(directory)
        wav = root / "roundtrip.wav"
        sf.write(wav, (0.1 * np.sin(np.arange(4410) * 2 * np.pi * 440 / 44100)).astype(np.float32), 44100)
        samples = extract_audio(wav, 0, 0.1)
        assert abs(len(samples) - 4800) <= 1
        assert float(np.std(samples)) > 0.01
        report.append("PASS: bundled FFmpeg and SoundFile decode/resample to 48 kHz mono")
        video = root / "roundtrip.avi"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 180))
        assert writer.isOpened(), "OpenCV video writer unavailable"
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        frame[:, :160] = [30, 120, 200]
        for _ in range(3):
            writer.write(frame)
        writer.release()
        capture = cv2.VideoCapture(str(video))
        ok, decoded = capture.read()
        capture.release()
        assert ok and decoded.shape == frame.shape
        report.append("PASS: packaged OpenCV VideoCapture decodes video frames")
    # Read-only COM/device discovery, no microphone stream is opened.
    from gui.com_audio import audio_apartment
    from audio.loopback import devices
    with audio_apartment():
        count = len(devices())
    report.append(f"PASS: SoundCard WASAPI enumeration ({count} endpoints); no capture stream opened")
    if app.platformName() == "windows":
        user32 = ct.WinDLL("user32")
        user32.GetForegroundWindow.restype = wt.HWND
        previous = user32.GetForegroundWindow()
        window.overlay.show()
        app.processEvents()
        hwnd = int(window.overlay.winId())
        get_style = user32.GetWindowLongPtrW if ct.sizeof(ct.c_void_p) == 8 else user32.GetWindowLongW
        get_style.argtypes, get_style.restype = [wt.HWND, ct.c_int], ct.c_ssize_t
        assert get_style(hwnd, -20) & STYLE_BITS == STYLE_BITS, "Missing native overlay styles"
        rect = wt.RECT()
        user32.GetWindowRect.argtypes = [wt.HWND, ct.POINTER(wt.RECT)]
        user32.GetWindowRect(hwnd, ct.byref(rect))
        assert (rect.right - rect.left, rect.bottom - rect.top) == (3840, 2160), "Overlay is not 4K in physical pixels"
        assert (rect.left, rect.top) == tuple(window.profile.data["game_origin"])
        assert user32.GetForegroundWindow() == previous, "Overlay stole focus"
        rendered = window.overlay.grab().toImage()
        assert rendered.pixelColor(rendered.width() - 2, rendered.height() - 2).alpha() == 0, "Overlay background is opaque"
        window.overlay.hide()
        report.append("PASS: native overlay is 3840x2160, correct origin, all click-through styles, and does not activate")
        report.append(f"Capture exclusion accepted by Windows: {window.overlay.capture_excluded}")
    return report
