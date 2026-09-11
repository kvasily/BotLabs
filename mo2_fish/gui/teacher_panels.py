"""Session-only audio panel sizing; no document or playback state lives here."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QSplitterHandle, QToolButton


class AudioToolsHandle(QSplitterHandle):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.splitter().prepare_handle_drag()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            # A drag may collapse and reopen without releasing the pointer.
            self.splitter().prepare_handle_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.splitter().finish_handle_drag()


class AudioToolsSplitter(QSplitter):
    expanded_changed = Signal(bool)

    def __init__(self, picture, audio):
        super().__init__(Qt.Orientation.Vertical)
        self.audio = audio
        self.audio_expanded = True
        self._audio_maximum = audio.maximumHeight()
        self._audio_minimum = audio.minimumHeight()
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
        handle = AudioToolsHandle(self.orientation(), self)
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

    def prepare_handle_drag(self):
        if not self.audio_expanded:
            # Keep the pane collapsed, but let Qt move it beyond zero again.
            self.audio.setMaximumHeight(self._audio_maximum)
            self.audio.setMinimumHeight(self._audio_minimum)
            self.setSizes([1, 0])

    def finish_handle_drag(self):
        self.sync_toggle()
        if not self.audio_expanded:
            self.set_audio_expanded(False)

    def set_audio_expanded(self, expanded):
        expanded = bool(expanded)
        changed = expanded != self.audio_expanded
        sizes = self.sizes()
        self.audio_expanded = expanded
        if expanded:
            self.audio.setMaximumHeight(self._audio_maximum)
            self.audio.setMinimumHeight(self._audio_minimum)
            # Restore enough room for all controls even after a window resize.
            self.expanded_sizes[1] = max(self.expanded_sizes[1], self.audio.minimumSizeHint().height())
            self.setSizes(self.expanded_sizes)
        else:
            if changed and sizes[1] > 0:
                self.expanded_sizes = sizes
                self._audio_minimum = self.audio.minimumHeight()
                self._audio_maximum = self.audio.maximumHeight()
            # Constrain the splitter child itself, not a nested waveform. This
            # survives resize, tab switches and audio-layout size-hint changes.
            self.audio.setMinimumHeight(0)
            self.audio.setMaximumHeight(0)
            self.setSizes([1, 0])
        self.update_toggle()
        if changed:
            self.expanded_changed.emit(expanded)

    def sync_toggle(self, *args):
        expanded = self.sizes()[1] > 0
        if expanded != self.audio_expanded:
            if expanded:
                self.expanded_sizes = self.sizes()
            self.set_audio_expanded(expanded)
            return
        if expanded:
            self.expanded_sizes = self.sizes()
        self.update_toggle()

    def update_toggle(self):
        self.button.blockSignals(True)
        self.button.setChecked(self.audio_expanded)
        self.button.setText("▾ Audio tools" if self.audio_expanded else "▸ Audio tools")
        self.button.blockSignals(False)
