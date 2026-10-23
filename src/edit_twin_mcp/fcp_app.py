"""Live control of the Final Cut Pro app on macOS.

Final Cut Pro's AppleScript dictionary is read-only (libraries, events,
projects), so actions inside the app are driven the way a person would:
menu clicks and keyboard shortcuts via System Events. That needs a one-time
macOS permission (Accessibility) for the app that runs Claude Code.
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
import time

APP = "Final Cut Pro"

PERMISSION_HELP = (
    "macOS blocked the request. Open System Settings > Privacy & Security and allow the app "
    "you run Claude Code from (Terminal, iTerm, VS Code, or Claude) under BOTH 'Accessibility' "
    "and 'Automation > Final Cut Pro / System Events'. For screenshots also allow 'Screen & "
    "System Audio Recording'. Then restart that app and try again."
)

# Final Cut Pro default keyboard shortcuts.
# Each value: (key, [modifiers]) where key is a character or a named key.
SHORTCUTS: dict[str, tuple[str, list[str]]] = {
    "play_pause": ("space", []),
    "play_reverse": ("j", []),
    "stop": ("k", []),
    "play_forward": ("l", []),
    "blade": ("b", ["command"]),
    "blade_all": ("b", ["command", "shift"]),
    "add_marker": ("m", []),
    "add_todo_marker": ("m", ["option"]),
    "ripple_delete": ("delete", []),
    "lift_to_gap": ("delete", ["shift"]),
    "undo": ("z", ["command"]),
    "redo": ("z", ["command", "shift"]),
    "go_to_start": ("home", []),
    "go_to_end": ("end", []),
    "previous_edit": ("up", []),
    "next_edit": ("down", []),
    "previous_frame": ("left", []),
    "next_frame": ("right", []),
    "select_clip_under_playhead": ("c", []),
    "set_range_start": ("i", []),
    "set_range_end": ("o", []),
    "clear_range": ("x", ["option"]),
    "trim_start_to_playhead": ("[", ["option"]),
    "trim_end_to_playhead": ("]", ["option"]),
    "zoom_to_fit": ("z", ["shift"]),
    "zoom_in": ("=", ["command"]),
    "zoom_out": ("-", ["command"]),
    "render_all": ("r", ["control", "shift"]),
    "focus_timeline": ("2", ["command"]),
    "focus_browser": ("1", ["command"]),
    "toggle_snapping": ("n", []),
    "toggle_skimming": ("s", []),
    "solo_clip": ("s", ["option"]),
    "detach_audio": ("s", ["control", "shift"]),
    "connect_title_basic": ("t", ["control"]),
    "toggle_inspector": ("4", ["command"]),
    "toggle_video_scopes": ("7", ["command"]),
    "add_default_transition": ("t", ["command"]),
    "share_default": ("e", ["command"]),
    "import_media": ("i", ["command"]),
}

_KEY_CODES = {
    "space": 49, "return": 36, "enter": 76, "delete": 51, "forward_delete": 117, "escape": 53,
    "tab": 48, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
    "page_up": 116, "page_down": 121,
}


def is_mac() -> bool:
    return platform.system() == "Darwin"


def _need_mac() -> None:
    if not is_mac():
        raise RuntimeError("Live Final Cut Pro control only works on the Mac running Final Cut Pro.")


def osascript(script: str, timeout: float = 30) -> str:
    _need_mac()
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if any(code in err for code in ("-1743", "-1719", "-25211", "not allowed", "assistive access")):
            raise PermissionError(f"{err}\n{PERMISSION_HELP}")
        raise RuntimeError(err or "AppleScript failed")
    return proc.stdout.strip()


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def is_running() -> bool:
    if not is_mac():
        return False
    return subprocess.run(["pgrep", "-x", APP], capture_output=True).returncode == 0


def status() -> dict:
    if not is_mac():
        return {"mac": False, "note": "Not running on macOS; only timeline (FCPXML) tools are available."}
    info: dict = {"mac": True, "running": is_running()}
    try:
        info["version"] = osascript(f'version of application "{APP}"', timeout=10)
    except Exception as e:  # app not installed
        info["version"] = None
        info["error"] = str(e)
    if info["running"]:
        try:
            info["frontmost"] = osascript(
                'tell application "System Events" to get name of first process whose frontmost is true') == APP
        except Exception:
            pass
    return info


def launch(wait: float = 8) -> dict:
    _need_mac()
    subprocess.run(["open", "-a", APP], check=True)
    deadline = time.time() + wait
    while time.time() < deadline and not is_running():
        time.sleep(0.5)
    return status()


def activate() -> None:
    osascript(f'tell application "{APP}" to activate')
    time.sleep(0.4)


def library_overview() -> list[dict]:
    """Libraries > events > projects, read via Final Cut Pro's AppleScript dictionary."""
    script = f'''
    tell application "{APP}"
        set out to ""
        repeat with lib in libraries
            set out to out & "L" & tab & (name of lib) & linefeed
            try
                repeat with ev in events of lib
                    set out to out & "E" & tab & (name of ev) & linefeed
                    try
                        repeat with pr in projects of ev
                            set out to out & "P" & tab & (name of pr) & linefeed
                        end repeat
                    end try
                end repeat
            end try
        end repeat
        return out
    end tell'''
    raw = osascript(script, timeout=60)
    libs: list[dict] = []
    for line in raw.splitlines():
        kind, _, name = line.partition("\t")
        if kind == "L":
            libs.append({"library": name, "events": []})
        elif kind == "E" and libs:
            libs[-1]["events"].append({"event": name, "projects": []})
        elif kind == "P" and libs and libs[-1]["events"]:
            libs[-1]["events"][-1]["projects"].append(name)
    return libs


def import_xml(path: str) -> str:
    """Send an FCPXML file/bundle to Final Cut Pro (same as File > Import > XML)."""
    _need_mac()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    subprocess.run(["open", "-a", APP, path], check=True)
    return path


def _menu_ref(path: list[str]) -> str:
    """AppleScript reference for a menu item path like ["File", "Export XML…"]."""
    ref = f'menu bar item "{_esc(path[0])}" of menu bar 1'
    for name in path[1:]:
        ref = f'menu item "{_esc(name)}" of menu 1 of {ref}'
    return ref


def _variants(name: str) -> list[str]:
    out = [name]
    if name.endswith("..."):
        out.append(name[:-3] + "…")
    elif name.endswith("…"):
        out.append(name[:-1] + "...")
    else:
        out.append(name + "…")
    return out


def list_menu(path: list[str]) -> list[str]:
    ref = f'menu bar item "{_esc(path[0])}" of menu bar 1'
    for name in path[1:]:
        ref = f'menu item "{_esc(name)}" of menu 1 of {ref}'
    script = f'''tell application "System Events" to tell process "{APP}"
        set n to name of every menu item of menu 1 of {ref}
        set AppleScript's text item delimiters to linefeed
        return n as text
    end tell'''
    return [x for x in osascript(script).splitlines() if x and x != "missing value"]


def click_menu(path: list[str]) -> str:
    """Click a menu item, e.g. ["File", "Share", "Master File (default)…"]."""
    if len(path) < 2:
        raise ValueError("Give at least a menu and an item, e.g. ['File', 'Export XML…']")
    activate()
    resolved = [path[0]]
    for depth, name in enumerate(path[1:], start=1):
        available = list_menu(resolved)
        match = next((v for v in _variants(name) if v in available), None)
        if match is None:
            low = name.lower().rstrip(".…")
            match = next((a for a in available if a.lower().rstrip(".…") == low), None)
        if match is None:
            raise ValueError(f"No menu item {name!r} under {' > '.join(resolved)}. Available: {available}")
        resolved.append(match)
    osascript(f'tell application "System Events" to tell process "{APP}" to click {_menu_ref(resolved)}')
    return " > ".join(resolved)


def press(key: str, modifiers: list[str] | None = None) -> str:
    modifiers = modifiers or []
    using = ""
    if modifiers:
        using = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
    if key.lower() in _KEY_CODES:
        action = f"key code {_KEY_CODES[key.lower()]}{using}"
    else:
        action = f'keystroke "{_esc(key)}"{using}'
    activate()
    osascript(f'tell application "System Events" to tell process "{APP}" to {action}')
    return "+".join(modifiers + [key])


def shortcut(action: str) -> str:
    if action not in SHORTCUTS:
        raise ValueError(f"Unknown action {action!r}. Known: {sorted(SHORTCUTS)}")
    key, mods = SHORTCUTS[action]
    return press(key, mods)


def type_text(text: str) -> None:
    osascript(f'tell application "System Events" to tell process "{APP}" to keystroke "{_esc(text)}"')


def go_to_timecode(tc_digits: str) -> None:
    """Move the playhead: Control-P opens timecode entry, then digits + Return."""
    activate()
    press("2", ["command"])  # focus timeline
    time.sleep(0.2)
    osascript(f'tell application "System Events" to tell process "{APP}" to keystroke "p" using {{control down}}')
    time.sleep(0.2)
    type_text(tc_digits)
    osascript(f'tell application "System Events" to tell process "{APP}" to key code 36')


def export_xml(dest_folder: str, filename: str, timeout: float = 120) -> str:
    """Export the project open in the timeline via File > Export XML…, saving into
    dest_folder. Returns the path of the exported .fcpxml / .fcpxmld."""
    _need_mac()
    dest_folder = os.path.abspath(os.path.expanduser(dest_folder))
    os.makedirs(dest_folder, exist_ok=True)
    started = time.time()
    activate()
    press("2", ["command"])  # make sure the timeline (open project) is what gets exported
    time.sleep(0.3)
    click_menu(["File", "Export XML…"])
    time.sleep(1.5)
    script = f'''
    tell application "System Events" to tell process "{APP}"
        set w to front window
        set dlg to w
        try
            set dlg to sheet 1 of w
        end try
        try
            set value of text field 1 of dlg to "{_esc(filename)}"
        end try
        keystroke "g" using {{command down, shift down}}
        delay 0.8
        keystroke "{_esc(dest_folder)}"
        delay 0.4
        key code 36
        delay 1.0
        try
            click button "Save" of dlg
        on error
            key code 36
        end try
        delay 1.0
        try
            click button "Replace" of sheet 1 of dlg
        end try
        try
            click button "Replace" of sheet 1 of w
        end try
    end tell'''
    osascript(script, timeout=60)
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [p for p in glob.glob(os.path.join(dest_folder, "*.fcpxml*"))
                 if os.path.getmtime(p) >= started - 1]
        if found:
            newest = max(found, key=os.path.getmtime)
            # bundles are written in steps; wait for Info.fcpxml
            if newest.endswith(".fcpxmld") and not os.path.exists(os.path.join(newest, "Info.fcpxml")):
                time.sleep(0.5)
                continue
            time.sleep(0.5)
            return newest
        time.sleep(0.5)
    raise TimeoutError(
        "Final Cut Pro didn't produce an export. Make sure the project is open in the timeline, "
        "or export it yourself with File > Export XML… and give me the file path.")


def screenshot(out_png: str, max_width: int = 1600) -> str:
    _need_mac()
    proc = subprocess.run(["screencapture", "-x", "-t", "png", out_png], capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_png):
        raise PermissionError(f"Screenshot failed. {PERMISSION_HELP}")
    subprocess.run(["sips", "--resampleWidth", str(max_width), out_png], capture_output=True)
    return out_png
