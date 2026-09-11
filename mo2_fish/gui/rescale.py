"""Explicit profile resolution migration; original PNGs and YAML are retained."""
from __future__ import annotations

import copy
from pathlib import Path
import uuid

import cv2
import numpy as np

from gui.coordmap import SpaceMap, pixel
from gui.profiles import Profile


def is_rect(value) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 4 and all(type(v) is int for v in value)


def rescale_profile(profile: Profile, resolution: tuple[int, int], mode: str) -> int:
    """Prepare all pixels first, write new assets, then atomically switch YAML.

    Missing assets stay missing. Image references are rebased into a new folder,
    including external/shared PNGs, so other profiles never change underneath us.
    """
    old = tuple(profile.data["resolution"])
    mapping = SpaceMap(*old, *resolution, *old, mode)
    sx, sy, _, _ = mapping.game_transform
    data = copy.deepcopy(profile.data)
    def rect(owner, key):
        if is_rect(owner.get(key)):
            owner[key] = list(mapping.video_to_game_rect(*owner[key]))
    for key in data.get("rois", {}):
        rect(data["rois"], key)
    for task in data.get("tasks", {}).values():
        for key in ("hook_inventory_rect", "bait_inventory_rect"):
            rect(task, key)
    logout = data.get("logout", {})
    rect(logout, "success_roi")
    for step in logout.get("sequence", []) if isinstance(logout.get("sequence"), list) else []:
        if isinstance(step, dict) and isinstance(step.get("click"), list) and len(step["click"]) == 2:
            step["click"] = list(mapping.video_to_game(*step["click"]))
    compass = data.get("compass", {})
    center = compass.get("center")
    if isinstance(center, list) and len(center) == 2 and all(isinstance(v, (int, float)) for v in center):
        compass["center"] = [pixel(center[0] * sx), pixel(center[1] * sy)]
    radius = compass.get("ring_radius")
    if isinstance(radius, (int, float)):
        # The existing compass models a circle. Stretching into an ellipse needs
        # a fresh radius calibration; never silently invent equivalent math.
        compass["ring_radius"] = radius * sx if abs(sx - sy) < 1e-9 else "TODO"
    if is_rect(compass.get("tick_top_rect")):
        old_roi, new_roi = profile.data["rois"]["compass"], data["rois"]["compass"]
        local = SpaceMap(*old_roi[2:], *new_roi[2:], *old_roi[2:], "stretch")
        compass["tick_top_rect"] = list(local.video_to_game_rect(*compass["tick_top_rect"]))

    refs = [(data.get("templates", {}), key) for key in data.get("templates", {})]
    refs += [(task, key) for task in data.get("tasks", {}).values() for key in ("hook_template", "bait_template")]
    refs += [(compass, "north_template"), (logout, "success_template")]
    refs += [(tick, "path") for tick in compass.get("tick_templates", [])]
    # Collect the actual search regions of shared templates, including fish names
    # (searched in both quest panes) and hook/bait (inventory and equipped slots).
    from gui.video_teacher import ROLES
    limits: dict[Path, list[tuple[int, int]]] = {}
    def lookup(dotted):
        value = data
        for part in dotted.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value
    def bind(path, box):
        if isinstance(path, str) and is_rect(box):
            limits.setdefault(profile.settings.asset(path), []).append(tuple(box[2:]))
    for roi_key, template_key, _ in ROLES.values():
        if template_key:
            bind(lookup(template_key), lookup(roi_key))
    for name, task in data.get("tasks", {}).items():
        bind(data.get("templates", {}).get(name), data.get("rois", {}).get("quest_tooltip"))
        for kind in ("hook", "bait"):
            bind(task.get(f"{kind}_template"), data.get("rois", {}).get(f"{kind}_slot"))
    for tick in compass.get("tick_templates", []):
        bind(tick.get("path"), compass.get("tick_top_rect"))

    prepared: dict[Path, bytes] = {}
    for owner, key in refs:
        value = owner.get(key)
        if not isinstance(value, str) or value.upper() == "TODO":
            continue
        path = profile.settings.asset(value)
        if path in prepared or not path.is_file():
            continue
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"Cannot rescale PNG: {path}")
        scaled = mapping.scale_image_video_to_game(image)
        width, height = scaled.shape[1], scaled.shape[0]
        for rw, rh in limits.get(path, []):
            if width > rw + 1 or height > rh + 1:
                raise ValueError(f"{path.name}: scaled template exceeds its ROI; fix that template before rescaling")
            width, height = min(width, rw), min(height, rh)
        if (width, height) != (scaled.shape[1], scaled.shape[0]):
            scaled = cv2.resize(scaled, (width, height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", scaled)
        if not ok:
            raise ValueError(f"Cannot encode scaled PNG: {path}")
        prepared[path] = encoded.tobytes()

    token = uuid.uuid4().hex[:12]
    directory = profile.path.parent / "templates" / f"rescaled_{token}"
    rebased = {path: f"templates/{directory.name}/{index}_{path.stem}.png" for index, path in enumerate(prepared)}
    for owner, key in refs:
        value = owner.get(key)
        if isinstance(value, str) and profile.settings.asset(value) in rebased:
            owner[key] = rebased[profile.settings.asset(value)]
    data["resolution"], data["video_scale_mode"] = list(resolution), mode
    backup = profile.path.with_name(f"config.before-resolution-{token}.yaml")
    backup.write_bytes(profile.path.read_bytes())
    if prepared:
        directory.mkdir(parents=True)
        for path, content in prepared.items():
            (profile.path.parent / rebased[path]).write_bytes(content)
    # Failure before this commit leaves the original live YAML/assets intact.
    profile.write(data)
    return len(prepared)
