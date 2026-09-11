"""Absolute yaw from the N glyph bearing OR calibrated, distinguishable top patches."""
from __future__ import annotations

import math

import cv2
import numpy as np

from config import Rect, Settings
from vision.capture import ScreenCapture
from vision.templates import TemplateStore, gray


class CompassLost(RuntimeError):
    pass


def yaw_from_north(x: float, y: float, center: tuple[float, float],
                   sign: int = -1, offset: float = 0) -> float:
    bearing = math.degrees(math.atan2(x - center[0], center[1] - y))
    return (sign * bearing + offset) % 360


class Compass:
    def __init__(self, settings: Settings, screen: ScreenCapture, templates: TemplateStore) -> None:
        self.settings, self.screen, self.templates = settings, screen, templates
        self.last_score = 0.0
        self.last_heading = 0.0

    def read(self) -> float:
        cfg = self.settings["compass"]
        image = self.screen.grab("compass")
        if cfg["mode"] == "north":
            template = self.templates.load(cfg["north_template"])
            source = gray(image)
            if template.shape[0] > source.shape[0] or template.shape[1] > source.shape[1]:
                raise CompassLost("N template exceeds compass ROI")
            scores = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
            yy, xx = np.indices(scores.shape)
            cx, cy = cfg["center"]
            radii = np.hypot(xx + template.shape[1] / 2 - cx, yy + template.shape[0] / 2 - cy)
            scores[np.abs(radii - cfg["ring_radius"]) > cfg["ring_tolerance_px"]] = -1
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score < self.settings["vision"]["threshold"]:
                raise CompassLost(f"Cannot locate N on compass ring (score={score:.3f})")
            heading = yaw_from_north(x + template.shape[1] / 2, y + template.shape[0] / 2,
                                     tuple(cfg["center"]), cfg["bearing_sign"], cfg["zero_offset_deg"])
        else:
            r = Rect.parse(cfg["tick_top_rect"], "compass.tick_top_rect", (image.shape[1], image.shape[0]))
            patch = image[r.y:r.y + r.height, r.x:r.x + r.width]
            ranked = sorted(((self.templates.match(patch, t["path"]).score, float(t["heading"]))
                             for t in cfg["tick_templates"]), reverse=True)
            score, heading = ranked[0]
            if score < self.settings["vision"]["threshold"] or score - ranked[1][0] < cfg["tick_ambiguity_margin"]:
                raise CompassLost("Top tick patch is ambiguous; include labels or use N mode")
        self.last_score, self.last_heading = float(score), heading % 360
        return self.last_heading
