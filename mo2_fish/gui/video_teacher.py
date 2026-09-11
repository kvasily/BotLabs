"""Game-space video crops and bounded 48 kHz audio authoring, never runtime input."""
from __future__ import annotations

import copy
from pathlib import Path
import queue
import subprocess
import sys
import threading

import cv2
import numpy as np
import soundfile as sf
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from audio.detectors import rms
from gui.jobs import Jobs
from gui.meters import LoopbackProbe
from gui.profiles import Profile
from gui.coordmap import SpaceMap
from gui.rescale import is_rect, rescale_profile


class FrameMapping:
    """Compatibility facade for the former preview mapping API."""
    def __init__(self, frame_width, frame_height, view_width, view_height, game_width=3840, game_height=2160, mode="fit"):
        self.spaces = SpaceMap(frame_width, frame_height, game_width, game_height, view_width, view_height, mode)

    @property
    def display(self):
        return self.spaces.preview_rect

    def box(self, start, end):
        return self.spaces.preview_to_video_rect(start, end)

    def config_box(self, box):
        return list(self.spaces.video_to_game_rect(*box))


def native_crop(frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, width, height = box
    if width < 2 or height < 2 or x < 0 or y < 0 or x + width > frame.shape[1] or y + height > frame.shape[0]:
        raise ValueError("Select a nonempty rectangle inside the video frame")
    return frame[y:y + height, x:x + width].copy()


# role -> ROI path, template path, conventional filename. Fish slots are resolved separately.
ROLES = {
    "compass": ("rois.compass", "compass.north_template", "compass_n.png"),
    "hit_marker": ("rois.hit_marker", "templates.hit_marker", "hit_marker.png"),
    "interact_prompt": ("rois.interact_prompt", "templates.taskmaster", "taskmaster_fishing.png"),
    "quest_list": ("rois.quest_list", "templates.quest_gui", "quest_gui.png"),
    "confirm_button": ("rois.confirm_button", "templates.confirm", "confirm.png"),
    "turn_in_button": ("rois.turn_in_button", "templates.turn_in", "turn_in.png"),
    "hook_slot": ("rois.hook_slot", None, None), "bait_slot": ("rois.bait_slot", None, None),
    "inventory": ("rois.inventory", None, None), "quest_paper": ("rois.quest_paper", None, None),
    "quest_tooltip": ("rois.quest_tooltip", None, None),
    "bait_empty": ("rois.bait_slot", "templates.bait_empty", "bait_empty.png"),
    "bait_count": ("rois.bait_count", "templates.bait_zero", "bait_zero.png"),
    "catch_indicator": ("rois.catch_indicator", "templates.catch", "catch.png"),
    "bassle": ("rois.quest_list", "templates.bassle", "bassle.png"),
    "redline_torp": ("rois.quest_list", "templates.redline_torp", "redline_torp.png"),
    "logout_success": ("logout.success_roi", "logout.success_template", "logged_out.png"),
}
for _task in ("bassle", "redline_torp"):
    for _kind in ("hook", "bait"):
        ROLES[f"{_kind}_{_task}_source"] = (f"tasks.{_task}.{_kind}_inventory_rect", f"tasks.{_task}.{_kind}_template", f"{_kind}_{_task}.png")
for _count in range(4):
    ROLES[f"progress_{_count}"] = ("rois.quest_tooltip", f"templates.progress_{_count}", f"progress_{_count}.png")


def save_frame_role(profile: Profile, frame: np.ndarray, outer: tuple[int, int, int, int],
                    inner: tuple[int, int, int, int] | None, role: str, task: str = "bassle",
                    save_template: bool = True) -> Path | None:
    roi_key, template_key, filename = ROLES[role]
    mapping = SpaceMap(frame.shape[1], frame.shape[0], *profile.data["resolution"],
                       frame.shape[1], frame.shape[0], profile.data.get("video_scale_mode", "fit"))
    native_crop(frame, outer)
    game_outer = mapping.video_to_game_rect(*outer)
    if inner is not None:
        x, y, w, h = inner
        ox, oy, ow, oh = outer
        if x < ox or y < oy or x + w > ox + ow or y + h > oy + oh:
            raise ValueError("The inner template crop must be inside the outer search ROI")
    if role in ("hook_slot", "bait_slot"):
        kind = role.split("_")[0]
        template_key, filename = f"tasks.{task}.{kind}_template", f"{kind}_{task}.png"
    updated = copy.deepcopy(profile.data)
    def patch_value(dotted, value):
        parent = updated
        parts = dotted.split(".")
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[parts[-1]] = value
    output = None
    if save_template:
        source_box = inner or outer
        game_inner = mapping.video_to_game_rect(*source_box)
        gx, gy, gw, gh = game_inner
        ox, oy, ow, oh = game_outer
        if gx < ox or gy < oy or gx + gw > ox + ow or gy + gh > oy + oh:
            raise ValueError("Rounded template must fit inside the game-space ROI; enlarge the outer box")
        scaled = mapping.scale_image_video_to_game(native_crop(frame, source_box), source_box)
        filename = filename or f"{role}.png"
        output = profile.asset("templates", filename)
        ok, encoded = cv2.imencode(".png", scaled)
        if not ok:
            raise ValueError("Could not encode the scaled template PNG")
        encoded.tofile(output)
        if template_key:
            patch_value(template_key, f"templates/{filename}")
    patch_value(roi_key, list(game_outer))
    updated["video_source_resolution"] = [frame.shape[1], frame.shape[0]]
    updated["video_scale_mode"] = mapping.mode
    profile.write(updated)
    return output


class FrameCanvas(QWidget):
    selection_changed = Signal()
    selection_started = Signal()
    cursor_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(540, 290)
        self.frame: np.ndarray | None = None
        self.image: QImage | None = None
        self.outer: tuple[int, int, int, int] | None = None
        self.inner: tuple[int, int, int, int] | None = None
        self.mode = "outer"
        self.start_point: QPointF | None = None
        self.game_size = (3840, 2160)
        self.scale_mode = "fit"
        self.saved_roi = None
        self.setMouseTracking(True)

    def set_frame(self, frame: np.ndarray) -> None:
        self.frame = frame
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self.image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self.outer = self.inner = None
        self.update()

    def mapping(self) -> SpaceMap:
        assert self.frame is not None
        return SpaceMap(self.frame.shape[1], self.frame.shape[0], *self.game_size, self.width(), self.height(), self.scale_mode)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080d14"))
        if self.image is None:
            p.setPen(QColor("#71859b"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "OPEN A RECORDING\nDraw a search ROI, then an optional inner template crop")
            return
        mapping = self.mapping()
        left, top, width, height = mapping.preview_rect
        p.drawImage(QRectF(left, top, width, height), self.image)
        if self.saved_roi:
            saved = mapping.game_to_preview_rect(*self.saved_roi)
            if saved:
                p.setPen(QPen(QColor("#81a8ef"), 2, Qt.PenStyle.DashLine))
                p.drawRect(QRectF(*saved))
        scale = width / self.frame.shape[1]
        for box, color in ((self.outer, "#5ce0b3"), (self.inner, "#ffbf69")):
            if box:
                x, y, w, h = box
                p.setPen(QPen(QColor(color), 2))
                p.setBrush(QColor(0, 0, 0, 0))
                p.drawRect(QRectF(left + x * scale, top + y * scale, w * scale, h * scale))

    def mousePressEvent(self, event) -> None:
        if self.frame is not None and event.button() == Qt.MouseButton.LeftButton:
            if self.mapping().preview_to_video(event.position().x(), event.position().y()) is None:
                return
            self.selection_started.emit()
            self.start_point = event.position()

    def mouseMoveEvent(self, event) -> None:
        if self.frame is None:
            return
        x, y = event.position().x(), event.position().y()
        mapping = self.mapping()
        video = mapping.preview_to_video(x, y)
        game = mapping.video_to_game(*video) if video else None
        self.cursor_changed.emit(f"preview ({round(x)}, {round(y)})  |  video {video or '— letterbox'}  |  game {game or '—'}")
        if self.start_point is None:
            return
        box = mapping.preview_to_video_rect((self.start_point.x(), self.start_point.y()), (x, y))
        if self.mode == "inner":
            self.inner = box
        else:
            self.outer, self.inner = box, None
        self.update()
        self.selection_changed.emit()

    def mouseReleaseEvent(self, event) -> None:
        self.mouseMoveEvent(event)
        self.start_point = None


class VideoSource(QObject):
    frame_ready = Signal(object, int, int, float)
    metadata = Signal(int, int)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.commands: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="video-authoring")
        self.thread.start()

    def open(self, path: Path) -> None:
        self.commands.put(("open", path))

    def seek(self, frame: int) -> None:
        self.commands.put(("seek", frame))

    def close(self) -> None:
        self.commands.put(("close", None))

    def _run(self) -> None:
        capture = None
        try:
            while True:
                action, value = self.commands.get()
                # Coalesce scrub requests so old frames do not build up behind the UI.
                while action == "seek" and not self.commands.empty():
                    action, value = self.commands.get_nowait()
                if action == "close":
                    break
                try:
                    if action == "open":
                        if capture:
                            capture.release()
                        capture = cv2.VideoCapture(str(value))
                        if not capture.isOpened():
                            raise RuntimeError(f"OpenCV could not decode {value}. Try an H.264 MP4 recording.")
                        value = 0
                    if capture is None:
                        continue
                    capture.set(cv2.CAP_PROP_POS_FRAMES, int(value))
                    ok, image = capture.read()
                    if not ok:
                        raise RuntimeError(f"Cannot decode video frame {value}")
                    self.metadata.emit(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
                    self.frame_ready.emit(image, int(value), int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), float(capture.get(cv2.CAP_PROP_FPS)) or 30)
                except Exception as exc:
                    self.failed.emit(str(exc))
        finally:
            if capture:
                capture.release()


def extract_audio(path: Path, start: float, duration: float) -> np.ndarray:
    import imageio_ffmpeg
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-ss", str(max(0, start)), "-i", str(path),
               "-t", str(min(120, max(0.1, duration))), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode:
        raise RuntimeError(f"Audio extraction failed: {result.stderr.decode(errors='replace')[-1500:]}")
    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if not len(samples):
        raise RuntimeError("No audio samples found in this file/window")
    return samples


def export_audio(profile: Profile, samples: np.ndarray, start: float, end: float, role: str) -> tuple[Path, float]:
    if role not in ("bubble", "splash", "tension", "quest_complete", "catch"):
        raise ValueError("Choose one of the five supported sound roles")
    if not 0 <= start < end <= len(samples) / 48000 + 1 / 48000:
        raise ValueError("In and Out must describe a nonempty selection inside the loaded audio")
    clip = samples[round(start * 48000):round(end * 48000)]
    maximum = 1 - profile.data["audio"]["hop_ms"] / 1000
    if not 0.02 <= len(clip) / 48000 <= maximum:
        raise ValueError(f"Choose 20–{round(maximum * 1000)} ms to fit the existing detector ring")
    if not np.isfinite(clip).all() or rms(clip - clip.mean()) < 1e-6:
        raise ValueError("The selected audio is silent or invalid")
    path = profile.asset("sfx", f"{role}.wav")
    sf.write(path, clip, 48000, subtype="PCM_16")
    profile.patch(f"audio.templates.{role}", f"sfx/{role}.wav")
    hop = round(48000 * profile.data["audio"]["hop_ms"] / 1000)
    peak = max(rms(clip[i:i + hop]) for i in range(0, len(clip), hop))
    return path, peak


class Waveform(QWidget):
    selection = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(100)
        self.samples = np.empty(0, dtype=np.float32)
        self.in_s, self.out_s = 0.0, 0.0
        self.drag_start: float | None = None

    def set_samples(self, samples: np.ndarray) -> None:
        self.samples = samples
        self.in_s, self.out_s = 0, min(0.5, len(samples) / 48000)
        self.selection.emit(self.in_s, self.out_s)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0a111b"))
        if not len(self.samples):
            p.setPen(QColor("#71859b"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "LOAD AUDIO OR RECORD A LIVE CLIP   •   DRAG TO TRIM")
            return
        duration = len(self.samples) / 48000
        p.fillRect(QRectF(self.in_s / duration * self.width(), 0, (self.out_s - self.in_s) / duration * self.width(), self.height()), QColor(50, 143, 122, 80))
        p.setPen(QPen(QColor("#73c6d8"), 1))
        bins = np.array_split(self.samples, min(self.width(), len(self.samples)))
        for x, part in enumerate(bins):
            peak = float(np.max(np.abs(part)))
            height = min(1, peak * 2) * (self.height() - 8) / 2
            p.drawLine(QPointF(x, self.height() / 2 - height), QPointF(x, self.height() / 2 + height))

    def _time(self, x: float) -> float:
        return min(1, max(0, x / max(1, self.width()))) * len(self.samples) / 48000

    def mousePressEvent(self, event) -> None:
        self.drag_start = self._time(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if self.drag_start is not None:
            current = self._time(event.position().x())
            self.in_s, self.out_s = sorted((self.drag_start, current))
            self.selection.emit(self.in_s, self.out_s)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self.mouseMoveEvent(event)
        self.drag_start = None


class VideoTeacher(QWidget):
    edited = Signal()
    authoring = Signal()
    message = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, profile: Profile, jobs: Jobs) -> None:
        super().__init__()
        self.profile, self.jobs = profile, jobs
        self.source = VideoSource()
        self.source.frame_ready.connect(self.on_frame)
        self.source.metadata.connect(self.on_metadata)
        self.source.failed.connect(self.message)
        self.probe = LoopbackProbe()
        self.probe.clip.connect(self.on_audio)
        self.probe.failed.connect(self.message)
        self.probe.finished.connect(lambda: self.live_button.setText("Record live clip"))
        self.video_path: Path | None = None
        self.position, self.frame_count, self.fps = 0, 0, 30.0
        self.crop_frozen = False
        self.pending_write = False
        self.reported_size: tuple[int, int] | None = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        open_button, self.play = QPushButton("Open MP4 / MKV"), QPushButton("Play")
        open_button.clicked.connect(self.open_video)
        self.play.clicked.connect(self.toggle_play)
        self.position_label = QLabel("No recording loaded")
        top.addWidget(open_button)
        top.addWidget(self.play)
        top.addWidget(self.position_label, 1)
        layout.addLayout(top)
        self.video_label = QLabel("Video: —")
        layout.addWidget(self.video_label)
        target = QHBoxLayout()
        target.addWidget(QLabel("Game / monitor capture"))
        self.resolution_preset = QComboBox()
        for label, size in (("3840 × 2160", (3840, 2160)), ("2560 × 1440", (2560, 1440)),
                            ("1920 × 1080", (1920, 1080)), ("Custom W × H", None)):
            self.resolution_preset.addItem(label, size)
        self.game_width, self.game_height = QSpinBox(), QSpinBox()
        for spin in (self.game_width, self.game_height):
            spin.setRange(2, 32768)
            spin.valueChanged.connect(self.custom_dimensions)
        self.resolution_preset.currentIndexChanged.connect(self.choose_preset)
        self.scale_mode = QComboBox()
        self.scale_mode.addItem("Fit (keep aspect)", "fit")
        self.scale_mode.addItem("Stretch", "stretch")
        self.apply_target = QPushButton("Apply target / mode")
        self.apply_target.clicked.connect(self.apply_mapping)
        for widget in (self.resolution_preset, self.game_width, QLabel("×"), self.game_height,
                       QLabel("Mapping"), self.scale_mode, self.apply_target):
            target.addWidget(widget)
        layout.addLayout(target)
        self.canvas = FrameCanvas()
        layout.addWidget(self.canvas, 4)
        self.cursor_info = QLabel("preview —  |  video —  |  game —")
        self.cursor_info.setObjectName("muted")
        self.canvas.cursor_changed.connect(self.cursor_info.setText)
        layout.addWidget(self.cursor_info)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderMoved.connect(self.seek)
        layout.addWidget(self.timeline)
        self.warning = QLabel("Video authoring only. The fishing brain always sees the LIVE screen.")
        self.warning.setWordWrap(True)
        self.warning.setObjectName("muted")
        layout.addWidget(self.warning)
        row = QHBoxLayout()
        self.role, self.box_mode, self.task = QComboBox(), QComboBox(), QComboBox()
        self.role.addItems(ROLES)
        self.role.currentIndexChanged.connect(self.show_saved_roi)
        self.box_mode.addItems(["Outer search ROI", "Inner template crop"])
        self.box_mode.currentIndexChanged.connect(lambda i: setattr(self.canvas, "mode", "inner" if i else "outer"))
        self.task.addItem("Bassle", "bassle")
        self.task.addItem("Redline Torp", "redline_torp")
        save, roi_only = QPushButton("Save PNG + ROI"), QPushButton("Save ROI only")
        save.clicked.connect(lambda: self.save_crop(True))
        roi_only.clicked.connect(lambda: self.save_crop(False))
        for item in (self.role, self.task, self.box_mode, save, roi_only):
            row.addWidget(item)
        layout.addLayout(row)
        self.crop_info = QLabel("Outer = search area • Inner = template • PNGs and YAML are saved in game pixels")
        self.crop_info.setObjectName("muted")
        self.canvas.selection_changed.connect(self.show_box)
        self.canvas.selection_started.connect(self.freeze_frame)
        layout.addWidget(self.crop_info)
        audio_row = QHBoxLayout()
        audio_button = QPushButton("Load audio at current frame")
        audio_button.clicked.connect(self.load_video_audio)
        wav_button = QPushButton("Open audio file")
        wav_button.clicked.connect(self.open_audio)
        self.live_button = QPushButton("Record live clip")
        self.live_button.clicked.connect(self.record_live)
        self.window_s = QDoubleSpinBox()
        self.window_s.setRange(1, 120)
        self.window_s.setValue(15)
        self.window_s.setSuffix(" s window")
        for item in (audio_button, wav_button, self.live_button, self.window_s):
            audio_row.addWidget(item)
        layout.addLayout(audio_row)
        self.wave = Waveform()
        layout.addWidget(self.wave, 1)
        trim = QHBoxLayout()
        self.in_s, self.out_s = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.in_s, self.out_s):
            spin.setDecimals(3)
            spin.setRange(0, 120)
            spin.setSingleStep(0.01)
            spin.valueChanged.connect(self.spin_selection)
        self.wave.selection.connect(self.wave_selection)
        self.audio_role = QComboBox()
        self.audio_role.addItems(["bubble", "splash", "tension", "quest_complete", "catch"])
        preview, export = QPushButton("Preview selection"), QPushButton("Export 48 kHz WAV")
        preview.clicked.connect(self.preview_audio)
        export.clicked.connect(self.save_audio)
        for item in (QLabel("In"), self.in_s, QLabel("Out"), self.out_s, self.audio_role, preview, export):
            trim.addWidget(item)
        layout.addLayout(trim)
        self.audio_info = QLabel("Tension: use a short 40–120 ms sustained excerpt. Export must fit the 1 s ring.")
        self.audio_info.setObjectName("muted")
        layout.addWidget(self.audio_info)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self.sync_profile()

    def sync_profile(self) -> None:
        size = self.profile.data.get("resolution", [3840, 2160])
        if not isinstance(size, list) or len(size) != 2 or not all(type(v) is int and 2 <= v <= 32768 for v in size):
            size = [3840, 2160]  # Display fallback only; validation rejects malformed YAML.
        for spin, value in zip((self.game_width, self.game_height), size):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.resolution_preset.blockSignals(True)
        match = next((i for i in range(3) if tuple(self.resolution_preset.itemData(i)) == tuple(size)), 3)
        self.resolution_preset.setCurrentIndex(match)
        self.resolution_preset.blockSignals(False)
        mode = self.profile.data.get("video_scale_mode", "fit")
        self.scale_mode.setCurrentIndex(1 if mode == "stretch" else 0)
        self.canvas.game_size, self.canvas.scale_mode = tuple(size), mode if mode in ("fit", "stretch") else "fit"
        self.show_saved_roi()
        self.update_resolution_info()
        self.canvas.update()

    def choose_preset(self, index: int) -> None:
        size = self.resolution_preset.itemData(index)
        if size:
            for spin, value in zip((self.game_width, self.game_height), size):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

    def custom_dimensions(self) -> None:
        self.resolution_preset.blockSignals(True)
        self.resolution_preset.setCurrentIndex(3)
        self.resolution_preset.blockSignals(False)

    def apply_mapping(self) -> None:
        resolution = (self.game_width.value(), self.game_height.value())
        mode = self.scale_mode.currentData()
        old = tuple(self.profile.data["resolution"])
        if resolution == old and mode == self.profile.data.get("video_scale_mode", "fit"):
            return
        rescale = False
        existing = any(is_rect(value) for value in self.profile.data.get("rois", {}).values())
        existing |= any(is_rect(task.get(key)) for task in self.profile.data.get("tasks", {}).values()
                        for key in ("hook_inventory_rect", "bait_inventory_rect"))
        existing |= is_rect(self.profile.data.get("logout", {}).get("success_roi"))
        existing |= any(path.suffix.lower() == ".png" and path.is_file() for path in self.profile.settings.required_assets())
        if resolution != old and (existing or self.canvas.outer is not None):
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Change game capture resolution")
            dialog.setText(f"Rescale existing ROIs/templates from {old[0]} × {old[1]} → {resolution[0]} × {resolution[1]}?")
            dialog.setInformativeText("Rescale keeps the original YAML and PNGs. Keeping existing pixels changes only the target; inspect your assets and Validate again. Current video selections stay attached to the video.")
            yes = dialog.addButton("Rescale existing assets", QMessageBox.ButtonRole.AcceptRole)
            keep = dialog.addButton("Keep existing pixels", QMessageBox.ButtonRole.DestructiveRole)
            dialog.addButton(QMessageBox.StandardButton.Cancel)
            dialog.setDefaultButton(yes)
            dialog.exec()
            if dialog.clickedButton() not in (yes, keep):
                self.sync_profile()
                return
            rescale = dialog.clickedButton() == yes
        self.change_target(resolution, mode, rescale)

    def change_target(self, resolution: tuple[int, int], mode: str, rescale: bool) -> None:
        self.authoring.emit()
        profile = self.profile
        def work():
            if rescale:
                return rescale_profile(profile, resolution, mode)
            data = copy.deepcopy(profile.data)
            data["resolution"], data["video_scale_mode"] = list(resolution), mode
            profile.write(data)
            return 0
        def done(count):
            self.sync_profile()
            self.show_box()
            self.message.emit(f"Game target saved; {count} PNGs rescaled. Inspect ROIs and Validate again.")
        self.write_job(work, done)

    def on_metadata(self, width: int, height: int) -> None:
        self.reported_size = (width, height)

    def update_resolution_info(self) -> None:
        if self.canvas.frame is None:
            return
        mapping = self.canvas.mapping()
        vw, vh, gw, gh = mapping.video_w, mapping.video_h, mapping.game_w, mapping.game_h
        label = f"Video: {vw} × {vh}"
        if self.reported_size and self.reported_size != (vw, vh):
            label += f" (decoded frame)  •  Container: {self.reported_size[0]} × {self.reported_size[1]}; using decoded pixels"
        self.video_label.setText(label)
        sx, sy, _, _ = mapping.game_transform
        scale = f"×{sx:.2f}" if abs(sx - sy) < 1e-9 else f"×{sx:.2f} horizontal, ×{sy:.2f} vertical"
        text = (f"Crops will be scaled from {vw}×{vh} → {gw}×{gh} ({scale})."
                if (vw, vh) != (gw, gh) else f"Video and game match: {gw}×{gh}. PNGs keep 1:1 pixels.")
        if mapping.aspect_mismatch:
            text += " ASPECT MISMATCH • " + ("Fit centers the video; game padding has no source pixels." if mapping.mode == "fit" else "Stretch changes image proportions.")
        self.warning.setText(text + " Runtime always uses the live game capture.")
        self.warning.setStyleSheet("color: #ff7f8f" if mapping.aspect_mismatch else "color: #ffc36a" if (vw, vh) != (gw, gh) else "color: #5ce0b3")

    def show_saved_roi(self) -> None:
        key = ROLES[self.role.currentText()][0]
        value = self.profile.data
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        self.canvas.saved_roi = tuple(value) if is_rect(value) else None
        self.canvas.update()

    def open_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open recording", "", "Recordings (*.mp4 *.mkv *.avi *.mov)")
        if path:
            self.timer.stop()
            self.play.setText("Play")
            self.video_path = Path(path)
            self.crop_frozen = False
            self.source.open(self.video_path)

    def on_frame(self, frame: np.ndarray, index: int, count: int, fps: float) -> None:
        if self.crop_frozen:
            return
        self.position, self.frame_count, self.fps = index, count, fps
        self.canvas.set_frame(frame)
        self.timeline.setRange(0, max(0, count - 1))
        self.timeline.setValue(index)
        self.position_label.setText(f"{index / fps:,.2f}s   /   {count / fps:,.2f}s   ·   {frame.shape[1]} × {frame.shape[0]}")
        self.update_resolution_info()
        size = [frame.shape[1], frame.shape[0]]
        if self.profile.data.get("video_source_resolution") != size and not self.pending_write:
            self.authoring.emit()
            profile = self.profile
            self.write_job(lambda: profile.patch("video_source_resolution", size), lambda _: None)

    def toggle_play(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play.setText("Play")
        elif self.frame_count:
            self.crop_frozen = False
            self.timer.start(max(20, round(1000 / self.fps)))
            self.play.setText("Pause video")

    def next_frame(self) -> None:
        if self.position + 1 >= self.frame_count:
            self.timer.stop()
            self.play.setText("Play")
        else:
            self.source.seek(self.position + 1)

    def show_box(self) -> None:
        self.timer.stop()
        self.play.setText("Play")
        try:
            box = self.canvas.mapping().video_to_game_rect(*self.canvas.outer) if self.canvas.outer else None
            inner = self.canvas.mapping().video_to_game_rect(*(self.canvas.inner or self.canvas.outer)) if self.canvas.outer else None
            self.crop_info.setText(f"Video ROI {self.canvas.outer}  →  Game ROI {box}  •  Game template {inner[2:] if inner else '—'}  •  Dashed blue = saved ROI")
        except ValueError as exc:
            self.crop_info.setText(str(exc))

    def freeze_frame(self) -> None:
        self.crop_frozen = True
        self.timer.stop()
        self.play.setText("Play")

    def seek(self, frame: int) -> None:
        self.crop_frozen = False
        self.source.seek(frame)

    def save_crop(self, template: bool) -> None:
        if self.canvas.frame is None or self.canvas.outer is None:
            self.message.emit("Open a video and draw an outer search ROI first.")
            return
        self.authoring.emit()
        frame, outer, inner = self.canvas.frame, self.canvas.outer, self.canvas.inner
        role, task, profile = self.role.currentText(), self.task.currentData(), self.profile
        self.write_job(lambda: save_frame_role(profile, frame, outer, inner, role, task, template),
                       lambda path: self.message.emit(f"Saved {path.name if path else 'ROI'} to {profile.path.parent.name}"))

    def write_job(self, work, success) -> None:
        if self.pending_write:
            self.message.emit("Wait for the current asset export to finish.")
            return
        self.pending_write = True
        self.setEnabled(False)
        self.busy_changed.emit(True)
        def finish(value=None, error: str | None = None) -> None:
            self.pending_write = False
            self.setEnabled(True)
            self.busy_changed.emit(False)
            if error:
                self.message.emit(error)
            else:
                self.edited.emit()
                success(value)
        self.jobs.run(work, finish, lambda error: finish(error=error))

    def load_video_audio(self) -> None:
        if self.video_path is None:
            self.message.emit("Open a video first.")
            return
        self.load_audio(self.video_path, self.position / self.fps)

    def open_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open audio", "", "Audio (*.wav *.flac *.mp3 *.m4a *.ogg)")
        if path:
            self.load_audio(Path(path), 0)

    def load_audio(self, path: Path, start: float) -> None:
        self.audio_info.setText(f"Decoding from {start:.2f}s…")
        duration = self.window_s.value()
        self.jobs.run(lambda: extract_audio(path, start, duration), self.on_audio, self.message.emit)

    def on_audio(self, samples: np.ndarray) -> None:
        self.in_s.setMaximum(len(samples) / 48000)
        self.out_s.setMaximum(len(samples) / 48000)
        self.wave.set_samples(samples)
        self.audio_info.setText(f"{len(samples) / 48000:.3f}s loaded • 48 kHz mono • drag the waveform or edit In / Out")

    def record_live(self) -> None:
        self.authoring.emit()
        if self.probe.running:
            self.probe.stop()
            self.live_button.setText("Finishing clip…")
            return
        try:
            self.probe.start(self.profile.data["audio_device_name"], record=True, max_seconds=30)
            self.live_button.setText("Stop recording")
        except Exception as exc:
            self.message.emit(str(exc))

    def wave_selection(self, start: float, end: float) -> None:
        for spin, value in ((self.in_s, start), (self.out_s, end)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def spin_selection(self) -> None:
        self.wave.in_s, self.wave.out_s = self.in_s.value(), self.out_s.value()
        self.wave.update()

    def preview_audio(self) -> None:
        self.authoring.emit()
        clip = self.wave.samples[round(self.in_s.value() * 48000):round(self.out_s.value() * 48000)].copy()
        if not len(clip):
            self.message.emit("Select audio to preview.")
            return
        def play() -> None:
            from gui.com_audio import audio_apartment
            import soundcard as sc
            with audio_apartment():
                sc.default_speaker().play(np.column_stack((clip, clip)), samplerate=48000)
        self.jobs.run(play, lambda _: self.message.emit("Preview finished"), self.message.emit)

    def save_audio(self) -> None:
        self.authoring.emit()
        profile, samples = self.profile, self.wave.samples
        start, end, role = self.in_s.value(), self.out_s.value(), self.audio_role.currentText()
        def finished(result) -> None:
            path, peak = result
            self.audio_info.setText(f"Saved {path.name} • peak hop RMS {peak:.5f} • starting min_rms ≈ {peak * 0.15:.5f}")
        self.write_job(lambda: export_audio(profile, samples, start, end, role), finished)

    def close_workers(self) -> None:
        self.timer.stop()
        self.source.close()
        self.probe.stop()
