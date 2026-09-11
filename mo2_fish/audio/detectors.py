"""Normalized waveform correlation with sample-age gating and tension debounce."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
import soundfile as sf

from config import ConfigError, Settings

Samples = NDArray[np.float32]


def rms(samples: Samples) -> float:
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) if samples.size else 0.0


def load_wav(path: Path, rate: int = 48000, max_seconds: float = 1.0) -> Samples:
    if not path.is_file():
        raise ConfigError(f"Missing WAV: {path}")
    try:
        data, actual_rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:
        raise ConfigError(f"Cannot read WAV {path}: {exc}") from exc
    if actual_rate != rate:
        raise ConfigError(f"{path}: expected {rate} Hz, got {actual_rate}; re-record at 48 kHz")
    mono = data.mean(axis=1).astype(np.float32)
    if not 0.02 * rate <= len(mono) <= max_seconds * rate:
        raise ConfigError(f"{path}: clip must be 20–{round(max_seconds * 1000)} ms")
    if not np.isfinite(mono).all() or rms(mono - mono.mean()) < 1e-6:
        raise ConfigError(f"{path}: silent/constant/invalid WAV; record a clean sound")
    return mono


def normalized_correlation(signal: Samples, template: Samples) -> NDArray[np.float64]:
    """Zero-mean, energy-normalized valid cross-correlation via FFT, one score per start."""
    n, m = len(signal), len(template)
    if n < m:
        return np.empty(0, dtype=np.float64)
    x = np.asarray(signal, dtype=np.float64)
    t = np.asarray(template, dtype=np.float64)
    t = t - t.mean()
    size = 1 << (n + m - 2).bit_length()
    convolution = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(t[::-1], size), size)
    numerator = convolution[m - 1:n]
    sums = np.concatenate(([0.0], np.cumsum(x)))
    squares = np.concatenate(([0.0], np.cumsum(x * x)))
    energy = np.maximum(0, squares[m:] - squares[:-m] - (sums[m:] - sums[:-m]) ** 2 / m)
    denominator = np.sqrt(energy * np.dot(t, t))
    return np.clip(np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-12), -1, 1)


class Debounce:
    def __init__(self, on_seconds: float, off_seconds: float) -> None:
        self.on_seconds, self.off_seconds = on_seconds, off_seconds
        self.state = False
        self.candidate = False
        self.since = 0.0

    def update(self, raw: bool, now: float) -> bool:
        if raw != self.candidate:
            self.candidate, self.since = raw, now
        delay = self.on_seconds if raw else self.off_seconds
        if raw != self.state and now - self.since >= delay:
            self.state = raw
        return self.state


class SpectralFlux:
    def __init__(self, rate: int, band: list[float]) -> None:
        self.rate, self.band = rate, band
        self.previous: NDArray[np.float64] | None = None

    def update(self, hop: Samples) -> float:
        spectrum = np.abs(np.fft.rfft(hop * np.hanning(len(hop)))) / max(1, len(hop))
        freq = np.fft.rfftfreq(len(hop), 1 / self.rate)
        selected = spectrum[(freq >= self.band[0]) & (freq <= self.band[1])]
        value = 0.0
        if self.previous is not None and self.previous.shape == selected.shape:
            value = float(np.linalg.norm(np.maximum(0, selected - self.previous)))
        self.previous = selected
        return value


@dataclass(frozen=True)
class SoundEvent:
    name: str
    timestamp: float
    score: float


@dataclass(frozen=True)
class AudioSnapshot:
    timestamp: float
    rms: float
    scores: dict[str, float]
    tension: bool
    flux: float
    events: tuple[SoundEvent, ...]


class DetectorBank:
    def __init__(self, settings: Settings) -> None:
        self.cfg: dict[str, Any] = settings["audio"]
        self.rate: int = self.cfg["sample_rate"]
        # Leave a hop of headroom so a complete event survives arbitrary hop alignment.
        max_seconds = self.cfg["ring_seconds"] - self.cfg["hop_ms"] / 1000
        self.templates = {name: load_wav(settings.asset(path), self.rate, max_seconds)
                          for name, path in self.cfg["templates"].items() if path}
        for name, task in settings["tasks"].items():
            if task.get("audio_template"):
                self.templates[f"fish_{name}"] = load_wav(settings.asset(task["audio_template"]), self.rate, max_seconds)
        self.events: deque[SoundEvent] = deque(maxlen=128)
        self.last_event: dict[str, float] = {}
        self.debounce = Debounce(self.cfg["tension_on_ms"] / 1000, self.cfg["tension_off_ms"] / 1000)
        self.flux = SpectralFlux(self.rate, self.cfg["spectral_flux"]["band_hz"])

    def process(self, ring: Samples, hop: Samples, now: float) -> AudioSnapshot:
        scores: dict[str, float] = {}
        peaks: dict[str, tuple[float, float]] = {}
        level = rms(hop)
        flux = self.flux.update(hop)
        for name, template in self.templates.items():
            # Tension matches only in a short trailing window; it cannot remain held
            # just because an old bend sound is still in the one-second ring.
            tail = round(self.rate * self.cfg["tension_tail_ms"] / 1000) if name == "tension" else len(hop)
            recent = ring[-(len(template) + tail - 1):]
            correlation = normalized_correlation(recent, template)
            if not len(correlation):
                scores[name] = 0.0
                continue
            index = int(np.argmax(correlation))
            score = max(0.0, float(correlation[index]))
            matched = recent[index:index + len(template)]
            if rms(matched) < self.cfg["min_rms"]:
                score = 0.0
            scores[name] = score
            age = (len(recent) - index - len(template)) / self.rate
            peaks[name] = score, now - age
        for name, (score, when) in peaks.items():
            if name == "tension":
                continue
            threshold = self.cfg["thresholds"].get(name, 0.80)
            if name == "splash":
                if score < scores.get("bubble", 0) + self.cfg["splash_over_bubble_margin"]:
                    continue
                if self.cfg["spectral_flux"]["enabled"] and flux < self.cfg["spectral_flux"]["threshold"]:
                    continue
            if score >= threshold and when - self.last_event.get(name, float("-inf")) >= self.cfg["event_cooldown_s"]:
                self.events.append(SoundEvent(name, when, score))
                self.last_event[name] = when
        raw_tension = scores.get("tension", 0) >= self.cfg["thresholds"]["tension"]
        tension = self.debounce.update(raw_tension, now)
        return AudioSnapshot(now, level, scores, tension, flux, tuple(self.events))
