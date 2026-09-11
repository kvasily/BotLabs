"""High-level observations used by the state machine, without input side effects."""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from vision.capture import ScreenCapture
from vision.ocr import OCR
from vision.templates import Match, TemplateStore


@dataclass(frozen=True)
class Progress:
    task: str
    count: int


class Perception:
    def __init__(self, cfg: Settings, screen: ScreenCapture, store: TemplateStore, ocr: OCR) -> None:
        self.cfg, self.screen, self.store, self.ocr = cfg, screen, store, ocr

    def match(self, name: str, roi: str) -> Match | None:
        return self.store.named(self.screen.grab(roi), name)

    def text(self, name: str, phrase: str, roi: str) -> Match | None:
        image = self.screen.grab(roi)
        return self.store.named(image, name) or self.ocr.exact(image, phrase)

    def taskmaster(self) -> bool:
        return self.text("taskmaster", "Taskmaster: Fishing", "interact_prompt") is not None

    def quests(self) -> dict[str, Match]:
        image = self.screen.grab("quest_list")
        found: dict[str, Match] = {}
        for task, spec in self.cfg["tasks"].items():
            match = self.store.named(image, task) or self.ocr.exact(image, spec["fish_name"])
            if match:
                found[task] = match
        return found

    def progress(self) -> Progress | None:
        image = self.screen.grab("quest_tooltip")
        tasks = [name for name, spec in self.cfg["tasks"].items()
                 if self.store.named(image, name) or self.ocr.exact(image, spec["fish_name"])]
        if len(tasks) != 1:
            return None
        scores = sorted(((self.store.match(image, self.cfg["templates"][f"progress_{i}"]).score, i)
                         for i in range(4)), reverse=True)
        count = None
        if scores[0][0] >= self.cfg["vision"]["threshold"] and scores[0][0] - scores[1][0] >= self.cfg["vision"]["progress_margin"]:
            count = scores[0][1]
        if count is None:
            count = self.ocr.progress(image)
        return Progress(tasks[0], count) if count is not None else None

    def equipped(self, task: str, kind: str) -> bool:
        result = self.store.match(self.screen.grab(f"{kind}_slot"), self.cfg["tasks"][task][f"{kind}_template"])
        return result.score >= self.cfg["vision"]["threshold"]

    def bait_zero(self) -> bool:
        return self.match("bait_zero", "bait_count") is not None

    def bait_empty(self) -> bool:
        return self.match("bait_empty", "bait_slot") is not None or self.bait_zero()
