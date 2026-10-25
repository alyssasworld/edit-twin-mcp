"""MCP server that lets Claude Code edit Final Cut Pro projects.

Round trip:  Final Cut Pro --Export XML--> timeline tools edit it --> Import XML --> Final Cut Pro
plus live app control (menus, shortcuts, playhead, screenshots) on the Mac.
"""

from __future__ import annotations

import datetime as _dt
import functools
import glob
import os
import pathlib
import re
import subprocess
import tempfile
from fractions import Fraction
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import fcp_app, media, style
from .fcpxml import EditError, Timeline
from .timecode import secs, snap, to_timecode

WORK_DIR = os.path.expanduser(os.environ.get("EDIT_TWIN_DIR", "~/Movies/Edit Twin"))

INSTRUCTIONS = f"""
Edit Final Cut Pro projects for the user.

How it works: Final Cut Pro can't be edited directly by scripts, so the workflow is
1) get the project as FCPXML: `fcp_export_project` (exports the project open in the FCP timeline)
   or the user exports it via File > Export XML…; `find_fcpxml_files` lists recent exports;
2) `open_timeline` on that file, then read it with `get_timeline` / `list_markers`;
3) edit with the timeline tools (cuts, silence removal, markers, titles, B-roll, volume, copying
   effects such as a colour grade between clips). Every edit can be undone with `undo`;
4) `send_to_final_cut` saves a NEW project next to the original and imports it into Final Cut Pro.
   The user's original project is never modified.

Times are seconds from the start of the timeline (decimals OK) or timecode "MM:SS", "HH:MM:SS:FF".
Clip refs: "4" = the 5th clip in the primary storyline, "4.0" = first connected item on it
(B-roll, title, music). Check `get_timeline` for refs after any edit, since indexes shift.
The user's style (get_style) is learned from their own hand edits: use its defaults for
pause removal, apply_look for their colour look, apply_audio_style for their dialogue
sound and add_captions with their saved caption styles (save_caption_style, list_fonts).
After they finish an edit by hand, offer learn_from_edit.
Before destructive bulk edits (e.g. remove_silences) run the matching dry-run tool
(`find_silences`) and tell the user what will change. Use `preview_frame` or
`render_preview` to check results. Live app tools (`fcp_*`) only work on the Mac running
Final Cut Pro and need macOS Accessibility permission. Working folder: {WORK_DIR}
""".strip()

mcp = MCPServer("edit-twin", instructions=INSTRUCTIONS)

_state: dict = {"tl": None}


def tool(fn):
    """Register a tool and pass error messages through to Claude so it can react
    (e.g. a wrong clip ref, missing permission, file not found)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (EditError, ValueError, FileNotFoundError, PermissionError, TimeoutError,
                RuntimeError, subprocess.CalledProcessError) as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    return mcp.tool()(wrapper)


def _tl() -> Timeline:
    if _state["tl"] is None:
        raise EditError("No timeline open. Call open_timeline with an exported .fcpxml/.fcpxmld first.")
    return _state["tl"]


def _edit(fn, *args, **kwargs) -> dict:
    tl = _tl()
    tl.checkpoint()
    try:
        rep = fn(*args, **kwargs)
    except Exception:
        tl.undo()
        tl.redo_stack.clear()
        raise
    out = rep.as_dict()
    out["duration_now"] = secs(tl.duration())
    problems = tl.check()
    if problems:
        out["check_problems"] = problems[:10]
    return out


def _seconds_list(ranges) -> list[tuple]:
    out = []
    for r in ranges:
        if isinstance(r, dict):
            out.append((r["start"], r["end"]))
        else:
            out.append((r[0], r[1]))
    return out


# --------------------------------------------------------------------------- #
#  Live Final Cut Pro app
# --------------------------------------------------------------------------- #

@tool
def fcp_status() -> dict:
    """Is Final Cut Pro installed/running, its version, and whether it is frontmost."""
    return fcp_app.status()


@tool
def fcp_launch() -> dict:
    """Open Final Cut Pro (or bring it to the front)."""
    return fcp_app.launch()


@tool
def fcp_library_overview() -> list:
    """List open libraries, their events and projects (read from Final Cut Pro)."""
    return fcp_app.library_overview()


@tool
def fcp_export_project(filename: str | None = None, open_after: bool = True) -> dict:
    """Export the project currently open in Final Cut Pro's timeline as FCPXML
    (drives File > Export XML…), then open it for editing. The user should have the
    project open in the timeline first."""
    name = filename or f"export {_dt.datetime.now():%Y-%m-%d %H%M%S}"
    path = fcp_app.export_xml(WORK_DIR, name)
    result: dict = {"exported": path}
    try:
        check = Timeline(path)
        if not check.items():
            result["warning"] = (f"The exported project '{check.project.get('name')}' is EMPTY. Final Cut "
                                 "exports the project selected in the Browser, so select the project you "
                                 "want (or open it in the timeline and click in the timeline) and try again.")
            open_after = False
    except EditError as e:
        result["warning"] = str(e)
        open_after = False
    if open_after:
        result["timeline"] = open_timeline(path)
    return result


@tool
def fcp_import_xml(path: str) -> dict:
    """Import an .fcpxml / .fcpxmld into Final Cut Pro (File > Import > XML)."""
    return {"imported": fcp_app.import_xml(path),
            "note": "Final Cut Pro may ask which library to use; the new project appears in its event."}


@tool
def fcp_menu(path: list[str]) -> dict:
    """Click a Final Cut Pro menu item, e.g. ["Modify", "Balance Color"] or
    ["File", "Share", "Master File (default)…"]. With a single entry (["Modify"])
    it lists that menu's items instead of clicking."""
    if len(path) == 1:
        return {"menu": path[0], "items": fcp_app.list_menu(path)}
    return {"clicked": fcp_app.click_menu(path)}


@tool
def fcp_shortcut(action: str | None = None, key: str | None = None, modifiers: list[str] | None = None) -> dict:
    """Press a Final Cut Pro shortcut in the app. Either a named `action`
    (play_pause, blade, blade_all, add_marker, undo, redo, next_edit, previous_edit,
    select_clip_under_playhead, set_range_start, set_range_end, trim_start_to_playhead,
    trim_end_to_playhead, zoom_to_fit, render_all, detach_audio, add_default_transition,
    connect_title_basic, focus_timeline, ...) or a raw `key` + `modifiers`
    (command/option/control/shift). Acts on whatever is selected in FCP."""
    if action:
        return {"pressed": fcp_app.shortcut(action)}
    if key:
        return {"pressed": fcp_app.press(key, modifiers)}
    return {"actions": sorted(fcp_app.SHORTCUTS)}


@tool
def fcp_go_to(time: str, fps: float = 30) -> dict:
    """Move Final Cut Pro's playhead to a time (seconds or timecode) in the open project."""
    tl = _state["tl"]
    frame = tl.frame if tl else Fraction(1) / Fraction(str(fps))
    from .timecode import seconds_to_fraction
    t = seconds_to_fraction(time, frame)
    tc = to_timecode(t + (tl.origin if tl else 0), frame)
    fcp_app.go_to_timecode(tc.replace(":", ""))
    return {"playhead": tc}


@tool
def fcp_screenshot() -> Image:
    """Screenshot of the screen, to see what Final Cut Pro is showing."""
    out = os.path.join(tempfile.gettempdir(), "edit_twin_screen.png")
    fcp_app.screenshot(out)
    return Image(path=out)


# --------------------------------------------------------------------------- #
#  Timeline: open / read
# --------------------------------------------------------------------------- #

@tool
def find_fcpxml_files(folder: str | None = None, days: int = 14) -> list:
    """Find recent FCPXML exports (.fcpxml/.fcpxmld) in the working folder, Movies,
    Desktop, Downloads and Documents (or a given folder)."""
    roots = [folder] if folder else [WORK_DIR, "~/Movies", "~/Desktop", "~/Downloads", "~/Documents"]
    cutoff = _dt.datetime.now().timestamp() - days * 86400
    found = {}
    for root in roots:
        root = os.path.expanduser(root)
        for pattern in ("*.fcpxml", "*.fcpxmld", "*/*.fcpxml", "*/*.fcpxmld"):
            for p in glob.glob(os.path.join(root, pattern)):
                if p.endswith(".fcpxml") and ".fcpxmld" in p:
                    continue
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m >= cutoff:
                    found[p] = m
    return [{"path": p, "modified": _dt.datetime.fromtimestamp(m).strftime("%Y-%m-%d %H:%M")}
            for p, m in sorted(found.items(), key=lambda kv: -kv[1])][:30]


@tool
def open_timeline(path: str, project: str | None = None) -> dict:
    """Load an exported FCPXML file or .fcpxmld bundle for editing. If the file holds
    several projects, pick one by name. Returns a summary and the clip list."""
    tl = Timeline(path, project)
    _state["tl"] = tl
    return {"summary": tl.summary(), "items": tl.list_items()}


@tool
def get_timeline(start: float | None = None, end: float | None = None) -> dict:
    """Clips in the primary storyline with refs, times, source media, effects,
    volume and their connected items (B-roll, titles, audio). Optionally only
    those overlapping start..end seconds."""
    tl = _tl()
    return {"summary": tl.summary(), "items": tl.list_items(start, end)}


@tool
def list_markers() -> list:
    """All markers (standard, to-do, chapter) with their times."""
    return _tl().list_markers()


@tool
def probe_media(path: str) -> dict:
    """Media file info: duration, resolution, frame rate, audio, timecode."""
    return media.probe(os.path.abspath(os.path.expanduser(path)))


# --------------------------------------------------------------------------- #
#  Timeline edits
# --------------------------------------------------------------------------- #

@tool
def cut_ranges(ranges: list[list[float]], connected: Literal["keep", "delete"] = "keep") -> dict:
    """Ripple-delete time ranges from the primary storyline, e.g. [[12.5, 15], [40, 41.2]].
    Later clips slide left. connected="keep" re-attaches B-roll/titles anchored inside a
    removed range at the cut point; "delete" removes them like FCP does."""
    tl = _tl()
    rs = [(tl.abs_time(a), tl.abs_time(b)) for a, b in _seconds_list(ranges)]
    return _edit(tl.cut_ranges, rs, connected=connected)


@tool
def keep_ranges(ranges: list[list[float]]) -> dict:
    """Keep only these time ranges (in order) and remove everything else — handy for
    building a short/teaser from a longer edit."""
    tl = _tl()
    return _edit(tl.keep_only, _seconds_list(ranges))


@tool
def split_clip(time: str) -> dict:
    """Blade the primary storyline at a time (seconds or timecode)."""
    return _edit(_tl().split_at, time)


@tool
def delete_clip(ref: str, ripple: bool = True) -> dict:
    """Delete a clip by ref ("3" or connected "3.0"). ripple=True closes the gap;
    ripple=False leaves a gap so timing is unchanged."""
    return _edit(_tl().delete_item, ref, ripple)


@tool
def trim_clip(ref: str, trim_start: float = 0, trim_end: float = 0) -> dict:
    """Shorten a clip: remove `trim_start` seconds from its beginning and `trim_end`
    from its end (ripples the storyline)."""
    return _edit(_tl().trim, ref, trim_start, trim_end)


@tool
def move_clip(index: int, to_index: int) -> dict:
    """Reorder the primary storyline: move clip at `index` to position `to_index`."""
    return _edit(_tl().move_item, index, to_index)


@tool
def add_marker(time: str, name: str, kind: Literal["standard", "todo", "chapter"] = "standard",
               note: str | None = None) -> dict:
    """Add a marker. kind="chapter" makes YouTube-style chapter markers; "todo" a to-do."""
    return _edit(_tl().add_marker, time, name, kind, note)


@tool
def remove_markers(name_contains: str | None = None,
                   kind: Literal["standard", "todo", "chapter"] | None = None) -> dict:
    """Remove markers, optionally only those whose name contains text or of one kind."""
    return _edit(_tl().remove_markers, name_contains, kind)


@tool
def add_title(text: str, time: str, duration: float = 3.0, lane: int = 1,
              font: str = "Helvetica Neue", size: float = 72, color: str = "#ffffff",
              bold: bool = False, y_offset: float | None = None,
              alignment: Literal["left", "center", "right"] = "center") -> dict:
    """Connect a Basic Title above the storyline. color is hex (#ff6fb5) or a name.
    y_offset moves it vertically in pixels from centre (e.g. -380 for a lower third)."""
    return _edit(_tl().add_title, text, time, duration, lane, font, size, color, bold, y_offset, alignment)


@tool
def edit_title(ref: str, text: str | None = None, color: str | None = None, size: float | None = None) -> dict:
    """Change an existing title's text, colour or size (ref like "2.0")."""
    return _edit(_tl().edit_title, ref, text, color, size)


@tool
def set_volume(ref: str, db: float) -> dict:
    """Set a clip's volume in dB (0 = unchanged, -6 = quieter, -20 = background music)."""
    return _edit(_tl().set_volume, ref, db)


@tool
def enable_clip(ref: str, enabled: bool = True) -> dict:
    """Enable or disable (mute/hide) a clip without deleting it."""
    return _edit(_tl().set_enabled, ref, enabled)


@tool
def copy_effects(from_ref: str, to_refs: list[str], name_contains: str | None = None,
                 replace_existing: bool = True) -> dict:
    """Copy video/audio effects from one clip to others, e.g. a colour grade or LUT.
    to_refs=["all"] applies to every clip in the storyline. name_contains limits which
    effects are copied. Only effects already used in this project can be copied."""
    return _edit(_tl().copy_effects, from_ref, to_refs, name_contains, replace_existing)


@tool
def remove_effects(ref: str, name_contains: str | None = None) -> dict:
    """Remove effects from a clip (all, or those whose name contains text)."""
    return _edit(_tl().remove_effects, ref, name_contains)


@tool
def insert_media(path: str, time: str | None = None,
                 mode: Literal["connect", "insert", "append"] = "connect",
                 source_in: float = 0, duration: float | None = None,
                 lane: int | None = None, role: str | None = None) -> dict:
    """Add a media file to the timeline. mode="connect" puts it above (video, lane 1) or below
    (audio, lane -1) the storyline at `time` — B-roll or music; "insert" splits the storyline
    and pushes later clips right; "append" adds to the end. source_in/duration pick the part
    of the file. role e.g. "music" for audio."""
    return _edit(_tl().insert_media, path, time, mode, source_in, duration, lane, role)


def _silence_args(threshold_db, min_silence, keep_after_speech, keep_before_speech) -> dict:
    """Fill unspecified pause settings from the saved style (learned from your edits)."""
    st = style.load(WORK_DIR)["silence"]
    return {
        "threshold_db": st["threshold_db"] if threshold_db is None else threshold_db,
        "min_silence": st["min_silence"] if min_silence is None else min_silence,
        "keep_after_speech": st["keep_after_speech"] if keep_after_speech is None else keep_after_speech,
        "keep_before_speech": st["keep_before_speech"] if keep_before_speech is None else keep_before_speech,
    }


@tool
def find_silences(threshold_db: float | None = None, min_silence: float | None = None,
                  keep_after_speech: float | None = None, keep_before_speech: float | None = None) -> dict:
    """Dry run for remove_silences: list pauses in the storyline clips' audio. Nothing is
    changed. Settings default to the user's saved style (get_style), which is calibrated
    from their own hand trims; pass values only to override. threshold_db: how quiet counts
    as a pause (-30 catches breaths, -40 only near-silence); min_silence: shortest pause to
    cut; keep_after_speech / keep_before_speech: air left after a word / before the next."""
    tl = _tl()
    args = _silence_args(threshold_db, min_silence, keep_after_speech, keep_before_speech)
    ranges, notes = tl.silence_ranges(**args)
    total = sum(b - a for a, b in ranges)
    out = {"settings": args, "count": len(ranges), "total_seconds": secs(total),
           "duration_after": secs(tl.duration() - total),
           "ranges": [[tl.rel(a), tl.rel(b)] for a, b in ranges]}
    if notes:
        out["notes"] = notes
    return out


@tool
def remove_silences(threshold_db: float | None = None, min_silence: float | None = None,
                    keep_after_speech: float | None = None, keep_before_speech: float | None = None) -> dict:
    """Jump-cut edit: ripple-delete pauses from the storyline clips, using the user's saved
    style unless overridden (see find_silences). Connected B-roll, titles, music and the
    adjustment clip are kept. Run find_silences first to show the user what will go."""
    tl = _tl()
    args = _silence_args(threshold_db, min_silence, keep_after_speech, keep_before_speech)
    ranges, notes = tl.silence_ranges(**args)
    out = _edit(tl.cut_ranges, ranges, connected="keep", label="Removed pauses:")
    out["settings"] = args
    if notes:
        out["notes"] = notes
    return out


@tool
def transcribe(start: float | None = None, end: float | None = None, include_words: bool = False,
               language: str | None = None, vocabulary: str | None = None, refresh: bool = False) -> dict:
    """Transcribe what's said in the storyline (local Whisper, word timestamps), with
    times on the CURRENT timeline (they update after cuts). Use it to find repeated
    takes, flubs and filler. include_words=True adds per-word times for precise cuts —
    best with start/end to limit the window. vocabulary: names/jargon to help spelling
    (e.g. product names, places, people). First run per file takes a while and
    may download the model; later calls are instant."""
    from . import transcribe as tr

    tl = _tl()
    files = tl.storyline_media()
    if not files:
        raise EditError("No playable clips in the storyline to transcribe")
    cache = os.path.join(WORK_DIR, ".transcripts")
    segments, engines = [], set()
    for f in files:
        data = tr.transcribe_file(f, cache, language, vocabulary, force=refresh)
        engines.add(data["engine"])
        segments += tl.map_transcript(f, data, include_words, start, end)
    segments.sort(key=lambda s: s["start"])
    return {"engine": ", ".join(sorted(engines)), "segments": segments,
            "tip": "To remove a take, cut_ranges from just before its first word to just before "
                   "the next kept word; keep ~0.1s of air."}


def _timeline_words(language: str | None = None, vocabulary: str | None = None,
                    start: float | None = None, end: float | None = None) -> list[list]:
    from . import transcribe as tr

    tl = _tl()
    words = []
    for f in tl.storyline_media():
        data = tr.transcribe_file(f, os.path.join(WORK_DIR, ".transcripts"), language, vocabulary)
        for seg in tl.map_transcript(f, data, include_words=True, start=start, end=end):
            words += seg.get("words", [])
    words = [w for w in words if (start is None or w[2] > start) and (end is None or w[1] < end)]
    words.sort(key=lambda w: w[1])
    return words


@tool
def find_repeats(window_seconds: float = 20.0) -> dict:
    """Find stutters ("to new to new tech"), retakes (a phrase or sentence started
    again) and filler sounds (um/uh) in the transcript. Each result proposes removing the FIRST take and keeping
    the later one; times are on the current timeline, ready for cut_ranges. Nothing is
    changed. Review them: when the first take is clearly better, keep it instead."""
    from .learn import find_repeats as fr

    words = _timeline_words()
    if not words:
        raise EditError("No transcript words; run transcribe first")
    from .learn import find_fillers

    reps = fr(words, window_s=window_seconds)
    fillers = find_fillers(words)
    return {"count": len(reps), "seconds": round(sum(r["remove_to"] - r["remove_from"] for r in reps), 2),
            "repeats": reps, "fillers": fillers,
            "how_to_apply": "cut_ranges with [[remove_from, remove_to], ...] for the ones you approve"}


@tool
def normalize_audio(target_peak_db: float = -2.0, per_clip: bool = False, refs: list[str] | None = None) -> dict:
    """Set dialogue gain so the loudest peak of the edit sits at target_peak_db (default −2 dB).
    One shared gain by default so takes stay balanced; per_clip=True levels each clip
    separately. Measures only the parts used in the edit. Music/connected audio is left
    alone unless listed in refs."""
    return _edit(_tl().normalize_peaks, target_peak_db, per_clip, refs)


@tool
def fit_to_timeline(ref: str | None = None, name_contains: str | None = None) -> dict:
    """Stretch/shrink a connected item — e.g. the Adjustment Clip carrying a look like
    your colour preset, or a watermark title — so it starts at 0 and covers the whole
    edit. Identify it by ref ("0.1") or by part of its name or effect name. Run after cuts."""
    tl = _tl()
    if not ref:
        if name_contains:
            ref = tl.find_connected(name_contains)
        else:
            adj = tl.adjustment_refs()
            if len(adj) != 1:
                raise EditError(f"Found {len(adj)} adjustment clips {adj}; give ref")
            ref = adj[0]
    return _edit(tl.fit_to_timeline, ref)


# --------------------------------------------------------------------------- #
#  Captions
# --------------------------------------------------------------------------- #

@tool
def list_fonts(query: str | None = None) -> dict:
    """Fonts: the ones the user works with (saved in their style) and, with query, the
    installed fonts matching it ("avenir", "futura") with their exact family and face
    names as Final Cut Pro needs them. Use it to turn a font the user names into the
    real name before save_caption_style."""
    from . import fonts

    st = style.load(WORK_DIR)
    out: dict = {"yours": st["fonts"]}
    if query:
        found = fonts.search(query)
        out["installed"] = [{"family": f["family"], "faces": f["faces"]} for f in found[:20]]
        if not found:
            out["note"] = (f"No installed font matches {query!r}. Install it (double-click the font "
                           "file > Install) and try again.")
    else:
        out["installed_count"] = len({f["family"] for f in fonts.installed()})
    return out


def _caption_format(fmt: str) -> str:
    from . import captions

    if fmt != "auto":
        return fmt
    tl = _tl()
    w, h = tl.frame_size()
    return captions.pick_format(w, h, secs(tl.duration()))


@tool
def save_caption_style(name: str, mode: Literal["word", "line"] | None = None,
                       from_title: str | None = None, font: str | None = None, face: str | None = None,
                       size: float | None = None, color: str | None = None,
                       position: Literal["top", "upper", "middle", "lower", "bottom"] | None = None,
                       case: Literal["as_spoken", "lower", "upper"] | None = None,
                       punctuation: Literal["keep", "strip", "questions"] | None = None,
                       keep_caps: list[str] | None = None, max_words: int | None = None, max_lines: int | None = None,
                       max_width: float | None = None, stroke_color: str | None = None,
                       stroke_width: float | None = None, shadow: bool | None = None,
                       default_for: Literal["short", "long"] | None = None) -> dict:
    """Save (or update) a named caption style in the user's style, e.g. "reel words" or
    "vlog subtitles". Best: from_title = ref of a title the user styled by hand in the open
    timeline ("2.0"); its font, size, colour, position and any animation are copied exactly.
    Or describe it: mode "word" = one word (max_words) at a time as it's said, for short form;
    "line" = subtitle lines that fit the frame (max_width = fraction of frame width, max_lines),
    for long form. font: any installed font, loosely named ("avenir next"); it's matched to the
    exact name. position/size without from_title scale to any resolution. default_for makes
    it the style add_captions uses for short (vertical) or long videos. The font is remembered
    as one of the user's fonts. punctuation "questions" strips all but question marks.
    keep_caps: names and words written as given even when case is "lower" ("YouTube",
    "Wi-Fi"); added to the style's list. Acronyms the transcript writes in capitals
    ("AI") are always kept."""
    from . import captions, fonts

    st = style.load(WORK_DIR)
    styles = st["captions"]["styles"]
    mode = mode or (styles.get(name) or {}).get("mode") or {"short": "word", "long": "line"}.get(default_for or "")
    if name in styles:
        s = dict(styles[name])
    elif mode:
        s = dict(captions.BUILTIN_STYLES["one_word" if mode == "word" else "subtitle"])
    elif from_title:
        s = dict(captions.BUILTIN_STYLES["one_word"])
    else:
        raise EditError("Say which kind of caption this is: mode 'word' (short form) or 'line' (long form)")
    if mode:
        s["mode"] = mode
    notes = []
    if from_title:
        tl = _tl()
        learned = tl.extract_title_style(from_title)
        # the title's own look wins: drop size, position and text effects from the base style
        for k in ("size_ratio", "position", "y", "size", "shadow", "stroke_color", "stroke_width"):
            s.pop(k, None)
        s.update(learned)
        if learned.get("font") and not fonts.resolve(learned["font"], learned.get("face")):
            notes.append(f"The title's font {learned['font']!r} isn't installed on this Mac, so lines can't be "
                         "measured exactly; Final Cut will substitute a font.")
    if font:
        found = fonts.resolve(font, face)
        if not found:
            near = [f["family"] for w in font.split() if len(w) > 3 for f in fonts.search(w)][:8]
            raise EditError(f"Font {font!r} isn't installed. Similar installed fonts: {near or 'none'}. "
                            "Use list_fonts to search.")
        if face and fonts.squash(found["face"]) != fonts.squash(face):
            faces = fonts.search(found["family"])[0]["faces"]
            raise EditError(f"{found['family']} has no {face!r} face. Faces: {faces}")
        s["font"], s["face"] = found["family"], found["face"]
    elif face:
        s["face"] = face
    if size is not None:
        s.pop("size_ratio", None)
        s["size"] = size
        if _state["tl"] is not None:
            s["frame"] = list(_tl().frame_size())
    if position is not None:
        s.pop("y", None)
        s["position"] = position
    if keep_caps:
        known = {w.lower(): w for w in s.get("keep_caps") or []}
        known.update({w.lower(): w for w in keep_caps})
        s["keep_caps"] = sorted(known.values(), key=str.lower)
    for k, v in {"color": color, "case": case, "punctuation": punctuation, "max_words": max_words,
                 "max_lines": max_lines, "max_width": max_width, "stroke_color": stroke_color,
                 "stroke_width": stroke_width, "shadow": shadow}.items():
        if v is not None:
            s[k] = v
    styles[name] = s
    if default_for:
        st["captions"]["default"][default_for] = name
    if s.get("font"):
        style.remember_font(st, s["font"], s.get("face"),
                            f"{'one-word' if s['mode'] == 'word' else 'subtitle'} captions ({name})")
    style.save(WORK_DIR, st)
    out = {"saved": name, "style": captions.summary(s), "defaults": st["captions"]["default"]}
    if notes:
        out["notes"] = notes
    return out


def _build_captions(style_name, fmt, start, end, fixes, language, vocabulary):
    from . import captions

    tl = _tl()
    fmt = _caption_format(fmt)
    name, s = captions.resolve_style(style.load(WORK_DIR)["captions"], style_name, fmt)
    w, h = tl.frame_size()
    s = captions.concrete(s, w, h)
    warnings = []
    if s.get("frame") and list(s["frame"]) != [w, h]:
        warnings.append(f"Caption style '{name}' was made on a {s['frame'][0]}x{s['frame'][1]} project and this one "
                        f"is {w}x{h}; check its size and position in Final Cut.")
    words = captions.apply_fixes(_timeline_words(language, vocabulary, start, end), fixes)
    if not words:
        raise EditError("No transcript words in that range")
    limit = end if end is not None else secs(tl.duration())
    return name, fmt, s, captions.build(words, s, captions.title_width(w, h), limit), warnings


@tool
def add_captions(style_name: str | None = None, format: Literal["auto", "short", "long"] = "auto",
                 start: float | None = None, end: float | None = None,
                 fixes: dict[str, str] | None = None, dry_run: bool = False,
                 replace_existing: bool = True, language: str | None = None,
                 vocabulary: str | None = None) -> dict:
    """Caption the edit from the transcript, timed to the words as they're said on the
    CURRENT timeline (run after cutting). Each caption is a title on its own lane above
    everything, so it isn't affected by an adjustment-clip look. style_name: a saved caption
    style (default: the user's default for the format; format auto = short if vertical or
    under 3 minutes). fixes: whole-word corrections for misheard words {"cloud": "Claude"};
    vocabulary helps the transcript spell names. dry_run=True returns the captions without
    adding them, to check wording and line breaks first. replace_existing removes captions
    added before (only within start/end if given). Fix a single caption with edit_title."""
    from . import captions

    name, fmt, s, caps, warnings = _build_captions(style_name, format, start, end, fixes, language, vocabulary)
    out: dict = {"style": name, "format": fmt, "count": len(caps)}
    if warnings:
        out["warnings"] = warnings
    if dry_run:
        out["captions"] = caps[:300]
        if len(caps) > 300:
            out["note"] = "First 300 shown; use start/end to see the rest."
        return out
    tl = _tl()

    def run():
        rep = None
        if replace_existing:
            old = [(c, p) for c, p in tl.caption_items()
                   if (start is None or tl.rel(tl.timeline_time(c, tl.start(c), parent=p)) >= start - 0.01)
                   and (end is None or tl.rel(tl.timeline_time(c, tl.start(c), parent=p)) < end)]
            for c, p in old:
                p.remove(c)
            if old:
                rep = f"Replaced {len(old)} earlier captions."
        lane = None
        remaining = tl.caption_items()
        if remaining:
            lane = int(remaining[0][0].get("lane"))
        r = tl.add_captions(caps, s, lane)
        if rep:
            r.changes.insert(0, rep)
        return r

    out.update(_edit(run))
    out["first_captions"] = caps[:6]
    out["style_used"] = captions.summary(s)
    out["tip"] = ("Final Cut titles don't show in preview_frame/render_preview; after send_to_final_cut "
                  "use fcp_screenshot to check the look.")
    return out


@tool
def remove_captions() -> dict:
    """Remove all captions added by add_captions (other titles are left alone)."""
    return _edit(_tl().remove_captions)


@tool
def export_srt(output_path: str | None = None, style_name: str | None = None,
               fixes: dict[str, str] | None = None, max_lines: int = 2) -> dict:
    """Write an .srt subtitle file of the current edit (e.g. to upload to YouTube as closed
    captions). Lines are broken using a long-form caption style (default: the user's long
    style) with up to max_lines lines each."""
    from . import captions

    tl = _tl()
    st = style.load(WORK_DIR)
    name, s = captions.resolve_style(st["captions"], style_name, "long")
    s = captions.concrete({**s, "mode": "line", "max_lines": max_lines}, *tl.frame_size())
    words = captions.apply_fixes(_timeline_words(), fixes)
    if not words:
        raise EditError("No transcript words")
    caps = captions.build(words, s, captions.title_width(*tl.frame_size()), secs(tl.duration()))
    out = output_path or os.path.join(WORK_DIR, f"{_safe(tl.project.get('name'))}.srt")
    with open(os.path.expanduser(out), "w") as fh:
        fh.write(captions.to_srt(caps))
    return {"saved": os.path.expanduser(out), "count": len(caps), "style": name}


# --------------------------------------------------------------------------- #
#  Your style: looks, audio chain, learning from your own edits
# --------------------------------------------------------------------------- #

@tool
def get_style() -> dict:
    """The user's saved editing style: pause-cut tightness, audio chain, default look,
    saved looks, caption styles, the fonts they use and notes learned from their edits.
    Read this before a rough cut or captioning."""
    from . import captions

    st = style.load(WORK_DIR)
    out = {k: v for k, v in st.items() if k not in ("audio", "captions")}
    out["audio"] = st["audio"]["summary"] if st.get("audio") else None
    out["caption_styles"] = {k: captions.summary(v) for k, v in st["captions"]["styles"].items()}
    out["caption_defaults"] = st["captions"]["default"]
    out["saved_looks"] = style.list_looks(WORK_DIR)
    out["file"] = style.style_path(WORK_DIR)
    return out


@tool
def update_style(threshold_db: float | None = None, min_silence: float | None = None,
                 keep_after_speech: float | None = None, keep_before_speech: float | None = None,
                 default_look: str | None = None, remove_stutters: bool | None = None,
                 add_note: str | None = None) -> dict:
    """Change saved style defaults, e.g. when the user says cuts feel too tight
    (raise keep_after_speech) or too loose (lower min_silence). add_note stores a
    preference in words (e.g. "keep 'So' at the start of the hook")."""
    st = style.load(WORK_DIR)
    for k, v in {"threshold_db": threshold_db, "min_silence": min_silence,
                 "keep_after_speech": keep_after_speech, "keep_before_speech": keep_before_speech}.items():
        if v is not None:
            st["silence"][k] = v
    if default_look is not None:
        st["default_look"] = default_look or None
    if remove_stutters is not None:
        st["remove_stutters"] = remove_stutters
    if add_note:
        st["notes"].append(add_note)
    style.save(WORK_DIR, st)
    return get_style()


@tool
def save_look(name: str, ref: str | None = None, make_default: bool = True) -> dict:
    """Save the effects on an adjustment clip in the open timeline (e.g. the one you made
    with your colour preset) as a reusable look, so future rough cuts can add it
    automatically with apply_look — no more Option-A by hand. ref defaults to the only
    adjustment clip."""
    tl = _tl()
    if not ref:
        adj = tl.adjustment_refs()
        if len(adj) != 1:
            raise EditError(f"Found {len(adj)} adjustment clips {adj}; give ref")
        ref = adj[0]
    look = tl.extract_look(ref)
    path = style.save_look(WORK_DIR, name, look)
    if make_default:
        st = style.load(WORK_DIR)
        st["default_look"] = name
        style.save(WORK_DIR, st)
    return {"saved": path, "effects": look["effects"], "default": make_default}


@tool
def apply_look(name: str | None = None, replace_existing: bool = True) -> dict:
    """Add an adjustment clip with a saved look (default: the style's default look) over the
    whole timeline. Replaces an existing empty/same-named adjustment clip. Run after cutting
    (or run fit_to_timeline after later cuts)."""
    st = style.load(WORK_DIR)
    name = name or st.get("default_look")
    if not name:
        raise EditError(f"No look given and no default look saved. Saved looks: {style.list_looks(WORK_DIR)}")
    look = style.load_look(WORK_DIR, name)
    return _edit(_tl().apply_look, look, look.get("name", name), replace_existing)


@tool
def save_audio_style(ref: str = "0") -> dict:
    """Save a clip's audio chain (volume, Loudness / Noise Removal / EQ enhancements,
    audio effects) as the user's default dialogue sound, from a clip they set up by hand."""
    tl = _tl()
    a = tl.extract_audio_style(ref)
    st = style.load(WORK_DIR)
    st["audio"] = a
    style.save(WORK_DIR, st)
    return {"saved": a["summary"]}


@tool
def apply_audio_style(refs: list[str] | None = None) -> dict:
    """Apply the saved dialogue sound (see save_audio_style) to every dialogue clip in the
    storyline, or to the given refs. Use instead of normalize_audio when a style is saved."""
    st = style.load(WORK_DIR)
    if not st.get("audio"):
        raise EditError("No audio style saved yet; use save_audio_style or learn_from_edit")
    return _edit(_tl().apply_audio_style, st["audio"], refs)


@tool
def measure_loudness(start: float | None = None, end: float | None = None) -> dict:
    """Measure the edit's dialogue loudness (integrated LUFS, true peak) from the storyline
    clips with their clip volume applied. Final Cut's Loudness enhancement isn't simulated,
    so treat it as the level BEFORE that effect. Reels usually sit around -14 LUFS."""
    tl = _tl()
    segs = []
    for el in tl.items():
        path = tl.media_path(el) if el.tag == "asset-clip" else None
        if not path or not os.path.exists(path) or el.get("enabled") == "0":
            continue
        a, b = tl.rel(tl.off(el)), tl.rel(tl.off(el) + tl.dur(el))
        lo, hi = max(a, start if start is not None else a), min(b, end if end is not None else b)
        if hi <= lo:
            continue
        asset = tl.resource(el.get("ref"))
        file_in = float(tl.start(el) - Timeline.start(asset)) + (lo - a)
        vol = el.find("adjust-volume")
        gain = float(vol.get("amount", "0dB").rstrip("dB")) if vol is not None else 0.0
        segs.append((path, file_in, hi - lo, gain))
    if not segs:
        raise EditError("No audio to measure")
    return media.measure_loudness(segs)


@tool
def learn_from_edit(your_version: str | None = None, claude_version: str | None = None,
                    save: bool = False, look_name: str | None = None) -> dict:
    """Compare Claude's rough cut with the user's hand-finished version and learn their style.
    your_version: their exported .fcpxml/.fcpxmld (default: export the project open in Final
    Cut now). claude_version: what Claude sent (default: the last one sent for that project).
    Reports what they removed or restored (with the words), how tight they trim pauses,
    their audio chain and adjustment-clip look, the fonts in their titles, and pause settings
    calibrated to match them.
    save=True stores the pause settings, audio chain and look (named look_name, e.g. the preset
    they used, e.g. "warm_vlog") as their defaults."""
    from .learn import compare
    from . import transcribe as tr

    if your_version is None:
        your_version = fcp_app.export_xml(WORK_DIR, f"your edit {_dt.datetime.now():%Y-%m-%d %H%M%S}")
    hand = Timeline(your_version)
    if claude_version is None:
        claude_version = style.last_sent(WORK_DIR, hand.project.get("name"))
        if not claude_version:
            raise EditError("Couldn't tell which file Claude sent for this project; pass claude_version "
                            "(see find_fcpxml_files)")
    claude = Timeline(claude_version)
    transcripts = {}
    for f in set(hand.storyline_media()) | set(claude.storyline_media()):
        try:
            transcripts[f] = tr.transcribe_file(f, os.path.join(WORK_DIR, ".transcripts"))
        except Exception:
            pass
    report = compare(claude, hand, transcripts, calibrate=True)
    report["files"] = {"yours": your_version, "claude": claude_version}
    title_fonts = hand.title_fonts()
    if title_fonts:
        report["your_title_fonts"] = title_fonts
    if save:
        st = style.load(WORK_DIR)
        saved = []
        if report.get("suggested_pause_settings"):
            sp = {k: v for k, v in report["suggested_pause_settings"].items() if k != "match_score"}
            st["silence"].update(sp)
            saved.append("pause settings")
        if report.get("your_audio"):
            st["audio"] = hand.extract_audio_style(report["your_audio"]["example_ref"])
            saved.append("audio chain")
        looks = [l for l in report.get("your_adjustment_clips", []) if l["effects"]]
        if len(looks) == 1:
            look = hand.extract_look(looks[0]["ref"])
            name = look_name or st.get("default_look") or "my look"
            style.save_look(WORK_DIR, name, look)
            st["default_look"] = name
            saved.append(f"look '{name}'")
        for f in title_fonts:
            style.remember_font(st, f["family"], f["face"], "titles")
        if title_fonts:
            saved.append("fonts " + ", ".join(sorted({f["family"] for f in title_fonts})))
        if report.get("often_removed_words"):
            st["notes"].append("Often removes these words when they start a sentence or follow a pause: "
                               + ", ".join(report["often_removed_words"]))
        style.save(WORK_DIR, st)
        report["saved_to_style"] = saved
    return report


@tool
def undo() -> dict:
    """Undo the last timeline edit."""
    tl = _tl()
    return {"undone": tl.undo(), "duration_now": secs(tl.duration())}


@tool
def redo() -> dict:
    """Redo the last undone timeline edit."""
    tl = _tl()
    return {"redone": tl.redo(), "duration_now": secs(tl.duration())}


# --------------------------------------------------------------------------- #
#  Checking, previewing, saving
# --------------------------------------------------------------------------- #

def _find_dtd(version: str) -> str | None:
    name = f"FCPXMLv{version.replace('.', '_')}.dtd"
    candidates = [os.environ.get("EDIT_TWIN_DTD_DIR", "")]
    candidates += glob.glob("/Applications/Final Cut Pro*.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources")
    for d in candidates:
        if d and os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    for p in glob.glob(f"/Applications/Final Cut Pro*.app/Contents/**/{name}", recursive=True):
        return p
    return None


def _validate_file(path: str, version: str) -> dict:
    xml = os.path.join(path, "Info.fcpxml") if os.path.isdir(path) else path
    dtd = _find_dtd(version)
    if not dtd or not media.which("xmllint"):
        return {"dtd_check": "skipped (Final Cut Pro's DTD not found on this machine)"}
    # xmllint reads --dtdvalid as a URI, so a plain path with spaces ("Final Cut Pro.app") fails.
    proc = subprocess.run(["xmllint", "--noout", "--dtdvalid", pathlib.Path(dtd).as_uri(), xml],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return {"dtd_check": f"passed ({os.path.basename(dtd)})"}
    return {"dtd_check": "FAILED", "dtd_errors": proc.stderr.strip().splitlines()[:15]}


@tool
def validate_timeline() -> dict:
    """Check the edited timeline: references, gaps/overlaps, frame alignment, and
    Apple's FCPXML DTD (when Final Cut Pro is installed)."""
    tl = _tl()
    out: dict = {"problems": tl.check()}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "check.fcpxml")
        tl.save(p)
        out.update(_validate_file(p, tl.version))
    out["ok"] = not out["problems"] and out.get("dtd_check") != "FAILED"
    return out


@tool
def preview_frame(time: str, width: int = 640) -> Image:
    """Image of the storyline's main clip at a time (ignores titles/effects/B-roll),
    for checking what's on screen at a cut."""
    tl = _tl()
    t = tl.abs_time(time)
    el = tl.item_at(t)
    if el is None or el.tag != "asset-clip" or not tl.media_path(el):
        raise EditError("No playable storyline clip at that time")
    asset = tl.resource(el.get("ref"))
    file_t = float(tl.local_time(el, t) - Timeline.start(asset))
    out = os.path.join(tempfile.gettempdir(), f"edit_twin_frame_{int(float(t) * 1000)}.png")
    media.grab_frame(tl.media_path(el), max(0.0, file_t), out, width)
    return Image(path=out)


@tool
def render_preview(output_path: str | None = None, height: int = 360,
                   start: float | None = None, end: float | None = None) -> dict:
    """Render a quick low-res MP4 of the primary storyline (straight cuts, no effects,
    titles or connected clips) so the user can review an edit before importing.
    Optional start/end seconds render only part of it."""
    tl = _tl()
    segs = tl.preview_segments()
    if start is not None or end is not None:
        s0 = Fraction(str(start or 0))
        e0 = Fraction(str(end)) if end is not None else tl.duration()
        cut, pos = [], Fraction(0)
        for s in segs:
            d = Fraction(str(s["dur"]))
            a, b = max(pos, s0), min(pos + d, e0)
            if b > a:
                ns = dict(s, dur=float(b - a))
                if not s.get("black"):
                    ns["in"] = s["in"] + float(a - pos)
                cut.append(ns)
            pos += d
        segs = cut
    if not segs:
        raise EditError("Nothing to render")
    fmt = tl.format_el
    w = int(fmt.get("width") or 1920) if fmt is not None else 1920
    h = int(fmt.get("height") or 1080) if fmt is not None else 1080
    height = height - height % 2
    width = int(round(w * height / h / 2)) * 2
    fps = round(float(1 / tl.frame), 3)
    os.makedirs(WORK_DIR, exist_ok=True)
    out = output_path or os.path.join(WORK_DIR, f"{_safe(tl.project.get('name'))} preview.mp4")
    media.render_segments(segs, os.path.expanduser(out), width, height, fps)
    return {"preview": os.path.expanduser(out), "seconds": secs(sum(Fraction(str(s['dur'])) for s in segs))}


def _safe(name: str | None) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", name or "Project").strip() or "Project"


def _default_out(tl: Timeline, project_name: str) -> str:
    os.makedirs(WORK_DIR, exist_ok=True)
    return os.path.join(WORK_DIR, f"{_safe(project_name)} {_dt.datetime.now():%Y-%m-%d %H%M%S}.fcpxml")


@tool
def save_timeline(output_path: str | None = None, project_name: str | None = None) -> dict:
    """Save the edited timeline to a new .fcpxml (or .fcpxmld) without importing it.
    The project is renamed (default: "<name> (Claude edit)") so it won't clash."""
    tl = _tl()
    name = project_name or f"{tl.project.get('name') or 'Project'} (Claude edit)"
    out = tl.save(output_path or _default_out(tl, name), project_name=name)
    return {"saved": out, "project_name": name, **_validate_file(out, tl.version)}


@tool
def send_to_final_cut(project_name: str | None = None, output_path: str | None = None) -> dict:
    """Save the edited timeline as a new project and import it into Final Cut Pro.
    The original project is untouched; the new one appears in the same event."""
    tl = _tl()
    problems = tl.check()
    if problems:
        raise EditError(f"Fix these before importing: {problems[:5]}")
    saved = save_timeline(output_path, project_name)
    if saved.get("dtd_check") == "FAILED":
        saved["imported"] = False
        saved["note"] = "Not imported because Apple's DTD check failed; see dtd_errors."
        return saved
    style.record_sent(WORK_DIR, saved["project_name"], saved["saved"])
    saved["imported"] = fcp_app.import_xml(saved["saved"]) if fcp_app.is_mac() else False
    saved["tip"] = ("If you finish this edit by hand, ask me to learn_from_edit afterwards so the "
                    "next rough cut matches your style.")
    if not fcp_app.is_mac():
        saved["note"] = "Not on a Mac: open the saved file in Final Cut Pro with File > Import > XML."
    return saved


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
