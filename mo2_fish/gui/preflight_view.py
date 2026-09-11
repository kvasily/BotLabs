"""Checklist presentation of main.check_assets; no separate validation rules."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from config import load_config
from gui.profiles import fingerprint
from main import check_assets


def validate_profile(path: Path) -> tuple[bool, list[str], str | None]:
    try:
        before = fingerprint(path)
        check_assets(load_config(path))
        after = fingerprint(path)
        if before != after:
            raise RuntimeError("Profile changed during validation. Validate again.")
        return True, ["YAML, ROI bounds, required PNGs/WAVs and enabled OCR passed check_assets.",
                      "Profile and asset fingerprint saved for this session.",
                      "Live focus and input permission are checked again at Start."], after
    except Exception as exc:
        return False, [line.strip().removeprefix("- ") for line in str(exc).splitlines() if line.strip()], None


class PreflightView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.button = QPushButton("Validate setup")
        self.summary = QLabel("Not validated this session")
        self.summary.setObjectName("muted")
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setMinimumHeight(190)
        layout.addWidget(self.button)
        layout.addWidget(self.summary)
        layout.addWidget(self.items)

    def show_result(self, passed: bool, lines: list[str]) -> None:
        self.summary.setText("✓ READY TO ARM" if passed else "SETUP NEEDS ATTENTION")
        self.summary.setStyleSheet("color: #5ce0b3" if passed else "color: #ffc36a")
        self.items.clear()
        self.items.addItems([("PASS  " if passed else "FIX    ") + line for line in lines])
        self.button.setEnabled(True)
