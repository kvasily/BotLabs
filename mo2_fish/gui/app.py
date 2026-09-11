"""MO2Fish desktop shell. No Hotkeys are instantiated on any GUI path."""
from __future__ import annotations

import argparse
import ctypes as ct
from ctypes import wintypes as wt
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import traceback

import yaml
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)

from audio.loopback import devices
from gui.jobs import Jobs
from gui.meters import LoopbackProbe, Meters
from gui.overlay import Overlay
from gui.preflight_view import PreflightView, validate_profile
from gui.profiles import Profile, ProfileStore, app_root, fingerprint
from gui.runtime import RuntimeController
from gui.video_teacher import VideoTeacher
from tools.heading_calibrator import save_calibration
from vision.capture import enable_dpi_awareness

STYLE = """
QWidget { background:#101722; color:#d8e3ef; font-family:'Segoe UI'; font-size:10pt; }
QMainWindow { background:#0b111a; }
QLabel#title { font-size:22pt; font-weight:700; color:#f1f7ff; }
QLabel#eyebrow { color:#5ce0b3; font-size:9pt; font-weight:700; }
QLabel#muted { color:#8b9db2; font-size:9pt; }
QLabel#status { background:#192434; padding:10px; border-radius:6px; color:#a9bed3; }
QLabel#message { background:#152232; color:#b5cbe0; padding:8px; border-radius:5px; }
QPushButton { background:#233247; border:1px solid #34465e; padding:8px 15px; border-radius:5px; font-weight:600; }
QPushButton:hover { background:#30455d; border-color:#5789a5; }
QPushButton:disabled { color:#5e7188; background:#162130; border-color:#243448; }
QPushButton#start { background:#61dcb1; border-color:#61dcb1; color:#07231b; }
QPushButton#start:disabled { background:#1d413b; color:#52776e; border-color:#28544a; }
QPushButton#panic { background:#572934; color:#ffb5bf; border-color:#8c414f; }
QPushButton#arm:checked { background:#234638; color:#81edbd; border-color:#4c9275; }
QTabWidget::pane { border:1px solid #26364b; border-radius:7px; }
QTabBar::tab { background:#101722; color:#8c9fb5; padding:13px 23px; border-bottom:2px solid transparent; }
QTabBar::tab:selected { color:#77e9c0; border-bottom:2px solid #5ce0b3; background:#162333; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background:#0b131f; border:1px solid #34465b; border-radius:4px; padding:6px; }
QPlainTextEdit, QListWidget { background:#0b131f; border:1px solid #26384d; border-radius:5px; padding:8px; }
QListWidget::item { padding:6px; border-bottom:1px solid #17283b; }
QProgressBar { border:1px solid #2b3e55; border-radius:4px; background:#0b1420; text-align:center; min-height:20px; }
QProgressBar::chunk { background:#347b78; border-radius:3px; }
QCheckBox { spacing:8px; padding:5px; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #496279; border-radius:3px; background:#101a27; }
QCheckBox::indicator:checked { background:#5ce0b3; }
QSlider::groove:horizontal { height:5px; background:#253950; border-radius:2px; }
QSlider::handle:horizontal { width:13px; margin:-5px 0; border-radius:6px; background:#72dfc1; }
QScrollArea { border:none; }
QToolTip { color:#e1eaf5; background:#233447; border:1px solid #536c84; }
"""


class PreserveGameFocus:
    """Keep native game activation when a user clicks an exposed control button."""
    no_activate_controls: list[QWidget]

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            msg = wt.MSG.from_address(int(message))
            if msg.message == 0x21:  # WM_MOUSEACTIVATE: deliver click, do not activate us.
                widget = QApplication.widgetAt(QCursor.pos())
                if widget and any(control is widget or control.isAncestorOf(widget) for control in self.no_activate_controls):
                    return True, 3  # MA_NOACTIVATE (not MA_NOACTIVATEANDEAT).
        return super().nativeEvent(event_type, message)


class CalibrationDialog(PreserveGameFocus, QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Heading calibration")
        self.setModal(False)
        self.resize(590, 360)
        layout = QVBoxLayout(self)
        text = QLabel("Face exactly N in mouse-look mode. Click Send test movement while MO2 remains focused, then enter the observed heading. No full rotation: use a move of 2–150°.\n\nA test move requires Arm and a passed Validate. For first setup, save a provisional counts_per_degree value (for example 1), finish the other required setup, and Validate. The test uses exact counts, not that provisional scale.")
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.counts = QSpinBox()
        self.counts.setRange(1, 5000)
        self.counts.setValue(300)
        self.heading = QDoubleSpinBox()
        self.heading.setRange(0, 359.99)
        self.heading.setDecimals(2)
        form.addRow("Positive mouse counts", self.counts)
        form.addRow("Observed heading", self.heading)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.move_button = QPushButton("Send test movement")
        self.move_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.no_activate_controls = [self.move_button]
        self.move_button.clicked.connect(lambda: window.controller.start(calibration_counts=self.counts.value()))
        save = QPushButton("Save measured scale")
        save.clicked.connect(self.save)
        row.addWidget(self.move_button)
        row.addWidget(save)
        layout.addLayout(row)
        self.result = QLabel("Calibration does not register hotkeys. Pause / Panic remain available in the main window.")
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        window.controller.calibration_finished.connect(self.finished_move)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def finished_move(self, success: bool) -> None:
        self.result.setText("Movement complete. Enter the observed heading, then save." if success else "Movement cancelled. Face N again before another test.")

    def save(self) -> None:
        try:
            self.window.begin_authoring()
            scale = save_calibration(self.window.profile.path, self.counts.value(), self.heading.value())
            self.window.profile.reload()
            self.window.refresh_profile()
            self.result.setText(f"Saved {scale:.8f} counts/degree. Validate the updated profile before starting.")
        except Exception as exc:
            self.result.setText(str(exc))

    def closeEvent(self, event) -> None:
        if self.window.controller.calibrating or self.window.controller.starting:
            self.window.controller.pause("Heading calibration closed")
        super().closeEvent(event)


class MainWindow(PreserveGameFocus, QMainWindow):
    def __init__(self, store: ProfileStore, profile: Profile, *, backend=None, observe: bool = True) -> None:
        super().__init__()
        self.store, self.profile = store, profile
        self.jobs = Jobs(self)
        self.controller = RuntimeController(profile.path, backend=backend, observe=observe)
        self.controller.notice.connect(self.show_message)
        self.controller.changed.connect(self.update_controls)
        self.controller.snapshot.connect(self.on_snapshot)
        self.overlay = Overlay()
        self.overlay.configure(profile.settings)
        self.probe = LoopbackProbe()
        self.probe.level.connect(lambda value: self.device_meter.set_level("RMS", value))
        self.probe.failed.connect(self.show_message)
        self.probe.finished.connect(lambda: self.probe_button.setText("Test device RMS"))
        self.editing_dirty = False
        self.validation_request = 0
        self.latest: dict = {}
        self.calibration_dialog: CalibrationDialog | None = None
        self.setWindowTitle("MO2Fish • Fishing workspace")
        self.resize(1440, 1040)
        self.setMinimumSize(1100, 840)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        brand = QHBoxLayout()
        labels = QVBoxLayout()
        eyebrow = QLabel("MORTAL ONLINE 2  /  EXTERNAL ASSISTANT")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Fishing workspace")
        title.setObjectName("title")
        labels.addWidget(eyebrow)
        labels.addWidget(title)
        brand.addLayout(labels, 1)
        self.profile_badge = QLabel()
        self.profile_badge.setObjectName("muted")
        brand.addWidget(self.profile_badge)
        layout.addLayout(brand)
        controls = QHBoxLayout()
        self.arm, self.start_button, self.pause_button, self.panic_button = (QPushButton("Disarmed"), QPushButton("Start"), QPushButton("Pause"), QPushButton("Panic"))
        self.arm.setCheckable(True)
        self.arm.setObjectName("arm")
        self.start_button.setObjectName("start")
        self.panic_button.setObjectName("panic")
        self.no_activate_controls = [self.arm, self.start_button, self.pause_button, self.panic_button]
        for button in self.no_activate_controls:
            button.setMinimumWidth(116)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            controls.addWidget(button)
        self.arm.toggled.connect(self.controller.set_armed)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(lambda: self.controller.pause("Paused from GUI"))
        self.panic_button.clicked.connect(lambda: self.controller.pause("Panic from GUI"))
        hint = QLabel("Default disarmed   •   GUI controls only   •   Live screen perception")
        hint.setObjectName("muted")
        controls.addWidget(hint, 1, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(controls)
        self.status = QLabel("DISARMED  |  Focus pending  |  READY  |  No task  |  0/3")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self.make_control_tab(), "Control")
        self.teacher = VideoTeacher(profile, self.jobs)
        self.teacher.edited.connect(self.on_profile_authored)
        self.teacher.authoring.connect(self.begin_authoring)
        self.teacher.message.connect(self.show_message)
        self.teacher.busy_changed.connect(self.authoring_busy)
        self.tabs.addTab(self.teacher, "Teach from video")
        self.tabs.addTab(self.make_audio_tab(), "Audio device")
        self.tabs.addTab(self.make_profile_tab(), "Profile")
        self.tabs.addTab(self.make_overlay_tab(), "Overlay")
        from gui.audio_clips import AudioClips
        self.audio_clips = AudioClips()
        self.audio_clips.message.connect(self.show_message)
        self.audio_clips.audition.connect(self.begin_authoring)
        self.audio_clips.audition.connect(self.teacher.source.pause)
        self.tabs.addTab(self.audio_clips, "Audio clips")
        self.message = QLabel("Validate your setup to see exactly which files and fields are still missing.")
        self.message.setObjectName("message")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.refresh_profile()
        self.update_controls()
        if observe:
            QTimer.singleShot(250, self.refresh_devices)

    def make_control_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        headline = QLabel("Prepare → validate → arm → start")
        headline.setStyleSheet("font-size:15pt; font-weight:600; padding:12px 6px;")
        layout.addWidget(headline)
        columns = QHBoxLayout()
        self.preflight = PreflightView()
        self.preflight.button.clicked.connect(self.validate)
        columns.addWidget(self.preflight, 3)
        right = QVBoxLayout()
        self.runtime_detail = QLabel("Fishing brain idle\nNo audio stream or worker has been started.")
        self.runtime_detail.setWordWrap(True)
        right.addWidget(self.runtime_detail)
        self.meters = Meters()
        right.addWidget(self.meters)
        self.overlay_check = QCheckBox("Show click-through overlay")
        self.overlay_check.toggled.connect(self.toggle_overlay)
        right.addWidget(self.overlay_check)
        right.addStretch()
        columns.addLayout(right, 2)
        layout.addLayout(columns, 1)
        help_ = QLabel("Close game dialogs and restore mouse-look before Start. Focus MO2, then click the Start button from an exposed part of this window. It preserves the game’s focus. Losing focus pauses immediately; returning focus never resumes automatically.")
        help_.setWordWrap(True)
        help_.setObjectName("muted")
        layout.addWidget(help_)
        self.override = QLineEdit()
        self.override.setPlaceholderText("Optional: type OVERRIDE to request a fresh validation at Start; missing assets still block")
        self.override.textChanged.connect(self.update_controls)
        layout.addWidget(self.override)
        return tab

    def make_audio_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        title = QLabel("Choose the game’s playback loopback")
        title.setStyleSheet("font-size:16pt; font-weight:600; padding:10px;")
        layout.addWidget(title)
        text = QLabel("Use the named VB-CABLE / VoiceMeeter playback endpoint. Testing here opens only a microphone loopback probe; it does not start the fishing brain.")
        text.setWordWrap(True)
        layout.addWidget(text)
        row = QHBoxLayout()
        self.devices_combo = QComboBox()
        self.devices_combo.setMinimumWidth(420)
        self.devices_combo.setPlaceholderText("Refresh to discover WASAPI loopbacks")
        refresh, use = QPushButton("Refresh devices"), QPushButton("Use selected device")
        refresh.clicked.connect(self.refresh_devices)
        use.clicked.connect(self.use_device)
        row.addWidget(self.devices_combo, 1)
        row.addWidget(refresh)
        row.addWidget(use)
        layout.addLayout(row)
        self.device_meter = Meters(("RMS",))
        layout.addWidget(self.device_meter)
        self.probe_button = QPushButton("Test device RMS")
        self.probe_button.clicked.connect(self.test_device)
        layout.addWidget(self.probe_button)
        self.device_info = QLabel("Device idle. No audio is captured until you choose Test, Record live clip, or Start.")
        self.device_info.setWordWrap(True)
        layout.addWidget(self.device_info)
        layout.addStretch()
        return tab

    def make_profile_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        load, save, save_as, calibrate = (QPushButton("Load profile"), QPushButton("Save profile"), QPushButton("Save As…"), QPushButton("Calibrate heading"))
        load.clicked.connect(self.load_profile)
        save.clicked.connect(self.save_profile)
        save_as.clicked.connect(self.save_as)
        calibrate.clicked.connect(self.calibrate)
        for button in (load, save, save_as, calibrate):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.profile_summary = QLabel()
        self.profile_summary.setWordWrap(True)
        layout.addWidget(self.profile_summary)
        note = QLabel("Live YAML settings • the same schema used by the fishing engine. Edit game_origin, compass center/radius, thresholds, and your logout sequence here. Fish identities are fixed: Bassle and Redline Torp.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.textChanged.connect(self.yaml_changed)
        layout.addWidget(self.editor, 1)
        return tab

    def make_overlay_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        title = QLabel("Live ROI overlay")
        title.setStyleSheet("font-size:16pt; font-weight:600; padding:10px;")
        layout.addWidget(title)
        text = QLabel("A separate window uses the profile's game resolution and game_origin. Clicks pass through, it never activates, and its HUD is excluded from screen capture on supported Windows builds.")
        text.setWordWrap(True)
        layout.addWidget(text)
        self.overlay_toggle = QCheckBox("Overlay visible")
        self.overlay_toggle.toggled.connect(self.overlay_check.setChecked)
        self.labels_check = QCheckBox("Show ROI labels")
        self.labels_check.setChecked(True)
        self.labels_check.toggled.connect(lambda value: (setattr(self.overlay, "labels", value), self.overlay.update()))
        layout.addWidget(self.overlay_toggle)
        layout.addWidget(self.labels_check)
        self.roi_container = QWidget()
        self.roi_layout = QVBoxLayout(self.roi_container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.roi_container)
        layout.addWidget(scroll, 1)
        return tab

    def show_message(self, text: str) -> None:
        self.message.setText(text)

    def update_controls(self) -> None:
        if not hasattr(self, "override"):
            return
        controller = self.controller
        self.arm.blockSignals(True)
        self.arm.setChecked(controller.armed)
        self.arm.setText("Armed" if controller.armed else "Disarmed")
        self.arm.blockSignals(False)
        permitted = bool(controller.validated or self.override.text() == "OVERRIDE") and not self.editing_dirty
        self.start_button.setEnabled(controller.armed and permitted and not controller.starting and not controller.started)
        self.start_button.setText("Starting…" if controller.starting else "Start")

    def on_snapshot(self, data: dict) -> None:
        self.latest = data
        focus = "MO2 focused" if data["focused"] else "MO2 not focused"
        device = self.profile.data.get("audio_device_name", "TODO")
        task = self.profile.data.get("tasks", {}).get(data["task"], {}).get("fish_name", "No task")
        self.status.setText(f"{'ARMED' if data['armed'] else 'DISARMED'}   |   {focus}   |   {data['state']}   |   {task}   |   {data['count']}/3 verified\nAudio: {device}   •   {data['reason']}")
        color = "#5ce0b3" if data["started"] and data["focused"] else "#ffc36a" if data["armed"] or not data["focused"] else "#a9bed3"
        self.status.setStyleSheet(f"color:{color};")
        heading = f"{data['heading']:.1f}°" if data["heading"] is not None else "—"
        self.runtime_detail.setText(f"{data['state']}   •   {task}   •   {data['count']}/3 verified\nHeading {heading}\n{data['message'] or data['reason']}")
        self.meters.update_snapshot(data["audio"], self.profile.data.get("audio", {}).get("stale_seconds", 1.5))
        self.overlay.set_snapshot(data)
        self.update_controls()

    def start(self) -> None:
        if self.teacher.pending_write:
            self.show_message("Wait for the asset export to finish, then Validate.")
            return
        if self.probe.running or self.teacher.probe.running:
            self.show_message("Stop the authoring audio probe/recording before starting the fishing session.")
            return
        if self.overlay.isVisible() and not self.overlay.capture_excluded:
            self.overlay_check.setChecked(False)
            self.show_message("Overlay hidden because this Windows build could not exclude it from live captures.")
        self.controller.start(override=self.override.text() == "OVERRIDE")

    def begin_authoring(self) -> None:
        self.controller.set_armed(False)
        self.controller.invalidate("Authoring setup • Validate again before Start")
        self.validation_request += 1
        self.update_controls()

    def authoring_busy(self, busy: bool) -> None:
        self.tabs.setTabEnabled(3, not busy)
        self.preflight.button.setEnabled(not busy)
        self.show_message("Exporting the asset…" if busy else "Asset export finished • Validate the profile")

    def yaml_changed(self) -> None:
        self.editing_dirty = True
        self.controller.invalidate("Unsaved YAML changes • Save and Validate")
        self.validation_request += 1
        self.preflight.summary.setText("Settings changed • validation expired")
        self.update_controls()

    def parsed_editor(self) -> dict:
        data = yaml.safe_load(self.editor.toPlainText())
        if not isinstance(data, dict):
            raise ValueError("The profile must contain a YAML mapping")
        return data

    def save_profile(self) -> bool:
        try:
            if self.editing_dirty:
                self.begin_authoring()
                self.profile.write(self.parsed_editor())
            self.refresh_profile()
            self.store.remember(self.profile)
            self.show_message(f"Saved profile: {self.profile.path}")
            return True
        except Exception as exc:
            self.show_message(str(exc))
            return False

    def validate(self) -> None:
        if self.teacher.pending_write:
            self.show_message("Wait for the asset export to finish.")
            return
        if not self.save_profile():
            return
        self.controller.pause("Validating setup")
        self.validation_request += 1
        request, path = self.validation_request, self.profile.path
        self.preflight.button.setEnabled(False)
        self.preflight.summary.setText("Checking the saved YAML and every required asset…")
        def done(result) -> None:
            if request != self.validation_request or path != self.profile.path:
                self.preflight.button.setEnabled(True)
                return
            passed, lines, token = result
            self.preflight.show_result(passed, lines)
            if passed:
                self.controller.accept_validation(token)
                self.show_message("Validation passed. Arm, focus MO2, then click Start. The app never resumes on focus alone.")
            else:
                self.controller.validated = None
                self.show_message("Fill the listed fields or author the missing captures, then Validate again.")
            self.update_controls()
        self.jobs.run(lambda: validate_profile(path), done, self.show_message)

    def refresh_profile(self) -> None:
        self.profile.reload()
        data = self.profile.data
        resolution = " × ".join(map(str, data.get("resolution", [])))
        self.profile_badge.setText(f"PROFILE   {self.profile.path.parent.name}\n{resolution} • local assets")
        self.profile_summary.setText(f"{self.profile.path}\nGame capture: {resolution}    |    game_origin: {data.get('game_origin')}    |    counts_per_degree: {data.get('counts_per_degree')}")
        self.editor.blockSignals(True)
        self.editor.setPlainText(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        self.editor.blockSignals(False)
        self.editing_dirty = False
        self.teacher.profile = self.profile
        self.teacher.sync_profile()
        self.audio_clips.refresh(self.profile)
        self.overlay.configure(self.profile.settings)
        while self.roi_layout.count():
            item = self.roi_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name, rect in data.get("rois", {}).items():
            checkbox = QCheckBox(f"{name}   ·   {rect}")
            checkbox.setChecked(True)
            checkbox.toggled.connect(lambda value, n=name: self.toggle_roi(n, value))
            self.roi_layout.addWidget(checkbox)
        self.roi_layout.addStretch()
        self.update_controls()

    def on_profile_authored(self) -> None:
        self.refresh_profile()
        self.preflight.summary.setText("Assets changed • Validate again")

    def load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load profile", str(self.store.root), "YAML profile (config.yaml *.yaml)")
        if not path:
            return
        try:
            self.begin_authoring()
            self.profile = self.store.load(Path(path))
            self.controller.set_profile(self.profile.path)
            self.refresh_profile()
            self.preflight.items.clear()
            self.preflight.summary.setText("Profile loaded • not validated this session")
        except Exception as exc:
            self.show_message(str(exc))

    def save_as(self) -> None:
        name, ok = QInputDialog.getText(self, "Save profile as", "New profile name")
        if not ok or not name:
            return
        try:
            data = self.parsed_editor()
            self.begin_authoring()
            self.profile = self.store.clone(self.profile.path, name, data=data)
            self.controller.set_profile(self.profile.path)
            self.refresh_profile()
            self.show_message(f"Created {self.profile.path.parent.name} with its own YAML and assets")
        except Exception as exc:
            self.show_message(str(exc))

    def refresh_devices(self) -> None:
        def enumerate_() -> list[tuple[str, str]]:
            from gui.com_audio import audio_apartment
            with audio_apartment():
                return [(device.name, device.id) for device in devices()]
        def done(items) -> None:
            self.devices_combo.clear()
            for name, identifier in items:
                self.devices_combo.addItem(f"{name}  [{identifier}]", identifier)
            current = self.profile.data["audio_device_name"]
            index = next((i for i, (name, ident) in enumerate(items) if current in (name, ident)), -1)
            self.devices_combo.setCurrentIndex(index)
            self.device_info.setText(f"{len(items)} playback loopbacks found. Choose the endpoint receiving game audio.")
        self.jobs.run(enumerate_, done, self.show_message)

    def use_device(self) -> None:
        device = self.devices_combo.currentData()
        if device:
            self.begin_authoring()
            self.profile.patch("audio_device_name", device)
            self.refresh_profile()
            self.show_message(f"Audio device saved: {self.devices_combo.currentText()}")

    def test_device(self) -> None:
        self.begin_authoring()
        if self.probe.running:
            self.probe.stop()
            self.probe_button.setText("Stopping probe…")
            return
        device = self.devices_combo.currentData() or self.profile.data["audio_device_name"]
        try:
            self.probe.start(device)
            self.device_meter.state.setText("Live RMS probe • no fishing worker")
            self.probe_button.setText("Stop RMS test")
        except Exception as exc:
            self.show_message(str(exc))

    def toggle_roi(self, name: str, value: bool) -> None:
        if value:
            self.overlay.enabled_rois.add(name)
        else:
            self.overlay.enabled_rois.discard(name)
        self.overlay.update()

    def toggle_overlay(self, visible: bool) -> None:
        self.overlay_toggle.blockSignals(True)
        self.overlay_toggle.setChecked(visible)
        self.overlay_toggle.blockSignals(False)
        if visible:
            self.overlay.show()
            if self.controller.started and not self.overlay.capture_excluded:
                self.overlay_check.setChecked(False)
                self.show_message("This Windows build cannot exclude the overlay from capture; overlay stays hidden during fishing.")
        else:
            self.overlay.hide()

    def calibrate(self) -> None:
        if self.editing_dirty and not self.save_profile():
            return
        self.calibration_dialog = CalibrationDialog(self)
        self.calibration_dialog.show()

    def closeEvent(self, event) -> None:
        self.controller.close()
        self.teacher.close_workers()
        self.audio_clips.stop()
        self.probe.stop()
        self.overlay.close()
        self.store.remember(self.profile)
        super().closeEvent(event)


class ReadOnlySmokeBackend:
    """Packaged smoke tests prove idle startup without a real input device."""
    def mouse(self, *args) -> None:
        raise AssertionError("Smoke test attempted SendInput")

    def key(self, *args) -> None:
        raise AssertionError("Smoke test attempted SendInput")

    def foreground_title(self) -> str:
        return "Smoke test • not the game"

    def cursor(self) -> tuple[int, int]:
        return 0, 0

    def desktop(self) -> tuple[int, int, int, int]:
        return 0, 0, 3840, 2160


def main() -> int:
    parser = argparse.ArgumentParser(description="MO2Fish desktop workspace")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--smoke-test", type=Path, help="Write a UI screenshot/preflight report, then exit; no audio/input")
    args = parser.parse_args()
    root = app_root()
    os.chdir(root)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(logs / "gui.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")])
    enable_dpi_awareness()
    app = QApplication(sys.argv[:1])
    if app.platformName() == "offscreen" and sys.platform == "win32":
        # The offscreen Qt plugin has no native Windows font discovery. QA still
        # renders the same installed system font; no font files are distributed.
        for filename in ("segoeui.ttf", "segoeuib.ttf", "consola.ttf"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
    app.setApplicationName("MO2Fish")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    def exception_hook(kind, value, tb) -> None:
        logging.error("Uncaught GUI exception", exc_info=(kind, value, tb))
        if args.smoke_test:
            args.smoke_test.mkdir(parents=True, exist_ok=True)
            (args.smoke_test / "smoke-error.txt").write_text("".join(traceback.format_exception(kind, value, tb)), encoding="utf-8")
            app.exit(2)
            return
        QMessageBox.critical(None, "MO2Fish error", str(value))
    sys.excepthook = exception_hook
    try:
        store = ProfileStore()
        profile = store.load(args.profile) if args.profile else store.startup()
        window = MainWindow(store, profile, backend=ReadOnlySmokeBackend() if args.smoke_test else None, observe=not bool(args.smoke_test))
        window.show()
        if args.smoke_test:
            destination = args.smoke_test.resolve()
            destination.mkdir(parents=True, exist_ok=True)
            def smoke() -> None:
                try:
                    assert not window.controller.armed and not window.controller.started
                    assert window.controller.runtime is None
                    assert not window.start_button.isEnabled()
                    result = validate_profile(profile.path)
                    from gui.smoke import exercise_package
                    checks = exercise_package(window, app, destination)
                    window.preflight.show_result(result[0], result[1])
                    app.processEvents()
                    window.grab().save(str(destination / "MO2Fish-control.png"))
                    window.tabs.setCurrentIndex(1)
                    app.processEvents()
                    window.grab().save(str(destination / "MO2Fish-teacher.png"))
                    (destination / "smoke.txt").write_text("Startup disarmed; no runtime; Start disabled; no SendInput.\n" + "\n".join(checks) + "\nPreflight passed: " + str(result[0]) + "\n" + "\n".join(result[1]), encoding="utf-8")
                    window.close()
                    app.quit()
                except Exception:
                    (destination / "smoke-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                    app.exit(2)
            QTimer.singleShot(500, smoke)
        return app.exec()
    except Exception as exc:
        logging.exception("Desktop startup failed")
        if args.smoke_test:
            args.smoke_test.mkdir(parents=True, exist_ok=True)
            (args.smoke_test / "smoke-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            return 2
        QMessageBox.critical(None, "MO2Fish could not start", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
