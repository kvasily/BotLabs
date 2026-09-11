"""Record a short WAV while F6 is held. This tool sends no keyboard/mouse input."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.recorder import record_clip
from config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--device", help="Override audio_device_name")
    parser.add_argument("--key", type=int, default=6, choices=range(1, 13), help="Function key to hold, default F6")
    parser.add_argument("--max-seconds", type=float, default=0.9)
    args = parser.parse_args()
    cfg = load_config(args.config)
    record_clip(args.device or cfg["audio_device_name"], args.output, 0x6F + args.key, args.max_seconds)


if __name__ == "__main__":
    main()
