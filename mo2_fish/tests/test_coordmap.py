"""Real pixel/PNG regression checks for Steam 1440p → live 4K authoring."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
import soundfile as sf
import yaml

from config import ConfigError, Settings, load_config
from gui.coordmap import SpaceMap
from gui.profiles import Profile
from gui.rescale import rescale_profile
from gui.video_teacher import FrameCanvas, VideoTeacher, save_frame_role
from main import check_assets
from test_gui import APP, FakeBackend, MainWindow, profile_at
from test_project import settings


class ImmediateJobs:
    def run(self, work, success, failed):
        try:
            result = work()
        except Exception as exc:
            failed(str(exc))
        else:
            success(result)


class CoordinateTests(unittest.TestCase):
    def test_steam_1440p_to_4k_center_full_frame_and_box(self):
        for mode in ("fit", "stretch"):
            mapping = SpaceMap(2560, 1440, 3840, 2160, 1000, 1000, mode)
            self.assertEqual(mapping.preview_to_video(500, 500), (1280, 720))
            self.assertEqual(mapping.video_to_game(1280, 720), (1920, 1080))
            self.assertEqual(mapping.video_to_game_rect(0, 0, 2560, 1440), (0, 0, 3840, 2160))
            self.assertEqual(mapping.video_to_game_rect(10, 20, 100, 50), (15, 30, 150, 75))

    def test_preview_bars_and_far_edges(self):
        mapping = SpaceMap(2560, 1440, 3840, 2160, 1000, 1000)
        for point in ((500, 100), (500, 900), (-1, 500), (1000, 500)):
            self.assertIsNone(mapping.preview_to_video(*point))
        self.assertEqual(mapping.preview_to_video(999.99, 781.24), (2559, 1439))
        self.assertEqual(mapping.preview_to_video_rect((0, 218.75), (1000, 781.25)), (0, 0, 2560, 1440))

    def test_fit_wide_video_has_top_bottom_pads_and_inverse(self):
        # A wider source produces top/bottom pads; a narrower source side pads.
        mapping = SpaceMap(3360, 1440, 3840, 2160, 1000, 600)
        self.assertTrue(mapping.aspect_mismatch)
        x, y, w, h = mapping.video_to_game_rect(0, 0, 3360, 1440)
        self.assertEqual((x, w), (0, 3840))
        self.assertGreater(y, 0)
        self.assertLess(h, 2160)
        self.assertIsNone(mapping.game_to_preview_rect(0, 0, 100, 100))
        box = mapping.video_to_game_rect(500, 400, 200, 100)
        preview = mapping.game_to_preview_rect(*box)
        ox, oy, _, _ = mapping.preview_rect
        expected = (ox + 500 * mapping.scale_pv, oy + 400 * mapping.scale_pv,
                    200 * mapping.scale_pv, 100 * mapping.scale_pv)
        for actual, target in zip(preview, expected):
            self.assertAlmostEqual(actual, target, delta=0.5)
        self.assertEqual(SpaceMap(640, 480, 3840, 2160, 640, 480).video_to_game_rect(0, 0, 640, 480), (480, 0, 2880, 2160))

    def test_stretch_differs_from_fit_for_mismatched_aspect(self):
        mapping = SpaceMap(640, 480, 1920, 1080, 640, 480, "stretch")
        self.assertEqual(mapping.video_to_game_rect(0, 0, 640, 480), (0, 0, 1920, 1080))
        self.assertEqual(mapping.video_to_game_rect(10, 20, 100, 40), (30, 45, 300, 90))

    def test_rounding_clamping_and_tiny_box_rejection(self):
        mapping = SpaceMap(2560, 1440, 3840, 2160, 2560, 1440)
        self.assertEqual(mapping.video_to_game_rect(1, 1, 3, 3), (2, 2, 4, 4))
        self.assertEqual(mapping.video_to_game_rect(-10, -10, 3000, 2000), (0, 0, 3840, 2160))
        with self.assertRaises(ValueError):
            SpaceMap(3840, 2160, 1920, 1080, 960, 540).video_to_game_rect(0, 0, 2, 2)

    def test_shrink_area_enlarge_cubic_and_rounding_consistency(self):
        crop = np.random.default_rng(3).integers(0, 255, (50, 100, 3), dtype=np.uint8)
        up = SpaceMap(2560, 1440, 3840, 2160, 2560, 1440)
        self.assertTrue(np.array_equal(up.scale_image_video_to_game(crop, (10, 20, 100, 50)),
                                       cv2.resize(crop, (150, 75), interpolation=cv2.INTER_CUBIC)))
        down = SpaceMap(3840, 2160, 1920, 1080, 960, 540)
        self.assertTrue(np.array_equal(down.scale_image_video_to_game(crop),
                                       cv2.resize(crop, (50, 25), interpolation=cv2.INTER_AREA)))
        odd = crop[:3, :3]
        rect = (1, 1, 3, 3)
        self.assertEqual(up.scale_image_video_to_game(odd, rect).shape[:2], (4, 4))

    def test_saved_1440p_png_and_yaml_pass_original_preflight_at_4k(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Settings(Path(directory) / "config.yaml", settings().data)
            rng = np.random.default_rng(55)
            for path in cfg.required_assets():
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == ".wav":
                    sf.write(path, rng.normal(0, 0.1, 2400), 48000)
                else:
                    cv2.imencode(".png", rng.integers(0, 255, (12, 15), dtype=np.uint8))[1].tofile(path)
            cfg.path.write_text(yaml.safe_dump(cfg.data), encoding="utf-8")
            profile = Profile(cfg.path)
            frame = rng.integers(0, 255, (1440, 2560, 3), dtype=np.uint8)
            png = save_frame_role(profile, frame, (10, 20, 100, 50), (11, 21, 60, 30), "hit_marker")
            image = cv2.imdecode(np.fromfile(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(image.shape[:2], (45, 90))
            self.assertEqual(profile.data["rois"]["hit_marker"], [15, 30, 150, 75])
            self.assertEqual(profile.data["video_source_resolution"], [2560, 1440])
            check_assets(load_config(cfg.path))


class ResolutionMigrationTests(unittest.TestCase):
    def test_rescale_references_keeps_originals_and_moves_local_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            profile.patch("resolution", [2560, 1440])
            frame = np.random.default_rng(8).integers(0, 255, (1440, 2560, 3), dtype=np.uint8)
            image_path = save_frame_role(profile, frame, (10, 20, 100, 50), None, "hit_marker")
            profile.patch("rois.compass", [100, 100, 100, 100])
            profile.patch("compass.center", [50, 50])
            profile.patch("compass.ring_radius", 40)
            profile.patch("tasks.bassle.hook_inventory_rect", [100, 200, 50, 50])
            profile.patch("logout.sequence", [{"click": [1000, 800]}])
            profile.patch("logout.success_roi", [0, 0, 200, 100])
            original_png, original_yaml = image_path.read_bytes(), profile.path.read_bytes()
            self.assertEqual(rescale_profile(profile, (3840, 2160), "fit"), 1)
            self.assertEqual(profile.data["rois"]["hit_marker"], [15, 30, 150, 75])
            self.assertEqual(profile.data["compass"]["center"], [75, 75])
            self.assertEqual(profile.data["compass"]["ring_radius"], 60)
            self.assertEqual(profile.data["tasks"]["bassle"]["hook_inventory_rect"], [150, 300, 75, 75])
            self.assertEqual(profile.data["logout"]["sequence"][0]["click"], [1500, 1200])
            self.assertEqual(profile.data["logout"]["success_roi"], [0, 0, 300, 150])
            resized_path = profile.settings.asset(profile.data["templates"]["hit_marker"])
            self.assertNotEqual(resized_path, image_path)
            self.assertEqual(cv2.imdecode(np.fromfile(resized_path, np.uint8), cv2.IMREAD_COLOR).shape[:2], (75, 150))
            self.assertEqual(image_path.read_bytes(), original_png)
            self.assertEqual(next(profile.path.parent.glob("config.before-resolution-*.yaml")).read_bytes(), original_yaml)

    def test_failed_rescale_leaves_live_yaml_and_pngs_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            profile.patch("rois.hit_marker", [0, 0, 100, 100])
            png = profile.asset("templates", "hit_marker.png")
            png.write_bytes(b"not a PNG")
            original = profile.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Cannot rescale"):
                rescale_profile(profile, (1920, 1080), "fit")
            self.assertEqual(profile.path.read_bytes(), original)
            self.assertEqual(png.read_bytes(), b"not a PNG")

    def test_arbitrary_resolution_validates_and_checks_its_bounds(self):
        for size in ([1920, 1080], [2560, 1440], [5120, 2160]):
            cfg = settings()
            cfg.data["resolution"] = size
            cfg.validate(assets=False)
            cfg.data["tasks"]["bassle"]["hook_inventory_rect"] = [size[0] - 10, 0, 20, 20]
            with self.assertRaisesRegex(ConfigError, "outside"):
                cfg.validate(assets=False)
        for size in ("TODO", [0, 1080], [1920.0, 1080], [True, 1080]):
            cfg = settings()
            cfg.data["resolution"] = size
            with self.assertRaisesRegex(ConfigError, "resolution must"):
                cfg.validate(assets=False)

    def test_live_capture_still_uses_game_roi_and_origin(self):
        from vision.capture import ScreenCapture
        cfg = settings()
        cfg.data["resolution"] = [1920, 1080]
        cfg.data["game_origin"] = [-1920, 25]
        cfg.data["rois"]["hit_marker"] = [10, 20, 100, 50]
        capture = ScreenCapture(cfg)
        capture.local.capture = Mock()
        capture.local.capture.grab.return_value = np.zeros((50, 100, 4), np.uint8)
        self.assertEqual(capture.grab("hit_marker").shape, (50, 100, 3))
        capture.local.capture.grab.assert_called_once_with({"left": -1910, "top": 45, "width": 100, "height": 50})


class TeacherUiTests(unittest.TestCase):
    def test_profile_default_metadata_readout_and_preview_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            profile.patch("resolution", [1920, 1080])
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                self.assertEqual(teacher.canvas.game_size, (1920, 1080))
                teacher.on_metadata(1920, 1080)
                teacher.on_frame(np.zeros((1440, 2560, 3), np.uint8), 0, 100, 30)
                self.assertIn("Video: 2560 × 1440", teacher.video_label.text())
                self.assertIn("Container: 1920 × 1080", teacher.video_label.text())
                self.assertEqual(profile.data["video_source_resolution"], [2560, 1440])
                before = profile.path.read_bytes()
                teacher.canvas.resize(800, 600)
                APP.processEvents()
                self.assertEqual(before, profile.path.read_bytes())
                teacher.canvas.resize(1000, 1000)
                mouse = Mock()
                mouse.position.return_value.x.return_value = 500
                mouse.position.return_value.y.return_value = 500
                teacher.canvas.mouseMoveEvent(mouse)
                self.assertIn("video (1280, 720)", teacher.cursor_info.text())
                self.assertIn("game (960, 540)", teacher.cursor_info.text())
            finally:
                teacher.close_workers()
                teacher.close()

    def test_resolution_change_declined_rescale_still_invalidates_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            profile.patch("rois.hit_marker", [10, 20, 100, 50])
            window = MainWindow(store, profile, backend=FakeBackend(), observe=False)
            window.teacher.jobs = ImmediateJobs()
            try:
                window.controller.validated = "old validation"
                window.teacher.change_target((2560, 1440), "fit", False)
                self.assertIsNone(window.controller.validated)
                self.assertFalse(window.controller.armed)
                self.assertEqual(profile.data["rois"]["hit_marker"], [10, 20, 100, 50])
                self.assertEqual(window.overlay.resolution, (2560, 1440))
                self.assertEqual(window.teacher.canvas.game_size, (2560, 1440))
            finally:
                window.close()

    def test_bar_click_does_not_start_selection(self):
        from PySide6.QtCore import Qt
        canvas = FrameCanvas()
        canvas.resize(1000, 1000)
        canvas.set_frame(np.zeros((1440, 2560, 3), np.uint8))
        event = Mock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.position.return_value.x.return_value = 500
        event.position.return_value.y.return_value = 50
        canvas.mousePressEvent(event)
        self.assertIsNone(canvas.start_point)
        self.assertIsNone(canvas.outer)
        canvas.close()
