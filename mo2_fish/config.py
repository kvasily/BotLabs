"""Configuration and preflight. No device is opened and no input is sent here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

import yaml


class ConfigError(ValueError):
    """Actionable configuration/asset error."""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @classmethod
    def parse(cls, value: Any, name: str, bounds: tuple[int, int]) -> Rect:
        if not isinstance(value, list) or len(value) != 4 or not all(type(v) is int for v in value):
            raise ConfigError(f"{name}: set [x, y, width, height] in physical pixels (currently {value!r})")
        rect = cls(*value)
        if min(rect.x, rect.y) < 0 or min(rect.width, rect.height) <= 0 or rect.x + rect.width > bounds[0] or rect.y + rect.height > bounds[1]:
            raise ConfigError(f"{name}: rectangle is outside {bounds}")
        return rect


@dataclass(frozen=True)
class Settings:
    path: Path
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def asset(self, value: str) -> Path:
        return (self.path.parent / value).resolve()

    def rect(self, name: str) -> Rect:
        return Rect.parse(self["rois"][name], f"rois.{name}", self.resolution)

    @property
    def resolution(self) -> tuple[int, int]:
        value = self.data.get("resolution")
        if not isinstance(value, list) or len(value) != 2 or not all(type(v) is int and v >= 2 for v in value):
            raise ConfigError("resolution must be [width, height], integers of at least 2 physical pixels")
        return tuple(value)

    def required_assets(self) -> list[Path]:
        values = [v for v in self["templates"].values() if v]
        values += [v for v in self["audio"]["templates"].values() if v]
        for task in self["tasks"].values():
            values += [task["hook_template"], task["bait_template"]]
            if task.get("audio_template"):
                values.append(task["audio_template"])
        c = self["compass"]
        values += [c["north_template"]] if c["mode"] == "north" else [t["path"] for t in c["tick_templates"]]
        values.append(self["logout"]["success_template"])
        return sorted({self.asset(v) for v in values})

    def validate(self, assets: bool = True) -> None:
        errors: list[str] = []

        def require(ok: bool, message: str) -> None:
            if not ok:
                errors.append(message)

        def todo(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    if prefix == "compass" and ((k == "tick_top_rect" and value.get("mode") != "ticks") or
                                                (k in ("center", "ring_radius", "north_template") and value.get("mode") == "ticks")):
                        continue
                    todo(v, f"{prefix}.{k}".lstrip("."))
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    todo(v, f"{prefix}[{i}]")
            elif isinstance(value, str) and value.upper() == "TODO":
                errors.append(f"{prefix}: replace TODO")

        todo(self.data)
        try:
            bounds = self.resolution
        except ConfigError as exc:
            errors.append(str(exc))
            bounds = (1, 1)
        require(self.data.get("video_scale_mode", "fit") in ("fit", "stretch"), "video_scale_mode must be fit or stretch")
        scale = self["counts_per_degree"]
        require(isinstance(scale, (float, int)) and math.isfinite(float(scale)) and scale != 0,
                "counts_per_degree: run tools/heading_calibrator.py; nonzero signed number required")
        require(self["water_heading_arc_deg"] == [270, 360], "water_heading_arc_deg must be [270, 360]")
        require(self["quest_catch_count"] == 3, "quest_catch_count must be 3")
        require(bool(self["window_title_contains"]), "window_title_contains cannot be empty")
        require(isinstance(self["game_origin"], list) and len(self["game_origin"]) == 2 and
                all(type(v) is int for v in self["game_origin"]), "game_origin must be [desktop_x, desktop_y] in physical pixels")
        required_images = ("hit_marker", "taskmaster", "bassle", "redline_torp", "quest_gui", "confirm", "turn_in",
                           "bait_empty", "bait_zero", "progress_0", "progress_1", "progress_2", "progress_3")
        for name in required_images:
            require(isinstance(self["templates"].get(name), str) and bool(self["templates"].get(name)), f"templates.{name}: required PNG path")
        require(0 < self["vision"]["threshold"] <= 1, "vision.threshold must be > 0 and <= 1")
        require(self["vision"]["stable_frames"] >= 2, "vision.stable_frames must be >= 2")
        for name in self["rois"]:
            try:
                self.rect(name)
            except ConfigError as exc:
                errors.append(str(exc))
        expected = {"bassle": ("Bassle", "hold_rmb_only", 0, False),
                    "redline_torp": ("Redline Torp", "reel_lmb_and_pull_rmb_on_tension", 1, True)}
        require(set(self["tasks"]) == set(expected), "tasks must contain only bassle and redline_torp")
        for name, spec in expected.items():
            task = self["tasks"].get(name, {})
            require(tuple(task.get(k) for k in ("fish_name", "fight", "precedence", "always_available")) == spec,
                    f"tasks.{name}: fish name, fight, precedence, or availability changed")
            for kind in ("hook", "bait"):
                try:
                    Rect.parse(task.get(f"{kind}_inventory_rect"), f"tasks.{name}.{kind}_inventory_rect", bounds)
                except ConfigError as exc:
                    errors.append(str(exc))
        a, c = self["audio"], self["compass"]
        require(a["sample_rate"] == 48000 and a["ring_seconds"] == 1.0, "audio must use 48 kHz and a 1.0s ring")
        require(20 <= a["hop_ms"] <= 50 and a["channels"] >= 2, "audio: hop_ms 20–50, channels >= 2 required")
        for side in ("on", "off"):
            require(80 <= a[f"tension_{side}_ms"] <= 200, f"audio.tension_{side}_ms must be 80–200")
        for name in ("bubble", "splash", "tension", "quest_complete"):
            require(bool(a["templates"].get(name)), f"audio.templates.{name}: required WAV")
            require(0 < a["thresholds"].get(name, 0) <= 1, f"audio.thresholds.{name} must be > 0 and <= 1")
        require(a["min_rms"] > 0, "audio.min_rms must be positive")
        require(bool(self["templates"].get("catch") or a["templates"].get("catch")), "configure a catch PNG or catch WAV")
        require(c["mode"] in ("north", "ticks"), "compass.mode must be north or ticks")
        require(c["bearing_sign"] in (-1, 1), "compass.bearing_sign must be -1 or 1")
        if c["mode"] == "north":
            require(isinstance(c["north_template"], str) and bool(c["north_template"]), "compass.north_template: required PNG path")
            require(isinstance(c["center"], list) and len(c["center"]) == 2, "compass.center must be [cx, cy]")
            require(isinstance(c["ring_radius"], (int, float)) and c["ring_radius"] > 0, "compass.ring_radius must be positive")
        else:
            require(len(c["tick_templates"]) >= 4, "ticks mode needs labeled top-patch templates spanning the circle")
            try:
                r = self.rect("compass")
                Rect.parse(c["tick_top_rect"], "compass.tick_top_rect", (r.width, r.height))
            except ConfigError as exc:
                errors.append(str(exc))
        require(self["progress"]["verify_every_n_catches"] >= 1, "verify_every_n_catches must be >= 1")
        require(self["progress"]["max_unverified_catches"] >= self["progress"]["verify_every_n_catches"],
                "max_unverified_catches must be >= verify_every_n_catches")
        require(type(self["enable_wasd_recovery"]) is bool, "enable_wasd_recovery must be true or false (unquoted)")
        require(self["recovery"]["wasd_key"] in "wasd" and len(self["recovery"]["wasd_key"]) == 1, "recovery.wasd_key must be w/a/s/d")
        require(0 < self["recovery"]["wasd_hold_s"] <= 0.5, "recovery.wasd_hold_s must be > 0 and <= 0.5")
        require(2 <= self["recovery"]["direct_cast_tries"] <= 3, "recovery.direct_cast_tries must be 2–3")
        require(1 <= self["recovery"]["sweep_cast_tries"] <= 6, "recovery.sweep_cast_tries must be 1–6")
        require(0 < self["recovery"]["npc_scan_step_deg"] <= 15, "npc_scan_step_deg must be > 0 and <= 15")
        for key, value in self["timing"].items():
            nums = value if isinstance(value, list) else [value]
            require(all(isinstance(n, (int, float)) and n > 0 for n in nums), f"timing.{key} must be positive")
            if isinstance(value, list):
                require(len(value) == 2 and value[0] <= value[1], f"timing.{key}: expected ascending [min, max]")
        sequence = self["logout"]["sequence"]
        require(isinstance(sequence, list) and bool(sequence), "logout.sequence: configure keys/menu clicks")
        if isinstance(sequence, list):
            for step in sequence:
                require(isinstance(step, dict) and (('key' in step) != ('click' in step)), "logout step: choose key OR click")
                if isinstance(step, dict) and "click" in step:
                    p = step["click"]
                    require(isinstance(p, list) and len(p) == 2 and all(type(v) is int for v in p) and 0 <= p[0] < bounds[0] and 0 <= p[1] < bounds[1],
                            "logout.click must be a point within the game rectangle")
                if isinstance(step, dict):
                    require(isinstance(step.get("wait_s", 0.7), (int, float)) and step.get("wait_s", 0.7) >= 0,
                            "logout.wait_s must be a nonnegative number")
        require(bool(self["logout"]["success_template"]), "logout.success_template is required")
        try:
            Rect.parse(self["logout"]["success_roi"], "logout.success_roi", bounds)
        except ConfigError as exc:
            errors.append(str(exc))
        if assets:
            try:
                for path in self.required_assets():
                    if not path.is_file():
                        errors.append(f"Missing asset: {path}")
            except (TypeError, KeyError) as exc:
                errors.append(f"Invalid asset configuration: {exc}")
        if errors:
            raise ConfigError("Preflight failed:\n  - " + "\n  - ".join(errors))


def load_config(path: str | Path) -> Settings:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"Missing configuration: {path}")
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    return Settings(path, data)
