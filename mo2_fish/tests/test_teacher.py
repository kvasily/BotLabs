"""Teacher document, slider ownership, and classic Save/Load regression tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox

from gui.teacher_document import TeacherDocument
from gui.teacher_player import ScrubState
from gui.video_teacher import VideoTeacher
from test_coordmap import ImmediateJobs
from test_gui import APP, FakeBackend, MainWindow, profile_at


class TeacherDocumentTests(unittest.TestCase):
    def test_redrawing_compass_replaces_only_compass_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            profile.patch("rois.compass", [50, 50, 200, 200])
            profile.patch("rois.inventory", [1000, 1000, 500, 500])
            original = profile.path.read_bytes()
            doc = TeacherDocument(profile)
            doc.begin("compass")
            self.assertNotIn("compass", doc.rects())
            self.assertEqual(doc.rects()["inventory"], (1000, 1000, 500, 500))
            frame = np.zeros((1440, 2560, 3), np.uint8)
            doc.replace("compass", frame, (10, 20, 100, 50))
            self.assertEqual(doc.rects()["compass"], (15, 30, 150, 75))
            doc.begin("compass")
            doc.replace("compass", frame, (20, 40, 120, 60))
            self.assertEqual(doc.rects()["compass"], (30, 60, 180, 90))
            self.assertEqual(len(doc.changes), 1)
            self.assertTrue(doc.dirty)
            self.assertEqual(original, profile.path.read_bytes())

    def test_save_as_game_space_and_load_restores_all_outlines(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            original = profile.path.read_bytes()
            frame = np.random.default_rng(2).integers(0, 255, (1440, 2560, 3), np.uint8)
            doc = TeacherDocument(profile)
            doc.replace("hit_marker", frame, (10, 20, 100, 50))
            doc.replace("inventory", frame, (400, 200, 600, 400))
            saved = doc.save_as(store, "Second")
            self.assertEqual(saved.data["rois"]["hit_marker"], [15, 30, 150, 75])
            png = saved.settings.asset(saved.data["templates"]["hit_marker"])
            self.assertEqual(cv2.imdecode(np.fromfile(png, np.uint8), cv2.IMREAD_COLOR).shape[:2], (75, 150))
            self.assertFalse(doc.dirty)
            self.assertEqual(profile.path.read_bytes(), original)
            teacher = VideoTeacher(saved, ImmediateJobs())
            try:
                self.assertEqual(teacher.canvas.role_boxes["hit_marker"], (15, 30, 150, 75))
                self.assertEqual(teacher.canvas.role_boxes["inventory"], (600, 300, 900, 600))
                red = teacher.canvas.outline_color(False)
                green = teacher.canvas.outline_color(True)
                self.assertGreater(red.red(), red.green())
                self.assertLess(red.alpha(), 255)
                self.assertEqual(green.alpha(), 255)
                self.assertGreater(green.green(), green.red())
            finally:
                teacher.close()

    def test_task_template_does_not_overwrite_universal_search_region(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            frame = np.random.default_rng(2).integers(0, 255, (1440, 2560, 3), np.uint8)
            doc = TeacherDocument(profile)
            doc.replace("quest_list", frame, (100, 100, 600, 600))
            doc.replace("bassle", frame, (120, 130, 100, 20))
            doc.replace("redline_torp", frame, (120, 230, 150, 20))
            doc.save()
            self.assertEqual(profile.data["rois"]["quest_list"], [150, 150, 900, 900])
            self.assertEqual(profile.data["teacher_boxes"]["bassle"], [180, 195, 150, 30])

    def test_failure_keeps_document_dirty_and_current_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            doc = TeacherDocument(profile)
            doc.begin("compass")
            before = profile.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Finish"):
                doc.save()
            self.assertTrue(doc.dirty)
            self.assertEqual(before, profile.path.read_bytes())


class TeacherControlTests(unittest.TestCase):
    def test_slider_drag_ignores_player_updates(self):
        state = ScrubState()
        self.assertTrue(state.player_position(1000))
        state.dragging = True
        self.assertFalse(state.player_position(2000))
        self.assertEqual(state.position, 1000)
        state.dragging = False
        self.assertTrue(state.player_position(3000))

    def test_ui_scrub_seeks_without_resetting_source_or_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                teacher.on_duration(10000)
                teacher.on_position(1000)
                with patch.object(teacher.source, "seek") as seek:
                    teacher.begin_scrub()
                    teacher.timeline.setValue(4000)
                    teacher.scrub_to(4000)
                    teacher.on_position(1100)
                    self.assertEqual(teacher.timeline.value(), 4000)
                    self.assertEqual(teacher.position, 4000)
                    teacher.end_scrub()
                    seek.assert_called_with(4000)
                    teacher.on_position(4300)
                    self.assertEqual(teacher.timeline.value(), 4300)
                teacher.source.player.setPlaybackRate(1.5)
                with patch.object(teacher.source.player, "setSource") as source:
                    teacher.source.seek(1000)
                    source.assert_not_called()
                self.assertEqual(teacher.source.player.playbackRate(), 1.5)
            finally:
                teacher.close()

    def test_ignore_does_not_write_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                before = profile.path.read_bytes()
                teacher.audio_role.setCurrentText("ignore")
                teacher.save_audio()
                self.assertEqual(before, profile.path.read_bytes())
                self.assertFalse(list((profile.path.parent / "sfx").glob("*.wav")))
            finally:
                teacher.close()

    def test_close_cancel_discard_and_save_are_classic(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            frame = np.zeros((1440, 2560, 3), np.uint8)
            try:
                teacher.document.replace("inventory", frame, (10, 20, 100, 50))
                with patch("gui.teacher_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
                    self.assertFalse(teacher.maybe_save())
                    self.assertTrue(teacher.document.dirty)
                with patch("gui.teacher_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Discard):
                    self.assertTrue(teacher.maybe_save())
                    self.assertFalse(teacher.document.dirty)
                teacher.document.replace("inventory", frame, (10, 20, 100, 50))
                with patch("gui.teacher_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Save):
                    self.assertTrue(teacher.maybe_save())
                    self.assertEqual(profile.data["rois"]["inventory"], [15, 30, 150, 75])
            finally:
                teacher.document.load(profile)
                teacher.close()

    def test_parent_close_cancel_prevents_worker_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            window = MainWindow(store, profile, backend=FakeBackend(), observe=False)
            teacher = window.teacher
            teacher._close_host = window
            teacher.document.begin("compass")
            event = QCloseEvent()
            try:
                with patch("gui.teacher_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
                    self.assertTrue(teacher.eventFilter(window, event))
                    self.assertFalse(event.isAccepted())
                    self.assertFalse(window.controller.stop_event.is_set())
            finally:
                teacher.document.load(profile)
                window.close()
