"""Multiple takes, legacy profiles, waveform clock and clip browser regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import yaml
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from audio.detectors import DetectorBank
from gui.audio_clips import AudioClips
from gui.profiles import fingerprint
from gui.teacher_document import TeacherDocument
from gui.video_teacher import VideoTeacher, Waveform, export_audio, waveform_playhead_x, waveform_seek_ms
from test_coordmap import ImmediateJobs
from test_gui import APP, profile_at


class MultipleAudioTests(unittest.TestCase):
    def test_middle_waveform_click_seeks_offset_time_without_changing_trim(self):
        self.assertEqual(waveform_seek_ms(100, 400, 2, 10), 10500)
        self.assertEqual(waveform_seek_ms(-10, 400, 2, 10), 10000)
        self.assertEqual(waveform_seek_ms(500, 400, 2, 10), 12000)
        self.assertIsNone(waveform_seek_ms(100, 400, 0, 10))
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            try:
                teacher.wave.resize(400, 100)
                with patch.object(teacher.source, "seek") as seek:
                    QTest.mouseClick(teacher.wave, Qt.MouseButton.MiddleButton, pos=QPoint(100, 50))
                    seek.assert_not_called()
                    teacher.audio_offset = 10
                    teacher.on_audio(np.zeros(96000, np.float32))
                    teacher.wave.in_s, teacher.wave.out_s = .2, .4
                    teacher.crop_frozen = True
                    QTest.mouseClick(teacher.wave, Qt.MouseButton.MiddleButton, pos=QPoint(100, 50))
                    seek.assert_called_once_with(10500)
                    self.assertEqual((teacher.wave.in_s, teacher.wave.out_s), (.2, .4))
                    self.assertEqual(teacher.wave.video_ms, 10500)
                    self.assertIsNone(teacher.wave.drag_start)
                    self.assertIsNone(teacher.canvas.start_point)
                    self.assertFalse(teacher.crop_frozen)
                    QTest.mousePress(teacher.wave, Qt.MouseButton.LeftButton, pos=QPoint(120, 50))
                    QTest.mouseRelease(teacher.wave, Qt.MouseButton.LeftButton, pos=QPoint(200, 50))
                    self.assertEqual((teacher.wave.in_s, teacher.wave.out_s), (.6, 1.0))
                    seek.assert_called_once()
            finally:
                teacher.close()

    def test_two_exports_are_numbered_mono_files_and_yaml_list(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            samples = np.random.default_rng(5).normal(0, .1, 4800).astype(np.float32)
            first, _ = export_audio(profile, samples, 0, .1, "splash")
            original = first.read_bytes()
            second, _ = export_audio(profile, samples[::-1], 0, .1, "splash")
            data = yaml.safe_load(profile.path.read_text(encoding="utf-8"))
            self.assertEqual(data["audio"]["templates"]["splash"], ["sfx/splash/01.wav", "sfx/splash/02.wav"])
            self.assertEqual(first.read_bytes(), original)
            self.assertNotEqual(first, second)
            for path in (first, second):
                info = sf.info(str(path))
                self.assertEqual((info.samplerate, info.channels), (48000, 1))
                self.assertAlmostEqual(info.duration, .1)
                self.assertIn(path, profile.settings.required_assets())

    def test_existing_single_wav_survives_export_clone_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            store, profile = profile_at(directory)
            legacy = profile.asset("sfx", "splash.wav")
            samples = np.random.default_rng(7).normal(0, .1, 1920).astype(np.float32)
            sf.write(legacy, samples, 48000)
            original = legacy.read_bytes()
            profile.patch("audio.templates", {"splash": "sfx/splash.wav"})
            bank = DetectorBank(profile.settings)
            self.assertEqual(len(bank.templates["splash"]), 1)
            bank.process(samples, samples[-1440:], 10)
            self.assertEqual([event.name for event in bank.events], ["splash"])
            new, _ = export_audio(profile, samples, 0, .04, "splash")
            self.assertEqual(profile.data["audio"]["templates"]["splash"], ["sfx/splash.wav", "sfx/splash/01.wav"])
            clone = store.clone(profile.path, "Cloned audio")
            for relative in clone.data["audio"]["templates"]["splash"]:
                self.assertEqual((clone.path.parent / relative).read_bytes(), (profile.path.parent / relative).read_bytes())
            self.assertEqual(legacy.read_bytes(), original)
            document = TeacherDocument(profile)
            document.replace("compass", np.zeros((1440, 2560, 3), np.uint8), (10, 20, 100, 50))
            document.save()
            self.assertEqual(profile.data["audio"]["templates"]["splash"], ["sfx/splash.wav", "sfx/splash/01.wav"])
            self.assertTrue(new.is_file())
            before = fingerprint(profile.path)
            sf.write(new, samples * .5, 48000)
            self.assertNotEqual(before, fingerprint(profile.path))

    def test_detector_uses_max_take_score_and_emits_one_role_event(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            rng = np.random.default_rng(12)
            clips = [rng.normal(0, .1, length).astype(np.float32) for length in (1920, 2400)]
            for clip in clips:
                export_audio(profile, clip, 0, len(clip) / 48000, "splash")
            paths = profile.data["audio"]["templates"]["splash"]
            profile.patch("audio.templates", {"splash": paths})
            for clip in clips:
                bank = DetectorBank(profile.settings)
                ring = np.concatenate((np.zeros(3000, np.float32), clip))
                result = bank.process(ring, ring[-1440:], 10)
                self.assertGreater(result.scores["splash"], .99)
                self.assertEqual([event.name for event in result.events], ["splash"])
                self.assertAlmostEqual(result.events[0].timestamp, 10)

    def test_export_failure_removes_new_file_and_keeps_previous_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            before = profile.path.read_bytes()
            with patch.object(profile, "patch", side_effect=OSError("disk failed")):
                with self.assertRaisesRegex(OSError, "disk failed"):
                    export_audio(profile, np.random.default_rng(1).normal(0, .1, 1920), 0, .04, "splash")
            self.assertEqual(profile.path.read_bytes(), before)
            self.assertFalse(list((profile.path.parent / "sfx" / "splash").glob("*.wav")))

    def test_waveform_playhead_tracks_offset_without_changing_selection(self):
        wave = Waveform()
        wave.resize(401, 100)
        try:
            self.assertIsNone(wave.playhead_x())
            wave.set_samples(np.zeros(96000, np.float32))
            wave.in_s, wave.out_s = .2, .4
            wave.set_playhead(11500, 10)
            self.assertEqual(wave.playhead_x(), 300)
            self.assertEqual((wave.in_s, wave.out_s), (.2, .4))
            self.assertIsNone(waveform_playhead_x(9000, 10, 2, 401))
            self.assertIsNone(waveform_playhead_x(13000, 10, 2, 401))
            self.assertEqual(waveform_playhead_x(1000, 0, 2, 401), 200)
        finally:
            wave.close()

    def test_teacher_playback_scrub_and_export_status_use_file_clock_and_full_path(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            teacher = VideoTeacher(profile, ImmediateJobs())
            messages = []
            teacher.message.connect(messages.append)
            try:
                teacher.audio_offset = 10
                teacher.on_audio(np.random.default_rng(2).normal(0, .1, 96000).astype(np.float32))
                teacher.wave.in_s, teacher.wave.out_s = .2, .4
                teacher.on_duration(20000)
                teacher.on_position(11500)
                self.assertEqual(teacher.wave.video_ms, 11500)
                self.assertEqual(teacher.wave.audio_offset, 10)
                with patch.object(teacher.source, "seek"):
                    teacher.begin_scrub()
                    teacher.scrub_to(11750)
                self.assertEqual(teacher.wave.video_ms, 11750)
                self.assertEqual((teacher.wave.in_s, teacher.wave.out_s), (.2, .4))
                teacher.audio_role.setCurrentText("splash")
                samples = np.random.default_rng(3).normal(0, .1, 1920).astype(np.float32)
                with patch.object(teacher, "audio_selection", return_value=lambda: samples):
                    teacher.save_audio()
                self.assertEqual(messages[-1], f"Saved splash → {profile.path.parent / 'sfx' / 'splash' / '01.wav'}")
            finally:
                teacher.document.load(profile)
                teacher.close()

    def test_clip_browser_groups_rows_and_plays_selected_full_path_through_qt(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = profile_at(directory)
            samples = np.random.default_rng(4).normal(0, .1, 1920).astype(np.float32)
            paths = [export_audio(profile, samples, 0, .04, "splash")[0] for _ in range(2)]
            browser = AudioClips()
            try:
                browser.refresh(profile)
                group = next(browser.tree.topLevelItem(i) for i in range(browser.tree.topLevelItemCount())
                             if browser.tree.topLevelItem(i).text(0) == "splash")
                self.assertEqual(group.childCount(), 2)
                row = group.child(1)
                self.assertEqual(row.text(2), "0.040 s")
                controls = browser.tree.itemWidget(row, 4).findChildren(QPushButton)
                with patch.object(browser.player, "play") as play:
                    next(button for button in controls if button.text() == "Play").click()
                    play.assert_called_once()
                self.assertEqual(Path(browser.player.source().toLocalFile()), paths[1])
                self.assertEqual(browser.path_label.text(), str(paths[1]))
                next(button for button in controls if button.text() == "Stop").click()
                self.assertTrue(browser.player.source().isEmpty())
            finally:
                browser.stop()
                browser.close()
