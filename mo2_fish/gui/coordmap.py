"""Preview → decoded video → game coordinates, independent of Qt and capture."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

import cv2
import numpy as np


def pixel(value: float) -> int:
    """Nearest physical pixel, with deterministic half-up rounding."""
    return math.floor(value + 0.5)


@dataclass(frozen=True)
class SpaceMap:
    video_w: int
    video_h: int
    game_w: int
    game_h: int
    preview_w: int
    preview_h: int
    mode: Literal["fit", "stretch"] = "fit"

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 2 for v in
               (self.video_w, self.video_h, self.game_w, self.game_h, self.preview_w, self.preview_h)):
            raise ValueError("Video, game and preview dimensions must be integers of at least 2 pixels")
        if self.mode not in ("fit", "stretch"):
            raise ValueError("Video scale mode must be fit or stretch")

    @property
    def scale_pv(self) -> float:
        return min(self.preview_w / self.video_w, self.preview_h / self.video_h)

    @property
    def preview_rect(self) -> tuple[float, float, float, float]:
        w, h = self.video_w * self.scale_pv, self.video_h * self.scale_pv
        return (self.preview_w - w) / 2, (self.preview_h - h) / 2, w, h

    @property
    def video_rect(self) -> tuple[int, int, int, int]:
        return 0, 0, self.video_w, self.video_h

    @property
    def game_rect(self) -> tuple[int, int, int, int]:
        return 0, 0, self.game_w, self.game_h

    @property
    def game_transform(self) -> tuple[float, float, float, float]:
        sx, sy = self.game_w / self.video_w, self.game_h / self.video_h
        if self.mode == "fit":
            sx = sy = min(sx, sy)
        return sx, sy, (self.game_w - self.video_w * sx) / 2, (self.game_h - self.video_h * sy) / 2

    @property
    def aspect_mismatch(self) -> bool:
        return abs(self.video_w / self.video_h - self.game_w / self.game_h) > 0.01

    def preview_to_video(self, x: float, y: float) -> tuple[int, int] | None:
        ox, oy, w, h = self.preview_rect
        if not (ox <= x < ox + w and oy <= y < oy + h):
            return None
        return (min(self.video_w - 1, pixel((x - ox) / self.scale_pv)),
                min(self.video_h - 1, pixel((y - oy) / self.scale_pv)))

    def preview_to_video_rect(self, start: tuple[float, float], end: tuple[float, float]) -> tuple[int, int, int, int]:
        # Drag endpoints may reach the inclusive frame edges. Mouse-down in bars
        # is rejected by preview_to_video before a selection is started.
        ox, oy, _, _ = self.preview_rect
        points = [(min(self.video_w, max(0, pixel((x - ox) / self.scale_pv))),
                   min(self.video_h, max(0, pixel((y - oy) / self.scale_pv)))) for x, y in (start, end)]
        x, right = sorted(p[0] for p in points)
        y, bottom = sorted(p[1] for p in points)
        return x, y, right - x, bottom - y

    def video_to_game(self, x: float, y: float) -> tuple[int, int]:
        sx, sy, px, py = self.game_transform
        return (min(self.game_w - 1, max(0, pixel(x * sx + px))),
                min(self.game_h - 1, max(0, pixel(y * sy + py))))

    def video_to_game_rect(self, x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
        sx, sy, px, py = self.game_transform
        left, right = [min(self.game_w, max(0, pixel(v * sx + px))) for v in (x, x + w)]
        top, bottom = [min(self.game_h, max(0, pixel(v * sy + py))) for v in (y, y + h)]
        if right - left < 2 or bottom - top < 2:
            raise ValueError("Mapped box must be at least 2 × 2 game pixels")
        return left, top, right - left, bottom - top

    def game_to_preview_rect(self, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float] | None:
        sx, sy, px, py = self.game_transform
        # Half a pixel accounts for rounding an authored boundary at a pad edge.
        if (w < 2 or h < 2 or x < px - 0.5 or y < py - 0.5 or
                x + w > px + self.video_w * sx + 0.5 or y + h > py + self.video_h * sy + 0.5):
            return None
        ox, oy, _, _ = self.preview_rect
        return (ox + (x - px) / sx * self.scale_pv, oy + (y - py) / sy * self.scale_pv,
                w / sx * self.scale_pv, h / sy * self.scale_pv)

    def scale_image_video_to_game(self, bgr: np.ndarray, rect: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """Resize a native crop; padding belongs to ROI position, never the PNG."""
        if rect is None:
            sx, sy, _, _ = self.game_transform
            width, height = pixel(bgr.shape[1] * sx), pixel(bgr.shape[0] * sy)
        else:
            if bgr.shape[:2] != (rect[3], rect[2]):
                raise ValueError("Native crop dimensions disagree with its video rectangle")
            _, _, width, height = self.video_to_game_rect(*rect)
        if min(width, height) < 2:
            raise ValueError("Scaled template must be at least 2 × 2 game pixels")
        if (width, height) == (bgr.shape[1], bgr.shape[0]):
            return bgr.copy()
        shrinking = width <= bgr.shape[1] and height <= bgr.shape[0]
        return cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_CUBIC)
