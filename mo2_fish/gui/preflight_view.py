"""Checklist presentation of main.check_assets; no separate validation rules."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget
from gui.field_help import HelpButton

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
    fix_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.button = QPushButton("Validate setup")
        self.summary = QLabel("Not validated this session")
        self.summary.setObjectName("muted")
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setMinimumHeight(190)
        self.items.itemClicked.connect(self.activate_fix)
        self.items.itemActivated.connect(self.activate_fix)
        self.help_key = lambda line: "profile"
        layout.addWidget(self.button)
        layout.addWidget(self.summary)
        layout.addWidget(self.items)

    def show_result(self, passed: bool, lines: list[str]) -> None:
        self.summary.setText("✓ READY TO ARM" if passed else "SETUP NEEDS ATTENTION")
        self.summary.setStyleSheet("color: #5ce0b3" if passed else "color: #ffc36a")
        self.items.clear()
        for line in lines:
            item = QListWidgetItem(("PASS  " if passed else "FIX    ") + line, self.items)
            if passed:
                continue
            item.setData(Qt.ItemDataRole.UserRole, line)
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(4, 4, 4, 4)
            fix = QPushButton("FIX")
            fix.setFixedSize(60, 28)
            fix.setStyleSheet("QPushButton { padding:3px 8px; }")
            fix.clicked.connect(lambda checked=False, text=line: self.fix_requested.emit(text))
            label = QLabel(line)
            label.setWordWrap(True)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(fix)
            layout.addWidget(label, 1)
            layout.addWidget(HelpButton(self.help_key(line)))
            height = max(56, label.heightForWidth(max(200, self.items.viewport().width()-120)) + 28)
            item.setSizeHint(QSize(0, height))
            self.items.setItemWidget(item, row)
        self.button.setEnabled(True)

    def activate_fix(self, item):
        line = item.data(Qt.ItemDataRole.UserRole)
        if line:
            self.fix_requested.emit(line)
