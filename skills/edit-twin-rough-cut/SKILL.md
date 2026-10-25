---
name: edit-twin-rough-cut
description: Rough-cut the project open in Final Cut Pro with the Edit Twin MCP — retakes, um/uh, pauses, your saved look and sound — adapting to reels/shorts or landscape vlogs.
---

# Rough cut with Edit Twin

Use the `edit-twin` MCP tools. The user's original project is never modified: the result is imported as a NEW project next to it. Times are seconds on the current timeline. Clip refs like "4" (storyline clip) and "4.0" (connected item) shift after every edit, so re-read `get_timeline` before using refs.

## 1. Get the project and the user's style
1. `get_style`: saved defaults (pause settings, audio chain, default look, notes learned from their edits). Follow the notes.
2. `fcp_export_project`. If it warns the project is empty, ask the user to select the right project in Final Cut's Browser (or click in its timeline) and retry. If export fails, ask them to use File > Export XML… and give you the path, or use `find_fcpxml_files` + `open_timeline`.
3. `get_timeline`, then decide the format from `summary.format` (width/height) and duration, unless the user said which:
   - **Reel / short**: vertical (height > width), or under ~3 minutes.
   - **Vlog / landscape**: horizontal, or longer pieces with B-roll, scenery, music or ambient moments.
   Tell the user which format you picked in one line.

## 2. Analyse (change nothing yet)
1. `transcribe`, with `vocabulary` set to names and jargon likely in the video.
2. `find_repeats`: stutters, retakes and um/uh fillers. It doesn't catch everything: read the transcript (`include_words`) sentence by sentence for false starts and abandoned fragments ("she wanted her… and as well as that"), and for gaps of ~0.3s or more between words where the transcript missed an um or a breath. For each retake, compare the takes and choose. Keep the one with clearer delivery, no stumbles and a clean ending; if they're equal, keep the later take. When removing a retake, also drop a leftover connector ("and", "so") at the start of the kept take if it no longer reads naturally.
3. Pauses, depending on format:
   - **Reel**: `find_silences` with `format` "short" (the saved reel settings).
   - **Vlog**: `find_silences` with `format` "long". If `get_style` has no `silence_long` yet (no vlog learned), add looser overrides (`min_silence` 0.6, `keep_after_speech` 0.15, `keep_before_speech` 0.15); once it exists, use it as saved. Only propose pauses inside talking sections (between transcript words). Never cut silent stretches that are B-roll, scenery, music or reaction shots; use `get_timeline` and `preview_frame` when unsure.
4. **Stop and show a short plan**: retakes (quote both takes, say which you'd keep and why), how many fillers, how many pauses and how many seconds they add up to, and the expected new length. Wait for the user's OK or changes.

## 3. Cut (after approval)
1. `cut_ranges` for the approved retakes and fillers.
2. Reels: `remove_silences` with `format` "short". Vlogs: `cut_ranges` with only the approved pause ranges.
3. Reels: check that the first ~3 seconds work as a hook, i.e. the first words state the topic or a surprising claim. If a stronger hook line comes later, suggest it, but don't move it without asking. Flag it if the reel runs over 90 seconds.

## 4. Look and sound
1. If there's an adjustment clip with effects, run `fit_to_timeline`. Otherwise, if the style has a default look, run `apply_look`.
2. If the style has an audio chain, run `apply_audio_style`. If not, run `measure_loudness` and suggest a gain (social video usually sits around -14 LUFS), or offer `normalize_audio`.
3. Leave music and connected audio alone unless asked.

## 5. Check and send
1. `validate_timeline`, then `render_preview`. Tell the user where the preview is and the new length.
2. `send_to_final_cut` with the project name "<original name> (rough cut)".
3. Summarise in a few lines: what was cut (retakes, fillers, pauses, seconds saved) and the look and sound applied.
4. Finish by suggesting that, if they polish the edit by hand, they ask Claude to `learn_from_edit` (with `save=true` and `look_name`) so the next rough cut matches their style. Pass `format` ("short" or "long") so a vlog's pause settings are saved separately from the reel ones.

## Rules
- Never change anything before the user approves the plan in step 2.4.
- Never edit their original project; always send a new one.
- Media on an external drive must be plugged in.
- If a tool fails because of macOS permissions, point them to System Settings > Privacy & Security (Accessibility, and Automation for the app running Claude Code).
