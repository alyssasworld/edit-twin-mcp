# ✂️ Edit Twin MCP

**Your editing twin for Final Cut Pro.** Edit Twin lets [Claude Code](https://claude.com/claude-code) rough-cut your Final Cut Pro projects for you: it cuts retakes, "um"s and dead air, adds your colour look and dialogue sound, and hands you back a new project to finish. Then it **learns from how you finish it**, so each rough cut gets closer to how you'd have cut it yourself.

> Edit Twin is an independent, free, open-source project. It is not affiliated with, endorsed by, or sponsored by Apple Inc. Final Cut Pro is a trademark of Apple Inc.

---

## What it does

Open a project in Final Cut Pro, then in Claude Code say:

> *rough cut my open project*

1. **Reads your edit.** It exports the project and transcribes the speech on your Mac. Your video and audio never leave your Mac; Claude sees the transcript and timeline details it needs to plan the cut (see [Privacy](#privacy)).
2. **Finds what to cut**: retakes where you said a line twice (it keeps the better take), stutters ("to new to new tech"), um/uh fillers and pauses.
3. **Shows you the plan and waits.** Nothing changes until you say OK.
4. **Cuts it, then adds your style**: your saved colour look on an adjustment clip over the whole edit, and your dialogue sound (gain plus Final Cut's Loudness effect).
5. **Sends it back to Final Cut Pro as a new project.** Your original is never touched.

It adapts to the format. **Reels and shorts** get tight jump cuts and a check that the first 3 seconds work as a hook. **Landscape vlogs** get their own pacing (looser at first, then learned from your hand edits separately from reels), and quiet B-roll, scenery and music moments are never cut as "silence".

## Captions 💬

> *caption my open project*

Captions are timed to every word on your edit and added as Final Cut Pro titles, in **your** caption styles:

- **Short form**: one word (or a few) pops on as you say it, e.g. in a display font at the top of a vertical reel.
- **Long form**: subtitle lines near the bottom, measured in your font so each line fits the frame and stays comfortable to read.

Set a style up once, either by **styling one title by hand** in Final Cut (Edit Twin copies its font, size, colour, position and any animation exactly), or by describing it (*"one word at a time, Futura, lowercase, near the top, for my reels"*). Any font installed on your Mac works; Edit Twin reads the real font names from the font files and remembers the fonts you use, so next time *"caption it"* is enough. Misheard words are fixed before anything is added, and `export_srt` writes an `.srt` for YouTube closed captions.

## How it learns your style ✨

After you polish an Edit Twin rough cut by hand, say:

> *learn from my edit*

Edit Twin compares its version with yours, frame by frame against the original footage, and tells you what you did differently:

- which **words or retakes** you removed or put back,
- how much **breathing room** you leave before and after words,
- your **audio chain** (gain, Loudness, Noise Removal, EQ),
- the **look** on your adjustment clip,
- the **fonts** in your titles.

It then **calibrates its pause detection to match your own trims** and saves everything as your style. On the reel Edit Twin was developed with, the calibrated settings overlapped with the creator's own hand trims **72%**, compared with **13%** for the old loose settings. The rest were creative word-level choices, and those you keep making yourself.

Your style (including caption styles and fonts) lives on your Mac in `~/Movies/Edit Twin/style.json` and `~/Movies/Edit Twin/looks/`.

## Install

You need:
- a Mac with **Final Cut Pro** (developed on 11.2; FCPXML 1.11 or newer). Apple Silicon is recommended; Intel Macs work but transcribe more slowly,
- **[Claude Code](https://claude.com/claude-code)** (needs a Claude subscription or API key),
- **[Homebrew](https://brew.sh)**, which the installer uses to get ffmpeg and Python 3.10+ if they're missing,
- **git** (macOS offers to install it the first time you run `git`),
- about **3 GB of free disk space** (Python packages plus the speech model).

Then, in Terminal:

```bash
git clone https://github.com/alyssasworld/edit-twin-mcp.git
cd edit-twin-mcp
./install.sh
```

The installer:
- installs **ffmpeg** and **Python 3.12** with Homebrew, only if they're missing,
- sets everything up in a private folder (`.venv` inside the cloned folder), so it won't touch your other Python stuff,
- installs the Python packages there: the MCP SDK, plus the speech-to-text engine (`mlx-whisper` on Apple Silicon, `faster-whisper` on Intel) and its dependencies (on Apple Silicon these include PyTorch, NumPy and SciPy, roughly 1 GB),
- registers Edit Twin with Claude Code for all your projects (`claude mcp add --scope user`),
- copies the `/edit-twin-rough-cut` skill to `~/.claude/skills/edit-twin-rough-cut` (re-running the installer updates it),
- creates the working folder `~/Movies/Edit Twin`.

The first transcription downloads a free speech model from Hugging Face, once: [`mlx-community/whisper-large-v3-turbo`](https://huggingface.co/mlx-community/whisper-large-v3-turbo) on Apple Silicon, or `large-v3-turbo` for faster-whisper on Intel (about 1.5 GB).

### One-time Mac permissions

Open **System Settings → Privacy & Security** and allow the app you run Claude Code in (Terminal, iTerm, VS Code…):

| Setting | Why |
|---|---|
| **Accessibility** | so Edit Twin can click Final Cut Pro's menus (Export / Import XML) |
| **Automation** → Final Cut Pro, System Events | macOS asks the first time; click **Allow** |
| **Screen & System Audio Recording** *(optional)* | only for the screenshot tool |

Quit and reopen that app, start `claude`, and type `/mcp`. You should see **edit-twin** connected.

If your footage is on an external drive, keep it plugged in while editing. macOS may also ask to let Terminal access removable volumes; click Allow.

## Things to ask

- *"Rough cut my open project. Show me the cuts first."*
- *"Find my retakes and tell me which take is better."*
- *"Remove the pauses but keep it a bit looser, it's a vlog."*
- *"Save the look on my adjustment clip as `warm_vlog` and use it by default."*
- *"Put coffee.mov over 0:42 for 3 seconds and add music.m4a under everything at −18 dB."*
- *"Add chapter markers at each topic change."*
- *"Caption this reel one word at a time in Futura, lowercase, near the top. Make that my reel style."*
- *"Save the title at 0:03 as my vlog subtitle style."*
- *"Caption it"* (uses your saved style for the format) · *"Export an SRT for YouTube."*
- *"How loud is this edit?"*
- *"Learn from my edit."*

## How it works (for the curious)

Final Cut Pro doesn't let other apps edit a timeline directly, so Edit Twin uses Apple's official interchange format, **FCPXML**:

```
Final Cut Pro ──Export XML──▶ Edit Twin edits the timeline ──Import XML──▶ new project in Final Cut Pro
```

- **Frame-accurate.** All maths uses exact rational time, the same way FCPXML stores it, so cuts never drift.
- **Non-destructive.** Effects, keyframes, colour, roles and metadata Edit Twin doesn't touch are carried through as they are. Connected clips (B-roll, titles, music) survive cuts underneath them.
- **Checked.** Every result is validated against Apple's FCPXML DTD (from inside Final Cut Pro) before import, and every edit can be undone.
- **Local speech-to-text** with Whisper (`mlx-whisper` on Apple Silicon), prompted to transcribe verbatim, so it usually keeps the fillers and repeats you want to cut.
- **Style learning** compares the kept source ranges of the two versions, uses the audio to tell pauses from words, and grid-searches pause settings to match your trims.
- **Live control** of the app (menus, shortcuts, playhead) uses AppleScript / System Events.

<details>
<summary><b>All tools</b></summary>

| Area | Tools |
|---|---|
| Final Cut Pro app | `fcp_status`, `fcp_launch`, `fcp_library_overview`, `fcp_export_project`, `fcp_import_xml`, `fcp_menu`, `fcp_shortcut`, `fcp_go_to`, `fcp_screenshot` |
| Open & read | `find_fcpxml_files`, `open_timeline`, `get_timeline`, `list_markers`, `probe_media` |
| Speech & sound | `transcribe`, `find_repeats`, `find_silences`, `remove_silences`, `measure_loudness`, `normalize_audio`, `set_volume` |
| Your style | `get_style`, `update_style`, `learn_from_edit`, `save_look`, `apply_look`, `save_audio_style`, `apply_audio_style` |
| Captions | `add_captions`, `remove_captions`, `save_caption_style`, `list_fonts`, `export_srt` |
| Edit | `cut_ranges`, `keep_ranges`, `split_clip`, `delete_clip`, `trim_clip`, `move_clip`, `add_marker`, `remove_markers`, `add_title`, `edit_title`, `insert_media`, `enable_clip`, `copy_effects`, `remove_effects`, `fit_to_timeline`, `undo`, `redo` |
| Check & finish | `validate_timeline`, `preview_frame`, `render_preview`, `save_timeline`, `send_to_final_cut` |

</details>

## Limits (honest list)

- **Beta.** So far it's been used on one creator's Mac (Final Cut Pro 11.2, Apple Silicon) with single-camera talking-head footage. I will refine this as I go, so please report what breaks!
- **Mac only**, and the live app controls need the permissions above. If menu automation fails on your setup, export with File → Export XML… yourself and give Claude the file path.
- Edits apply to the **primary storyline**. Speed-changed (retimed) clips aren't cut inside. Multicam, compound and synced clips are skipped by pause and silence detection.
- A **look** is saved as the effects stack on an adjustment clip. Paid plugins (e.g. colour plugins) and LUT files must be installed on the Mac that uses it.
- `render_preview` is a rough preview (straight cuts, no effects or titles). The real result is in Final Cut Pro.
- **Captions** are regular titles (one per caption), so they're easy to tweak but a long video can have hundreds of them. A caption style described in words uses Final Cut's Basic Title; copy a title you styled yourself for animation or effects. A style copied from a title keeps that title's exact size and position, so check it when using it on a project with a different resolution.
- Whisper occasionally stretches word timings over pauses, so always check the plan before approving.

## Files and disk space

Edit Twin never changes your Final Cut Pro library or media directly. It writes:

| Where | What | Safe to delete? |
|---|---|---|
| `~/Movies/Edit Twin/*.fcpxml(d)` | exports of your project, and each rough cut it sends to Final Cut Pro | yes |
| `~/Movies/Edit Twin/* preview.mp4` | low-res previews from `render_preview` | yes |
| `~/Movies/Edit Twin/.transcripts/` | cached transcripts, so a clip is only transcribed once | yes (they're redone when needed) |
| `~/Movies/Edit Twin/style.json`, `looks/` | your learned style and saved looks | only to reset your style |
| `~/Movies/Edit Twin/.sent.json` | which rough cut was sent for which project, used by `learn_from_edit` | yes |
| macOS temp folder | screenshots and preview frames (`edit_twin_*.png`) | macOS clears these |
| `~/.cache/huggingface/` | the speech model (~1.5 GB) | yes (it's downloaded again when needed) |
| `.venv/` in the cloned folder | Python and packages (~1 GB) | removes Edit Twin |
| `~/.claude/skills/edit-twin-rough-cut/`, `edit-twin-captions/` | the rough-cut and captions skills | yes |
| `~/Movies/Edit Twin/*.srt` | subtitle files from `export_srt` | yes |

Settings you can pass as environment variables when registering:

| Variable | Default | What it does |
|---|---|---|
| `EDIT_TWIN_DIR` | `~/Movies/Edit Twin` | working folder |
| `HF_HOME` | `~/.cache/huggingface` | where the speech model is stored |
| `EDIT_TWIN_WHISPER_MODEL` | see above | a different Whisper model (e.g. a smaller one for older Macs) |
| `EDIT_TWIN_DTD_DIR` | inside Final Cut Pro | folder with Apple's `FCPXMLv1_*.dtd` files, if Final Cut Pro isn't in `/Applications` |

For example, to keep everything on an external drive, run this from the cloned folder:
```bash
claude mcp remove edit-twin --scope user
claude mcp add --scope user -e EDIT_TWIN_DIR="/Volumes/MyDrive/Edit Twin" -e HF_HOME="/Volumes/MyDrive/.huggingface" edit-twin -- "$PWD/.venv/bin/edit-twin-mcp"
```

## Privacy

- Transcription, pause detection, loudness and previews run **on your Mac**. Your video and audio files are never uploaded.
- To plan and make edits, Claude receives what the tools return: transcripts, timeline details (clip names, timings, effects and media file paths), and the screenshots or preview frames you ask for. That goes to Anthropic like the rest of your Claude Code conversation.
- Edit Twin itself sends nothing anywhere. The only network access is the one-time model download from Hugging Face.

## Uninstall

```bash
claude mcp remove edit-twin --scope user
rm -rf ~/.claude/skills/edit-twin-rough-cut ~/.claude/skills/edit-twin-captions
rm -rf /path/to/edit-twin-mcp            # the cloned folder, including .venv
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo   # Apple Silicon model
```
Delete `~/Movies/Edit Twin` too if you don't want to keep your style and exports.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[transcribe]' pytest
.venv/bin/pytest -q tests     # self-contained: generates its own footage, no Final Cut Pro needed
```

The same tests run on GitHub Actions for every pull request (`.github/workflows/tests.yml`), on macOS and Linux. Apple's DTD check only runs locally on a Mac with Final Cut Pro.

## Support

Edit Twin is a free, as-is side project. Issues and pull requests are welcome, but I can't promise to fix every setup. If it saves you editing time, a ⭐ or a tag on socials (@loveaction) would be nice!

## License

[MIT](LICENSE)
