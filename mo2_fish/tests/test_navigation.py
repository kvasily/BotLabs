"""GUI-only preflight navigation, focus hints, and field explanations."""
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from gui.field_help import HELP, HelpButton
from gui.fix_navigation import fields, yaml_line
from gui.preflight_view import validate_profile
from test_gui import APP, FakeBackend, MainWindow, profile_at


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        store, self.profile = profile_at(self.directory.name)
        self.profile.patch("rois.compass", [10, 20, 100, 100])
        self.window = MainWindow(store, self.profile, backend=FakeBackend(), observe=False)
        self.window.resize(1440, 1040)
        self.window.show()
        self.window.activateWindow()
        APP.processEvents()

    def tearDown(self):
        self.window.teacher.document.load(self.window.profile)
        self.window.close()
        self.directory.cleanup()

    def test_fix_button_targets_device_and_highlight_clears_on_focus(self):
        w = self.window
        w.preflight.show_result(False, ["audio_device_name: replace TODO"])
        item = w.preflight.items.item(0)
        row = w.preflight.items.itemWidget(item)
        next(button for button in row.findChildren(QPushButton) if button.text() == "FIX").click()
        APP.processEvents()
        self.assertIs(w.tabs.currentWidget(), w.audio_tab)
        self.assertEqual(w.setup_navigation.highlight.target_id, "audio_device_name")
        self.assertTrue(w.devices_combo.property("fixHighlighted"))
        self.assertEqual(w.devices_combo.property("fixTargetId"), "audio_device_name")
        APP.sendEvent(w.devices_combo, QFocusEvent(QEvent.Type.FocusIn))
        self.assertIsNone(w.setup_navigation.highlight.target_id)
        self.assertFalse(w.devices_combo.property("fixHighlighted"))
        w.tabs.setCurrentIndex(0)
        APP.processEvents()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(160, row.height()//2))
        self.assertIs(w.tabs.currentWidget(), w.audio_tab)

    def test_role_fix_selects_role_and_outlines_row_and_existing_canvas(self):
        w, teacher = self.window, self.window.teacher
        w.setup_navigation.fix_line("rois.compass: rectangle is outside the game")
        APP.processEvents()
        self.assertIs(w.tabs.currentWidget(), teacher)
        self.assertEqual(teacher.canvas.active_role, "compass")
        self.assertEqual(teacher.role_list.property("fixRole"), "compass")
        self.assertTrue(teacher.canvas.property("fixHighlighted"))
        self.assertEqual(teacher.role_list.currentItem(), teacher.role_items["compass"])
        APP.sendEvent(teacher.role_list, QFocusEvent(QEvent.Type.FocusIn))
        self.assertFalse(teacher.canvas.property("fixHighlighted"))
        self.assertIsNone(teacher.role_list.property("fixRole"))

    def test_yaml_fix_selects_exact_nested_line_without_editing_file(self):
        w = self.window
        before = self.profile.path.read_bytes()
        for key in ("counts_per_degree", "logout.sequence", "logout.success_template", "resolution", "game_origin", "window_title_contains", "compass.center"):
            w.setup_navigation.navigate(key)
            self.assertIs(w.tabs.currentWidget(), w.profile_tab)
            self.assertEqual(w.editor.property("fixTargetId"), key)
            self.assertTrue(w.editor.textCursor().selectedText().strip().startswith(key.split(".")[-1]+":"))
        self.assertFalse(w.editing_dirty)
        self.assertEqual(before, self.profile.path.read_bytes())
        self.assertEqual(yaml_line("audio:\n  templates:\n    splash: x.wav\n", "audio.templates.splash"), 2)

    def test_sound_and_missing_asset_fixes_route_to_owner_with_fallback(self):
        w = self.window
        teacher = w.teacher
        teacher.audio_splitter.set_audio_expanded(False)
        path = self.profile.settings.asset(self.profile.data["audio"]["templates"]["splash"])
        w.setup_navigation.fix_line(f"Missing asset: {path}")
        self.assertIs(w.tabs.currentWidget(), teacher)
        self.assertEqual(teacher.audio_role.currentText(), "splash")
        self.assertTrue(teacher.audio_splitter.audio_expanded)
        self.assertEqual(teacher.audio_role.property("fixTargetId"), "audio.templates.splash")
        path = self.profile.settings.asset(self.profile.data["templates"]["taskmaster"])
        w.setup_navigation.fix_line(f"Missing asset: {path}")
        self.assertEqual(teacher.canvas.active_role, "interact_prompt")
        w.setup_navigation.navigate("audio.templates")
        self.assertIs(w.tabs.currentWidget(), w.audio_clips)
        w.setup_navigation.fix_line("Unknown future check failed")
        self.assertIs(w.tabs.currentWidget(), w.profile_tab)
        self.assertIn("Profile YAML", w.message.text())
        self.assertIsNotNone(w.setup_navigation.highlight.target_id)

    def test_all_default_fields_have_help_and_help_click_or_delayed_hover(self):
        for key in ("counts_per_degree", "audio_device_name", "rois.compass"):
            self.assertIn(key, HELP)
        for key, _ in fields(self.profile.data):
            self.assertIn(key, HELP, key)
            self.assertNotIn("TODO", HELP[key])
        button = HelpButton("counts_per_degree")
        try:
            self.assertEqual(button.timer.interval(), 2000)
            with patch("gui.field_help.QToolTip.showText") as show:
                APP.sendEvent(button, QEnterEvent(QPointF(), QPointF(), QPointF()))
                self.assertTrue(button.timer.isActive())
                show.assert_not_called()
                APP.sendEvent(button, QEvent(QEvent.Type.Leave))
                self.assertFalse(button.timer.isActive())
                button.click()
                self.assertIn("Face North", show.call_args.args[1])
        finally:
            button.close()

    def test_actual_check_assets_rows_all_have_working_fix_actions(self):
        passed, lines, _ = validate_profile(self.profile.path)
        self.assertFalse(passed)
        w = self.window
        before = self.profile.path.read_bytes()
        w.preflight.show_result(passed, lines)
        for index in range(w.preflight.items.count()):
            item = w.preflight.items.item(index)
            row = w.preflight.items.itemWidget(item)
            fix = next(button for button in row.findChildren(QPushButton) if button.text() == "FIX")
            fix.click()
            self.assertIsNotNone(w.setup_navigation.highlight.target_id, item.text())
            self.assertTrue(w.message.text())
        self.assertEqual(before, self.profile.path.read_bytes())
