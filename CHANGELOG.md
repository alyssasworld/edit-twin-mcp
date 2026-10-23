# Changelog

## Unreleased
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
