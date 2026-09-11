"""Browse profile WAV takes and audition them through Qt audio only."""
from pathlib import Path

import soundfile as sf
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QHeaderView, QHBoxLayout, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from config import audio_paths


class AudioClips(QWidget):
    message = Signal(str)
    audition = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Saved reference takes • Play uses your audio output"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Role", "Filename", "Duration", "Full path", "Playback"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 190), (1, 120), (2, 100), (4, 180)):
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(self.select_clip)
        layout.addWidget(self.tree, 1)
        self.path_label = QLabel("Select a clip to see its full path.")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(0.8)
        self.player.setAudioOutput(self.output)
        self.player.errorOccurred.connect(lambda _, text: self.message.emit(f"Clip playback failed: {text}"))

    def refresh(self, profile):
        self.stop()
        self.tree.clear()
        self.path_label.setText("Select a clip to see its full path.")
        roles = profile.data.get("audio", {}).get("templates", {})
        folders = profile.path.parent / "sfx"
        names = set(roles)
        if folders.exists():
            names.update(p.name for p in folders.iterdir() if p.is_dir())
        for role in sorted(names):
            try:
                paths = {profile.settings.asset(p) for p in audio_paths(roles.get(role))}
            except ValueError as exc:
                self.message.emit(f"{role}: {exc}")
                continue
            paths.update(p.resolve() for p in (folders / role).glob("*.wav"))
            if not paths:
                continue
            group = QTreeWidgetItem(self.tree, [role])
            for path in sorted(paths):
                try:
                    info = sf.info(str(path))
                    duration = f"{info.duration:.3f} s"
                    readable = True
                except (OSError, RuntimeError):
                    duration, readable = "Missing / invalid", False
                row = QTreeWidgetItem(group, [role, path.name, duration, str(path)])
                row.setData(0, Qt.ItemDataRole.UserRole, str(path))
                row.setToolTip(3, str(path))
                controls = QWidget()
                buttons = QHBoxLayout(controls)
                buttons.setContentsMargins(0, 0, 0, 0)
                play, stop = QPushButton("Play"), QPushButton("Stop")
                play.setEnabled(readable)
                play.clicked.connect(lambda checked=False, item=row, clip=path: self.play_clip(item, clip))
                stop.clicked.connect(self.stop)
                buttons.addWidget(play)
                buttons.addWidget(stop)
                self.tree.setItemWidget(row, 4, controls)
            group.setExpanded(True)

    def select_clip(self, item, previous=None):
        path = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self.path_label.setText(path or "Select a clip to see its full path.")

    def play_clip(self, item, path: Path):
        self.audition.emit()
        self.tree.setCurrentItem(item)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self.message.emit(f"Playing {path}")

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
