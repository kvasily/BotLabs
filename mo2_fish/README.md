# MO2 fishing assistant — Windows / Python 3.11+

## Assumptions — read before setup

- The character is already on the dock, with a usable rod and the camera pitched toward water. Normal operation changes yaw only. The water arc is W→N, 270°–360°; the taskmaster is roughly SE.
- The game image is exactly **3840×2160 physical pixels**, borderless or windowed, at a fixed desktop origin. The compass, inventory, rod slots, quest paper, and all configured interface regions stay visible and fixed. The game uses English text.
- Both task rows, when posted, fit in the quest-list region without scrolling. Clicking a fish-name row then its confirm button accepts that task. Turning in uses one distinct button. A stable quest-panel feature is visible inside `quest_list` while the panel is open. Adapt the configured layout to these assumptions before running.
- The selected quest paper occupies one known inventory position. Its tooltip shows the **fish name plus an unambiguous `0/3`–`3/3` counter**. Recognition of that counter is the authority for selected-fish progress. If the actual tooltip differs, replace the corresponding perception adapter and templates before use.
- A short RMB pull hooks a full splash. Bassle then keeps RMB held; Redline Torp reels with LMB and adds RMB only for debounced tension. The hook pulse duration is configurable.
- A successful catch provides a distinct, newly appearing visual indicator, or you configure `sfx/catch.wav`. The transient center hit marker is distinct from any permanent crosshair. Game-specific images and sounds must be captured by you; none are fabricated or bundled.
- The compass N glyph can be matched around the ring at your UI scale. In ticks mode, the top crop includes enough distinctive labels/context to identify an absolute heading. **Identical unlabeled ticks alone cannot determine absolute yaw.**
- The quest GUI exposes its cursor automatically. `ui.cursor_toggle_key` controls entering/leaving cursor mode for inventory hovering and gear dragging while the quest GUI is closed. Set it to your binding, or `null` only if no toggle is needed. Returning from either interface restores mouse-look.
- Before the first F8, close dialogs, cancel any existing cast, and restore mouse-look. After a pause/panic, do the same before F8. If interrupted during quest acceptance or turn-in, inspect the active quest manually; restarting the program starts a new quest-selection flow. Quest state is retained in memory only.
- Audio routing is already configured to a named Windows playback endpoint. Capture uses that endpoint's WASAPI loopback. This project installs no drivers and contains no game-memory access, process hooks, injection, packet capture, or game-client modifications.

This is real, runnable source with offline tests. It has **not been validated against a live MO2 client**. Your ROIs, UI behavior, compass samples, input sensitivity, and sound thresholds require calibration.

## GUI

Launch **`D:\Fishing Bot\MO2Fish.exe`**. It includes Python, PySide6, OpenCV, FFmpeg, and the audio libraries; no Python installation is needed to launch it. The GUI starts **Disarmed**, with no audio stream, fishing worker, or registered hotkeys. The fishing FSM, detectors, compass math, and SendInput backend remain unchanged behind wrappers.

From source, run `D:\Fishing Bot\.venv\Scripts\python.exe D:\Fishing Bot\gui_app.py`, or `python -m mo2_fish.gui` from the repository root with the dependencies installed. `main.start_assistant(cfg, gate)` is the shared construction helper. The GUI adds input permission/ownership checks and initializes a Windows COM apartment for each audio worker.

### Profiles and validation

Profiles are portable folders beside the executable:

```text
D:\Fishing Bot\profiles\<name>\config.yaml
D:\Fishing Bot\profiles\<name>\templates\
D:\Fishing Bot\profiles\<name>\sfx\
```

First launch creates **Default** from the repository config, or from the bundled default YAML if only the executable was distributed. Later launches open the last profile. Load / Save / Save As are in the Profile tab; Save As copies actual referenced assets into the new folder and leaves missing assets missing. Load is restricted to the profiles folder. `.last-profile` stores only the last YAML filename; configuration remains the existing YAML schema. GUI saves replace comments with normalized YAML.

Edit game origin, counts per degree, compass center/radius, thresholds and logout steps in the Profile YAML editor. The original validator enforces 3840×2160 and the two fixed fish identities. **Validate setup calls `main.check_assets`**, showing each error line and full missing filenames. Validation passes expire when the profile or referenced assets change. New profiles intentionally fail until calibrated.

### Controls and focus

Arming changes a flag only. Start requires **Armed + MO2 focused + passed preflight + unchanged saved profile**. Close game dialogs, cancel any existing line, and restore mouse-look before Start or resume. The top Arm/Start/Pause/Panic controls accept clicks without activating the app: keep those controls exposed beside/over MO2 or on a second monitor, focus MO2, then click Start. Clicking other app areas can take focus and pauses fishing. The app does not force game activation and **never auto-resumes when focus returns**.

Pause and Panic drive `SafetyGate.pause` and immediately release the assistant's held inputs. Disarm releases owned input first, then blocks all further SendInput, including redundant cleanup releases. Closing the app shuts down the gate and joins workers with bounded timeouts. **No F8/F9/F10 hotkeys are registered anywhere in GUI mode**, including heading calibration. The old CLI uses hotkeys only with `--hotkeys`.

The optional `OVERRIDE` text unlocks a **fresh validation attempt at Start**, not a bypass of `check_assets`: missing assets and TODO values still prevent input. UI edits/authoring invalidate the session pass; an additional monitor checks external YAML/asset changes.

### Teach from video and audio

Open an MP4/MKV and scrub the timeline. Draw an **outer search ROI**, then optionally draw an **inner template crop** inside it. Drawing freezes the displayed frame. Save PNG + ROI writes the native crop into the profile and patches the corresponding existing YAML fields; Save ROI only preserves the template reference. For `compass`, use the whole widget as the outer box and N as the inner crop. Fish-name/progress roles update `quest_list`/`quest_tooltip`, so their outer box should cover the whole search region. The role menu also includes the four fish-specific hook/bait sources, bait empty/zero states, and logout confirmation. Choose Bassle or Redline Torp when teaching hook/bait slots.

**PNG crops are never resized.** Preview letterboxing is inverted before cropping. Non-4K video shows a warning; YAML boxes use an aspect-preserving fit into a 3840×2160 canvas with letterbox offsets. Smaller native templates generally will not match 4K runtime pixels: recapture at native 4K before fishing. Prefer constant-frame-rate recordings for timeline accuracy. Video is **authoring only**; runtime perception always uses live `mss` screen captures.

Video decoding uses **OpenCV VideoCapture**, so a missing Qt Multimedia backend does not block authoring. Audio extraction uses bundled FFmpeg without a console window. Unsupported codecs produce a clear error; try an H.264 MP4 or WAV excerpt. Load audio at the current frame or open an audio file; extraction defaults to 15 seconds and is capped at 120 seconds. Drag the waveform or edit In/Out, preview, assign a sound role, then export **48 kHz mono WAV**. Peak hop RMS is shown. Clips must be 20–`1000 - hop_ms` milliseconds; use a short 40–120 ms sustained tension excerpt.

Record live clip captures the configured loopback until Stop or 30 seconds, then uses the same trimmer. Audio device → Refresh lists playback loopbacks by name and ID; Use selected device writes its ID to the existing `audio_device_name` field. Test device RMS opens only a small loopback probe. Authoring/preview actions disarm the fishing session; stop probes before Start. None of these authoring tools sends game input.

### Heading calibration and overlay

Calibrate heading reuses `tools.heading_calibrator`'s exact-count movement and signed-scale save logic in-process. No CLI or hotkeys are launched. Its test move needs the same Arm/focus/preflight checks. For initial setup, save a provisional numeric `counts_per_degree` such as 1, finish other required setup, and Validate. The test sends **raw counts**, independent of that provisional scale. Face N, click Send test movement while MO2 is focused, read the new heading, then save the scale and Validate again. Pause/Panic remain available. Keep the motion within 2–150° without a full rotation.

The overlay is a separate topmost Qt window with physical geometry `game_origin + 3840×2160` and `WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE`. It draws enabled ROIs, highlights the active state's regions, and shows state/task/count/heading/splash/tension. Labels and individual outlines can be toggled. It never activates and no DLL enters the game.

Windows 10 build 19041+ / Windows 11 support the requested `WDA_EXCLUDEFROMCAPTURE`, preventing the HUD from contaminating detector pixels. If exclusion is unavailable, the app hides the overlay during fishing. Keep the **main app** outside perception ROIs; only the overlay is excluded. See Microsoft's [capture-affinity API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity) and Qt's [window flags](https://doc.qt.io/qt-6/qt.html).

### Build and verification

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build_gui.ps1
```

From the `mo2_fish` folder, run `..\.venv\Scripts\python.exe -m unittest discover -s tests -v`. There are 36 core tests plus 20 GUI/wrapper tests. The executable's `--smoke-test <output-directory>` mode verifies disarmed startup, rendering, bundled media decoding and native overlay properties with no game input. Temporary test media is never saved in profiles or bundled.

`MO2Fish.spec` produces a one-file, console-free executable in the repository root. Its working folder, profiles and logs are beside the executable, not in the extraction folder. Logs are in `logs/gui.log`. No fake game PNGs/WAVs are bundled. Optional OCR requires Tesseract plus `pytesseract` in the source/build environment and a rebuilt executable; enabling it without those dependencies produces the existing preflight error. Live game behavior still needs your captures and calibration.

## Install and run the legacy CLI

Open PowerShell in this `mo2_fish` folder. With a standard 64-bit Python 3.11+ installation:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --list-devices
```

Python's normal Windows installer includes Tk, used by the debug meter. For local OCR, optionally install `pytesseract` into the environment and install the Tesseract executable separately, then enable `vision.ocr_enabled` and set `vision.tesseract_cmd` if needed. OCR runs locally and is only a fallback.

For this delivered workspace, a tested environment also exists at `D:\Fishing Bot\.venv`; from `mo2_fish`, its interpreter is `..\.venv\Scripts\python.exe`.

1. Configure audio as described below, then record the four required WAVs.
2. Fill every applicable `TODO` in `config.yaml`. Capture the PNG templates and all fixed ROIs. `tick_top_rect` may remain TODO in north mode; north-only settings may remain TODO in ticks mode.
3. Calibrate signed mouse counts/degree with the heading tool.
4. Configure and manually verify the logout sequence, including its success-screen template.
5. Run the preflight, then the assistant:

```powershell
.\.venv\Scripts\python.exe main.py --check
.\.venv\Scripts\python.exe main.py --hotkeys
```

`--check` opens no devices and sends no input. It checks configuration, missing/invalid WAVs and PNGs, ROI bounds, template sizes, and optional OCR availability. Missing assets are reported with their full filenames. The supplied config intentionally fails until it is completed.

The assistant starts **paused**. Position the debug window outside all perception ROIs, focus the game, and press F8. `--no-debug` disables the debug window. The assistant pauses when the foreground window title no longer contains `window_title_contains`.

- **F8:** start/pause. Resuming restarts from a normalized interface and rechecks the active quest paper when a task is already selected.
- **F9:** panic-stop and release the assistant's held keys plus both mouse buttons. It remains paused.
- **F10:** exit immediately through bounded cleanup; release occurs before worker shutdown. Ctrl+C and closing the meter also stop the program.

Global hotkeys use Windows `RegisterHotKey`, with no keyboard hook. Another program owning F8/F9/F10 prevents startup with a clear error. Camera input uses relative `SendInput` counts. Only visible UI cursor paths use absolute `SendInput` coordinates; no absolute camera teleports are used. See Microsoft's [mouse input definition](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput) and [hotkey API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).

## Audio routing and recording

Use an existing VB-CABLE or VoiceMeeter setup, or a normal named playback endpoint. Keep unrelated app sounds off the endpoint used by the assistant.

With **VB-CABLE**, route MO2's output to the playback endpoint typically named `CABLE Input (VB-Audio Virtual Cable)`. Select that **playback endpoint's loopback** in `audio_device_name`. `CABLE Output` is the corresponding recording endpoint; this implementation deliberately enumerates playback loopbacks. You can monitor the cable through your existing mixer/headphones setup. See the [official VB-CABLE documentation](https://vb-audio.com/Cable/VBCABLE_ReferenceManual.pdf).

With **VoiceMeeter**, route MO2 to one dedicated virtual playback input, select the same named playback input's loopback here, and route its mixer strip to your headphone output (A/A1, according to edition). Names vary between editions, so use `--list-devices` instead of copying a guessed name. See [VoiceMeeter's input/output guide](https://voicemeeter.com/quick-tips-voicemeeter-virtual-inputs-and-outputs-windows-10-and-up/).

Use 48 kHz throughout the route and a stereo endpoint. The capture thread records two channels, then averages them into **48 kHz mono**, using 30 ms hops by default (20–50 ms supported) and a one-second ring. This avoids SoundCard's documented Windows single-channel capture problem. WASAPI may not honor the requested latency exactly; verify observed response times. See [SoundCard's documentation](https://soundcard.readthedocs.io/en/stable/).

```powershell
.\.venv\Scripts\python.exe tools\sfx_recorder.py sfx\bubble.wav
.\.venv\Scripts\python.exe tools\sfx_recorder.py sfx\splash.wav
.\.venv\Scripts\python.exe tools\sfx_recorder.py sfx\tension.wav --max-seconds 0.12
.\.venv\Scripts\python.exe tools\sfx_recorder.py sfx\quest_complete.wav
```

Hold **F6** around the desired sound, then release. Esc cancels. `--key 7` selects a different function key. The recorder writes the requested file (overwriting it on a successful recording), prints peak hop RMS and a suggested minimum-RMS starting point, and suggests an initial NCC threshold. It sends no game input. Use excerpts 20–950 ms long, and no longer than `1000 - hop_ms` milliseconds. Long effects should be represented by a short distinctive excerpt. Record tension as a short, repeating part of sustained twig bending; long tension templates delay activation and release.

Set `audio.templates.catch: sfx/catch.wav` to enable an optional catch sound. Then `templates.catch` can be `null` if you do not use a visual catch detector. The per-task `audio_template` accepts an optional fish-identifying WAV, reported as secondary evidence only.

Run the audio meter before configuring vision if useful:

```powershell
.\.venv\Scripts\python.exe main.py --audio-meter
```

It requires the named endpoint and enabled WAVs, but no camera calibration or PNGs. It never sends game input. Ctrl+C or closing the meter exits; add `--hotkeys` to enable F10 in this CLI tool.

The meter displays RMS, normalized cross-correlation scores, optional band-limited spectral flux, and debounced tension state. Collect positive examples and ordinary background/nibble examples. Tune each threshold above negative scores and below consistent positive scores. RMS is a noise floor, **not** a splash trigger. Bubble events never hook. Splash correlation must exceed both its threshold and the bubble score by `splash_over_bubble_margin`. Optional spectral flux is an extra splash gate, never an independent trigger. If positive and negative scores overlap, capture better templates or improve audio isolation.

Waveform correlation is sensitive to randomized pitch, altered mixing, and overlapping sounds. No universal threshold can be inferred from one recording. Calibration may show that a single template is insufficient for your client's SFX; do not run unattended on ambiguous detections.

Audio events are dated to their matching sample window. Only newly completed matches produce events, with cooldowns; old sounds remaining in the ring do not retrigger. Tension only considers matches ending within `tension_tail_ms`, followed by independent 80–200 ms on/off debounce. Its practical release latency also includes the template length, capture latency, and that tail. Set a short template and verify that RMB releases on silence in the meter.

## Fixed ROIs and 4K templates

All main config rectangles are `[x, y, width, height]` in physical pixels relative to `game_origin`, the desktop location of the game image's upper-left corner. Negative desktop origins are allowed for other monitors. Rectangles themselves must lie inside the 3840×2160 game image. Keep resolution, UI scale, monitor scaling, and window position unchanged after calibration.

The ROI picker accepts a saved unscaled screenshot or captures the configured game rectangle after a three-second countdown:

```powershell
.\.venv\Scripts\python.exe tools\roi_picker.py --image capture.png --name compass
.\.venv\Scripts\python.exe tools\roi_picker.py --image capture.png --name hit_marker --save templates\hit_marker.png
.\.venv\Scripts\python.exe tools\roi_picker.py --name quest_tooltip
```

Drag a rectangle and press Enter. Copy the printed rectangle into YAML. The preview is downscaled to fit your monitor; saved crops retain original pixels. For tiny glyphs, use `--preview-width 3840` or an image editor for a tighter crop. The tool does not rewrite your YAML. Runtime perception captures only configured ROIs; full-game capture is confined to this manual setup utility.

Create every PNG listed under `templates`, the four task loadout images, the compass image(s), and `logout.success_template`. The exact prompt template must contain **Taskmaster: Fishing**. Fish-name crops must contain **Bassle** or **Redline Torp** in full. Templates must include distinctive detail, fit inside their search ROI, and use the same pixel scale. Blank or constant images are rejected.

`quest_gui` is a stable panel feature inside `quest_list`, not a fish name. `confirm` and `turn_in` identify their distinct buttons. Their ROIs should have the clickable center inside the button. `catch` must be an indicator which clears between catches. `bait_empty` depicts an empty rod bait slot; `bait_zero` depicts the exact zero-count state in `bait_count`. These are separate from “template did not match,” which might mean occlusion or a different item. Inventory source rectangles for each hook/bait must contain the item with their centers on its draggable icon.

The quest tooltip must be fully inside `quest_tooltip` when hovering the center of `quest_paper`. Crop each complete fraction `0/3`, `1/3`, `2/3`, `3/3`. The best progress match must beat the runner-up by `progress_margin`; ambiguous fractions are rejected. If list and tooltip text render at different sizes, enable OCR fallback or adapt separate templates in `vision/perception.py`.

## Compass and mouse calibration

For `compass.mode: north`, crop `compass_n.png`. Set `center: [cx, cy]` relative to the compass ROI, `ring_radius` to the N glyph center's radius, and `ring_tolerance_px` to allow small positional variation. Heading is computed from N's bearing around the widget: top = 0°, left = 90°, bottom = 180°, right = 270° for a conventional rotating compass. `bearing_sign` and `zero_offset_deg` support the opposite rotation convention or a measured fixed offset. Verify all four cardinal headings manually. If the glyph itself changes appearance around the ring, use calibrated top patches instead.

For `compass.mode: ticks`, set `tick_top_rect` relative to the compass ROI and populate `tick_templates`, for example:

```yaml
tick_templates:
  - {heading: 0, path: templates/tick_000.png}
  - {heading: 10, path: templates/tick_010.png}
  # Continue around the full circle with your measured samples.
```

Each crop must identify the tick at the fixed top location using distinguishable nearby labels/context. Repeated identical tick images will be rejected as ambiguous. Use a dense angular sample bank and set `tolerance_deg` at least half the largest sampling gap plus observation error; otherwise random targets may never converge. The N method is preferable when reliable because it produces continuous yaw.

Measure input scale separately:

```powershell
.\.venv\Scripts\python.exe tools\heading_calibrator.py --counts 300
```

Face N, acknowledge the prompt, then focus the game during the five-second countdown. The tool sends exactly +300 relative counts using a smooth path. Read the new heading and enter it in the terminal. It writes the signed `counts_per_degree` while preserving the other YAML text. Select a smaller count if the movement exceeds 150°; there must be no full rotation. F9 cancels movement; F10 exits. Recalibrate after changing sensitivity, acceleration settings, UI/display configuration, or input settings. Validate several target headings before fishing.

Mouse paths use minimum jerk with randomized acceleration, 5–15% interval jitter, and a small corrected overshoot. Camera pitch is unchanged. Normal water headings are sampled uniformly in W→N, avoiding repeated quantized targets. Waiting look-arounds occur at 8–25 seconds, move ±8–20° inside the water arc, then return. Timing variation is an interaction behavior, with no claim about avoiding game enforcement.

## Fishing, progress, and recovery

The implemented flow is:

```text
BOOT → FIND_TASKMASTER → READ_QUESTS → SELECT_TASK → PREP_GEAR
     → FACE_WATER → CAST → WAIT_BITE → FIGHT_BASSLE / FIGHT_REDLINE
     → RESOLVE_CATCH → FACE_WATER, or FIND_TASKMASTER → TURN_IN → READ_QUESTS
```

Every transition is timestamped in the console and rotating `logs/mo2_fish.log` files. The debug window shows current state, selected task, verified count, last compass reading/confidence, and audio meters.

The slow taskmaster scan checks the prompt during its approach toward SE and across a configurable wider sweep. It holds E only after a stable prompt match. Bassle is selected whenever recognized; otherwise Redline Torp must be recognized before selection. The list is read again immediately before clicking. The paper confirms the accepted task. Gear is checked, and a curved drag from the configured inventory source fills a mismatched slot. A failed drag or an unknown loadout pauses.

Each cast waits 0.4–2.2 seconds, holds LMB for 1–2 seconds, then releases. A **new center hit marker** must appear within `T_land_ms`. Landing audio is unused. Cast audio is ignored until `T_ignore_s` after release. Full splash hooks; bubbles do nothing. No splash by `T_bored_s` causes recasting. Fight timeout releases both buttons and credits no catch.

During Bassle fights, LMB stays released and RMB stays held. During Redline Torp fights, LMB stays held; RMB follows only the debounced tension state after the initial hook pulse. A fresh visual catch edge or enabled catch sound releases the buttons and resolves the catch. A quest-completion sound forces a paper check.

`verify_every_n_catches` defaults to one. With a larger value, generic catches are provisional until the paper is read; the displayed verified count never advances from generic catch signals alone. Verification is always forced before a potential third catch or turn-in. Repeated catches without selected-fish progress pause after `max_unverified_catches`. Optional fish audio cannot override the tooltip.

After missing landing markers, the assistant tries 2–3 fresh water headings, then a wider in-arc sweep with varied offsets. If still unsuccessful, it looks for the taskmaster. Default recovery pauses and alerts without walking. A bounded WASD tap is allowed only if `enable_wasd_recovery: true`, water retries failed, **and** the taskmaster scan failed. The default configured tap is 0.15 seconds; the direction is your calibration responsibility. A visible taskmaster with failed water casts always pauses for dock position/pitch inspection. No movement is used in the normal cycle.

## Bait exhaustion and logout

Configure `logout.sequence` using your actual menu bindings and click coordinates, in order. A step has either `key` or `click: [x, y]`, plus optional `wait_s`. Keys supported by the input wrapper are letters, digits, F1–F12, `esc`, `tab`, `enter`, `space`, `shift`, `ctrl`, and `alt`. Do not bind the assistant's F8/F9/F10 to actions. Put enough settling time between menu steps and provide a distinctive logout-success screen crop plus its ROI.

Missing selected bait at its inventory source during preparation, a recognized empty rod bait slot, or a recognized zero stack routes to LOGOUT. Empty/zero checks are repeated before casting and after catches. The configured logout sequence is executed once, then its success screen is checked. On success the program stops. If confirmation is absent, it stops with an explicit failure rather than claiming logout succeeded. Pause/panic always interrupts the sequence and releases inputs.

## Verification and project layout

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .
```

All **36 regression tests passed** on Windows with Python 3.12.14. Tests use synthetic WAVs/PNGs and fake mouse/keyboard backends. They cover correlation, noise/old-event rejection, tension release, compass geometry, configuration errors, quest priority, fight outputs, new catch edges, progress verification, input cancellation, bait exhaustion, and recovery. Importing the Windows backend for its ABI-size check sends no input. A synthetic full-asset preflight exercises startup validation without devices. Live game input, real audio routing, real OCR, and real UI recognition remain calibration checks for your installation.

```text
main.py / config.py / config.yaml   entry point and strict setup checks
audio/                              loopback, detectors, recorder
vision/                             ROI capture, compass, templates, OCR, observations
input/                              SendInput, hotkeys, cancellation, human mouse paths
fsm/states.py                       state machine and recovery
debug.py                            local calibration meter
tools/                              ROI picker, heading calibrator, SFX recorder
tests/                              offline regression suite
sfx/ and templates/                 your captured WAV/PNG assets
```

## Official-server rules / ban risk

Star Vault's EULA prohibits unattended gameplay through macros or bots, and its Terms of Service restrict third-party game tools without written consent. Automation can result in account termination. External pixel/audio perception and timing variation do not establish permission. Review the [official EULA](https://www.mortalonline2.com/eula/) and [Terms of Service](https://www.mortalonline2.com/terms-of-service/) and obtain the operator's permission before using this automation on official servers. Sources checked September 10, 2026.
