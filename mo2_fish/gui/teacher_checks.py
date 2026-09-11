"""Packaged playback verification with temporary H.264/AAC media, no game input."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtMultimedia import QAudioBufferOutput


def check_playback(teacher, app, directory: Path, destination: Path | None = None):
    import imageio_ffmpeg
    path = directory / "teacher-clock-aac.mp4"
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=size=2560x1440:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
               "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-threads", "2",
               "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", str(path)]
    result = subprocess.run(command, capture_output=True, timeout=45,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    source = teacher.source
    buffers = QAudioBufferOutput(source)
    source.player.setAudioBufferOutput(buffers)
    decoded = {"audio": 0, "frames": 0, "frame_ms": 0}
    def audio(buffer):
        if buffer.isValid() and any(buffer.constData()):
            decoded["audio"] += buffer.sampleCount()
    def frame(value):
        if value.isValid():
            decoded["frames"] += 1
            decoded["frame_ms"] = value.startTime()/1000
    buffers.audioBufferReceived.connect(audio)
    source.frame.connect(frame)
    errors = []
    source.failed.connect(errors.append)
    def pump_until(test, seconds=6):
        deadline = time.monotonic() + seconds
        while not test() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert test(), f"Playback timeout: {errors}"
    volume = source.audio.volume()
    try:
        source.audio.setVolume(0.03)  # Short quiet diagnostic tone; output is unmuted.
        teacher.open_file(path)
        pump_until(lambda: source.player.duration() >= 5900)
        pump_until(lambda: teacher.canvas.image is not None)
        assert source.player.playbackRate() == 1.0
        assert not source.audio.isMuted()
        assert not source.audio.device().isNull(), "No playback audio output device"
        source.player.play()
        pump_until(lambda: source.player.position() >= 300 and decoded["audio"] > 0 and decoded["frames"] > 0)
        started, initial = time.monotonic(), source.player.position()
        pump_until(lambda: time.monotonic()-started >= 1.5, 3)
        elapsed = time.monotonic()-started
        ratio = (source.player.position()-initial)/1000/elapsed
        assert 0.7 <= ratio <= 1.3, f"Default clock ran at {ratio:.2f}× instead of 1.0×"
        assert decoded["frames"] >= 8 and decoded["audio"] > 48000
        assert teacher.canvas.image.width() == 2560 and teacher.canvas.image.height() == 1440
        source.player.setPlaybackRate(1.5)
        teacher.begin_scrub()
        teacher.timeline.setValue(3000)
        teacher.scrub_to(3000)
        started = time.monotonic()
        pump_until(lambda: time.monotonic()-started >= 0.25, 1)
        assert teacher.timeline.value() == 3000, "Player updates bounced the drag thumb"
        assert decoded["frame_ms"] >= 2900, "Seek did not update the displayed crop frame"
        teacher.end_scrub()
        assert source.player.playbackRate() == 1.5 and source.playing
        pump_until(lambda: teacher.timeline.value() >= 3200, 2)
        source.pause()
        if destination:
            host = teacher.window()
            if hasattr(host, "tabs"):
                host.tabs.setCurrentWidget(teacher)
            app.processEvents()
            host.grab().save(str(destination / "MO2Fish-synchronized-player.png"))
        return f"PASS: Qt FFmpeg H.264/AAC 2560x1440 playback {ratio:.2f}×, {decoded['audio']} non-silent decoded audio samples; live drag stable; seek retains 1.5×"
    finally:
        source.close()
        source.audio.setVolume(volume)
        source.player.setPlaybackRate(1.0)
        source.player.setAudioBufferOutput(None)
        source.frame.disconnect(frame)
        source.failed.disconnect(errors.append)
        teacher.video_path = teacher.audio_path = None
        teacher.document.source_size = teacher.profile.data.get("video_source_resolution")
