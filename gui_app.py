"""Desktop entry point, also used by the windowed PyInstaller executable."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "mo2_fish"))
from gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
