"""Session-only audio panel sizing; no document or playback state lives here."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QSplitterHandle, QToolButton


class AudioToolsSplitter(QSplitter):
    def __init__(self, picture, audio):
        super().__init__(Qt.Orientation.Vertical)
        self.setHandleWidth(30)
        self.addWidget(picture)
        self.addWidget(audio)
        self.setCollapsible(0, False)
        self.setCollapsible(1, True)
        self.setStretchFactor(0, 4)
        self.setStretchFactor(1, 1)
        self.expanded_sizes = [600, 200]
        self.setSizes(self.expanded_sizes)
        self.button = self.handle(1).button
        self.button.toggled.connect(self.set_audio_expanded)
        self.splitterMoved.connect(self.sync_toggle)

    def createHandle(self):
        handle = QSplitterHandle(self.orientation(), self)
        layout = QHBoxLayout(handle)
        layout.setContentsMargins(0, 0, 0, 0)
        handle.button = QToolButton(handle)
        handle.button.setCheckable(True)
        handle.button.setChecked(True)
        handle.button.setText("▾ Audio tools")
        handle.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(handle.button)
        layout.addStretch()
        return handle

    def set_audio_expanded(self, expanded):
        sizes = self.sizes()
        if expanded:
            self.setSizes(self.expanded_sizes)
        else:
            if sizes[1] > 0:
                self.expanded_sizes = sizes
            self.setSizes([max(1, sum(sizes)), 0])
        self.sync_toggle()

    def sync_toggle(self, *args):
        expanded = self.sizes()[1] > 0
        if expanded:
            self.expanded_sizes = self.sizes()
        self.button.blockSignals(True)
        self.button.setChecked(expanded)
        self.button.setText("▾ Audio tools" if expanded else "▸ Audio tools")
        self.button.blockSignals(False)
