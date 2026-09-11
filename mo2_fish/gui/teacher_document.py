"""A teacher document is dirty in memory until an explicit Save succeeds."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import tempfile

import numpy as np

from gui.coordmap import SpaceMap
from gui.profiles import Profile, ProfileStore
from gui.rescale import is_rect, rescale_profile


UNIVERSAL = ("compass", "inventory", "hit_marker", "interact_prompt", "hook_slot", "bait_slot",
             "quest_paper", "quest_tooltip", "confirm_button", "turn_in_button", "quest_list",
             "bait_count", "catch_indicator", "logout_success")
COMMON = ("compass_north", "bait_empty", "progress_0", "progress_1", "progress_2", "progress_3")
GROUPS = (("Universal", UNIVERSAL), ("Common templates", COMMON),
          ("Bassle", ("bassle", "hook_bassle_source", "bait_bassle_source")),
          ("Redline Torp", ("redline_torp", "hook_redline_torp_source", "bait_redline_torp_source")))
ROI_ONLY = {"compass", "inventory", "hook_slot", "bait_slot", "quest_paper", "quest_tooltip"}


def lookup(data, dotted):
    value = data
    for key in dotted.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


@dataclass
class Crop:
    rect: tuple[int, int, int, int]
    source_size: tuple[int, int]
    pixels: np.ndarray

    @classmethod
    def capture(cls, frame, rect):
        from gui.video_teacher import native_crop
        return cls(tuple(rect), (frame.shape[1], frame.shape[0]), native_crop(frame, rect))

    def game_rect(self, resolution, mode):
        return SpaceMap(*self.source_size, *resolution, *self.source_size, mode).video_to_game_rect(*self.rect)

    def materialize(self):
        # Retain only selected pixels in memory. save_frame_role will read this
        # exact rectangle; no pixels outside the captured crop enter the PNG.
        frame = np.empty((self.source_size[1], self.source_size[0], 3), dtype=np.uint8)
        x, y, w, h = self.rect
        frame[y:y+h, x:x+w] = self.pixels
        return frame


class TeacherDocument:
    def __init__(self, profile: Profile):
        self.load(profile)

    def load(self, profile: Profile):
        from gui.video_teacher import ROLES
        self.profile = profile
        self.resolution = tuple(profile.data["resolution"])
        self.mode = profile.data.get("video_scale_mode", "fit")
        self.source_size = profile.data.get("video_source_resolution")
        self.rescale_existing = False
        self.settings_dirty = False
        self.changes: dict[str, Crop | None] = {}
        self.saved = {role: tuple(box) for role, box in profile.data.get("teacher_boxes", {}).items()
                      if role in ROLES and is_rect(box)}
        for role, (key, _, _) in ROLES.items():
            if role not in UNIVERSAL and not role.endswith("_source"):
                continue  # Template positions cannot be inferred from a search ROI.
            value = lookup(profile.data, key)
            if role not in self.saved and is_rect(value):
                self.saved[role] = tuple(value)

    @property
    def dirty(self):
        return self.settings_dirty or bool(self.changes)

    def begin(self, role):
        # A new draw removes precisely this role, even before mouse-up.
        self.changes[role] = None

    def replace(self, role, frame, rect):
        crop = Crop.capture(frame, rect)
        crop.game_rect(self.resolution, self.mode)  # Reject tiny game-space crops.
        self.changes[role] = crop
        self.source_size = list(crop.source_size)

    def rects(self):
        boxes = dict(self.saved)
        if self.rescale_existing and self.resolution != tuple(self.profile.data["resolution"]):
            old = tuple(self.profile.data["resolution"])
            mapping = SpaceMap(*old, *self.resolution, *old, self.mode)
            boxes = {key: mapping.video_to_game_rect(*box) for key, box in boxes.items()}
        for role, crop in self.changes.items():
            boxes.pop(role, None)
            if crop is not None:
                boxes[role] = crop.game_rect(self.resolution, self.mode)
        return boxes

    def target(self, resolution, mode, rescale=False):
        self.resolution, self.mode = tuple(resolution), mode
        self.rescale_existing = rescale
        self.settings_dirty = True

    def save(self, target: Profile | None = None):
        """Stage crop exports first; commit the live YAML last, with rollback."""
        from gui.video_teacher import ROLES, save_frame_role
        target = target or self.profile
        if any(crop is None for crop in self.changes.values()):
            raise ValueError("Finish or redraw the empty selection before saving.")
        with tempfile.TemporaryDirectory(prefix="mo2fish-teacher-") as directory:
            stage = ProfileStore(Path(directory)).clone(target.path, "Stage")
            if self.rescale_existing and tuple(stage.data["resolution"]) != self.resolution:
                rescale_profile(stage, self.resolution, self.mode)
            data = copy.deepcopy(stage.data)
            data["resolution"], data["video_scale_mode"] = list(self.resolution), self.mode
            data["video_source_resolution"] = self.source_size
            stage.write(data)
            # Template-specific selections cannot overwrite the common search
            # region. Explicit universal selections always define it once.
            common_rois = copy.deepcopy(stage.data.get("rois", {}))
            for role in UNIVERSAL:
                crop = self.changes.get(role)
                if crop and ROLES[role][0].startswith("rois."):
                    common_rois[ROLES[role][0].split(".")[1]] = list(crop.game_rect(self.resolution, self.mode))
            for role, crop in self.changes.items():
                save_frame_role(stage, crop.materialize(), crop.rect, None, role, save_template=role not in ROI_ONLY)
            data = copy.deepcopy(stage.data)
            data["video_source_resolution"] = self.source_size
            for key, box in common_rois.items():
                if is_rect(box):
                    data["rois"][key] = box
            data["teacher_boxes"] = {key: list(box) for key, box in self.rects().items()}
            stage.write(data)
            # Copy only staged PNG/WAV assets. Existing pixels are restored if
            # a write or the final atomic YAML commit fails.
            outputs = {p.relative_to(stage.path.parent): p.read_bytes()
                       for folder in ("templates", "sfx") for p in (stage.path.parent / folder).rglob("*") if p.is_file()}
            backups = {}
            try:
                for relative, content in outputs.items():
                    path = target.path.parent / relative
                    backups[path] = path.read_bytes() if path.exists() else None
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(path.suffix + ".teacher-tmp")
                    temporary.write_bytes(content)
                    temporary.replace(path)
                target.write(stage.data)
            except Exception:
                for path, content in backups.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
                raise
        self.load(target)
        return target

    def save_as(self, store: ProfileStore, name: str):
        target = store.clone(self.profile.path, name)
        return self.save(target)
