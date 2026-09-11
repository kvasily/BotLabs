"""Qt/FFmpeg owns the audio/video clock. The slider is only a seek controller."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QSlider, QStyle


@dataclass
class ScrubState:
    dragging: bool = False
    position: int = 0

    def player_position(self, value: int) -> bool:
        if self.dragging:
            return False
        self.position = value
        return True


class SeekSlider(QSlider):
    """Click-to-seek and continuous drag, with explicit pointer ownership."""
    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(0, 0)

    def at(self, event):
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_SliderLength) // 2
        value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                               round(event.position().x()) - margin,
                                               max(1, self.width() - 2 * margin))
        self.setSliderPosition(value)
        self.setValue(value)
        self.sliderMoved.emit(value)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.at(event)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.at(event)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.at(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class FilePlayer(QObject):
    frame = Signal(object)
    position = Signal(int)
    duration = Signal(int)
    state = Signal(bool)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.audio.setMuted(False)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.player.setPlaybackRate(1.0)
        self.sink.videoFrameChanged.connect(self.frame.emit)
        self.player.positionChanged.connect(self.position.emit)
        self.player.durationChanged.connect(self.duration.emit)
        self.player.playbackStateChanged.connect(lambda _: self.state.emit(self.playing))
        self.player.errorOccurred.connect(lambda _, text: self.failed.emit(f"File playback failed: {text}"))

    @property
    def playing(self):
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def open(self, path: Path):
        rate = self.player.playbackRate()
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.player.setPlaybackRate(rate)
        # A paused first-frame preview. Loading never starts live capture/input.
        self.player.pause()

    def toggle(self):
        self.player.pause() if self.playing else self.player.play()

    def pause(self):
        self.player.pause()

    def seek(self, milliseconds: int):
        self.player.setPosition(max(0, min(milliseconds, self.player.duration())))

    def close(self):
        self.player.stop()
        self.player.setSource(QUrl())
