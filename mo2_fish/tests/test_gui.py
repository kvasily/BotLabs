"""Offline GUI/wrapper tests. Test backends never send operating-system input."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from config import load_config
from gui.app import MainWindow, ReadOnlySmokeBackend, STYLE
from gui.overlay import Overlay, STYLE_BITS
from gui.preflight_view import validate_profile
from gui.profiles import Profile, ProfileStore, fingerprint
from gui.runtime import PermissionBackend, RuntimeController
from gui.video_teacher import FrameMapping, export_audio, native_crop, save_frame_role, extract_audio
from input.keys import InputController, Interrupted, SafetyGate
from tools.heading_calibrator import save_calibration

ROOT = Path(__file__).resolve().parents[1]
APP = QApplication.instance() or QApplication([])
APP.setStyleSheet(STYLE)


class FakeBackend(ReadOnlySmokeBackend):
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.title = "Mortal Online 2"

    def mouse(self, flags, dx=0, dy=0) -> None:
        self.events.append((flags, dx, dy))

    def key(self, name, down) -> None:
        self.events.append((name, down))

    def foreground_title(self) -> str:
        return self.title


def profile_at(directory: str) -> tuple[ProfileStore, Profile]:
    store = ProfileStore(Path(directory) / "profiles")
    return store, store.clone(ROOT / "config.yaml", "Test")


class ProfileTests(unittest.TestCase):
    def test_yaml_patch_save_as_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            profile.patch("counts_per_degree", -12.25)
            profile.patch("rois.compass", [100, 100, 160, 160])
            other = store.clone(profile.path, "Second Profile")
            other.patch("counts_per_degree", 9.5)
            self.assertEqual(load_config(profile.path)["counts_per_degree"], -12.25)
            self.assertEqual(load_config(other.path)["counts_per_degree"], 9.5)
            self.assertEqual(store.startup().path, other.path)
            self.assertEqual(other.data["tasks"]["bassle"]["fish_name"], "Bassle")
            self.assertEqual(other.data["tasks"]["redline_torp"]["fish_name"], "Redline Torp")

    def test_profile_escape_and_overwrite_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            for name in ("../outside", "CON", "Test", "bad/name", "trailing "):
                with self.assertRaises(ValueError):
                    store.clone(profile.path, name)

    def test_validate_calls_existing_engine_and_todo_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            passed, lines, token = validate_profile(profile.path)
            self.assertFalse(passed)
            self.assertIsNone(token)
            self.assertTrue(any("counts_per_degree" in line for line in lines))
            self.assertTrue(any("splash.wav" in line for line in lines))
            with patch("gui.preflight_view.check_assets", side_effect=RuntimeError("original engine marker")) as validator:
                passed, lines, _ = validate_profile(profile.path)
                self.assertEqual(lines, ["original engine marker"])
                validator.assert_called_once()

    def test_fingerprint_changes_on_asset_and_yaml_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            before = fingerprint(profile.path)
            asset = profile.asset("sfx", "splash.wav")
            sf.write(asset, np.linspace(-0.1, 0.1, 2000), 48000)
            after_asset = fingerprint(profile.path)
            self.assertNotEqual(before, after_asset)
            profile.patch("counts_per_degree", 10)
            self.assertNotEqual(after_asset, fingerprint(profile.path))

    def test_save_as_copies_actual_external_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            external = Path(directory) / "external.wav"
            sf.write(external, np.linspace(-0.1, 0.1, 2000), 48000)
            profile.patch("audio.templates.splash", str(external))
            copy = store.clone(profile.path, "Copied")
            relative = copy.data["audio"]["templates"]["splash"]
            self.assertFalse(Path(relative).is_absolute())
            self.assertEqual(copy.settings.asset(relative).read_bytes(), external.read_bytes())

    def test_calibration_reuses_signed_scale_and_saves_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            self.assertEqual(save_calibration(profile.path, 300, 330), -10)
            self.assertEqual(load_config(profile.path)["counts_per_degree"], -10)
            with self.assertRaises(ValueError):
                save_calibration(profile.path, 300, 179)


class AuthoringTests(unittest.TestCase):
    def test_letterbox_preview_to_native_4k_roi(self) -> None:
        mapping = FrameMapping(3840, 2160, 1000, 1000)
        self.assertEqual(mapping.display, (0, 218.75, 1000, 562.5))
        box = mapping.box((100, 275), (200, 331.25))
        self.assertEqual(box, (384, 216, 384, 216))
        self.assertEqual(mapping.config_box(box), [384, 216, 384, 216])
        self.assertEqual(mapping.box((-100, 0), (1100, 1000)), (0, 0, 3840, 2160))

    def test_non_4k_mapping_preserves_aspect_ratio_and_letterbox(self) -> None:
        self.assertEqual(FrameMapping(1920, 1080, 960, 540).config_box((100, 50, 80, 40)), [200, 100, 160, 80])
        self.assertEqual(FrameMapping(640, 480, 640, 480).config_box((0, 0, 640, 480)), [480, 0, 2880, 2160])

    def test_fake_frame_crop_saves_native_pixels_and_existing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            frame = np.random.default_rng(21).integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
            outer, inner = (100, 50, 120, 80), (110, 60, 20, 30)
            output = save_frame_role(profile, frame, outer, inner, "hit_marker")
            decoded = cv2.imdecode(np.fromfile(output, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertTrue(np.array_equal(decoded, frame[60:90, 110:130]))
            self.assertEqual(load_config(profile.path)["rois"]["hit_marker"], [200, 100, 240, 160])
            self.assertEqual(profile.data["templates"]["hit_marker"], "templates/hit_marker.png")
            with self.assertRaisesRegex(ValueError, "inside"):
                save_frame_role(profile, frame, outer, (0, 0, 20, 20), "hit_marker")

    def test_task_source_and_logout_roles_use_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            frame = np.random.default_rng(2).integers(0, 255, (2160, 3840, 3), dtype=np.uint8)
            save_frame_role(profile, frame, (20, 30, 100, 100), None, "bait_redline_torp_source")
            self.assertEqual(profile.data["tasks"]["redline_torp"]["bait_inventory_rect"], [20, 30, 100, 100])
            save_frame_role(profile, frame, (40, 60, 100, 100), None, "logout_success")
            self.assertEqual(profile.data["logout"]["success_roi"], [40, 60, 100, 100])

    def test_export_is_48k_mono_and_bounded_by_detector_ring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            samples = np.random.default_rng(12).normal(0, 0.1, 96000).astype(np.float32)
            output, peak = export_audio(profile, samples, 0.2, 0.3, "tension")
            data, rate = sf.read(output)
            self.assertEqual(rate, 48000)
            self.assertEqual(data.shape, (4800,))
            self.assertGreater(peak, 0)
            self.assertEqual(profile.data["audio"]["templates"]["tension"], "sfx/tension.wav")
            with self.assertRaises(ValueError):
                export_audio(profile, samples, 0, 1.1, "splash")

    def test_bundled_ffmpeg_resamples_real_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.wav"
            signal = np.sin(np.arange(22050) * 2 * np.pi * 440 / 44100).astype(np.float32) * 0.1
            sf.write(path, signal, 44100)
            decoded = extract_audio(path, 0, 0.2)
            self.assertAlmostEqual(len(decoded) / 48000, 0.2, delta=0.001)
            self.assertGreater(float(np.std(decoded)), 0.01)


class SafetyTests(unittest.TestCase):
    def test_disarmed_cleanup_sends_nothing(self) -> None:
        raw, gate = FakeBackend(), SafetyGate()
        guarded = PermissionBackend(raw, lambda: False)
        io = InputController(gate, guarded, "Mortal Online 2")
        gate.pause("Disarmed")
        io.release_all()
        gate.pause("Quit", exit_=True)
        self.assertEqual(raw.events, [])

    def test_pause_releases_owned_inputs_once_then_disarmed_is_silent(self) -> None:
        raw, gate = FakeBackend(), SafetyGate()
        armed = True
        guarded = PermissionBackend(raw, lambda: armed and gate.active)
        io = InputController(gate, guarded, "Mortal Online 2")
        gate.resume()
        with gate.session():
            io.button("right", True)
            io.key("e", True)
        gate.pause("Panic from GUI")
        armed = False
        before = list(raw.events)
        io.release_all()
        self.assertEqual(before, raw.events)
        self.assertEqual(raw.events, [(8, 0, 0), ("e", True), (16, 0, 0), ("e", False)])
        with self.assertRaises(Interrupted):
            guarded.mouse(1, 10)

    def test_gui_startup_never_constructs_audio_fsm_or_hotkeys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            raw = FakeBackend()
            with patch("input.keys.Hotkeys.__init__", side_effect=AssertionError("No GUI hotkeys")), \
                 patch("audio.loopback.LoopbackAudio.__init__", side_effect=AssertionError("No startup audio")), \
                 patch("fsm.states.AssistantFSM.__init__", side_effect=AssertionError("No startup FSM")):
                window = MainWindow(store, profile, backend=raw, observe=False)
                self.assertFalse(window.controller.armed)
                self.assertFalse(window.start_button.isEnabled())
                window.controller.set_armed(True)
                self.assertFalse(window.controller.gate.active)
                self.assertFalse(window.start_button.isEnabled())
                window.close()
            self.assertEqual(raw.events, [])

    def test_start_requires_arm_focus_and_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            controller = RuntimeController(profile.path, FakeBackend(), observe=False)
            self.assertFalse(controller.start())
            controller.set_armed(True)
            self.assertFalse(controller.start())
            controller.backend.title = "Another app"
            self.assertFalse(controller.start(override=True))
            controller.close()

    def test_override_does_not_bypass_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            raw = FakeBackend()
            controller = RuntimeController(profile.path, raw, observe=False)
            controller.set_armed(True)
            with patch("gui.runtime.start_assistant", side_effect=AssertionError("Must never construct runtime")) as factory:
                self.assertTrue(controller.start(override=True))
                deadline = time.monotonic() + 3
                while controller.starting and time.monotonic() < deadline:
                    time.sleep(0.01)
                factory.assert_not_called()
            self.assertFalse(controller.gate.active)
            self.assertEqual(raw.events, [])
            controller.close()

    def test_pause_during_startup_cannot_resume_gate_later(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            raw = FakeBackend()
            controller = RuntimeController(profile.path, raw, observe=False)
            controller.accept_validation(fingerprint(profile.path))
            controller.set_armed(True)
            entered, release = threading.Event(), threading.Event()
            runtime = SimpleNamespace(close=Mock())
            def construct(*args, **kwargs):
                entered.set()
                release.wait(2)
                return runtime
            with patch("gui.runtime.check_assets"), patch("gui.runtime.start_assistant", side_effect=construct):
                self.assertTrue(controller.start())
                self.assertTrue(entered.wait(2))
                controller.pause("Panic during startup")
                release.set()
                deadline = time.monotonic() + 3
                while controller.starting and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(controller.gate.active)
            self.assertFalse(controller.started)
            self.assertEqual(raw.events, [])
            self.assertTrue(runtime.close.called)
            controller.close()

    def test_focus_loss_pauses_and_never_auto_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            raw = FakeBackend()
            controller = RuntimeController(profile.path, raw, observe=False)
            controller.accept_validation(fingerprint(profile.path))
            controller.armed, controller.started = True, True
            controller.gate.resume()
            raw.title = "Not the game"
            controller.observer.start()
            time.sleep(0.2)
            self.assertFalse(controller.gate.active)
            raw.title = "Mortal Online 2"
            time.sleep(0.15)
            self.assertFalse(controller.gate.active)
            self.assertFalse(controller.started)
            controller.close()

    def test_overlay_qt_flags_and_required_native_bits(self) -> None:
        overlay = Overlay()
        self.assertTrue(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertEqual(STYLE_BITS & 0x08080020, 0x08080020)
        self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        overlay.close()


if __name__ == "__main__":
    unittest.main()
