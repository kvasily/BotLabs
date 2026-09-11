"""Portable YAML profiles and asset authoring. The existing Settings is the schema."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import shutil
import sys
from typing import Any

import yaml

from config import Settings, load_config


def app_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]


def default_config() -> Path:
    live = app_root() / "mo2_fish" / "config.yaml"
    if live.is_file():
        return live
    return Path(getattr(sys, "_MEIPASS", app_root())) / "default_config.yaml"


def fingerprint(path: Path) -> str:
    """Invalidate a preflight pass on changes to YAML or any referenced asset."""
    digest = hashlib.sha256(path.read_bytes())
    cfg = load_config(path)
    try:
        assets = cfg.required_assets()
    except (KeyError, TypeError, ValueError):
        assets = []  # check_assets provides the errors, not a competing validator.
    for asset in assets:
        digest.update(str(asset).encode("utf-8"))
        digest.update(asset.read_bytes() if asset.is_file() else b"MISSING")
    return digest.hexdigest()


class Profile:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.reload()

    def reload(self) -> None:
        self.settings = load_config(self.path)

    @property
    def data(self) -> dict[str, Any]:
        return self.settings.data

    def write(self, data: dict[str, Any] | None = None) -> None:
        value = self.data if data is None else data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")
        temporary.replace(self.path)
        self.reload()

    def patch(self, dotted: str, value: Any) -> None:
        updated = copy.deepcopy(self.data)
        parent = updated
        keys = dotted.split(".")
        for key in keys[:-1]:
            parent = parent.setdefault(key, {})
        parent[keys[-1]] = value
        self.write(updated)

    def asset(self, subfolder: str, filename: str) -> Path:
        if subfolder not in ("templates", "sfx") or Path(filename).name != filename:
            raise ValueError("Asset name must be a filename within templates/ or sfx/")
        directory = self.path.parent / subfolder
        directory.mkdir(exist_ok=True)
        return directory / filename


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or app_root() / "profiles").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.last_file = self.root / ".last-profile"

    def remember(self, profile: Profile) -> None:
        profile.path.relative_to(self.root)
        self.last_file.write_text(str(profile.path), encoding="utf-8")

    def startup(self) -> Profile:
        if self.last_file.is_file():
            try:
                path = Path(self.last_file.read_text(encoding="utf-8").strip()).resolve()
                path.relative_to(self.root)
                if path.is_file():
                    return Profile(path)
            except (ValueError, OSError):
                pass
        existing = self.root / "Default" / "config.yaml"
        profile = Profile(existing) if existing.is_file() else self.clone(default_config(), "Default")
        self.remember(profile)
        return profile

    def load(self, path: Path) -> Profile:
        path.resolve().relative_to(self.root)
        profile = Profile(path)
        self.remember(profile)
        return profile

    def clone(self, source: Path, name: str, data: dict[str, Any] | None = None) -> Profile:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", name) or name.rstrip() != name:
            raise ValueError("Use 1–64 letters, numbers, spaces, underscores or hyphens for the profile name")
        if name.upper() in {"CON", "PRN", "AUX", "NUL", *[f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)]}:
            raise ValueError("That profile name is reserved by Windows")
        destination = (self.root / name).resolve()
        destination.relative_to(self.root)
        if destination.exists():
            raise ValueError(f"Profile already exists: {name}; use Save or a new name")
        cfg = load_config(source)
        if data is not None:
            cfg = Settings(cfg.path, data)
        data = copy.deepcopy(cfg.data)
        destination.mkdir()
        for folder in ("templates", "sfx"):
            (destination / folder).mkdir()
        # Rebase absolute/external references and copy actual assets only. Missing
        # assets remain missing and still fail the existing preflight by filename.
        refs: list[tuple[dict, str]] = []
        refs += [(data.get("templates", {}), k) for k in data.get("templates", {})]
        refs += [(data.get("audio", {}).get("templates", {}), k) for k in data.get("audio", {}).get("templates", {})]
        for task in data.get("tasks", {}).values():
            refs += [(task, k) for k in ("hook_template", "bait_template", "audio_template")]
        refs += [(data.get("compass", {}), "north_template"), (data.get("logout", {}), "success_template")]
        refs += [(tick, "path") for tick in data.get("compass", {}).get("tick_templates", [])]
        destinations: dict[Path, str] = {}
        for owner, key in refs:
            value = owner.get(key)
            if not isinstance(value, str) or value == "TODO":
                continue
            old = cfg.asset(value)
            folder = "sfx" if old.suffix.lower() == ".wav" else "templates"
            new = destinations.setdefault(old, f"{folder}/{old.stem}_{hashlib.sha256(str(old).encode()).hexdigest()[:6]}{old.suffix}")
            # Keep conventional filenames for already profile-relative assets.
            if not Path(value).is_absolute() and Path(value).parts[0] == folder and ".." not in Path(value).parts:
                new = value.replace("\\", "/")
                destinations[old] = new
            owner[key] = new
            target = destination / new
            target.parent.mkdir(parents=True, exist_ok=True)
            if old.is_file():
                shutil.copy2(old, target)
        config_path = destination / "config.yaml"
        config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        result = Profile(config_path)
        self.remember(result)
        return result
