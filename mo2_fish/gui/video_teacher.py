"""Game-space video crops and bounded 48 kHz audio authoring, never runtime input."""
from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import soundfile as sf
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from audio.detectors import rms
from config import audio_paths
from gui.profiles import Profile
from gui.coordmap import SpaceMap


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
    "compass_north": ("rois.compass", "compass.north_template", "compass_n.png"),
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


from gui.teacher_canvas import FrameCanvas


def extract_audio(path: Path, start: float, duration: float) -> np.ndarray:
    import imageio_ffmpeg
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-ss", str(max(0, start)), "-i", str(path),
               "-t", str(min(120, max(0.02, duration))), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
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
    maximum = profile.data["audio"]["ring_seconds"] - profile.data["audio"]["hop_ms"] / 1000
    if not 0.02 <= len(clip) / 48000 <= maximum:
        raise ValueError(f"Choose 20–{round(maximum * 1000)} ms to fit the existing detector ring")
    if not np.isfinite(clip).all() or rms(clip - clip.mean()) < 1e-6:
        raise ValueError("The selected audio is silent or invalid")
    profile.reload()
    old = profile.data["audio"]["templates"].get(role)
    paths = list(audio_paths(old))
    # Unrecorded default-profile placeholders are not previous takes.
    if isinstance(old, str) and not profile.settings.asset(old).is_file():
        paths = []
    folder = profile.path.parent / "sfx" / role
    folder.mkdir(parents=True, exist_ok=True)
    number = max((int(p.stem) for p in folder.glob("*.wav") if p.stem.isdigit()), default=0) + 1
    while True:
        path = folder / f"{number:02d}.wav"
        try:
            output = path.open("xb")  # Never replace a previous take.
            break
        except FileExistsError:
            number += 1
    try:
        with output:
            sf.write(output, clip, 48000, format="WAV", subtype="PCM_16")
        paths.append(path.relative_to(profile.path.parent).as_posix())
        profile.patch(f"audio.templates.{role}", paths)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    hop = round(48000 * profile.data["audio"]["hop_ms"] / 1000)
    peak = max(rms(clip[i:i + hop]) for i in range(0, len(clip), hop))
    return path, peak


def waveform_playhead_x(video_ms: int, offset: float, duration: float, width: int):
    seconds = video_ms / 1000 - offset
    if duration <= 0 or width <= 0 or not 0 <= seconds <= duration:
        return None
    return seconds / duration * max(0, width - 1)


def waveform_time_at_x(x: float, width: int, duration: float) -> float:
    return min(1, max(0, x / max(1, width))) * duration


def waveform_seek_ms(x: float, width: int, duration: float, offset: float = 0):
    if duration <= 0:
        return None
    return round((offset + waveform_time_at_x(x, width, duration)) * 1000)


class Waveform(QWidget):
    selection = Signal(float, float)
    seek_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(100)
        self.samples = np.empty(0, dtype=np.float32)
        self.in_s, self.out_s = 0.0, 0.0
        self.drag_start: float | None = None
        self.video_ms, self.audio_offset = 0, 0.0

    def set_playhead(self, video_ms: int, audio_offset: float = 0):
        self.video_ms, self.audio_offset = video_ms, audio_offset
        self.update()

    def playhead_x(self):
        return waveform_playhead_x(self.video_ms, self.audio_offset, len(self.samples) / 48000, self.width())

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
        x = self.playhead_x()
        if x is not None:
            p.setPen(QPen(QColor("#ff3333"), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def _time(self, x: float) -> float:
        return waveform_time_at_x(x, self.width(), len(self.samples) / 48000)

    def mousePressEvent(self, event) -> None:
        if not len(self.samples):
            event.ignore()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self.seek_requested.emit(waveform_seek_ms(event.position().x(), self.width(),
                                                     len(self.samples) / 48000, self.audio_offset))
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.drag_start = self._time(event.position().x())
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_start is not None:
            current = self._time(event.position().x())
            self.in_s, self.out_s = sorted((self.drag_start, current))
            self.selection.emit(self.in_s, self.out_s)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self.drag_start = None


from gui.teacher_ui import VideoTeacher
