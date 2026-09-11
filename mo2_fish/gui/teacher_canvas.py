"""Paint player frames and role boxes; convert native pixels only when drawing."""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from gui.coordmap import SpaceMap


class FrameCanvas(QWidget):
    selection_changed = Signal()
    selection_started = Signal()
    selection_finished = Signal()
    cursor_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(400, 220)
        self.image = None
        self._frame = None
        self.outer = self.inner = None
        self.saved_roi = None  # Compatibility with the existing smoke helper.
        self.start_point = None
        self.game_size = (3840, 2160)
        self.scale_mode = "fit"
        self.active_role = "compass"
        self.role_boxes = {}
        self.setMouseTracking(True)

    @property
    def frame(self):
        if self._frame is None and self.image is not None:
            rgb = self.image.convertToFormat(QImage.Format.Format_RGB888)
            pixels = np.frombuffer(rgb.constBits(), np.uint8).reshape(rgb.height(), rgb.bytesPerLine())
            self._frame = cv2.cvtColor(pixels[:, :rgb.width()*3].reshape(rgb.height(), rgb.width(), 3), cv2.COLOR_RGB2BGR)
        return self._frame

    @frame.setter
    def frame(self, value):
        self._frame = value

    def set_frame(self, frame):
        self._frame = frame
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self.image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self.update()

    def set_image(self, image):
        self.image, self._frame = image, None
        self.update()

    def mapping(self):
        assert self.image is not None
        return SpaceMap(self.image.width(), self.image.height(), *self.game_size, self.width(), self.height(), self.scale_mode)

    @staticmethod
    def outline_color(selected):
        return QColor(0, 255, 100, 255) if selected else QColor(255, 55, 65, 115)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#080d14"))
        if self.image is None:
            p.setPen(QColor("#71859b"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "OPEN A RECORDING\nSelect a role, then draw its rectangle")
            self.paint_fix_border(p)
            return
        mapping = self.mapping()
        p.drawImage(QRectF(*mapping.preview_rect), self.image)
        p.setBrush(Qt.BrushStyle.NoBrush)
        roles = sorted(self.role_boxes, key=lambda role: role == self.active_role)
        for role in roles:
            box = mapping.game_to_preview_rect(*self.role_boxes[role])
            if box:
                p.setPen(QPen(self.outline_color(role == self.active_role), 2))
                p.drawRect(QRectF(*box))
        if self.outer and self.start_point is not None:
            left, top, _, _ = mapping.preview_rect
            x, y, w, h = self.outer
            p.setPen(QPen(self.outline_color(True), 2))
            p.drawRect(QRectF(left+x*mapping.scale_pv, top+y*mapping.scale_pv, w*mapping.scale_pv, h*mapping.scale_pv))
        self.paint_fix_border(p)

    def paint_fix_border(self, painter):
        if self.property("fixHighlighted"):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ff555f"), 2))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def mousePressEvent(self, event):
        if self.image is not None and event.button() == Qt.MouseButton.LeftButton:
            if self.mapping().preview_to_video(event.position().x(), event.position().y()) is None:
                event.ignore()
                return
            self.outer = self.inner = None
            self.role_boxes.pop(self.active_role, None)
            self.selection_started.emit()
            self.start_point = event.position()
            self.update()

    def mouseMoveEvent(self, event):
        if self.image is None:
            return
        x, y = event.position().x(), event.position().y()
        mapping = self.mapping()
        video = mapping.preview_to_video(x, y)
        game = mapping.video_to_game(*video) if video else None
        self.cursor_changed.emit(f"preview ({round(x)}, {round(y)})  |  video {video or '— letterbox'}  |  game {game or '—'}")
        if self.start_point is not None:
            self.outer = mapping.preview_to_video_rect((self.start_point.x(), self.start_point.y()), (x, y))
            self.selection_changed.emit()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start_point is not None:
            self.mouseMoveEvent(event)
            self.start_point = None
            self.selection_finished.emit()
