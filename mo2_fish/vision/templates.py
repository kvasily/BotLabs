"""Strict image loading and explicit match confidence; missing assets never match."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from config import ConfigError, Settings


@dataclass(frozen=True)
class Match:
    score: float
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def gray(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


class TemplateStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache: dict[Path, NDArray[np.uint8]] = {}

    def load(self, value: str) -> NDArray[np.uint8]:
        path = self.settings.asset(value)
        if path not in self.cache:
            if not path.is_file():
                raise ConfigError(f"Missing PNG: {path}")
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ConfigError(f"Cannot decode PNG: {path}")
            if min(image.shape) < 2 or float(image.std()) < 1:
                raise ConfigError(f"{path}: template is too small/constant; include distinctive visible detail")
            self.cache[path] = image
        return self.cache[path]

    def match(self, image: NDArray[np.uint8], value: str) -> Match:
        template = self.load(value)
        source = gray(image)
        height, width = template.shape
        if height > source.shape[0] or width > source.shape[1]:
            raise ConfigError(f"{value}: {width}x{height} template exceeds {source.shape[1]}x{source.shape[0]} ROI")
        result = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        return Match(float(score), *location, width, height)

    def named(self, image: NDArray[np.uint8], name: str) -> Match | None:
        path = self.settings["templates"].get(name)
        if not path:
            return None
        result = self.match(image, path)
        return result if result.score >= self.settings["vision"]["threshold"] else None
