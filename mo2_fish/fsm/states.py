"""Single-worker FSM. Every blocking action is cancellable through SafetyGate."""
from __future__ import annotations

from contextlib import contextmanager
from enum import Enum, auto
import logging
import random
import time
from typing import Callable, Iterator

from audio.detectors import AudioSnapshot
from audio.loopback import LoopbackAudio
from config import Rect, Settings
from input.human_mouse import HumanMouse, heading_error
from input.keys import InputController, Interrupted, SafetyGate
from vision.capture import ScreenCapture
from vision.compass import Compass, CompassLost
from vision.perception import Perception, Progress


class State(Enum):
    BOOT = auto()
    FIND_TASKMASTER = auto()
    READ_QUESTS = auto()
    SELECT_TASK = auto()
    PREP_GEAR = auto()
    FACE_WATER = auto()
    CAST = auto()
    WAIT_BITE = auto()
    FIGHT_BASSLE = auto()
    FIGHT_REDLINE = auto()
    RESOLVE_CATCH = auto()
    TURN_IN = auto()
    RECOVER = auto()
    PAUSE = auto()
    PANIC = auto()
    LOGOUT = auto()
    FATAL = auto()


def choose_task(available: set[str]) -> str:
    if "bassle" in available:
        return "bassle"
    if "redline_torp" in available:
        return "redline_torp"
    raise RuntimeError("Neither Bassle nor Redline Torp was recognized; check the quest-list ROI/templates")


def fight_buttons(task: str, tension: bool) -> tuple[bool, bool]:
    if task == "bassle":
        return False, True
    if task == "redline_torp":
        return True, tension
    raise ValueError(f"Unknown task: {task}")


class AssistantFSM:
    def __init__(self, cfg: Settings, gate: SafetyGate, io: InputController, mouse: HumanMouse,
                 screen: ScreenCapture, vision: Perception, compass: Compass, audio: LoopbackAudio) -> None:
        self.cfg, self.gate, self.io, self.mouse = cfg, gate, io, mouse
        self.screen, self.vision, self.compass, self.audio = screen, vision, compass, audio
        self.state = State.BOOT
        self.task: str | None = None
        self.count = 0                     # Only tooltip-verified selected-fish count.
        self.pending = 0                   # Generic catches since last tooltip verification.
        self.without_progress = 0
        self.turning_in = False
        self.cast_failures = 0
        self.wasd_attempts = 0
        self.cast_time = 0.0
        self.fight_time = 0.0
        self.look_at = 0.0
        self.force_progress = False
        self.cast_heading = 0.0
        self.catch_armed = False
        self.cursor_open = False
        self.epoch_seen = -1
        self.rng = random.Random()
        self.last_audio = AudioSnapshot(0, 0, {}, False, 0, ())
        self.message = "Close dialogs, face the game, then F8. F9 panic; F10 exit."

    def transition(self, state: State, reason: str = "") -> None:
        logging.info("%s -> %s task=%s verified=%d pending=%d %s",
                     self.state.name, state.name, self.task, self.count, self.pending, reason)
        self.state, self.message = state, reason

    def pause(self, reason: str) -> None:
        self.transition(State.PAUSE, reason)
        self.gate.pause(f"PAUSE: {reason}. Close dialogs, cancel any line, restore look mode before F8.")
        try:
            import winsound
            winsound.MessageBeep()
        except (ImportError, RuntimeError):
            pass
        raise Interrupted(reason)

    def duration(self, key: str) -> float:
        value = self.cfg["timing"][key]
        return self.rng.uniform(*value) if isinstance(value, list) else float(value)

    def poll_audio(self) -> AudioSnapshot:
        snap = self.audio.snapshot()
        if time.monotonic() - snap.timestamp > self.cfg["audio"]["stale_seconds"]:
            raise RuntimeError("Loopback audio stalled; inputs released")
        self.last_audio = snap
        return snap

    def event(self, name: str, since: float) -> bool:
        return any(e.name == name and e.timestamp > since for e in self.last_audio.events)

    def until(self, predicate: Callable[[], bool], timeout: float, stable: int = 1) -> bool:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            self.gate.check()
            self.poll_audio()
            seen = seen + 1 if predicate() else 0
            if seen >= stable:
                return True
            self.gate.sleep(self.duration("poll_s"))
        return False

    def point(self, roi: str) -> tuple[int, int]:
        return self.screen.screen_point(self.cfg.rect(roi).center)

    @contextmanager
    def cursor(self) -> Iterator[None]:
        key = self.cfg["ui"]["cursor_toggle_key"]
        if key:
            self.io.tap(key)
            self.gate.sleep(self.cfg["ui"]["settle_s"])
        self.cursor_open = True
        try:
            yield
        finally:
            # Never issue a toggle after cancellation. User restores look mode before F8.
            if self.gate.active and getattr(self.gate.local, "epoch", None) == self.gate.epoch:
                if key:
                    self.io.tap(key)
                    self.gate.sleep(self.cfg["ui"]["settle_s"])
                self.cursor_open = False

    def read_progress(self) -> Progress:
        with self.cursor():
            self.mouse.move_gui(self.point("quest_paper"))
            self.gate.sleep(self.cfg["ui"]["hover_s"])
            deadline = time.monotonic() + self.duration("gui_timeout_s")
            last: Progress | None = None
            stable = 0
            while time.monotonic() < deadline:
                value = self.vision.progress()
                stable = stable + 1 if value is not None and value == last else 1
                last = value
                if value and stable >= self.cfg["vision"]["stable_frames"]:
                    if value.task != self.task:
                        self.pause(f"Quest paper says {value.task}, selected task is {self.task}; wrong-fish/task check failed")
                    return value
                self.poll_audio()
                self.gate.sleep(0.08)
        self.pause("Quest tooltip fish name/progress is unreadable; no catch was credited")
        raise AssertionError("unreachable")

    def face(self, target: float, slow: bool = False) -> None:
        c = self.cfg["compass"]
        self.mouse.face(target, self.compass.read, c["tolerance_deg"], c["max_correction_steps"], slow)

    def scan_taskmaster(self) -> bool:
        """Check the prompt throughout a slow SE-centered sweep, including the approach."""
        cfg = self.cfg["recovery"]
        center, width = cfg["npc_heading_deg"], cfg["npc_scan_half_arc_deg"]
        for target in (center, center - width, center + width, center):
            for _ in range(80):
                if self.vision.taskmaster():
                    if self.until(self.vision.taskmaster, 0.5, self.cfg["vision"]["stable_frames"]):
                        return True
                current = self.compass.read()
                error = heading_error(target, current)
                if abs(error) <= self.cfg["compass"]["tolerance_deg"]:
                    break
                step = max(-cfg["npc_scan_step_deg"], min(cfg["npc_scan_step_deg"], error))
                self.mouse.yaw(step, slow=True, overshoot=False)
                self.gate.sleep(self.rng.uniform(*cfg["scan_step_s"]))
                self.poll_audio()
        return False

    def boot(self) -> None:
        self.io.release_all()
        self.compass.read()
        if self.task is not None:
            progress = self.read_progress()
            self.count, self.pending = progress.count, 0
            self.turning_in = self.count >= 3
            self.transition(State.FIND_TASKMASTER if self.turning_in else State.PREP_GEAR, "Resuming verified quest")
        else:
            self.transition(State.FIND_TASKMASTER, "Find Taskmaster: Fishing")

    def find_taskmaster(self) -> None:
        self.io.release_all()
        if not self.scan_taskmaster():
            self.pause("Taskmaster: Fishing was not found in the look-only sweep")
        self.io.key(self.cfg["ui"]["interact_key"], True)
        try:
            self.gate.sleep(self.duration("interact_hold_s"))
        finally:
            self.io.release_all()
        if not self.until(lambda: self.vision.match("quest_gui", "quest_list") is not None, self.duration("gui_timeout_s"), 2):
            self.pause("Holding E did not open the recognized quest GUI")
        self.transition(State.TURN_IN if self.turning_in else State.READ_QUESTS)

    def read_quests(self) -> None:
        last: set[str] = set()
        stable = 0
        deadline = time.monotonic() + self.duration("gui_timeout_s")
        while time.monotonic() < deadline:
            available = set(self.vision.quests())
            stable = stable + 1 if available and available == last else 1
            last = available
            if available and stable >= self.cfg["vision"]["stable_frames"]:
                self.transition(State.SELECT_TASK, f"List recognized: {', '.join(sorted(available))}")
                return
            self.gate.sleep(0.1)
        self.pause("Quest list did not stabilize; check list ROI/templates")

    def select_task(self) -> None:
        # Re-read immediately before clicking, so a newly visible Bassle wins.
        rows = self.vision.quests()
        selected = choose_task(set(rows))
        match = rows[selected]
        roi = self.cfg.rect("quest_list")
        self.mouse.click(self.screen.screen_point((roi.x + match.center[0], roi.y + match.center[1])))
        self.gate.sleep(self.cfg["ui"]["settle_s"])
        if not self.until(lambda: self.vision.match("confirm", "confirm_button") is not None, self.duration("gui_timeout_s"), 2):
            self.pause("Quest confirm button not recognized")
        self.mouse.click(self.point("confirm_button"))
        self.gate.sleep(self.cfg["ui"]["settle_s"])
        # Some layouts close on confirm; close only while the quest panel is still visible.
        if self.vision.match("quest_gui", "quest_list"):
            self.io.tap(self.cfg["ui"]["close_quest_key"])
        if not self.until(lambda: self.vision.match("quest_gui", "quest_list") is None, self.duration("gui_timeout_s"), 2):
            self.pause("Quest GUI remained open after selection")
        self.task = selected
        progress = self.read_progress()  # Also proves the requested quest was accepted.
        self.count, self.pending, self.without_progress = progress.count, 0, 0
        self.transition(State.PREP_GEAR, f"Selected {self.cfg['tasks'][selected]['fish_name']}")

    def prep_gear(self) -> None:
        assert self.task is not None
        if self.count >= 3:
            self.turning_in = True
            self.transition(State.FIND_TASKMASTER)
            return
        task = self.cfg["tasks"][self.task]
        with self.cursor():
            for kind in ("hook", "bait"):
                if self.vision.equipped(self.task, kind):
                    continue
                rect = Rect.parse(task[f"{kind}_inventory_rect"], f"{kind}_inventory_rect", (3840, 2160))
                def source_visible() -> bool:
                    return self.vision.store.match(self.screen.grab_rect(rect), task[f"{kind}_template"]).score >= self.cfg["vision"]["threshold"]
                if not self.until(source_visible, 1.0, 2):
                    if kind == "bait":
                        self.transition(State.LOGOUT, "Selected bait is missing from its known inventory source")
                        return
                    self.pause(f"Selected {kind} is absent from its configured inventory rectangle")
                self.mouse.drag(self.screen.screen_point(rect.center), self.point(f"{kind}_slot"))
                if not self.until(lambda: self.vision.equipped(self.task, kind), self.duration("gui_timeout_s"), 2):
                    self.pause(f"{kind} slot did not match after drag; check source/slot coordinates")
        if self.until(self.vision.bait_zero, 0.25, 2):
            self.transition(State.LOGOUT, "Bait stack count is zero")
        else:
            self.transition(State.FACE_WATER)

    def face_water(self) -> None:
        target = self.mouse.water_heading()
        if self.cast_failures >= self.cfg["recovery"]["direct_cast_tries"]:
            # Widen coverage after direct retries, with different offsets each time.
            index = self.cast_failures - self.cfg["recovery"]["direct_cast_tries"]
            target = min(358, max(272, 280 + index * 30 + self.rng.uniform(-7, 7)))
        self.mouse.last_heading = target
        self.face(target)
        self.cast_heading = target
        self.transition(State.CAST, f"Water heading {target:.2f} degrees")

    def cast(self) -> None:
        assert self.task is not None
        self.io.release_all()
        if self.until(self.vision.bait_empty, 0.25, 2):
            self.transition(State.LOGOUT, "Bait stack is empty")
            return
        if not self.vision.equipped(self.task, "bait") or not self.vision.equipped(self.task, "hook"):
            self.pause("Rod loadout changed or is unreadable")
        # Require the old marker to clear; a static center ornament cannot count as a landing.
        if not self.until(lambda: self.vision.match("hit_marker", "hit_marker") is None, 1.5, 2):
            self.pause("Center marker is already present before cast; tighten the hit-marker template")
        self.gate.sleep(self.duration("pre_cast_s"))
        # Shoves during pre-cast delay are corrected with compass before sending LMB.
        self.face(self.cast_heading)
        self.io.button("left", True)
        try:
            self.gate.sleep(self.duration("cast_hold_s"))
        finally:
            self.io.release_all()
        self.cast_time = time.monotonic()
        if not self.until(lambda: self.vision.match("hit_marker", "hit_marker") is not None,
                          self.cfg["timing"]["T_land_ms"] / 1000):
            self.cast_failures += 1
            cfg = self.cfg["recovery"]
            self.transition(State.RECOVER if self.cast_failures >= cfg["direct_cast_tries"] + cfg["sweep_cast_tries"]
                            else State.FACE_WATER, "No new center hit marker; nudge and recast")
            return
        self.cast_failures = 0
        self.look_at = time.monotonic() + self.duration("look_interval_s")
        self.transition(State.WAIT_BITE, "Valid water landing")

    def wait_bite(self) -> None:
        now = time.monotonic()
        since = self.cast_time + self.duration("T_ignore_s")
        self.poll_audio()
        if self.event("quest_complete", since):
            self.io.release_all()
            self.force_progress = True
            self.transition(State.RESOLVE_CATCH, "Quest completion sound while waiting; verify paper")
            return
        if now >= since and self.event("splash", since):
            self.fight_time = time.monotonic()
            self.catch_armed = self.vision.match("catch", "catch_indicator") is None
            self.force_progress = False
            self.io.button("right", True)  # Assumption: a brief pull hooks the full splash.
            self.gate.sleep(self.duration("hook_hold_s"))
            self.transition(State.FIGHT_BASSLE if self.task == "bassle" else State.FIGHT_REDLINE, "Full splash; hook")
            return
        if now - self.cast_time >= self.duration("T_bored_s"):
            self.io.release_all()
            self.transition(State.FACE_WATER, "No full splash before T_bored")
            return
        if now >= self.look_at and now >= since:
            current = self.compass.read()
            if current < 270 and current > 2:
                self.io.release_all()
                self.transition(State.FACE_WATER, "Compass moved outside water arc")
                return
            # Keep every path point in W→N and preserve pitch. No overshoot at edges.
            current = 360 if current < 2 else current
            angle = self.duration("look_angle_deg")
            sign = self.rng.choice((-1, 1))
            if not 270 <= current + sign * angle <= 360:
                sign = -sign
            target = current + sign * angle
            self.mouse.yaw(target - current, overshoot=False)
            self.gate.sleep(self.rng.uniform(0.12, 0.3))
            self.mouse.yaw(current - target, overshoot=False)
            self.face(current)
            self.look_at = time.monotonic() + self.duration("look_interval_s")
        self.gate.sleep(self.duration("poll_s"))

    def fight(self) -> None:
        assert self.task is not None
        snap = self.poll_audio()
        left, right = fight_buttons(self.task, snap.tension)
        self.io.button("left", left)
        self.io.button("right", right)
        visual_catch = self.vision.match("catch", "catch_indicator") is not None
        if not visual_catch:
            self.catch_armed = True
        completed = self.event("quest_complete", self.fight_time)
        caught = self.event("catch", self.fight_time) or (visual_catch and self.catch_armed)
        if caught or completed:
            self.io.release_all()
            self.force_progress = completed
            self.transition(State.RESOLVE_CATCH, "Quest completion sound" if completed else "New catch evidence")
        elif time.monotonic() - self.fight_time >= self.duration("T_fight_max_s"):
            self.io.release_all()
            self.transition(State.FACE_WATER, "Fight timeout; no catch credited")
        else:
            self.gate.sleep(self.duration("poll_s"))

    def resolve_catch(self) -> None:
        self.io.release_all()
        self.gate.sleep(self.duration("resolve_s"))
        self.pending += 1
        self.without_progress += 1
        verify = (self.force_progress or self.pending >= self.cfg["progress"]["verify_every_n_catches"]
                  or self.count + self.pending >= 3)
        if verify:
            progress = self.read_progress()
            if progress.count < self.count:
                self.pause("Quest progress decreased unexpectedly; inspect the paper")
            if progress.count > self.count:
                self.without_progress = 0
            else:
                logging.warning("Catch did not advance %s; possible wrong fish, verify bait/hook", self.task)
            self.count, self.pending = progress.count, 0
            self.poll_audio()
            if self.task and self.event(f"fish_{self.task}", self.fight_time):
                logging.info("Optional fish audio agrees with selected task; tooltip remains authoritative")
            for other in self.cfg["tasks"]:
                if other != self.task and self.event(f"fish_{other}", self.fight_time):
                    logging.warning("Optional fish audio suggests %s; tooltip remains authoritative", other)
        if self.without_progress >= self.cfg["progress"]["max_unverified_catches"]:
            self.pause("Repeated catches did not advance selected quest; inspect loadout and tooltip")
        if self.until(self.vision.bait_empty, 0.25, 2):
            self.transition(State.LOGOUT, "Bait exhausted after catch")
            return
        if self.count >= 3:
            self.turning_in = True
            self.transition(State.FIND_TASKMASTER, "3/3 selected-fish catches verified")
        else:
            self.transition(State.FACE_WATER)

    def turn_in(self) -> None:
        if not self.until(lambda: self.vision.match("turn_in", "turn_in_button") is not None, self.duration("gui_timeout_s"), 2):
            self.pause("Turn-in button not recognized")
        self.mouse.click(self.point("turn_in_button"))
        def done() -> bool:
            return self.vision.match("turn_in", "turn_in_button") is None
        if not self.until(done, self.duration("gui_timeout_s"), 2):
            self.pause("Turn-in did not clear; no repeated blind click")
        self.task, self.count, self.pending, self.turning_in = None, 0, 0, False
        self.without_progress = 0
        if self.vision.match("quest_gui", "quest_list"):
            self.transition(State.READ_QUESTS, "Turned in; read refreshed list")
        else:
            self.transition(State.FIND_TASKMASTER, "Turn-in closed the GUI; reopen list")

    def recover(self) -> None:
        self.io.release_all()
        found = self.scan_taskmaster()
        cfg = self.cfg["recovery"]
        if not found and self.cfg["enable_wasd_recovery"] and self.wasd_attempts < cfg["max_wasd_attempts"]:
            self.wasd_attempts += 1
            self.face(self.mouse.water_heading())
            self.io.tap(cfg["wasd_key"], cfg["wasd_hold_s"])
            self.cast_failures = 0
            self.transition(State.FACE_WATER, "Configured, bounded WASD recovery after look-only failure")
        else:
            self.pause("Water could not be reacquired with look-only retries; " +
                       ("taskmaster is visible, inspect dock position/pitch" if found else "taskmaster also not visible"))

    def logout(self) -> None:
        self.io.release_all()
        for step in self.cfg["logout"]["sequence"]:
            if "key" in step:
                self.io.tap(step["key"])
            else:
                self.mouse.click(self.screen.screen_point(tuple(step["click"])))
            self.gate.sleep(float(step.get("wait_s", 0.7)))
        cfg = self.cfg["logout"]
        rect = Rect.parse(cfg["success_roi"], "logout.success_roi", (3840, 2160))
        def logged_out() -> bool:
            result = self.vision.store.match(self.screen.grab_rect(rect), cfg["success_template"])
            return result.score >= self.cfg["vision"]["threshold"]
        verified = self.until(logged_out, cfg["timeout_s"], 2)
        self.message = "Logout confirmed; stopped" if verified else "Logout sequence sent but not confirmed; inspect client"
        if not verified:
            self.transition(State.FATAL, self.message)
        self.gate.pause(self.message, exit_=True)

    def step(self) -> None:
        self.poll_audio()
        self.io._guard()  # Also catches focus loss while merely observing a bite.
        actions = {State.BOOT: self.boot, State.FIND_TASKMASTER: self.find_taskmaster,
                   State.READ_QUESTS: self.read_quests, State.SELECT_TASK: self.select_task,
                   State.PREP_GEAR: self.prep_gear, State.FACE_WATER: self.face_water,
                   State.CAST: self.cast, State.WAIT_BITE: self.wait_bite,
                   State.FIGHT_BASSLE: self.fight, State.FIGHT_REDLINE: self.fight,
                   State.RESOLVE_CATCH: self.resolve_catch, State.TURN_IN: self.turn_in,
                   State.RECOVER: self.recover, State.LOGOUT: self.logout}
        actions[self.state]()

    def run(self) -> None:
        try:
            while not self.gate.shutdown.is_set():
                if not self.gate.active:
                    paused = State.PANIC if self.gate.reason.startswith("PANIC") else State.PAUSE
                    if self.state != paused:
                        self.transition(paused, self.gate.reason)
                    self.gate.shutdown.wait(0.03)
                    continue
                try:
                    with self.gate.session():
                        if self.epoch_seen != self.gate.epoch:
                            self.epoch_seen = self.gate.epoch
                            self.cursor_open = False
                            self.transition(State.BOOT, "Start/resume from a normalized game UI")
                        self.step()
                except Interrupted:
                    continue
                except (CompassLost, RuntimeError) as exc:
                    self.transition(State.PAUSE, str(exc))
                    self.gate.pause(f"PAUSE: {exc}; inspect setup before F8")
                except Exception as exc:
                    logging.exception("Fatal state-machine failure")
                    self.transition(State.FATAL, str(exc))
                    self.gate.pause(f"FATAL: {exc}", exit_=True)
        finally:
            self.io.release_all()
            self.screen.close()
