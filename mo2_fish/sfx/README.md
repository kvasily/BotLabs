Record your own 48 kHz WAVs here with `tools/sfx_recorder.py`.

Required: `bubble.wav`, `splash.wav`, `tension.wav`, `quest_complete.wav`.
Optional: `catch.wav` and per-fish clips enabled via config.

Use 20–950 ms clips (at most `1000 - hop_ms` milliseconds); a short distinctive
excerpt is preferable to a long effect. For tension, use a repeatable 40–120 ms
excerpt of sustained bending. Long clips add their full duration to detection
latency. No game audio is supplied or fabricated.
