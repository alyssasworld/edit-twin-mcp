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

# Final Cut's Basic Title measures font size and position in units where the frame is
# 1080 tall, whatever the project's resolution (on a 2160x3840 reel a title at the
# usual one-word size is ~69 units, not ~230 pixels). Sizes and positions below are
# fractions of that height.
TITLE_HEIGHT = 1080

# Vertical positions as a fraction of the title height from the centre (up is positive).
POSITIONS = {"top": 0.38, "upper": 0.31, "middle": 0.0, "center": 0.0, "lower": -0.25, "bottom": -0.4}

BUILTIN_STYLES = {
    "one_word": {
        "mode": "word", "font": "Helvetica Neue", "face": "Bold", "size_ratio": 0.0641,
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
_STRIP = re.compile(r"[^\w'’\s\ue000\ue001-]|(?<!\w)-|-(?!\w)")
_COMMA, _QUESTION = "\ue000", "\ue001"   # stand-ins that _STRIP leaves alone
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d{3})")


def pick_format(width: int, height: int, duration: float) -> str:
    return "short" if height > width or duration < 180 else "long"


def title_width(width: int, height: int) -> float:
    """The frame's width in title units (see TITLE_HEIGHT)."""
    return width * TITLE_HEIGHT / height


def concrete(style: dict, width: int, height: int) -> dict:
    """Fill in title size and vertical position. Styles saved with size_ratio / a named
    position work at any resolution; ones copied from a title keep its exact size and
    position."""
    s = dict(style)
    if s.get("size") is None:
        s["size"] = round(float(s.get("size_ratio") or 0.05) * TITLE_HEIGHT, 1)
    if s.get("y") is None and s.get("position") is not None:
        pos = s["position"]
        frac = POSITIONS[pos] if isinstance(pos, str) else float(pos)
        s["y"] = round(frac * TITLE_HEIGHT)
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


def _strip(text: str, keep_questions: bool = False) -> str:
    text = _DIGIT_COMMA.sub(_COMMA, text)            # "10,000" stays one number
    if keep_questions:
        text = text.replace("?", _QUESTION)
    text = _STRIP.sub("", text).replace(_COMMA, ",").replace(_QUESTION, "?")
    return re.sub(r"\s+", " ", text).strip()


def _lower(word: str, keep_caps: dict[str, str]) -> str:
    """Lowercase, except acronyms the transcript writes in capitals ("AI") and the user's
    names and words kept capitalised ("YouTube", "Wi-Fi")."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", word)
    core = m.group(2)
    if _norm(core) in keep_caps:
        core = keep_caps[_norm(core)]
    elif not (len(re.sub(r"\W", "", core)) > 1 and core.isupper()):
        core = core.lower()
    return m.group(1) + core + m.group(3)


def text_of(words: list[str], style: dict) -> str:
    text = " ".join(words)
    punct = style.get("punctuation")
    if punct == "strip":
        text = _strip(text)
    elif punct == "questions":
        text = _strip(text, keep_questions=True)
    case = style.get("case", "as_spoken")
    if case == "lower":
        keep = {_norm(w): w for w in style.get("keep_caps") or []}
        text = " ".join(_lower(w, keep) for w in text.split(" "))
    elif case == "upper":
        text = text.upper()
    return text


def apply_fixes(words: list[list], fixes: dict[str, str] | None) -> list[list]:
    """Whole-word replacements for words the transcript got wrong ("cloud" -> "Claude")."""
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


def _join_thousands(words: list[list]) -> list[list]:
    """The transcript sometimes splits "10,000" into "10" and "000"; put it back together."""
    out: list[list] = []
    for w in words:
        if out and re.fullmatch(r"\d{3}\W*", w[0]) and re.fullmatch(r"\d{1,3}(,\d{3})*", out[-1][0]):
            out[-1] = [out[-1][0] + "," + w[0], out[-1][1], w[2]]
        else:
            out.append(list(w))
    return out


def _join_names(words: list[list], keep_caps: list[str] | None) -> list[list]:
    """Put a hyphenated name the user keeps ("Wi-Fi") back together when the
    transcript heard it as separate words, so it shows as one caption."""
    names = [n for n in keep_caps or [] if "-" in n]
    for name in names:
        parts = [_norm(p) for p in name.split("-")]
        out: list[list] = []
        i = 0
        while i < len(words):
            run = words[i:i + len(parts)]
            if len(run) == len(parts) and [_norm(w[0]) for w in run] == parts:
                tail = re.search(r"\W*$", run[-1][0]).group(0)
                out.append([name + tail, run[0][1], run[-1][2]])
                i += len(parts)
            else:
                out.append(words[i])
                i += 1
        words = out
    return words


def build(words: list[list], style: dict, frame_width: int, end_limit: float,
          skip_fillers: bool = True) -> list[dict]:
    """words: [[word, start, end], ...] in timeline seconds, in order; frame_width is
    in title units (title_width).
    Returns [{"start", "end", "text"}] with no overlaps, ready to place as titles."""
    words = _join_names(_join_thousands([w for w in words if w[0].strip()]), style.get("keep_caps"))
    if skip_fillers if style.get("skip_fillers") is None else style["skip_fillers"]:
        words = [w for w in words if _norm(w[0]) not in FILLERS]
    if style.get("punctuation") in ("strip", "questions"):
        words = [w for w in words if text_of([w[0]], style).strip("?")]
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
