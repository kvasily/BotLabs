"""Packaged smoke checks. Temporary test media never enters user profiles or bundles."""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import copy
from pathlib import Path
import tempfile

import cv2
import numpy as np
import soundfile as sf
import yaml

from gui.overlay import STYLE_BITS
from gui.video_teacher import extract_audio, save_frame_role
from gui.profiles import Profile
from config import Settings


def exercise_package(window, app, destination: Path | None = None) -> list[str]:
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
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (2560, 1440))
        assert writer.isOpened(), "OpenCV video writer unavailable"
        frame = np.zeros((1440, 2560, 3), dtype=np.uint8)
        frame[:, :1280] = [30, 120, 200]
        cv2.putText(frame, "1440p decoding / coordinate check", (400, 650), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        for _ in range(3):
            writer.write(frame)
        writer.release()
        capture = cv2.VideoCapture(str(video))
        ok, decoded = capture.read()
        capture.release()
        assert ok and decoded.shape == frame.shape
        report.append("PASS: packaged OpenCV VideoCapture decodes video frames")
        data = copy.deepcopy(window.profile.data)
        data["resolution"], data["video_scale_mode"] = [3840, 2160], "fit"
        path = root / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        profile = Profile(path)
        png = save_frame_role(profile, decoded, (10, 20, 100, 50), None, "hit_marker")
        assert cv2.imdecode(np.fromfile(png, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (75, 150)
        assert profile.data["rois"]["hit_marker"] == [15, 30, 150, 75]
        report.append("PASS: decoded 2560x1440 video exports a 150x75 game PNG and [15,30,150,75] ROI at 4K")
        teacher, original_profile = window.teacher, window.teacher.profile
        try:
            teacher.profile = profile
            teacher.document.load(profile)
            teacher.sync_profile()
            teacher.canvas.set_frame(decoded)
            teacher.reported_size = (2560, 1440)
            teacher.update_resolution_info()
            teacher.canvas.outer, teacher.canvas.inner = (400, 500, 1400, 300), (450, 560, 1200, 130)
            teacher.show_box()
            teacher.cursor_info.setText("preview (720, 400)  |  video (1280, 720)  |  game (1920, 1080)")
            assert "2560 × 1440" in teacher.video_label.text()
            assert "×1.50" in teacher.warning.text()
            window.tabs.setCurrentIndex(1)
            app.processEvents()
            if destination:
                window.grab().save(str(destination / "MO2Fish-teacher-1440p-to-4K.png"))
        finally:
            teacher.profile = original_profile
            teacher.document.load(original_profile)
            teacher.canvas.frame = teacher.canvas.image = None
            teacher.canvas.outer = teacher.canvas.inner = None
            teacher.video_label.setText("Video: —")
            teacher.warning.setText("Video authoring only. Runtime perception uses the live game capture.")
            teacher.crop_info.setText("Select a role and draw. Green = selected; red = other roles. Save commits all dirty boxes.")
            teacher.sync_profile()
            window.tabs.setCurrentIndex(0)
        from gui.teacher_checks import check_playback
        report.append(check_playback(window.teacher, app, root, destination))
        window.tabs.setCurrentIndex(0)
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
        assert (rect.right - rect.left, rect.bottom - rect.top) == tuple(window.profile.data["resolution"]), "Overlay does not match game resolution"
        assert (rect.left, rect.top) == tuple(window.profile.data["game_origin"])
        assert user32.GetForegroundWindow() == previous, "Overlay stole focus"
        rendered = window.overlay.grab().toImage()
        assert rendered.pixelColor(rendered.width() - 2, rendered.height() - 2).alpha() == 0, "Overlay background is opaque"
        try:
            for size in ([2560, 1440], [1920, 1080]):
                data = copy.deepcopy(window.profile.data)
                data["resolution"] = size
                window.overlay.configure(Settings(window.profile.path, data))
                app.processEvents()
                user32.GetWindowRect(hwnd, ct.byref(rect))
                actual = (rect.right - rect.left, rect.bottom - rect.top)
                assert actual == tuple(size), f"Overlay resize expected {size}, got {actual}, Qt DPR {window.overlay.devicePixelRatioF()}"
                assert user32.GetForegroundWindow() == previous
        finally:
            window.overlay.configure(window.profile.settings)
        window.overlay.hide()
        report.append(f"PASS: native overlay matches {window.profile.data['resolution']}, correct origin, all click-through styles, and does not activate")
        report.append("PASS: native overlay also resizes to 2560x1440 and 1920x1080 without activating")
        report.append(f"Capture exclusion accepted by Windows: {window.overlay.capture_excluded}")
    return report
