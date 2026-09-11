"""Small worker dispatcher: blocking work stays outside the Qt UI thread."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot


class Jobs(QObject):
    completed = Signal(object, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.completed.connect(self._deliver)

    def run(self, work: Callable[[], Any], success: Callable[[Any], None], failure: Callable[[str], None]) -> None:
        def execute() -> None:
            try:
                value = work()
                self.completed.emit(success, value, None)
            except Exception as exc:
                logging.exception("GUI background operation failed")
                self.completed.emit(failure, str(exc), exc)
        threading.Thread(target=execute, daemon=True, name="gui-job").start()

    @Slot(object, object, object)
    def _deliver(self, callback: Callable, value: Any, error: Exception | None) -> None:
        callback(value)
