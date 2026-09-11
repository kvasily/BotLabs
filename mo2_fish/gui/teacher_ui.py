"""Teach tab: synchronized playback, a role document, and explicit file commands."""
from __future__ import annotations

import copy
import logging
from pathlib import Path
import tempfile

import numpy as np
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QActionGroup, QShortcut
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gui.jobs import Jobs
from gui.meters import LoopbackProbe
from gui.profiles import Profile, ProfileStore
from gui.teacher_canvas import FrameCanvas
from gui.teacher_document import GROUPS, TeacherDocument
from gui.teacher_player import FilePlayer, ScrubState, SeekSlider
from gui.teacher_panels import AudioToolsSplitter
from gui.video_teacher import Waveform, export_audio, extract_audio


def timestamp(ms):
    seconds = max(0, ms) / 1000
    return f"{int(seconds)//60:02d}:{seconds%60:05.2f}"


class VideoTeacher(QWidget):
    edited = Signal()
    authoring = Signal()
    message = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, profile: Profile, jobs: Jobs):
        super().__init__()
        self.profile, self.jobs = profile, jobs
        self.document = TeacherDocument(profile)
        self.pending_write = False
        self.video_path = None
        self._video_profile = None
        self._redrawing_role = None
        self.reported_size = None
        self.crop_frozen = False
        self.draw_frame = None
        self.position, self.duration_ms = 0, 0
        self.scrub = ScrubState()
        self.skip_seconds = 5
        self._close_host = None
        self._switching = False
        self._audio_request = 0
        self.audio_path = None
        self.audio_offset = 0.0
        self.probe = LoopbackProbe()
        self.probe.clip.connect(self.on_live_audio)
        self.probe.failed.connect(self.message)
        self.probe.finished.connect(lambda: self.live_button.setText("Record live clip"))
        self.source = FilePlayer(self)
        self.source.frame.connect(self.on_video_frame)
        self.source.position.connect(self.on_position)
        self.source.duration.connect(self.on_duration)
        self.source.state.connect(lambda playing: self.play.setText("Pause" if playing else "Play"))
        self.source.failed.connect(self.message)

        layout = QVBoxLayout(self)
        files = QHBoxLayout()
        for label, callback in (("Open MP4 / MKV", self.open_video), ("Save", self.save_document),
                                ("Save As…", self.save_as), ("Load…", self.load_document)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            files.addWidget(button)
        self.document_label = QLabel()
        files.addWidget(self.document_label, 1)
        layout.addLayout(files)
        self.video_label = QLabel("Video: —")
        layout.addWidget(self.video_label)
        target = QHBoxLayout()
        target.addWidget(QLabel("Game / monitor capture"))
        self.resolution_preset = QComboBox()
        for name, size in (("3840 × 2160", (3840, 2160)), ("2560 × 1440", (2560, 1440)),
                           ("1920 × 1080", (1920, 1080)), ("Custom W × H", None)):
            self.resolution_preset.addItem(name, size)
        self.game_width, self.game_height = QSpinBox(), QSpinBox()
        for spin in (self.game_width, self.game_height):
            spin.setRange(2, 32768)
            spin.setMaximumWidth(95)
            spin.valueChanged.connect(self.custom_dimensions)
        self.resolution_preset.currentIndexChanged.connect(self.choose_preset)
        self.scale_mode = QComboBox()
        self.scale_mode.addItem("Fit (keep aspect)", "fit")
        self.scale_mode.addItem("Stretch", "stretch")
        self.apply_target = QPushButton("Apply target / mode")
        self.apply_target.clicked.connect(self.apply_mapping)
        for widget in (self.resolution_preset, self.game_width, QLabel("×"), self.game_height,
                       self.scale_mode, self.apply_target):
            target.addWidget(widget)
        target.addStretch()
        layout.addLayout(target)

        split = QSplitter()
        self.role_list = QTreeWidget()
        self.role_list.setHeaderHidden(True)
        self.role_list.setMinimumWidth(175)
        self.role_list.setMaximumWidth(260)
        self.role_items = {}
        for name, roles in GROUPS:
            group = QTreeWidgetItem(self.role_list, [name])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for role in roles:
                label = {"confirm_button": "confirm", "turn_in_button": "turn_in"}.get(role, role)
                item = QTreeWidgetItem(group, [label])
                item.setData(0, Qt.ItemDataRole.UserRole, role)
                self.role_items[role] = item
            group.setExpanded(True)
        self.role_list.currentItemChanged.connect(self.select_role)
        split.addWidget(self.role_list)
        picture = QWidget()
        picture_layout = QVBoxLayout(picture)
        picture_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = FrameCanvas()
        self.canvas.selection_started.connect(self.begin_draw)
        self.canvas.selection_changed.connect(self.show_box)
        self.canvas.selection_finished.connect(self.finish_draw)
        picture_layout.addWidget(self.canvas, 1)
        bar = QHBoxLayout()
        self.play = QPushButton("Play")
        self.play.clicked.connect(self.toggle_play)
        self.timeline = SeekSlider()
        self.timeline.sliderPressed.connect(self.begin_scrub)
        self.timeline.sliderMoved.connect(self.scrub_to)
        self.timeline.sliderReleased.connect(self.end_scrub)
        self.position_label = QLabel("00:00.00 / 00:00.00")
        self.mute = QPushButton("Mute")
        self.mute.setCheckable(True)
        self.mute.toggled.connect(self.set_muted)
        self.gear = QToolButton()
        self.gear.setText("⚙")
        self.gear.setToolTip("Playback speed and skip amount")
        self.gear.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.make_gear()
        bar.addWidget(self.play)
        bar.addWidget(self.timeline, 1)
        for widget in (self.position_label, self.mute, self.gear):
            bar.addWidget(widget)
        picture_layout.addLayout(bar)
        split.addWidget(picture)
        split.setStretchFactor(1, 1)
        video_panel = QWidget()
        video_layout = QVBoxLayout(video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(split, 1)
        self.cursor_info = QLabel("preview —  |  video —  |  game —")
        self.canvas.cursor_changed.connect(self.cursor_info.setText)
        video_layout.addWidget(self.cursor_info)
        self.warning = QLabel("Video authoring only. Runtime uses the live game capture.")
        self.warning.setWordWrap(True)
        video_layout.addWidget(self.warning)
        self.crop_info = QLabel("Select a role and draw. Green = selected; red = other roles. Save commits all dirty boxes.")
        video_layout.addWidget(self.crop_info)
        audio_panel = QWidget()
        audio_layout = QVBoxLayout(audio_panel)
        audio_layout.setContentsMargins(0, 0, 0, 0)
        audio = QHBoxLayout()
        for label, callback in (("Waveform at playhead", self.load_video_audio), ("Open audio file", self.open_audio)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            audio.addWidget(button)
        self.live_button = QPushButton("Record live clip")
        self.live_button.clicked.connect(self.record_live)
        audio.addWidget(self.live_button)
        self.window_s = QDoubleSpinBox()
        self.window_s.setRange(1, 120)
        self.window_s.setValue(15)
        self.window_s.setSuffix(" s waveform")
        audio.addWidget(self.window_s)
        audio_layout.addLayout(audio)
        self.wave = Waveform()
        self.wave.setMinimumHeight(70)
        self.wave.selection.connect(self.wave_selection)
        audio_layout.addWidget(self.wave, 1)
        trim = QHBoxLayout()
        self.in_s, self.out_s = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.in_s, self.out_s):
            spin.setDecimals(3)
            spin.setRange(0, 864000)
            spin.valueChanged.connect(self.spin_selection)
        for label, callback in (("In at playhead", self.mark_in), ("Out at playhead", self.mark_out)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            trim.addWidget(button)
            trim.addWidget(self.in_s if label.startswith("In") else self.out_s)
        self.audio_role = QComboBox()
        self.audio_role.addItems(["splash", "bubble", "tension", "quest_complete", "catch", "ignore"])
        trim.addWidget(self.audio_role)
        for label, callback in (("Preview selection", self.preview_audio), ("Export 48 kHz WAV", self.save_audio)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            trim.addWidget(button)
        audio_layout.addLayout(trim)
        self.audio_info = QLabel("Mark In / Out while listening. Keep detector clips under one second; tension 40–120 ms.")
        audio_layout.addWidget(self.audio_info)
        self.audio_splitter = AudioToolsSplitter(video_panel, audio_panel)
        self.audio_splitter.expanded_changed.connect(
            lambda expanded: self.message.emit("Audio tools shown" if expanded else "Audio tools hidden"))
        layout.addWidget(self.audio_splitter, 1)
        self.shortcuts = []
        for key, callback in ((Qt.Key.Key_Space, self.toggle_play), (Qt.Key.Key_Left, lambda: self.skip(-1)),
                              (Qt.Key.Key_Right, lambda: self.skip(1))):
            shortcut = QShortcut(key, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self.role_list.setCurrentItem(self.role_items["compass"])
        self.sync_profile()

    def make_gear(self):
        menu = QMenu(self.gear)
        speed = menu.addMenu("Playback speed")
        speeds = QActionGroup(menu)
        for value in (0.25, 0.5, 1, 1.25, 1.5, 2):
            action = speed.addAction(f"{value:g}×")
            action.setCheckable(True)
            action.setChecked(value == 1)
            speeds.addAction(action)
            action.triggered.connect(lambda checked, rate=value: self.source.player.setPlaybackRate(rate))
        skip = menu.addMenu("Skip amount")
        skips = QActionGroup(menu)
        for value in (1, 5, 10):
            action = skip.addAction(f"{value} seconds")
            action.setCheckable(True)
            action.setChecked(value == 5)
            skips.addAction(action)
            action.triggered.connect(lambda checked, seconds=value: setattr(self, "skip_seconds", seconds))
        self.gear.setMenu(menu)

    def showEvent(self, event):
        super().showEvent(event)
        host = self.window()
        if host is not self and host is not self._close_host:
            if self._close_host:
                self._close_host.removeEventFilter(self)
            host.installEventFilter(self)
            self._close_host = host

    def eventFilter(self, watched, event):
        if watched is self._close_host and event.type() == QEvent.Type.Close:
            if not self.maybe_save():
                event.ignore()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        if not self.maybe_save():
            event.ignore()
            return
        self.close_workers()
        super().closeEvent(event)

    def maybe_save(self):
        if self.pending_write:
            self.message.emit("Wait for the current file operation to finish.")
            return False
        if not self.document.dirty:
            return True
        answer = QMessageBox.question(self, "Unsaved teacher boxes", "Save changes to the teacher document?",
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_document()
        if answer == QMessageBox.StandardButton.Discard:
            self._redrawing_role = None
            self.document.load(self.document.profile)
            self.canvas.game_size, self.canvas.scale_mode = self.document.resolution, self.document.mode
            self.refresh_boxes()
            return True
        return False

    def select_role(self, item, previous=None):
        role = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if role and hasattr(self, "canvas"):
            self._redrawing_role = None
            self.canvas.active_role = role
            self.canvas.outer = None
            self.refresh_boxes()

    def refresh_boxes(self):
        self.canvas.role_boxes = self.document.rects()
        self.canvas.update()
        self.document_label.setText(f"{self.document.profile.path.parent.name}  {'● Unsaved changes' if self.document.dirty else 'Saved'}")
        self.document_label.setToolTip(str(self.document.profile.path))
        for role, item in self.role_items.items():
            label = {"confirm_button": "confirm", "turn_in_button": "turn_in"}.get(role, role)
            marked = role in self.canvas.role_boxes or role == self._redrawing_role
            item.setText(0, f"✓ {label}" if marked else label)
            item.setToolTip(0, str(self.canvas.role_boxes.get(role, "Draw this role")))

    def begin_draw(self):
        self.source.pause()
        self.crop_frozen = True
        self.draw_frame = self.canvas.frame
        self.authoring.emit()
        self._redrawing_role = self.canvas.active_role if self.canvas.active_role in self.document.rects() else None
        self.document.begin(self.canvas.active_role)
        self.refresh_boxes()

    def finish_draw(self):
        try:
            frame = self.draw_frame if self.draw_frame is not None else self.canvas.frame
            if frame is None:
                raise ValueError("No decoded BGR frame available; pause or seek the video and redraw this role.")
            if self.canvas.outer is None:
                raise ValueError("No rectangle selected; drag to draw this role.")
            self.document.replace(self.canvas.active_role, frame, self.canvas.outer)
            self._redrawing_role = None
            self.refresh_boxes()
            self.show_box()
        except Exception as exc:
            self._redrawing_role = None
            self.refresh_boxes()
            logging.exception("Teacher draw could not be stored")
            self.message.emit(f"Draw not stored: {exc}")

    def show_box(self):
        try:
            game = self.canvas.mapping().video_to_game_rect(*self.canvas.outer) if self.canvas.outer else None
            self.crop_info.setText(f"{self.canvas.active_role}  •  video {self.canvas.outer}  →  game {game}  •  Draw is unsaved until Save")
        except ValueError as exc:
            self.crop_info.setText(str(exc))

    def sync_profile(self):
        if self._video_profile is not self.profile:
            self._redrawing_role = None
        if self.profile.path != self.document.profile.path and not self._switching:
            requested = self.profile
            if not self.maybe_save():
                self.activate_profile(self.document.profile)
                return
            self.document.load(requested)
            self.activate_profile(requested)
            return
        elif not self.document.dirty:
            self.document.load(self.profile)
        for spin, value in zip((self.game_width, self.game_height), self.document.resolution):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.resolution_preset.blockSignals(True)
        index = next((i for i in range(3) if tuple(self.resolution_preset.itemData(i)) == self.document.resolution), 3)
        self.resolution_preset.setCurrentIndex(index)
        self.resolution_preset.blockSignals(False)
        self.scale_mode.setCurrentIndex(self.document.mode == "stretch")
        self.canvas.game_size, self.canvas.scale_mode = self.document.resolution, self.document.mode
        self.refresh_boxes()
        self.update_resolution_info()
        if self._video_profile is not self.profile:
            self._video_profile = self.profile
            self.restore_video()

    def activate_profile(self, profile):
        # Reuse the existing shell's YAML/profile hooks; no engine or overlay
        # behavior is changed. Both views continue to point at the same file.
        self._switching = True
        try:
            self.profile = profile
            host = self.window()
            if host is not self and hasattr(host, "controller"):
                host.profile = profile
                host.controller.set_profile(profile.path)
                host.store.remember(profile)
                host.refresh_profile()
            self.sync_profile()
        finally:
            self._switching = False

    def save_document(self):
        if self.pending_write:
            self.message.emit("Wait for the current file operation to finish.")
            return False
        if self.video_path is not None:
            self.document.set_video(self.video_path)
        if not self.document.dirty and not getattr(self.window(), "editing_dirty", False):
            self.message.emit(f"Nothing to commit: {self.document.profile.path}")
            return True
        return self.commit_document()

    def editor_changes(self):
        """Flush valid editor text, preserving an invalid draft without blocking crops."""
        host = self.window()
        if host is self or not getattr(host, "editing_dirty", False):
            return None, None
        if host.profile.path != self.document.profile.path:
            # During a profile switch the editor belongs to the incoming file.
            return None, None
        try:
            return host.parsed_editor(), None
        except Exception:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.document.profile.path.parent,
                                             prefix="unsaved-editor-", suffix=".txt") as draft:
                draft.write(host.editor.toPlainText())
            return None, Path(draft.name)

    def commit_document(self, name=None):
        # Small crops commit synchronously: Save-on-close gets a real Profile or
        # an exception, never a missing worker result or an unfinished Qt loop.
        self.pending_write = True
        self.setEnabled(False)
        self.source.pause()
        self.authoring.emit()
        self.busy_changed.emit(True)
        error, saved, draft = None, None, None
        try:
            data, draft = self.editor_changes()
            if self.video_path is not None:
                self.document.set_video(self.video_path)
            saved = (self.document.save_as(self.profile_store(), name, data=data) if name is not None
                     else self.document.save(data=data))
            if not isinstance(saved, Profile):
                raise RuntimeError("Teacher save returned no Profile; commit could not be confirmed.")
            saved.reload()
            self._video_profile = saved  # Save As keeps the current playhead and player.
            self.activate_profile(saved)
            self.edited.emit()
        except Exception as exc:
            logging.exception("Teacher profile commit failed")
            error = exc
        finally:
            self.pending_write = False
            self.setEnabled(True)
            self.busy_changed.emit(False)
        if error is not None:
            self.message.emit(f"Save failed for {self.document.profile.path}: {error}")
            return False
        self.message.emit(f"Saved {saved.path}" + (f" • Invalid editor text preserved in {draft}" if draft else ""))
        return True

    def save_as(self):
        if self.pending_write:
            self.message.emit("Wait for the current file operation to finish.")
            return False
        name, ok = QInputDialog.getText(self, "Save teacher as", "New profile name")
        if not ok or not name:
            return
        return self.commit_document(name)

    def profile_store(self):
        host = self.window()
        return host.store if host is not self and hasattr(host, "store") else ProfileStore(self.document.profile.path.parent.parent)

    def load_document(self):
        store = self.profile_store()
        root = store.root
        path, _ = QFileDialog.getOpenFileName(self, "Load teacher profile", str(root), "Profiles (*.yaml)")
        if path and self.maybe_save():
            try:
                profile = store.load(Path(path))
                self.authoring.emit()
                self.document.load(profile)
                self.activate_profile(profile)
                self.edited.emit()
            except Exception as exc:
                self.message.emit(str(exc))

    def choose_preset(self, index):
        size = self.resolution_preset.itemData(index)
        if size:
            for spin, value in zip((self.game_width, self.game_height), size):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

    def custom_dimensions(self):
        self.resolution_preset.blockSignals(True)
        self.resolution_preset.setCurrentIndex(3)
        self.resolution_preset.blockSignals(False)

    def apply_mapping(self):
        size = (self.game_width.value(), self.game_height.value())
        mode = self.scale_mode.currentData()
        rescale = False
        if size != self.document.resolution and self.document.saved:
            answer = QMessageBox.question(self, "Rescale existing boxes", "Rescale saved ROIs and templates to the new game size on Save?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            rescale = answer == QMessageBox.StandardButton.Yes
        self.change_target(size, mode, rescale)

    def change_target(self, resolution, mode, rescale):
        self.authoring.emit()
        self.document.target(resolution, mode, rescale)
        self.sync_profile()
        self.message.emit("Target changed in memory. Save to update the live profile YAML.")

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open recording", "", "Recordings (*.mp4 *.mkv *.avi *.mov)")
        if path:
            self.open_file(Path(path))

    def open_file(self, path):
        self.authoring.emit()
        self.clear_video()
        self.video_path = self.audio_path = Path(path).resolve()
        self.document.set_video(self.video_path)
        self.source.open(self.video_path)
        self.refresh_boxes()

    def clear_video(self):
        self.source.close()
        self.video_path = self.audio_path = None
        self.audio_offset = 0.0
        self._audio_request += 1
        self.reported_size = None
        self.canvas.image = self.canvas.frame = None
        self.draw_frame = None
        self.crop_frozen = False
        self.scrub.dragging = False
        self.wave.set_samples(np.empty(0, np.float32))
        self.canvas.outer = self.canvas.inner = None
        self.canvas.update()
        self.on_duration(0)
        self.on_position(0)
        self.video_label.setText("Video: —")
        self.warning.setText("Video authoring only. Runtime uses the live game capture.")
        self.cursor_info.setText("preview —  |  video —  |  game —")

    def restore_video(self):
        self.clear_video()
        if "teacher_video" not in self.profile.data:
            return  # A new profile has no previous recording to restore.
        saved = self.profile.data["teacher_video"]
        try:
            path = Path(saved) if isinstance(saved, str) and saved.strip() else None
            if path is None or not path.is_absolute() or not path.is_file():
                raise FileNotFoundError
        except (OSError, ValueError):
            text = f"The last used video couldn't be found:\n{saved or ''}"
            self.message.emit(text)
            QMessageBox.warning(self, "Teaching video unavailable", text)
            return
        self.video_path = self.audio_path = path
        self.source.open(path)

    def on_video_frame(self, video_frame):
        if not video_frame.isValid() or self.crop_frozen:
            return
        image = video_frame.toImage()
        if not image.isNull():
            changed = self.canvas.image is None or self.canvas.image.size() != image.size()
            self.canvas.set_image(image)
            if changed:
                self.document.source_size = [image.width(), image.height()]
                self.update_resolution_info()

    def on_frame(self, frame, index, count, fps):
        """Decoded-frame injection for offline authoring tests, never a clock."""
        self.canvas.set_frame(frame)
        self.document.source_size = [frame.shape[1], frame.shape[0]]
        self.update_resolution_info()

    def on_metadata(self, width, height):
        self.reported_size = (width, height)

    def update_resolution_info(self):
        if self.canvas.image is None:
            return
        m = self.canvas.mapping()
        self.video_label.setText(f"Video: {m.video_w} × {m.video_h}" +
                                (f" (decoded) • Container: {self.reported_size[0]} × {self.reported_size[1]}" if self.reported_size and self.reported_size != (m.video_w, m.video_h) else ""))
        sx, sy, _, _ = m.game_transform
        scale = f"×{sx:.2f}" if abs(sx-sy) < 1e-9 else f"×{sx:.2f} / ×{sy:.2f}"
        self.warning.setText(f"Crops will be scaled from {m.video_w}×{m.video_h} → {m.game_w}×{m.game_h} ({scale})." +
                             (" ASPECT MISMATCH: Fit pads have no source pixels; Stretch changes proportions." if m.aspect_mismatch else ""))
        self.warning.setStyleSheet("color:#ff7f8f" if m.aspect_mismatch else "color:#ffc36a")

    def toggle_play(self):
        if self.video_path:
            self.crop_frozen = False
            self.source.toggle()

    def set_muted(self, muted):
        self.source.audio.setMuted(muted)
        self.mute.setText("Unmute" if muted else "Mute")

    def on_duration(self, ms):
        self.duration_ms = ms
        self.timeline.setRange(0, ms)
        self.update_time()

    def on_position(self, ms):
        if self.scrub.player_position(ms):
            self.position = ms
            self.timeline.setValue(ms)
            self.update_time()

    def update_time(self):
        self.position_label.setText(f"{timestamp(self.position)} / {timestamp(self.duration_ms)}")
        self.wave.set_playhead(self.position, self.audio_offset)

    def begin_scrub(self):
        self.scrub.dragging = True
        self.crop_frozen = False

    def scrub_to(self, ms):
        self.position = self.scrub.position = ms
        self.source.seek(ms)
        self.update_time()

    def end_scrub(self):
        self.source.seek(self.timeline.value())
        self.scrub.dragging = False
        self.on_position(self.timeline.value())

    def skip(self, direction):
        self.crop_frozen = False
        self.source.seek(self.source.player.position() + direction*self.skip_seconds*1000)

    def mark_in(self):
        self.audio_path = self.video_path
        self.in_s.setValue(self.source.player.position()/1000)

    def mark_out(self):
        self.audio_path = self.video_path
        self.out_s.setValue(self.source.player.position()/1000)

    def load_video_audio(self):
        if self.video_path:
            self.load_audio(self.video_path, self.source.player.position()/1000)

    def open_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open audio", "", "Audio (*.wav *.flac *.mp3 *.m4a *.ogg)")
        if path:
            self.load_audio(Path(path), 0)

    def load_audio(self, path, start):
        self._audio_request += 1
        request = self._audio_request
        duration = self.window_s.value()
        def done(samples):
            if request == self._audio_request:
                self.audio_path, self.audio_offset = path, start
                self.on_audio(samples)
        self.jobs.run(lambda: extract_audio(path, start, duration), done, self.message.emit)

    def on_audio(self, samples):
        self.wave.set_samples(samples)
        self.wave.set_playhead(self.position, self.audio_offset)
        self.audio_info.setText(f"Waveform {self.audio_offset:.3f}–{self.audio_offset+len(samples)/48000:.3f}s • 48 kHz mono")

    def on_live_audio(self, samples):
        self.audio_path, self.audio_offset = None, 0
        self.on_audio(samples)

    def wave_selection(self, start, end):
        for spin, value in ((self.in_s, start+self.audio_offset), (self.out_s, end+self.audio_offset)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def spin_selection(self):
        duration = len(self.wave.samples)/48000
        self.wave.in_s = min(duration, max(0, self.in_s.value()-self.audio_offset))
        self.wave.out_s = min(duration, max(0, self.out_s.value()-self.audio_offset))
        self.wave.update()

    def audio_selection(self):
        start, end = self.in_s.value(), self.out_s.value()
        maximum = self.profile.data["audio"]["ring_seconds"]-self.profile.data["audio"]["hop_ms"]/1000
        if not 960 <= round((end-start)*48000) <= round(maximum*48000):
            raise ValueError(f"Choose a 20–{maximum*1000:.0f} ms clip; tension is best kept short.")
        path, samples = self.audio_path, self.wave.samples
        # Capture UI values here; the returned work function touches no widgets.
        return (lambda: extract_audio(path, start, end-start)) if path else (lambda: samples[round(start*48000):round(end*48000)].copy())

    def preview_audio(self):
        self.source.pause()
        self.authoring.emit()
        try:
            selection = self.audio_selection()
        except ValueError as exc:
            self.message.emit(str(exc))
            return
        def work():
            from gui.com_audio import audio_apartment
            import soundcard as sc
            clip = selection()
            with audio_apartment():
                sc.default_speaker().play(np.column_stack((clip, clip)), samplerate=48000)
        self.jobs.run(work, lambda _: self.message.emit("Preview finished"), self.message.emit)

    def save_audio(self):
        role = self.audio_role.currentText()
        if role == "ignore":
            self.message.emit("Ignored selection. No detector WAV was written.")
            return
        self.authoring.emit()
        profile = self.profile
        try:
            selection = self.audio_selection()
        except ValueError as exc:
            self.message.emit(str(exc))
            return
        def work():
            clip = selection()
            return export_audio(profile, clip, 0, len(clip)/48000, role)
        def saved(result):
            text = f"Saved {role} → {result[0]}"
            self.audio_info.setText(text)
            self.message.emit(text)
        self.write_job(work, saved)

    def record_live(self):
        self.authoring.emit()
        if self.probe.running:
            self.probe.stop()
            self.live_button.setText("Finishing clip…")
        else:
            self.source.pause()
            self.probe.start(self.profile.data["audio_device_name"], record=True, max_seconds=30)
            self.live_button.setText("Stop recording")

    def write_job(self, work, success):
        if self.pending_write:
            self.message.emit("Wait for the current file operation.")
            return
        self.pending_write = True
        self.setEnabled(False)
        self.busy_changed.emit(True)
        def finish(value=None, error=None):
            self.pending_write = False
            self.setEnabled(True)
            self.busy_changed.emit(False)
            if error:
                self.message.emit(error)
            else:
                success(value)
                self.edited.emit()
        self.jobs.run(work, finish, lambda error: finish(error=error))

    def close_workers(self):
        self.source.close()
        self.probe.stop()
