"""Captions from the transcript: which words go on screen together, and for how long.

Two modes:
  word  one (or a few) words at a time, each popping on as it's said (short form)
  line  subtitle lines sized to fit the frame in the chosen font (long form),
        at most max_chars per line (42 is the usual comfortable-reading limit)

Caption styles are the user's own, saved in style.json (see style.py); nothing here
assumes a particular font. The built-in defaults below are deliberately plain and
only used until the user saves a style of their own.
"""

from __future__ import annotations

import re

from . import fonts
from .learn import FILLERS, _norm

# Vertical positions as a fraction of frame height from the centre (up is positive).
POSITIONS = {"top": 0.38, "upper": 0.25, "middle": 0.0, "center": 0.0, "lower": -0.25, "bottom": -0.4}

BUILTIN_STYLES = {
    "one_word": {
        "mode": "word", "font": "Helvetica Neue", "face": "Bold", "size_ratio": 0.06,
        "color": "#ffffff", "position": "upper", "case": "as_spoken", "punctuation": "strip",
        "max_words": 1,
    },
    "subtitle": {
        "mode": "line", "font": "Helvetica Neue", "face": "Medium", "size_ratio": 0.045,
        "color": "#ffffff", "position": "bottom", "case": "as_spoken", "punctuation": "keep",
        "max_lines": 1, "max_width": 0.8, "max_chars": 42, "shadow": True,
    },
}
BUILTIN_DEFAULT = {"short": "one_word", "long": "subtitle"}

# Timing (seconds)
HOLD_GAP = 0.4        # word mode: keep a word up until the next one if it starts within this
LINGER = 0.25         # how long a caption stays after its last word when nothing follows soon
PAUSE_BREAK = 0.7     # line mode: a pause this long starts a new caption
MIN_LINE = 0.8        # line mode: shortest caption, if there's room before the next one
MAX_LINE = 6.0        # line mode: longest caption

# When a line is full, prefer starting the next one at one of these words.
BREAK_BEFORE = {"and", "but", "or", "so", "because", "like", "that", "which", "when", "then",
                "if", "while", "with", "to", "for", "about"}

_SENTENCE_END = re.compile(r"[.?!…]['\"”’)]*$")
_SOFT_END = re.compile(r"[,;:–—-]['\"”’)]*$")
_STRIP = re.compile(r"[^\w'’\s-]|(?<!\w)-|-(?!\w)")


def pick_format(width: int, height: int, duration: float) -> str:
    return "short" if height > width or duration < 180 else "long"


def concrete(style: dict, width: int, height: int) -> dict:
    """Fill in pixel size and vertical position for this frame. Styles saved with
    size_ratio / a named position scale to any resolution; ones copied from a title
    keep its exact size and position."""
    s = dict(style)
    if s.get("size") is None:
        s["size"] = round(float(s.get("size_ratio") or 0.05) * height)
    if s.get("y") is None and s.get("position") is not None:
        pos = s["position"]
        frac = POSITIONS[pos] if isinstance(pos, str) else float(pos)
        s["y"] = round(frac * height)
    return s


def summary(style: dict) -> dict:
    """A style without its bulky title template, for showing to the user."""
    out = {k: v for k, v in style.items() if k != "template"}
    if style.get("template"):
        out["copied_from_title"] = style["template"].get("effect") or True
    return out


def resolve_style(saved: dict, name: str | None, fmt: str) -> tuple[str, dict]:
    """(name, style) from the user's saved caption styles, falling back to built-ins."""
    styles = {**BUILTIN_STYLES, **(saved.get("styles") or {})}
    if not name:
        name = (saved.get("default") or {}).get(fmt) or BUILTIN_DEFAULT[fmt]
    if name in styles:
        return name, dict(styles[name])
    loose = [k for k in styles if name.lower() in k.lower()]
    if len(loose) == 1:
        return loose[0], dict(styles[loose[0]])
    raise ValueError(f"No caption style {name!r}. Saved styles: {sorted(styles)}")


def text_of(words: list[str], style: dict) -> str:
    text = " ".join(words)
    if style.get("punctuation") == "strip":
        text = re.sub(r"\s+", " ", _STRIP.sub("", text)).strip()
    case = style.get("case", "as_spoken")
    if case == "lower":
        text = text.lower()
    elif case == "upper":
        text = text.upper()
    return text


def apply_fixes(words: list[list], fixes: dict[str, str] | None) -> list[list]:
    """Whole-word replacements for words the transcript got wrong ("gorge" -> "gorg")."""
    if not fixes:
        return words
    table = {_norm(k): v for k, v in fixes.items()}
    out = []
    for w in words:
        m = re.match(r"^(\W*)(.*?)(\W*)$", w[0])
        core = _norm(m.group(2))
        out.append([m.group(1) + table[core] + m.group(3), *w[1:]] if core in table else list(w))
    return out


class Measure:
    """How wide a caption is in pixels, in the style's font (or an estimate)."""

    def __init__(self, style: dict):
        self.size = float(style["size"])
        self.metrics = fonts.metrics_for(style["font"], style.get("face"))

    def __call__(self, text: str) -> float:
        if self.metrics:
            return self.metrics.width(text, self.size)
        return len(text) * self.size * 0.52


def _wrap(words: list[str], style: dict, measure: Measure, limit: float) -> list[str] | None:
    """Split a caption's words into at most max_lines lines that each fit, as evenly
    as possible. None if they don't fit."""
    lines = int(style.get("max_lines") or 1)
    max_chars = style.get("max_chars")

    def fits(ws):
        t = text_of(ws, style)
        return measure(t) <= limit and (not max_chars or len(t) <= max_chars)

    if fits(words):
        return [text_of(words, style)]
    if lines == 1:
        return None
    if lines == 2:
        best = None
        for i in range(1, len(words)):
            a, b = words[:i], words[i:]
            if fits(a) and fits(b):
                score = max(measure(text_of(a, style)), measure(text_of(b, style)))
                if best is None or score < best[0]:
                    best = (score, [text_of(a, style), text_of(b, style)])
        return best[1] if best else None
    out, cur = [], []
    for w in words:
        if cur and not fits(cur + [w]):
            out.append(cur)
            cur = []
        cur.append(w)
    out.append(cur)
    if len(out) > lines or not all(fits(c) for c in out):
        return None
    return [text_of(c, style) for c in out]


def build(words: list[list], style: dict, frame_width: int, end_limit: float,
          skip_fillers: bool = True) -> list[dict]:
    """words: [[word, start, end], ...] in timeline seconds, in order.
    Returns [{"start", "end", "text"}] with no overlaps, ready to place as titles."""
    words = [w for w in words if w[0].strip()]
    if skip_fillers if style.get("skip_fillers") is None else style["skip_fillers"]:
        words = [w for w in words if _norm(w[0]) not in FILLERS]
    if style.get("punctuation") == "strip":
        words = [w for w in words if text_of([w[0]], style)]
    if not words:
        return []

    groups: list[list[list]] = []
    if style.get("mode", "word") == "word":
        n = max(1, int(style.get("max_words") or 1))
        cur: list[list] = []
        for w in words:
            if cur and (len(cur) >= n or w[1] - cur[-1][2] > PAUSE_BREAK or _SENTENCE_END.search(cur[-1][0])):
                groups.append(cur)
                cur = []
            cur.append(w)
        groups.append(cur)
        hold = HOLD_GAP
    else:
        measure = Measure(style)
        limit = float(style.get("max_width") or 0.8) * frame_width
        cur = []
        for w in words:
            if cur:
                prev = cur[-1][0]
                full = _wrap([x[0] for x in cur + [w]], style, measure, limit) is None
                long = w[2] - cur[0][1] > MAX_LINE
                pause = w[1] - cur[-1][2] > PAUSE_BREAK
                soft = (_SOFT_END.search(prev) and
                        measure(text_of([x[0] for x in cur], style)) > 0.5 * limit)
                if full or long or pause or _SENTENCE_END.search(prev) or soft:
                    carry = []
                    if full:
                        for j in range(len(cur) - 1, max(len(cur) // 2, 1) - 1, -1):
                            if _norm(cur[j][0]) in BREAK_BEFORE:
                                cur, carry = cur[:j], cur[j:]
                                break
                    groups.append(cur)
                    cur = carry
            cur.append(w)
        groups.append(cur)
        hold = PAUSE_BREAK

    out = []
    for i, g in enumerate(groups):
        start, last_end = g[0][1], g[-1][2]
        nxt = groups[i + 1][0][1] if i + 1 < len(groups) else end_limit
        end = nxt if nxt - last_end <= hold else last_end + LINGER
        if style.get("mode") == "line":
            end = max(end, start + MIN_LINE)
            lines = _wrap([x[0] for x in g], style, measure, limit) or [text_of([x[0] for x in g], style)]
            text = "\n".join(lines)
        else:
            text = text_of([x[0] for x in g], style)
        end = min(end, nxt, end_limit)
        if end > start and text:
            out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return out


def to_srt(captions: list[dict]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    return "\n".join(f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{c['text']}\n"
                     for i, c in enumerate(captions, 1))
