"""Minimum-jerk relative camera motion and curved visible-cursor drags."""
from __future__ import annotations

import math
import random
from typing import Callable

from input.keys import InputController


def minimum_jerk(t: float) -> float:
    return t * t * t * (10 + t * (-15 + 6 * t))


def heading_error(target: float, current: float) -> float:
    return (target - current + 180) % 360 - 180


class HumanMouse:
    def __init__(self, controller: InputController, counts_per_degree: float,
                 rng: random.Random | None = None) -> None:
        self.io = controller
        self.gate = controller.gate
        self.scale = counts_per_degree
        self.rng = rng or random.Random()
        self.last_heading: float | None = None

    def water_heading(self) -> float:
        # Continuous uniform draw over W→N. Reject only exact/quantized duplicates.
        while True:
            angle = self.rng.uniform(270, 360)
            if self.last_heading is None or round(angle * abs(self.scale)) != round(self.last_heading * abs(self.scale)):
                self.last_heading = angle
                return angle

    def _segment(self, dx: int, seconds: float) -> None:
        steps = max(8, math.ceil(seconds / 0.008))
        previous = 0
        power = self.rng.uniform(0.92, 1.08)
        jitter = self.rng.uniform(0.05, 0.15)
        for i in range(1, steps + 1):
            position = round(dx * minimum_jerk((i / steps) ** power))
            self.io.relative(position - previous, 0)
            previous = position
            self.gate.sleep(seconds / steps * self.rng.uniform(1 - jitter, 1 + jitter))

    def yaw(self, degrees: float, slow: bool = False, overshoot: bool = True) -> None:
        counts = round(degrees * self.scale)
        if not counts:
            return
        seconds = max(0.10, abs(degrees) / (45 if slow else 115)) * self.rng.uniform(0.9, 1.1)
        extra = round(counts * self.rng.uniform(0.004, 0.014)) if overshoot else 0
        self._segment(counts + extra, seconds)
        if extra:
            self._segment(-extra, self.rng.uniform(0.06, 0.12))

    def face(self, target: float, read_heading: Callable[[], float], tolerance: float,
             max_steps: int, slow: bool = False) -> None:
        for _ in range(max_steps):
            current = read_heading()
            error = heading_error(target, current)
            if abs(error) <= tolerance:
                return
            # Suppress overshoot near the water-arc boundaries.
            self.yaw(error, slow, overshoot=not (target < 273 or target > 357))
            self.gate.sleep(0.12)
        raise RuntimeError(f"Compass did not converge toward {target:.1f} degrees; recalibrate")

    def move_gui(self, target: tuple[int, int], seconds: float | None = None) -> None:
        start = self.io.backend.cursor()
        duration = seconds or self.rng.uniform(0.25, 0.55)
        steps = max(12, round(duration / 0.01))
        dx, dy = target[0] - start[0], target[1] - start[1]
        length = max(1, math.hypot(dx, dy))
        bend = self.rng.uniform(-0.08, 0.08) * min(length, 700)
        for i in range(1, steps + 1):
            t = minimum_jerk(i / steps)
            curve = 4 * t * (1 - t) * bend
            self.io.gui_position(round(start[0] + dx * t - dy / length * curve),
                                 round(start[1] + dy * t + dx / length * curve))
            self.gate.sleep(duration / steps * self.rng.uniform(0.90, 1.10))

    def click(self, target: tuple[int, int]) -> None:
        self.move_gui(target)
        self.io.button("left", True)
        try:
            self.gate.sleep(self.rng.uniform(0.06, 0.11))
        finally:
            self.io.release_all()

    def drag(self, source: tuple[int, int], target: tuple[int, int]) -> None:
        self.move_gui(source)
        self.io.button("left", True)
        try:
            self.gate.sleep(self.rng.uniform(0.10, 0.18))
            self.move_gui(target, self.rng.uniform(0.55, 0.95))
            self.gate.sleep(self.rng.uniform(0.10, 0.18))
        finally:
            self.io.release_all()
