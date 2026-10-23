# Changelog

## Unreleased
- Captions: `add_captions` places word-timed captions from the transcript as Final Cut Pro titles. Short form shows one word (or a few) at a time; long form shows subtitle lines measured in the chosen font to fit the frame (42 characters per line by default). Misheard words can be fixed first (`fixes`, `dry_run`).
- Caption styles: `save_caption_style` saves your styles, copied from a title you styled by hand (font, size, colour, position, animation) or described in words, with a default for short and long videos. Stored in `style.json`; no fonts are built in.
- Fonts: `list_fonts` finds installed fonts by loose name and reads their real family and face names from the font files. Fonts in your caption styles and in your hand-edited titles (`learn_from_edit`) are remembered as your fonts.
- `export_srt` writes an .srt of the edit for YouTube closed captions; `remove_captions` removes them again.
- New `/edit-twin-captions` skill, installed by `install.sh`.
- Fix: Apple's DTD check failed whenever Final Cut Pro's path contained spaces (always, in a normal install), so rough cuts were never imported.
- Installer: finds Homebrew's Python on Intel Macs; re-running it updates the rough-cut skill.
- README: full list of prerequisites, downloads, files written, settings, privacy and uninstall.

## 0.1.0 (beta)
- Rough cuts in Final Cut Pro through FCPXML round trips: cut ranges, split, trim, move, delete.
- Local Whisper transcription with word timings that follow your edits; retake, stutter and filler finder.
- Pause removal with separate breathing room before and after speech.
- Style learning: compare Claude's cut with your hand-finished version, calibrate pause settings, save your audio chain and look.
- Saved looks applied as a full-length Adjustment Clip; audio chain (gain + Loudness) applied to dialogue.
- Loudness metering (LUFS), previews, markers, titles, B-roll and music, copying effects.
- Live app control: export/import XML, menus, shortcuts, playhead, screenshots.
