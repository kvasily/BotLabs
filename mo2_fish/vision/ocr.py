"""Optional local Tesseract fallback; OCR is never required for template mode."""
from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np

from config import ConfigError
from vision.templates import Match, gray


class OCR:
    def __init__(self, enabled: bool, executable: str | None = None) -> None:
        self.engine: Any = None
        if enabled:
            try:
                import pytesseract
                if executable:
                    pytesseract.pytesseract.tesseract_cmd = executable
                pytesseract.get_tesseract_version()
                self.engine = pytesseract
            except Exception as exc:
                raise ConfigError(f"OCR enabled but Tesseract/pytesseract unavailable: {exc}") from exc

    def _image(self, image: np.ndarray) -> np.ndarray:
        scaled = cv2.resize(gray(image), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        return cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

    def exact(self, image: np.ndarray, phrase: str) -> Match | None:
        if self.engine is None:
            return None
        data = self.engine.image_to_data(self._image(image), config="--psm 6",
                                         output_type=self.engine.Output.DICT, timeout=2)
        lines: dict[tuple[int, int, int], list[int]] = {}
        for i, word in enumerate(data["text"]):
            if word.strip() and float(data["conf"][i]) >= 65:
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                lines.setdefault(key, []).append(i)
        wanted = phrase.split()
        for indices in lines.values():
            for start in range(len(indices) - len(wanted) + 1):
                selected = indices[start:start + len(wanted)]
                if [data["text"][i].strip() for i in selected] != wanted:
                    continue
                x = min(data["left"][i] for i in selected)
                y = min(data["top"][i] for i in selected)
                right = max(data["left"][i] + data["width"][i] for i in selected)
                bottom = max(data["top"][i] + data["height"][i] for i in selected)
                return Match(1.0, x // 2, y // 2, (right - x) // 2, (bottom - y) // 2)
        return None

    def progress(self, image: np.ndarray) -> int | None:
        if self.engine is None:
            return None
        text = self.engine.image_to_string(self._image(image), config="--psm 6", timeout=2)
        values = {int(m) for m in re.findall(r"(?<!\d)([0-3])\s*/\s*3(?!\d)", text)}
        return values.pop() if len(values) == 1 else None
