"""Field explanations and delayed/clickable help, independent of validation."""
import textwrap
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QToolTip, QWidget


HELP = {
    "profile": "Saved settings and asset paths for this profile. Use the searchable field list to find an entry, edit its YAML value, then Save and Validate to see the remaining issues.",
    "templates": "PNG images used to recognize game UI. Select the matching role in Teach and crop only its distinctive feature from a clear paused frame, then Save to write the image and its search region.",
    "audio.templates": "Reference WAVs grouped by sound role. Use Teach to trim and export short clean takes, or Audio clips to inspect their full paths and listen; each role can hold multiple takes.",
    "counts_per_degree": "Mouse counts needed to turn one degree. Face North, then run Calibrate: send a known mouse move and enter the new compass heading. Recalibrate after changing look sensitivity; the sign records the direction of the turn.",
    "audio_device_name": "WASAPI loopback of the playback endpoint MO2 is routed to, usually VB-CABLE Input. Refresh devices, select that playback loopback, then Use selected device and Test device RMS. Do not choose the recording CABLE Output microphone.",
    "resolution": "Width and height of the live game image in physical pixels. Read the game's display settings or capture dimensions; do not substitute the teaching recording's resolution. Teach crops are mapped into this game size.",
    "game_origin": "Desktop pixel [x, y] of the upper-left corner of the game image, excluding window borders. Measure it in a desktop capture; a monitor left of the primary display can have a negative x.",
    "window_title_contains": "Text identifying the MO2 window. Copy a distinctive part of its title, such as Mortal Online 2; a focused window must match this text before runtime input is allowed.",
    "video_source_resolution": "Decoded width and height of the teaching recording. Opening a video supplies these automatically; keep game capture dimensions in resolution.",
    "video_scale_mode": "How video coordinates map to the game image. Fit preserves proportions and accounts for bars; Stretch scales width and height separately. Compare the video and game aspect ratios before choosing.",
    "teacher_video": "Absolute path of the recording reopened for this profile. Open the desired MP4/MKV in Teach, then Save; only its path is stored, not a copy of the large video.",
    "water_heading_arc_deg": "Allowed water-facing compass arc, in degrees. This workflow requires [270, 360]; use the compass calibration to make those headings accurate.",
    "quest_catch_count": "Number of verified fish required by the quest. This workflow requires 3; author the exact 0/3, 1/3, 2/3 and 3/3 tooltip images.",
    "enable_wasd_recovery": "Whether the existing recovery routine may use a short movement-key nudge after look-only retries fail. Leave false unless the configured key and duration are appropriate for your position.",
    "vision.threshold": "Minimum image-template match score, between zero and one. Compare matches on correct and incorrect frames; raise it to reject lookalikes and recapture blurry templates before lowering it.",
    "vision.progress_margin": "Required score separation between the best progress-number template and the alternatives. Compare 0/3 through 3/3 crops and increase this margin if similar digits are confused.",
    "vision.stable_frames": "Consecutive matching frames needed to accept visual evidence. Use at least two and compare recorded transitions before changing the count.",
    "vision.ocr_enabled": "Enable OCR as an additional text-reading aid. Install Tesseract and set its executable path before enabling; keep the required visual templates.",
    "vision.tesseract_cmd": "Full path of the installed Tesseract executable. Browse its installation folder and copy the path to tesseract.exe; required when OCR cannot find it on PATH.",
    "compass.mode": "North mode finds a distinctive N glyph; ticks mode uses labeled top-patch samples. Choose the mode matching your compass artwork and author the corresponding templates.",
    "compass.center": "Center [cx, cy] of the round compass, measured relative to the compass ROI's top-left. Draw the complete compass first, then measure its center inside that crop.",
    "compass.ring_radius": "Radius in game pixels from the compass center to the N glyph's ring. Measure it inside the compass crop; recalculate after changing game resolution.",
    "compass.ring_tolerance_px": "Allowed distance from the expected compass ring, in pixels. Measure the N glyph's movement in recordings and allow its small variation without including unrelated text.",
    "compass.bearing_sign": "Direction used to convert the N glyph's bearing into player heading: -1 or 1. Turn a little clockwise from North and compare the observed reading to choose the correct sign.",
    "compass.zero_offset_deg": "Degree correction aligning the measured compass bearing with North. Face known North and adjust only the measured systematic offset.",
    "compass.tolerance_deg": "Accepted heading error in degrees. Compare calibrated turns with the compass reading and choose a tolerance larger than its small measurement jitter.",
    "compass.max_correction_steps": "Maximum heading-correction attempts before giving up. Inspect failed turn logs before changing it; fix incorrect calibration instead of masking it with extra attempts.",
    "compass.tick_templates": "Labeled compass top-patch images for ticks mode. Capture distinctive top patches at known headings around the full circle and enter each heading/path pair; identical unlabeled ticks cannot identify absolute heading.",
    "compass.tick_top_rect": "[x, y, width, height] of the top patch inside the compass ROI. In ticks mode, select the strip containing distinctive heading marks; coordinates are relative to the compass crop.",
    "compass.tick_ambiguity_margin": "Minimum score advantage for the winning labeled compass patch. Compare neighboring heading templates and increase the margin when their artwork is too similar.",
    "audio.sample_rate": "Audio sample rate in samples per second. Keep 48000; Teach exports reference clips at this rate and preflight checks it.",
    "audio.hop_ms": "Time between detector updates in milliseconds. Use 20–50 ms; each reference take must leave at least one hop of spare space in the one-second ring.",
    "audio.ring_seconds": "Length of recent audio retained for matching. Keep 1.0 second; trim each reference take to fit with one hop of headroom.",
    "audio.channels": "Number of channels requested from the WASAPI playback loopback. Use the endpoint's stereo configuration, at least two channels; matching downmixes to mono.",
    "audio.stale_seconds": "Maximum allowed age of captured audio before it is considered stale. Compare endpoint dropouts in the logs; fix routing or capture problems rather than extending this to hide them.",
    "audio.tension_on_ms": "How long matching tension audio must persist before tension becomes active. Use 80–200 ms and compare short bends against sustained tension in your recording.",
    "audio.tension_off_ms": "How long tension matching must stop before tension clears. Use 80–200 ms; compare the end of the bend sound to avoid a lingering tension indication.",
    "audio.tension_tail_ms": "How recently a tension match must end. Use short reference takes and inspect the tension meter so an old sound still in the ring cannot hold tension on.",
    "audio.event_cooldown_s": "Minimum gap between repeated events of the same sound role. Compare spacing in the recording and choose a gap that rejects repeated detection of one sound.",
    "audio.min_rms": "Minimum signal level for accepting a matched sound. Use Test device RMS and quiet/background samples to set this above noise without rejecting the real sound.",
    "audio.splash_over_bubble_margin": "How much the splash score must exceed the bubble score. Compare full hook splashes and light nibble bubbles in the meters; collect distinct takes before reducing the margin.",
    "audio.spectral_flux.enabled": "Optional splash gate based on a sudden change in sound energy. Enable only after comparing real splashes and background audio; this gate cannot trigger a splash on its own.",
    "audio.spectral_flux.band_hz": "Low and high frequencies used by the optional splash-energy gate. Inspect the splash recording's frequency content and enter the useful band in hertz.",
    "audio.spectral_flux.threshold": "Minimum energy change for the optional splash gate. Compare the displayed flux on real splashes and background sounds before choosing a threshold.",
    "ui.cursor_toggle_key": "Your in-game binding that makes the cursor available for inventory and quest interactions. Test the binding manually; use null only if no toggle is needed.",
    "ui.close_quest_key": "Key that closes the quest dialog and restores the game view. Test it in MO2 and enter the key name, usually esc.",
    "ui.interact_key": "Your MO2 interaction key for NPC prompts and actions. Verify the binding in game and enter its key name, usually e.",
    "ui.settle_s": "Seconds to let the interface settle after an action. Measure a normal dialog transition in the recording and allow it time to finish.",
    "ui.hover_s": "Seconds to hover before reading an item tooltip. Measure how long your tooltip takes to appear and allow a small margin.",
    "progress.verify_every_n_catches": "How often to read the quest tooltip after catches. Set at least one; use one to verify every catch against the displayed progress.",
    "progress.max_unverified_catches": "Limit on catches accepted without a successful tooltip verification. Keep it at least the verification interval and fix unreadable progress crops instead of allowing large uncertainty.",
    "logout.sequence": "Ordered keys or clicks that log out from your current UI. Test the sequence manually and enter steps such as {key: esc, wait_s: 0.7} or {click: [x, y], wait_s: 0.7}; click coordinates are relative to game_origin.",
    "logout.success_template": "PNG of a distinctive feature visible only after successful logout. Author the logout_success role in Teach, then enter/verify its PNG path here; keep the matching success_roi around that feature.",
    "logout.success_roi": "Search rectangle for the logged-out confirmation or character-selection feature. Select its screen region after logout; avoid artwork also visible while playing.",
    "logout.timeout_s": "Seconds allowed for the logout confirmation to appear. Measure the logout transition and include its normal delay.",
    "debug.window": "Whether the runtime debug view is enabled. Turn it on when inspecting recognition and off when you no longer need that view.",
    "debug.log_file": "Path for the runtime log file. Choose a writable location, usually logs/mo2_fish.log within the profile's working context, and use the log when diagnosing recognition.",
    "teacher.in": "Selection start on the full file timeline, in seconds. Press In at playhead while listening, enter a time, or left-drag the waveform; middle-click seeks the video.",
    "teacher.out": "Selection end on the full file timeline, in seconds. Stop just after the sound ends; the clip must be at least 20 ms and shorter than the detector ring minus one hop.",
    "teacher.waveform_window": "Seconds of waveform audio to extract beginning at the video playhead. Choose a short window containing the event; extraction is limited to 120 seconds.",
    "teacher.playback": "Use the timeline or middle-click the waveform to seek. Space plays/pauses; the gear sets playback speed and the Left/Right skip amount. Mute affects file playback only.",
    "teacher.audio_role": "Label the sound you selected: full hook splash, light nibble bubbles, tension, quest completion or catch. Export adds another short take; Ignore writes nothing.",
    "override": "Optional request to perform fresh validation at Start. Type OVERRIDE exactly only when intended; required assets and configuration checks still apply.",
    "overlay.visible": "Show the read-only ROI overlay on the live game image. It displays the saved profile's game coordinates and does not accept game clicks.",
    "overlay.labels": "Show each saved ROI's name on the live overlay. Use these labels to identify which region needs adjustment in Teach.",
    "calibration.counts": "Known positive horizontal mouse-count movement used by calibration. Choose enough counts for a visible 2–150 degree turn from North; Arm and Validate are required before Send test movement.",
    "calibration.heading": "Compass heading observed after the calibration movement from North. Read the new heading in game and enter degrees from 0 to 359.99, then Save measured scale.",
}

ROLE_HELP = {
    "compass": "Draw the complete round compass widget, including its ring. Use the separate compass_north role for a tight crop of the N glyph in north mode.",
    "compass_north": "Crop only the distinctive N glyph on the compass ring. First draw the full compass ROI; exclude other letters and surrounding scenery from this template.",
    "hit_marker": "Use a frame just after a cast lands in water. Crop only the transient marker at the center of the screen, not the permanent crosshair.",
    "interact_prompt": "Face the fishing taskmaster and crop the distinctive interaction prompt. Include its stable identifying feature, not nearby scenery or unrelated NPC text.",
    "quest_list": "Draw the quest-list panel containing the fishing quest entries. Include the region where both fish names can appear; individual fish-name templates are separate roles.",
    "inventory": "Draw the open inventory grid that contains your hooks and bait. Use the same inventory position for every task and keep unrelated screen areas outside the rectangle.",
    "hook_slot": "Draw the equipped rod's hook slot in its stable screen position. Capture each task's hook icon separately with the matching hook source role.",
    "bait_slot": "Draw the equipped rod's bait slot in its stable screen position. Capture each task's bait icon separately with the matching bait source role.",
    "quest_paper": "Draw the inventory location occupied by the fishing quest paper. Include the paper area used for hovering, while its tooltip is a separate role.",
    "quest_tooltip": "Hover the quest paper and draw the complete tooltip panel. It must include the fish name and progress text; capture the exact progress digits separately.",
    "confirm_button": "Crop the quest confirmation button while that dialog is open. Include its distinctive button artwork or text and avoid adjacent controls.",
    "turn_in_button": "Crop the quest turn-in button after the quest is complete. Use its stable identifying text or artwork, not another nearby action button.",
    "bait_empty": "Crop the empty rod bait-slot appearance. Show a truly empty slot, not a missing inventory icon; compare it with the equipped bait state.",
    "bait_count": "Crop the visible zero stack count tightly when bait is exhausted. Keep nearby digits and unrelated item counts out of the rectangle.",
    "catch_indicator": "Crop a distinctive confirmation that appears only for a successful catch. Use a newly appearing indicator, not the cast hit marker or permanent HUD artwork.",
    "logout_success": "Crop a distinctive logged-out or character-selection feature after a successful logout. Its ROI and PNG must identify that state unambiguously.",
}
for name in ("bassle", "redline_torp"):
    label = "Bassle" if name == "bassle" else "Redline Torp"
    ROLE_HELP[name] = f"Crop the {label} name tightly in the quest panel or tooltip. Include the whole name and exclude other quest names or changing progress digits."
    for kind in ("hook", "bait"):
        ROLE_HELP[f"{kind}_{name}_source"] = f"Open inventory and crop the {kind} item used for {label}. Include the item icon and its clickable inventory location; do not use the other task's item."
for count in range(4):
    ROLE_HELP[f"progress_{count}"] = f"Hover the quest paper when progress is exactly {count}/3. Crop the complete {count}/3 text tightly, excluding the fish name and tooltip border."
for role, text in ROLE_HELP.items():
    HELP[f"rois.{role}"] = text
HELP["compass.north_template"] = ROLE_HELP["compass_north"]

SOUND_HELP = {
    "splash": "A short WAV of the full hook splash when the cast lands in water. Include the main impact and its brief tail; exclude light nibble bubbles. Multiple distinct takes are allowed.",
    "bubble": "A short WAV of light nibble bubbles, not the full hook splash. Listen and trim around one clean event; multiple distinct takes are allowed.",
    "tension": "A short reference of the rod tension or bending sound. Start with 40–120 ms and keep silence or unrelated splashes out; multiple takes can cover its variations.",
    "quest_complete": "The sound played when the fishing quest completes. Isolate its distinctive section in the recording and export a short take without speech or other effects.",
    "catch": "A distinct successful-catch sound. Trim it separately from the cast splash and nibble bubbles; multiple short takes are allowed.",
    "ignore": "Marks an unwanted or ambiguous audio selection. Ignore exports no detector WAV and does not add a reference take.",
}
for role, text in SOUND_HELP.items():
    HELP[f"audio.templates.{role}"] = text
    HELP[f"audio.thresholds.{role}"] = f"Minimum {role} match score, between zero and one. Compare correct and incorrect examples in the audio meters; the best score across all reference takes is used."

_TIMING = {
    "T_land_ms": "Milliseconds allowed for a cast to land; measure release-to-splash time.",
    "T_ignore_s": "Seconds to ignore early post-cast noise; compare it with the splash tail.",
    "T_fight_max_s": "Maximum fight duration in seconds; compare your normal fights before choosing a limit.",
    "T_bored_s": "Seconds without a bite before recovery; compare quiet stretches in the recording.",
    "cast_hold_s": "Minimum and maximum cast-button hold time in seconds; measure a normal cast.",
    "pre_cast_s": "Minimum and maximum delay before casting, in seconds; allow the previous action to settle.",
    "hook_hold_s": "Minimum and maximum hook-action hold time in seconds; measure a normal hook input.",
    "interact_hold_s": "Minimum and maximum interaction hold time in seconds; match the in-game prompt.",
    "gui_timeout_s": "Seconds to wait for the expected game dialog; measure how long it normally takes to appear.",
    "resolve_s": "Seconds allowed for catch-result evidence to settle; inspect the end of a recorded catch.",
    "look_interval_s": "Minimum and maximum seconds between idle looks; choose the desired idle interval.",
    "look_angle_deg": "Minimum and maximum size of idle looks in degrees; use calibrated headings to judge the angle.",
    "poll_s": "Seconds between runtime checks; compare responsiveness and CPU use before changing the default.",
}
for key, text in _TIMING.items():
    HELP[f"timing.{key}"] = text + " Edit the saved profile YAML; time ranges must be positive and ascending."
_RECOVERY = {
    "direct_cast_tries": "Number of look-only direct-cast retries. Use 2–3 and inspect failed casts before changing it.",
    "sweep_cast_tries": "Number of cast retries across the water arc. Use 1–6 and compare the landing locations.",
    "npc_heading_deg": "Approximate compass heading toward the taskmaster. Face the NPC manually and record its heading.",
    "npc_scan_half_arc_deg": "Degrees to scan on either side of the taskmaster heading. Measure the possible NPC direction from your fishing spot.",
    "npc_scan_step_deg": "Degrees between NPC search checks. Choose a positive step no larger than 15 so the interaction prompt is not skipped.",
    "scan_step_s": "Minimum and maximum pause between scan steps, in seconds. Give the interaction prompt enough time to appear.",
    "wasd_key": "Movement key for optional last-resort recovery: w, a, s or d. Verify which direction leaves your fishing position usable.",
    "wasd_hold_s": "Duration of the optional movement nudge in seconds. Test a brief movement manually; use a positive duration no larger than 0.5 seconds.",
    "max_wasd_attempts": "Maximum number of optional movement nudges after look-only recovery fails. Keep it small and verify the resulting position manually.",
}
for key, text in _RECOVERY.items():
    HELP[f"recovery.{key}"] = text
for task in ("bassle", "redline_torp"):
    for field in ("fish_name", "fight", "precedence", "always_available"):
        HELP[f"tasks.{task}.{field}"] = f"The fixed {field.replace('_', ' ')} definition for {task.replace('_', ' ')}. Keep the supplied task definition; preflight rejects changes to these workflow constants."
    for kind in ("hook", "bait"):
        for suffix in ("template", "inventory_rect"):
            HELP[f"tasks.{task}.{kind}_{suffix}"] = ROLE_HELP[f"{kind}_{task}_source"]
    HELP[f"tasks.{task}.audio_template"] = "Optional sound identifying this particular fish. Record a distinct short 48 kHz WAV and enter its path; leave null when no reliable fish-specific sound exists."


def help_text(key):
    if key in HELP:
        return HELP[key]
    if key.startswith("teacher_boxes."):
        return ROLE_HELP.get(key.split(".")[-1], "Saved game-space teaching rectangle. Select its role and redraw it in Teach, then Save.")
    return (f"{key} is stored in this profile's YAML. Select its entry in the Profile field list to edit the matching YAML line, "
            "then Save and Validate. For a setting without a dedicated editor, consult the profile README for its expected format.")


class HelpButton(QToolButton):
    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.setText("?")
        self.setAccessibleName(f"Help for {key}")
        self.setFixedSize(22, 22)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QToolButton { border:1px solid #536c84; border-radius:10px; padding:0; color:#9fccec; }")
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.show_help)
        self.clicked.connect(self.show_help)

    def show_help(self):
        self.timer.stop()
        text = textwrap.fill(help_text(self.key), width=65)
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), text, self, self.rect(), 20000)

    def enterEvent(self, event):
        self.timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.timer.stop()
        super().leaveEvent(event)


def help_label(label, key):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QLabel(label))
    layout.addWidget(HelpButton(key))
    layout.addStretch()
    return widget
