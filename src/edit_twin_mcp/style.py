"""Your editing style: defaults the tools use, learned from your own edits.

Stored as JSON in the working folder:
  style.json          cut tightness, audio chain, default look, caption styles,
                      the fonts you use, notes
  looks/<name>.json   saved adjustment-clip looks (e.g. warm_vlog)
"""

from __future__ import annotations

import json
import os
import re

DEFAULT_STYLE = {
    "silence": {
        # Middle-of-the-road starting point. learn_from_edit replaces these with
        # settings calibrated to how tightly YOU trim (on the author's own reel the
        # calibrated settings matched her hand trims 72% vs 13% for loose defaults).
        "threshold_db": -32.0,
        "min_silence": 0.4,
        "keep_after_speech": 0.08,
        "keep_before_speech": 0.12,
    },
    "remove_stutters": True,
    "audio": None,          # saved audio chain (see learn_from_edit / save_audio_style)
    "default_look": None,   # name of a saved look to apply after cutting
    # caption styles by name, and which one to use for short (vertical) and long videos
    "captions": {"styles": {}, "default": {"short": None, "long": None}},
    # fonts the user works with: [{family, face, used_for: [...]}]
    "fonts": [],
    "notes": [],
}


def _dir(work_dir: str) -> str:
    os.makedirs(work_dir, exist_ok=True)
    return work_dir


def style_path(work_dir: str) -> str:
    return os.path.join(_dir(work_dir), "style.json")


def load(work_dir: str) -> dict:
    p = style_path(work_dir)
    style = json.loads(json.dumps(DEFAULT_STYLE))
    if os.path.exists(p):
        with open(p) as fh:
            saved = json.load(fh)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(style.get(k), dict):
                style[k].update(v)
            else:
                style[k] = v
    return style


def save(work_dir: str, style: dict) -> str:
    p = style_path(work_dir)
    with open(p, "w") as fh:
        json.dump(style, fh, indent=2)
    return p


def remember_font(st: dict, family: str, face: str | None = None, used_for: str | None = None) -> None:
    """Add a font to the user's fonts (or note another use for it)."""
    entry = next((f for f in st["fonts"] if f["family"] == family and f.get("face") == face), None)
    if entry is None:
        entry = {"family": family, "face": face, "used_for": []}
        st["fonts"].append(entry)
    if used_for and used_for not in entry["used_for"]:
        entry["used_for"].append(used_for)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "look"


def looks_dir(work_dir: str) -> str:
    d = os.path.join(_dir(work_dir), "looks")
    os.makedirs(d, exist_ok=True)
    return d


def save_look(work_dir: str, name: str, look: dict) -> str:
    p = os.path.join(looks_dir(work_dir), _slug(name) + ".json")
    with open(p, "w") as fh:
        json.dump({"name": name, **look}, fh, indent=2)
    return p


def load_look(work_dir: str, name: str) -> dict:
    d = looks_dir(work_dir)
    p = os.path.join(d, _slug(name) + ".json")
    if not os.path.exists(p):
        matches = [f for f in os.listdir(d) if name.lower() in f.lower()]
        if len(matches) == 1:
            p = os.path.join(d, matches[0])
        else:
            raise FileNotFoundError(
                f"No saved look {name!r}. Saved looks: {list_looks(work_dir) or 'none'}. "
                "Save one with save_look from a project that has the adjustment clip.")
    with open(p) as fh:
        return json.load(fh)


def list_looks(work_dir: str) -> list[str]:
    out = []
    for f in sorted(os.listdir(looks_dir(work_dir))):
        if f.endswith(".json"):
            with open(os.path.join(looks_dir(work_dir), f)) as fh:
                out.append(json.load(fh).get("name", f[:-5]))
    return out


# ---- remembering what Claude sent, so your later hand edits can be compared ---

def record_sent(work_dir: str, project_name: str, path: str) -> None:
    p = os.path.join(_dir(work_dir), ".sent.json")
    data = {}
    if os.path.exists(p):
        with open(p) as fh:
            data = json.load(fh)
    data[project_name] = path
    with open(p, "w") as fh:
        json.dump(data, fh, indent=2)


def last_sent(work_dir: str, project_name: str | None = None) -> str | None:
    p = os.path.join(_dir(work_dir), ".sent.json")
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        data = json.load(fh)
    if project_name and project_name in data:
        return data[project_name]
    if project_name:
        for k, v in data.items():
            if k.lower() in project_name.lower() or project_name.lower() in k.lower():
                return v
    return None
