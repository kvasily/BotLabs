"""Initialize WASAPI's COM apartment for each GUI audio worker, then wrap core audio."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as ct
import sys
from typing import Iterator

from audio.loopback import LoopbackAudio


@contextmanager
def audio_apartment() -> Iterator[None]:
    # SoundCard initializes COM on its first import thread. Import it BEFORE our
    # own balanced CoInitializeEx call; later worker threads still need their own
    # apartment. This avoids both CO_E_NOTINITIALIZED and SoundCard's S_FALSE path.
    import soundcard
    if sys.platform != "win32":
        yield
        return
    ole32 = ct.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ct.c_void_p, ct.c_uint]
    ole32.CoInitializeEx.restype = ct.c_long
    result = ole32.CoInitializeEx(None, 0)
    if result not in (0, 1, -2147417850):  # S_OK, S_FALSE, RPC_E_CHANGED_MODE
        raise RuntimeError(f"Audio COM initialization failed: {result:#x}")
    try:
        yield
    finally:
        if result in (0, 1):
            ole32.CoUninitialize()


class GuiLoopbackAudio(LoopbackAudio):
    """Only the thread entry is wrapped; capture and detectors are unchanged."""
    def _run(self) -> None:
        try:
            with audio_apartment():
                super()._run()
        except Exception as exc:
            self.error = exc
            self.ready.set()
