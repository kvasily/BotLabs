"""Teacher document, slider ownership, and classic Save/Load regression tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

import cv2
import numpy as np
import yaml
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QCloseEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from gui.teacher_document import TeacherDocument
from gui.teacher_player import ScrubState
from gui.profiles import Profile
from gui.video_teacher import VideoTeacher
from test_coordmap import ImmediateJobs
from test_gui import APP, FakeBackend, MainWindow, profile_at


class TeacherDocumentTests(unittest.TestCase):
    def test_save_commits_both_universal_rois_and_boxes_to_open_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            before = profile.path.read_bytes()
            doc = TeacherDocument(profile)
            frame = np.full((1440, 2560, 3), 73, np.uint8)
            doc.replace("compass", frame, (10, 20, 100, 50))
            doc.replace("inventory", frame, (400, 200, 600, 400))
            doc.replace("hit_marker", frame, (100, 100, 40, 20))
            self.assertEqual(doc.save().path, profile.path)
            disk = yaml.safe_load(profile.path.read_text(encoding="utf-8"))
            self.assertNotEqual(before, profile.path.read_bytes())
            for role, expected in (("compass", [15, 30, 150, 75]),
                                   ("inventory", [600, 300, 900, 600]),
                                   ("hit_marker", [150, 150, 60, 30])):
                self.assertEqual(disk["rois"][role], expected)
                self.assertEqual(disk["teacher_boxes"][role], expected)
            png = profile.path.parent / disk["templates"]["hit_marker"]
            self.assertEqual(png.parent, profile.path.parent / "templates")
            self.assertEqual(cv2.imread(str(png)).shape, (30, 60, 3))
            self.assertFalse(doc.changes)

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
    def test_collapsed_audio_handle_can_resize_up_without_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                teacher.resize(1280, 1000)
                teacher.show()
                APP.processEvents()
                splitter = teacher.audio_splitter
                splitter.set_audio_expanded(False)
                APP.processEvents()
                handle = splitter.handle(1)
                self.assertTrue(handle.isVisible() and handle.isEnabled())
                point = QPoint(handle.width() - 30, handle.height() // 2)
                QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=point)
                self.assertGreater(splitter.audio.maximumHeight(), 0)
                self.assertEqual(splitter.sizes()[1], 0)
                splitter.setSizes([2, 1])  # Simulate the handle's upward resize.
                splitter.splitterMoved.emit(splitter.sizes()[0], 1)
                QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=point)
                APP.processEvents()
                self.assertTrue(splitter.audio_expanded)
                self.assertTrue(splitter.button.isChecked())
                self.assertGreaterEqual(splitter.sizes()[1], splitter.audio.minimumSizeHint().height())
                # A downward drag may collapse it again; a click with no drag
                # must leave the zero-height constraint in place afterwards.
                splitter.setSizes([1, 0])
                splitter.splitterMoved.emit(splitter.sizes()[0], 1)
                QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=point)
                self.assertFalse(splitter.audio_expanded)
                self.assertEqual(splitter.audio.maximumHeight(), 0)
                # Exercise Qt's actual handle mouse-move path as well as sizes.
                origin = handle.mapToGlobal(point)
                QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=point)
                target = origin - QPoint(0, 240)
                APP.sendEvent(handle, QMouseEvent(QEvent.Type.MouseMove, QPointF(handle.mapFromGlobal(target)),
                                                  QPointF(target), Qt.MouseButton.NoButton,
                                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                APP.sendEvent(handle, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(handle.mapFromGlobal(target)),
                                                  QPointF(target), Qt.MouseButton.LeftButton,
                                                  Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
                APP.processEvents()
                self.assertTrue(splitter.audio_expanded)
                self.assertGreaterEqual(splitter.sizes()[1], splitter.audio.minimumSizeHint().height())
                splitter.button.click()
                self.assertEqual(splitter.sizes()[1], 0)
                splitter.button.click()
                self.assertGreater(splitter.sizes()[1], 0)
            finally:
                teacher.close()

    def test_save_and_save_as_remember_only_current_video_and_reopen_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            first, second = Path(directory) / "first.mp4", Path(directory) / "last recording.mp4"
            first.touch()
            second.touch()
            with patch("gui.teacher_ui.FilePlayer.open") as opened:
                teacher = VideoTeacher(profile, ImmediateJobs())
                try:
                    teacher.open_file(first)
                    teacher.open_file(second)
                    self.assertTrue(teacher.save_document())  # Video-only edit is saveable.
                    disk = yaml.safe_load(profile.path.read_text(encoding="utf-8"))
                    self.assertEqual(disk["teacher_video"], str(second.resolve()))
                    self.assertFalse(list(profile.path.parent.rglob("*.mp4")))
                    opened.reset_mock()
                    teacher.sync_profile()
                    opened.assert_not_called()  # Refreshing editor must not reset playback.
                    teacher.open_file(first)
                    with patch("gui.teacher_ui.QInputDialog.getText", return_value=("Video copy", True)):
                        self.assertTrue(teacher.save_as())
                    clone = teacher.profile
                    self.assertEqual(clone.data["teacher_video"], str(first.resolve()))
                    self.assertEqual(yaml.safe_load(profile.path.read_text(encoding="utf-8"))["teacher_video"], str(second.resolve()))
                    self.assertFalse(list(store.root.rglob("*.mp4")))
                    opened.reset_mock()
                    with patch("gui.teacher_ui.QFileDialog.getOpenFileName", return_value=(str(profile.path), "")):
                        teacher.load_document()
                    opened.assert_called_once_with(second.resolve())
                    self.assertEqual(teacher.video_path, second.resolve())
                    self.assertFalse(teacher.document.dirty)
                finally:
                    teacher.document.load(teacher.profile)
                    teacher.close()
                opened.reset_mock()
                restored = VideoTeacher(Profile(clone.path), ImmediateJobs())
                try:
                    opened.assert_called_once_with(first.resolve())  # App-start construction.
                    self.assertFalse(restored.document.dirty)
                    restored.on_frame(np.zeros((1440, 2560, 3), np.uint8), 0, 1, 30)
                    self.assertIn("2560 × 1440", restored.video_label.text())
                    self.assertIn("×1.50", restored.warning.text())
                finally:
                    restored.close()

    def test_missing_or_empty_saved_video_reports_modal_and_clears_player(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            for saved in (str(Path(directory) / "renamed.mp4"), "", None):
                with self.subTest(saved=saved):
                    profile.patch("teacher_video", saved)
                    with patch("gui.teacher_ui.QMessageBox.warning") as warning:
                        teacher = VideoTeacher(profile, ImmediateJobs())
                        try:
                            warning.assert_called_once()
                            self.assertEqual(warning.call_args.args[2], f"The last used video couldn't be found:\n{saved or ''}")
                            self.assertIsNone(teacher.video_path)
                            self.assertIsNone(teacher.canvas.image)
                            self.assertTrue(teacher.source.player.source().isEmpty())
                            self.assertEqual(teacher.video_label.text(), "Video: —")
                            teacher.sync_profile()
                            warning.assert_called_once()  # Ordinary refresh doesn't repeat modal.
                            replacement = Path(directory) / "replacement.mp4"
                            replacement.touch()
                            with patch.object(teacher.source, "open"):
                                teacher.open_file(replacement)
                            self.assertTrue(teacher.save_document())
                            self.assertEqual(yaml.safe_load(profile.path.read_text(encoding="utf-8"))["teacher_video"], str(replacement.resolve()))
                        finally:
                            teacher.document.load(teacher.profile)
                            teacher.close()

    def test_drawn_roles_have_text_markers_during_redraw_and_after_save_load(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                teacher.canvas.set_frame(np.zeros((1440, 2560, 3), np.uint8))
                self.assertEqual(teacher.role_items["compass"].text(0), "compass")
                teacher.begin_draw()
                teacher.canvas.outer = (10, 20, 100, 50)
                teacher.finish_draw()
                self.assertEqual(teacher.role_items["compass"].text(0), "✓ compass")
                self.assertEqual(teacher.role_items["inventory"].text(0), "inventory")
                teacher.begin_draw()
                self.assertEqual(teacher.role_items["compass"].text(0), "✓ compass")
                teacher.canvas.outer = (20, 40, 100, 50)
                teacher.finish_draw()
                self.assertTrue(teacher.save_document())
                teacher.profile = Profile(profile.path)
                teacher.sync_profile()
                self.assertEqual(teacher.role_items["compass"].text(0), "✓ compass")
                self.assertEqual(teacher.role_items["inventory"].text(0), "inventory")
            finally:
                teacher.document.load(teacher.profile)
                teacher.close()

    def test_audio_panel_collapse_preserves_dirty_boxes_and_expands_canvas(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            messages = []
            teacher.message.connect(messages.append)
            try:
                teacher.resize(1280, 1000)
                teacher.show()
                teacher.activateWindow()
                APP.processEvents()
                teacher.document.replace("compass", np.zeros((1440, 2560, 3), np.uint8), (10, 20, 100, 50))
                crop = teacher.document.changes["compass"]
                height = teacher.canvas.height()
                button = teacher.audio_splitter.button
                self.assertIs(teacher.childAt(button.mapTo(teacher, button.rect().center())), button)
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                APP.processEvents()
                self.assertEqual(teacher.audio_splitter.sizes()[1], 0)
                self.assertEqual(teacher.audio_splitter.widget(1).maximumHeight(), 0)
                self.assertEqual(teacher.audio_splitter.widget(1).height(), 0)
                self.assertEqual(messages[-1], "Audio tools hidden")
                self.assertGreater(teacher.canvas.height(), height)
                self.assertIs(teacher.document.changes["compass"], crop)
                self.assertTrue(teacher.document.dirty)
                teacher.video_path = Path(directory) / "keyboard-test.mp4"
                with patch.object(teacher.source, "toggle") as toggle:
                    QTest.keyClick(teacher, Qt.Key.Key_Space)
                    toggle.assert_called_once()
                teacher.video_path = None
                teacher.sync_profile()
                self.assertFalse(teacher.audio_splitter.button.isChecked())
                teacher.resize(1280, 1100)
                teacher.wave.setMinimumHeight(120)  # Child layout changes must not reopen the pane.
                teacher.hide()
                teacher.show()
                APP.processEvents()
                self.assertEqual(teacher.audio_splitter.sizes()[1], 0)
                self.assertEqual(teacher.audio_splitter.widget(1).height(), 0)
                teacher.resize(1280, 1000)
                teacher.wave.setMinimumHeight(70)
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                APP.processEvents()
                self.assertGreater(teacher.audio_splitter.sizes()[1], 0)
                self.assertGreater(teacher.audio_splitter.widget(1).maximumHeight(), 0)
                self.assertGreaterEqual(teacher.audio_splitter.widget(1).height(), teacher.audio_splitter.widget(1).minimumSizeHint().height())
                self.assertEqual(messages[-1], "Audio tools shown")
                self.assertAlmostEqual(teacher.canvas.height(), height, delta=2)
                self.assertIs(teacher.document.changes["compass"], crop)
            finally:
                teacher.document.load(profile)
                teacher.close()

    def test_save_flushes_dirty_editor_and_reloads_active_yaml_without_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            window = MainWindow(store, profile, backend=FakeBackend(), observe=False)
            teacher = window.teacher
            messages = []
            teacher.message.connect(messages.append)
            try:
                data = yaml.safe_load(window.editor.toPlainText())
                data["counts_per_degree"] = 12.75
                window.editor.setPlainText(yaml.safe_dump(data))
                self.assertTrue(window.editing_dirty)
                teacher.document.replace("compass", np.zeros((1440, 2560, 3), np.uint8), (10, 20, 100, 50))
                window.controller.validated = "previous-validation"
                with patch.object(teacher.jobs, "run", side_effect=AssertionError("Save must complete synchronously")):
                    self.assertTrue(teacher.save_document())
                disk = yaml.safe_load(profile.path.read_text(encoding="utf-8"))
                self.assertEqual(disk["counts_per_degree"], 12.75)
                self.assertEqual(disk["rois"]["compass"], [15, 30, 150, 75])
                self.assertEqual(yaml.safe_load(window.editor.toPlainText()), disk)
                self.assertEqual(window.profile.path, profile.path)
                self.assertEqual(teacher.document_label.toolTip(), str(profile.path))
                self.assertFalse(window.editing_dirty)
                self.assertIsNone(window.controller.validated)
                self.assertEqual(messages[-1], f"Saved {profile.path}")
            finally:
                teacher.document.load(profile)
                window.close()

    def test_invalid_editor_is_preserved_without_blocking_teacher_save(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            window = MainWindow(store, profile, backend=FakeBackend(), observe=False)
            try:
                window.editor.setPlainText("broken: [")
                window.teacher.document.replace("inventory", np.zeros((1440, 2560, 3), np.uint8), (20, 40, 100, 50))
                self.assertTrue(window.teacher.save_document())
                disk = yaml.safe_load(profile.path.read_text(encoding="utf-8"))
                self.assertEqual(disk["rois"]["inventory"], [30, 60, 150, 75])
                drafts = list(profile.path.parent.glob("unsaved-editor-*.txt"))
                self.assertEqual(len(drafts), 1)
                self.assertEqual(drafts[0].read_text(encoding="utf-8"), "broken: [")
                self.assertEqual(yaml.safe_load(window.editor.toPlainText()), disk)
            finally:
                window.teacher.document.load(profile)
                window.close()

    def test_save_as_switches_open_profile_and_commits_only_to_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            before = profile.path.read_bytes()
            window = MainWindow(store, profile, backend=FakeBackend(), observe=False)
            teacher = window.teacher
            try:
                teacher.document.replace("compass", np.zeros((1440, 2560, 3), np.uint8), (20, 40, 100, 50))
                with patch("gui.teacher_ui.QInputDialog.getText", return_value=("New profile", True)):
                    self.assertTrue(teacher.save_as())
                target = store.root / "New profile" / "config.yaml"
                self.assertEqual(window.profile.path, target)
                self.assertEqual(teacher.document.profile.path, target)
                self.assertEqual(profile.path.read_bytes(), before)
                self.assertEqual(yaml.safe_load(target.read_text(encoding="utf-8"))["rois"]["compass"], [30, 60, 150, 75])
                teacher.document.replace("inventory", np.zeros((1440, 2560, 3), np.uint8), (10, 20, 100, 50))
                self.assertTrue(teacher.save_document())
                self.assertEqual(profile.path.read_bytes(), before)
                self.assertEqual(yaml.safe_load(target.read_text(encoding="utf-8"))["teacher_boxes"]["inventory"], [15, 30, 150, 75])
            finally:
                teacher.document.load(window.profile)
                window.close()

    def test_empty_failed_and_missing_result_saves_report_accurately(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            messages = []
            teacher.message.connect(messages.append)
            try:
                with patch.object(teacher.document, "save") as save:
                    self.assertTrue(teacher.save_document())
                    save.assert_not_called()
                self.assertIn("Nothing to commit", messages[-1])
                teacher.document.replace("compass", np.zeros((1440, 2560, 3), np.uint8), (10, 20, 100, 50))
                for options, expected in (({"side_effect": OSError("disk unavailable")}, "disk unavailable"),
                                          ({"return_value": None}, "returned no Profile")):
                    with patch.object(teacher.document, "save", **options):
                        self.assertFalse(teacher.save_document())
                    self.assertIn("Save failed", messages[-1])
                    self.assertIn(expected, messages[-1])
                    self.assertTrue(teacher.document.dirty)
                    self.assertFalse(teacher.pending_write)
                    self.assertTrue(teacher.isEnabled())
            finally:
                teacher.document.load(profile)
                teacher.close()

    def test_draw_freezes_real_bgr_pixels_and_reports_missing_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            messages = []
            teacher.message.connect(messages.append)
            try:
                frame = np.full((1440, 2560, 3), (9, 27, 81), np.uint8)
                teacher.canvas.set_frame(frame)
                teacher.canvas.set_image(teacher.canvas.image.copy())  # QVideoSink's lazy conversion path.
                teacher.begin_draw()
                teacher.canvas.outer = (10, 20, 100, 50)
                with patch.object(type(teacher.canvas), "frame", new_callable=PropertyMock, return_value=None):
                    teacher.finish_draw()
                self.assertFalse(messages)
                np.testing.assert_array_equal(teacher.document.changes["compass"].pixels, frame[20:70, 10:110])
                teacher.draw_frame = None
                teacher.document.begin("inventory")
                teacher.canvas.active_role = "inventory"
                with patch.object(type(teacher.canvas), "frame", new_callable=PropertyMock, return_value=None):
                    teacher.finish_draw()
                self.assertIsNone(teacher.document.changes["inventory"])
                self.assertIn("No decoded BGR frame", messages[-1])
            finally:
                teacher.document.load(profile)
                teacher.close()

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
