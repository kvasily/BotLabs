"""Offline regression tests. Synthetic assets/fake input only; never touch the game."""
from __future__ import annotations

import copy
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
import soundfile as sf

from audio.detectors import AudioSnapshot, Debounce, DetectorBank, SoundEvent, load_wav, normalized_correlation
from config import ConfigError, Rect, Settings, load_config
from fsm.states import AssistantFSM, State, choose_task, fight_buttons
from input.human_mouse import HumanMouse, heading_error, minimum_jerk
from input.keys import InputController, Interrupted, SafetyGate, Win32Backend
from vision.compass import yaw_from_north
from vision.compass import Compass, CompassLost
from vision.perception import Perception, Progress
from vision.templates import Match, TemplateStore

ROOT = Path(__file__).resolve().parents[1]


def settings() -> Settings:
    cfg = load_config(ROOT / "config.yaml")
    data = copy.deepcopy(cfg.data)
    data["counts_per_degree"] = 10
    data["audio_device_name"] = "Test endpoint"
    data["ui"]["cursor_toggle_key"] = None
    data["compass"]["center"] = [50, 50]
    data["compass"]["ring_radius"] = 40
    for name in data["rois"]:
        data["rois"][name] = [100, 100, 200, 200]
    for task in data["tasks"].values():
        task["hook_inventory_rect"] = [100, 100, 30, 30]
        task["bait_inventory_rect"] = [150, 100, 30, 30]
    data["logout"]["sequence"] = [{"key": "esc", "wait_s": 0.1}]
    data["logout"]["success_roi"] = [0, 0, 200, 200]
    return Settings(cfg.path, data)


class FakeBackend:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.title = "Mortal Online 2"

    def mouse(self, flags: int, dx: int = 0, dy: int = 0) -> None:
        self.events.append(("mouse", flags, dx, dy))

    def key(self, name: str, down: bool) -> None:
        self.events.append(("key", name, down))

    def cursor(self) -> tuple[int, int]:
        return 0, 0

    def foreground_title(self) -> str:
        return self.title

    def desktop(self) -> tuple[int, int, int, int]:
        return 0, 0, 3840, 2160


def machine() -> tuple[AssistantFSM, FakeBackend]:
    cfg = settings()
    gate = SafetyGate()
    backend = FakeBackend()
    io = InputController(gate, backend, cfg["window_title_contains"])
    mouse = HumanMouse(io, 10, random.Random(42))
    screen, vision, compass, audio = Mock(), Mock(), Mock(), Mock()
    screen.screen_point.side_effect = lambda point: point
    vision.bait_empty.return_value = False
    audio.snapshot.side_effect = lambda: AudioSnapshot(time.monotonic(), 0, {}, False, 0, ())
    fsm = AssistantFSM(cfg, gate, io, mouse, screen, vision, compass, audio)
    fsm.task = "bassle"
    gate.resume()
    gate.sleep = lambda seconds: gate.check()  # type: ignore[method-assign]
    return fsm, backend


class InputTests(unittest.TestCase):
    def test_pause_resume_cannot_reanimate_old_lease(self) -> None:
        gate, backend = SafetyGate(), FakeBackend()
        io = InputController(gate, backend, "Mortal Online 2")
        gate.resume()
        with gate.session():
            io.button("left", True)
            io.key("e", True)
            gate.pause("PANIC")
            gate.resume()
            with self.assertRaises(Interrupted):
                io.button("right", True)
        self.assertIn(("mouse", 4, 0, 0), backend.events)
        self.assertIn(("key", "e", False), backend.events)
        self.assertNotIn(("mouse", 8, 0, 0), backend.events)

    def test_focus_loss_releases_held_buttons(self) -> None:
        gate, backend = SafetyGate(), FakeBackend()
        io = InputController(gate, backend, "Mortal Online 2")
        gate.resume()
        with gate.session():
            io.button("right", True)
            backend.title = "Other window"
            with self.assertRaises(Interrupted):
                io.relative(10)
        self.assertFalse(io.buttons)
        self.assertFalse(gate.active)
        self.assertNotIn(("mouse", 1, 10, 0), backend.events)

    def test_relative_camera_path_preserves_exact_counts(self) -> None:
        fsm, backend = machine()
        with fsm.gate.session():
            fsm.mouse.yaw(-27.3)
        moves = [event for event in backend.events if event[0] == "mouse"]
        self.assertEqual(sum(e[2] for e in moves), -273)
        self.assertTrue(all(e[1] == 1 and e[3] == 0 for e in moves))

    def test_minimum_jerk_endpoints_and_uniform_water_samples(self) -> None:
        self.assertEqual(minimum_jerk(0), 0)
        self.assertEqual(minimum_jerk(1), 1)
        fsm, _ = machine()
        samples = [fsm.mouse.water_heading() for _ in range(2000)]
        self.assertTrue(all(270 <= a <= 360 for a in samples))
        self.assertTrue(all(a != b for a, b in zip(samples, samples[1:])))
        self.assertAlmostEqual(float(np.mean(samples)), 315, delta=2)

    def test_win32_structure_layout_without_sending_input(self) -> None:
        import ctypes
        backend = Win32Backend()
        self.assertEqual(ctypes.sizeof(backend.Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


class AudioTests(unittest.TestCase):
    def test_ncc_gain_dc_offset_and_alignment(self) -> None:
        rng = np.random.default_rng(1)
        template = rng.normal(size=100).astype(np.float32)
        signal = rng.normal(scale=0.001, size=500).astype(np.float32)
        signal[173:273] = template * 0.3 + 0.2
        scores = normalized_correlation(signal, template)
        self.assertEqual(int(scores.argmax()), 173)
        self.assertGreater(scores[173], 0.999)
        self.assertFalse(np.isnan(normalized_correlation(np.zeros(200, dtype=np.float32), template)).any())

    def test_tension_debounce(self) -> None:
        detector = Debounce(0.12, 0.12)
        self.assertFalse(detector.update(True, 1))
        self.assertFalse(detector.update(False, 1.05))
        self.assertFalse(detector.update(True, 1.08))
        self.assertTrue(detector.update(True, 1.21))
        self.assertTrue(detector.update(False, 1.24))
        self.assertFalse(detector.update(False, 1.37))

    def test_missing_silent_and_wrong_rate_wavs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.wav"
            with self.assertRaisesRegex(ConfigError, "missing.wav"):
                load_wav(path)
            sf.write(path, np.zeros(2000), 48000)
            with self.assertRaisesRegex(ConfigError, "silent"):
                load_wav(path)
            sf.write(path, np.ones(2000), 44100)
            with self.assertRaisesRegex(ConfigError, "44100"):
                load_wav(path)

    def test_old_ring_splash_is_not_a_new_event_and_bubbles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = settings()
            cfg = Settings(Path(directory) / "config.yaml", cfg.data)
            rng = np.random.default_rng(3)
            clips = {}
            for name in ("bubble", "splash", "tension", "quest_complete"):
                clips[name] = rng.normal(0, 0.1, 1920).astype(np.float32)
                path = Path(directory) / f"{name}.wav"
                sf.write(path, clips[name], 48000, subtype="FLOAT")
                cfg["audio"]["templates"][name] = str(path)
            bank = DetectorBank(cfg)
            ring = np.concatenate((np.zeros(3000, dtype=np.float32), clips["splash"]))
            snap = bank.process(ring, ring[-1440:], 10)
            self.assertEqual([e.name for e in snap.events], ["splash"])
            old = np.concatenate((ring, np.zeros(3000, dtype=np.float32)))
            snap = bank.process(old, old[-1440:], 12)
            self.assertEqual(len(snap.events), 1)
            bank.templates["splash"] = bank.templates["bubble"]
            ring = np.concatenate((np.zeros(3000, dtype=np.float32), clips["bubble"]))
            snap = bank.process(ring, ring[-1440:], 15)
            self.assertEqual(sum(e.name == "splash" for e in snap.events), 1)
            self.assertTrue(any(e.name == "bubble" for e in snap.events))

    def test_tension_releases_while_old_match_remains_in_ring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = Settings(Path(directory) / "config.yaml", settings().data)
            rng = np.random.default_rng(9)
            for name in ("bubble", "splash", "tension", "quest_complete"):
                path = Path(directory) / f"{name}.wav"
                sf.write(path, rng.normal(0, 0.1, 1920), 48000, subtype="FLOAT")
                cfg["audio"]["templates"][name] = str(path)
            bank = DetectorBank(cfg)
            clip = bank.templates["tension"][0]
            ring = np.empty(0, dtype=np.float32)
            now = 1.0
            for _ in range(8):
                ring = np.concatenate((ring, clip))[-48000:]
                now += 0.04
                snap = bank.process(ring, clip, now)
            self.assertTrue(snap.tension)
            for _ in range(10):
                hop = np.zeros(1440, dtype=np.float32)
                ring = np.concatenate((ring, hop))[-48000:]
                now += 0.03
                snap = bank.process(ring, hop, now)
            self.assertFalse(snap.tension)
            self.assertTrue(np.any(ring != 0))


class VisionConfigTests(unittest.TestCase):
    def test_compass_cardinals_and_wrap(self) -> None:
        for point, heading in [((50, 10), 0), ((10, 50), 90), ((50, 90), 180), ((90, 50), 270)]:
            self.assertAlmostEqual(yaw_from_north(*point, (50, 50)), heading)
        self.assertEqual(heading_error(1, 359), 2)
        self.assertEqual(heading_error(359, 1), -2)

    def test_rect_boundaries(self) -> None:
        self.assertEqual(Rect.parse([0, 0, 3840, 2160], "full", (3840, 2160)).center, (1920, 1080))
        for rect in ([3800, 0, 50, 20], [-1, 0, 20, 20], [0, 0, 0, 20], "TODO"):
            with self.assertRaises(ConfigError):
                Rect.parse(rect, "test", (3840, 2160))

    def test_default_config_names_todos_and_missing_assets(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(ROOT / "config.yaml").validate()
        message = str(caught.exception)
        self.assertIn("counts_per_degree", message)
        self.assertIn("splash.wav", message)
        self.assertIn("hook_bassle.png", message)
        settings().validate(assets=False)

    def test_required_template_cannot_be_disabled(self) -> None:
        cfg = settings()
        cfg["templates"]["taskmaster"] = None
        with self.assertRaisesRegex(ConfigError, "templates.taskmaster"):
            cfg.validate(assets=False)

    def test_ticks_can_leave_unused_north_settings_unset(self) -> None:
        cfg = settings()
        cfg["compass"].update(mode="ticks", center="TODO", ring_radius="TODO", tick_top_rect=[0, 0, 100, 50],
                               tick_templates=[{"heading": h, "path": f"tick_{h}.png"} for h in (0, 90, 180, 270)])
        cfg.validate(assets=False)

    def test_ambiguous_identical_ticks_are_rejected(self) -> None:
        cfg = settings()
        cfg["compass"].update(mode="ticks", tick_top_rect=[0, 0, 100, 50],
                               tick_templates=[{"heading": h, "path": f"tick_{h}.png"} for h in (0, 90, 180, 270)])
        screen, store = Mock(), Mock()
        screen.grab.return_value = np.zeros((200, 200, 3), dtype=np.uint8)
        store.match.return_value = Match(0.99, 0, 0, 10, 10)
        with self.assertRaisesRegex(CompassLost, "ambiguous"):
            Compass(cfg, screen, store).read()

    def test_complete_synthetic_project_preflight(self) -> None:
        from main import check_assets
        with tempfile.TemporaryDirectory() as directory:
            cfg = Settings(Path(directory) / "config.yaml", settings().data)
            rng = np.random.default_rng(5)
            for path in cfg.required_assets():
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == ".wav":
                    sf.write(path, rng.normal(0, 0.1, 2400), 48000)
                else:
                    cv2.imencode(".png", rng.integers(0, 255, (12, 15), dtype=np.uint8))[1].tofile(path)
            # No hotkeys, screen capture, audio devices, or SendInput are involved.
            check_assets(cfg)

    def test_real_template_matching_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = Settings(Path(directory) / "config.yaml", settings().data)
            store = TemplateStore(cfg)
            rng = np.random.default_rng(4)
            template = rng.integers(0, 255, (12, 15), dtype=np.uint8)
            cv2.imencode(".png", template)[1].tofile(Path(directory) / "pattern.png")
            image = np.zeros((80, 90), dtype=np.uint8)
            image[31:43, 22:37] = template
            match = store.match(image, "pattern.png")
            self.assertEqual((match.x, match.y), (22, 31))
            self.assertGreater(match.score, 0.99)
            with self.assertRaisesRegex(ConfigError, "absent.png"):
                store.load("absent.png")


class FSMTests(unittest.TestCase):
    def test_bassle_always_has_priority(self) -> None:
        self.assertEqual(choose_task({"bassle", "redline_torp"}), "bassle")
        self.assertEqual(choose_task({"bassle"}), "bassle")
        self.assertEqual(choose_task({"redline_torp"}), "redline_torp")
        with self.assertRaises(RuntimeError):
            choose_task(set())

    def test_fight_outputs_and_timeout(self) -> None:
        for task, tension, expected in [("bassle", False, (False, True)), ("bassle", True, (False, True)),
                                         ("redline_torp", False, (True, False)), ("redline_torp", True, (True, True))]:
            self.assertEqual(fight_buttons(task, tension), expected)
            fsm, _ = machine()
            fsm.task, fsm.fight_time = task, time.monotonic()
            fsm.vision.match.return_value = None
            fsm.audio.snapshot.side_effect = lambda: AudioSnapshot(time.monotonic(), 0, {}, tension, 0, ())
            with fsm.gate.session():
                fsm.fight()
                self.assertEqual(("left" in fsm.io.buttons, "right" in fsm.io.buttons), expected)
                fsm.fight_time -= 1000
                fsm.fight()
            self.assertEqual(fsm.state, State.FACE_WATER)
            self.assertEqual(fsm.count, 0)
            self.assertFalse(fsm.io.buttons)

    def test_bubble_and_cast_whoosh_do_not_hook(self) -> None:
        fsm, backend = machine()
        fsm.cast_time = time.monotonic() - 5
        fsm.look_at = time.monotonic() + 30
        events = (SoundEvent("bubble", time.monotonic(), 1), SoundEvent("splash", fsm.cast_time + 1, 1))
        fsm.audio.snapshot.side_effect = lambda: AudioSnapshot(time.monotonic(), 0, {}, False, 0, events)
        fsm.state = State.WAIT_BITE
        with fsm.gate.session():
            fsm.wait_bite()
        self.assertEqual(fsm.state, State.WAIT_BITE)
        self.assertNotIn(("mouse", 8, 0, 0), backend.events)

    def test_new_splash_enters_selected_fight(self) -> None:
        fsm, _ = machine()
        fsm.cast_time = time.monotonic() - 5
        fsm.vision.match.return_value = None
        events = (SoundEvent("splash", time.monotonic(), 1),)
        fsm.audio.snapshot.side_effect = lambda: AudioSnapshot(time.monotonic(), 0, {}, False, 0, events)
        with fsm.gate.session():
            fsm.wait_bite()
        self.assertEqual(fsm.state, State.FIGHT_BASSLE)
        self.assertEqual(fsm.io.buttons, {"right"})

    def test_static_catch_indicator_is_not_a_new_catch(self) -> None:
        fsm, _ = machine()
        fsm.state, fsm.fight_time = State.FIGHT_BASSLE, time.monotonic()
        fsm.catch_armed = False
        fsm.vision.match.return_value = Match(1, 0, 0, 10, 10)
        with fsm.gate.session():
            fsm.fight()
            self.assertEqual(fsm.state, State.FIGHT_BASSLE)
            fsm.vision.match.return_value = None
            fsm.fight()
            fsm.vision.match.return_value = Match(1, 0, 0, 10, 10)
            fsm.fight()
        self.assertEqual(fsm.state, State.RESOLVE_CATCH)
        self.assertFalse(fsm.io.buttons)

    def test_generic_catch_does_not_increment_selected_count(self) -> None:
        fsm, _ = machine()
        fsm.read_progress = Mock(return_value=Progress("bassle", 0))
        with fsm.gate.session():
            fsm.resolve_catch()
        self.assertEqual(fsm.count, 0)
        self.assertEqual(fsm.state, State.FACE_WATER)

    def test_verified_three_catches_turn_in_then_read_list(self) -> None:
        fsm, _ = machine()
        fsm.count = 2
        fsm.read_progress = Mock(return_value=Progress("bassle", 3))
        fsm.until = Mock(side_effect=lambda predicate, timeout, stable=1: predicate() is not False)
        fsm.mouse.click = Mock()
        with fsm.gate.session():
            fsm.resolve_catch()
            self.assertEqual(fsm.state, State.FIND_TASKMASTER)
            self.assertTrue(fsm.turning_in)
            fsm.until = Mock(return_value=True)
            fsm.turn_in()
        self.assertEqual(fsm.state, State.READ_QUESTS)
        self.assertIsNone(fsm.task)
        self.assertEqual(fsm.count, 0)

    def test_wrong_quest_paper_pauses(self) -> None:
        fsm, _ = machine()
        fsm.mouse.move_gui = Mock()
        fsm.vision.progress.return_value = Progress("redline_torp", 1)
        with fsm.gate.session(), self.assertRaises(Interrupted):
            fsm.read_progress()
        self.assertFalse(fsm.gate.active)
        self.assertEqual(fsm.count, 0)

    def test_empty_bait_routes_to_logout_without_cast(self) -> None:
        fsm, backend = machine()
        fsm.until = Mock(return_value=True)
        with fsm.gate.session():
            fsm.cast()
        self.assertEqual(fsm.state, State.LOGOUT)
        self.assertNotIn(("mouse", 2, 0, 0), backend.events)

    def test_recovery_never_walks_by_default(self) -> None:
        fsm, backend = machine()
        fsm.scan_taskmaster = Mock(return_value=False)
        with fsm.gate.session(), self.assertRaises(Interrupted):
            fsm.recover()
        self.assertFalse(any(e[0] == "key" and e[2] for e in backend.events))

    def test_enabled_recovery_walks_only_after_failed_scan(self) -> None:
        fsm, backend = machine()
        fsm.cfg.data["enable_wasd_recovery"] = True
        fsm.scan_taskmaster = Mock(return_value=False)
        fsm.face = Mock()
        with fsm.gate.session():
            fsm.recover()
        self.assertEqual(fsm.state, State.FACE_WATER)
        self.assertIn(("key", "s", True), backend.events)
        self.assertIn(("key", "s", False), backend.events)

    def test_stale_audio_rejects_observation(self) -> None:
        fsm, _ = machine()
        fsm.audio.snapshot.side_effect = None
        fsm.audio.snapshot.return_value = AudioSnapshot(0, 0, {}, False, 0, ())
        with self.assertRaisesRegex(RuntimeError, "stalled"):
            fsm.poll_audio()

    def test_missing_landing_marker_retries_then_recovers(self) -> None:
        fsm, _ = machine()
        fsm.vision.equipped.return_value = True
        fsm.vision.match.return_value = None
        fsm.face = Mock()
        fsm.until = Mock(side_effect=[False, True, False])
        with fsm.gate.session():
            fsm.cast()
        self.assertEqual(fsm.cast_failures, 1)
        self.assertEqual(fsm.state, State.FACE_WATER)
        self.assertFalse(fsm.io.buttons)
        fsm.cast_failures = 5
        fsm.until = Mock(side_effect=[False, True, False])
        with fsm.gate.session():
            fsm.cast()
        self.assertEqual(fsm.state, State.RECOVER)

    def test_missing_bait_source_logs_out(self) -> None:
        fsm, _ = machine()
        fsm.vision.equipped.side_effect = lambda task, kind: kind == "hook"
        fsm.until = Mock(return_value=False)
        with fsm.gate.session():
            fsm.prep_gear()
        self.assertEqual(fsm.state, State.LOGOUT)

    def test_logout_requires_confirmation_and_stops(self) -> None:
        for confirmed in (True, False):
            fsm, backend = machine()
            fsm.until = Mock(return_value=confirmed)
            fsm.state = State.LOGOUT
            with fsm.gate.session():
                fsm.logout()
            self.assertTrue(fsm.gate.shutdown.is_set())
            self.assertFalse(fsm.gate.active)
            self.assertFalse(fsm.io.buttons)
            self.assertEqual(fsm.state, State.LOGOUT if confirmed else State.FATAL)
            self.assertIn(("key", "esc", False), backend.events)

    def test_quest_complete_sound_still_requires_paper_progress(self) -> None:
        fsm, _ = machine()
        fsm.cast_time = time.monotonic() - 5
        fsm.look_at = time.monotonic() + 30
        events = (SoundEvent("quest_complete", time.monotonic(), 1),)
        fsm.audio.snapshot.side_effect = lambda: AudioSnapshot(time.monotonic(), 0, {}, False, 0, events)
        fsm.read_progress = Mock(return_value=Progress("bassle", 1))
        with fsm.gate.session():
            fsm.wait_bite()
            self.assertEqual(fsm.state, State.RESOLVE_CATCH)
            fsm.resolve_catch()
        self.assertEqual(fsm.count, 1)
        self.assertEqual(fsm.state, State.FACE_WATER)

    def test_selection_rechecks_bassle_before_click(self) -> None:
        fsm, _ = machine()
        fsm.vision.quests.return_value = {"redline_torp": Match(1, 0, 40, 100, 10), "bassle": Match(1, 0, 10, 100, 10)}
        fsm.vision.match.return_value = None
        fsm.mouse.click = Mock()
        fsm.until = Mock(return_value=True)
        fsm.read_progress = Mock(return_value=Progress("bassle", 0))
        with fsm.gate.session():
            fsm.select_task()
        self.assertEqual(fsm.task, "bassle")
        self.assertEqual(fsm.mouse.click.call_args_list[0].args[0], (150, 115))
        self.assertEqual(fsm.state, State.PREP_GEAR)

    def test_exhausted_bait_after_catch_precedes_turn_in(self) -> None:
        fsm, _ = machine()
        fsm.read_progress = Mock(return_value=Progress("bassle", 3))
        fsm.vision.bait_empty.return_value = True
        with fsm.gate.session():
            fsm.resolve_catch()
        self.assertEqual(fsm.state, State.LOGOUT)


if __name__ == "__main__":
    unittest.main()
