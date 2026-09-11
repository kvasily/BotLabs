"""Pick a physical-pixel ROI from a 4K game capture or saved screenshot."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from config import Rect, load_config
from vision.capture import ScreenCapture, enable_dpi_awareness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--image", type=Path, help="Saved, unscaled 3840x2160 game screenshot")
    parser.add_argument("--save", type=Path, help="Save the selected native-resolution PNG crop")
    parser.add_argument("--name", default="compass", help="Name printed in the YAML snippet")
    parser.add_argument("--preview-width", type=int, default=1600)
    args = parser.parse_args()
    enable_dpi_awareness()
    cfg = load_config(args.config)
    if args.image:
        image = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Cannot decode {args.image}")
    else:
        print("Capturing the configured 4K game rectangle in 3 seconds. Focus the game now.")
        import time
        time.sleep(3)
        capture = ScreenCapture(cfg)
        try:
            image = capture.grab_rect(Rect(0, 0, *cfg["resolution"]))
        finally:
            capture.close()
    if image.shape[:2] != (2160, 3840):
        raise ValueError("Expected an unscaled 3840x2160 game image")
    scale = min(1.0, max(320, args.preview_width) / image.shape[1])
    preview = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    print("Drag a rectangle, Enter accepts, Esc cancels. Only the preview is resized.")
    x, y, width, height = cv2.selectROI("ROI picker", preview, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    if not width or not height:
        return 0
    left, top = round(x / scale), round(y / scale)
    right, bottom = round((x + width) / scale), round((y + height) / scale)
    print(f"  {args.name}: [{left}, {top}, {right - left}, {bottom - top}]")
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", image[top:bottom, left:right])[1].tofile(args.save)
        print(f"Saved native-resolution crop: {args.save.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
