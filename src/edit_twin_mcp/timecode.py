"""Rational time helpers for FCPXML.

FCPXML stores every time as a rational number of seconds, e.g. "1001/30000s",
"12s" or "0s". We keep times as Fractions internally so edits stay
frame-accurate, and only convert to floats when showing them to people.
"""

from __future__ import annotations

from fractions import Fraction
import math
import re

_TIME_RE = re.compile(r"^\s*(-?\d+)(?:/(\d+))?s\s*$")


def parse_time(value: str | None, default: Fraction = Fraction(0)) -> Fraction:
    """Parse an FCPXML time string ("100/2500s", "3s", "0s")."""
    if value is None or value == "":
        return default
    m = _TIME_RE.match(value)
    if not m:
        raise ValueError(f"Not an FCPXML time value: {value!r}")
    num = int(m.group(1))
    den = int(m.group(2) or 1)
    return Fraction(num, den)


def format_time(t: Fraction, timebase: int | None = None) -> str:
    """Format a Fraction as an FCPXML time string.

    If `timebase` is given (the denominator of the sequence frame duration,
    e.g. 30000 for 29.97 fps or 2500 for 25 fps) and the value fits it
    exactly, the result uses that denominator, which is how Final Cut Pro
    writes times itself.
    """
    t = Fraction(t)
    if t == 0:
        return "0s"
    if t.denominator == 1:
        return f"{t.numerator}s"
    if timebase:
        scaled = t * timebase
        if scaled.denominator == 1:
            return f"{scaled.numerator}/{timebase}s"
    return f"{t.numerator}/{t.denominator}s"


def snap(t: Fraction, frame: Fraction, mode: str = "nearest") -> Fraction:
    """Snap a time to the frame grid."""
    if frame <= 0:
        return t
    n = t / frame
    if mode == "floor":
        k = math.floor(n)
    elif mode == "ceil":
        k = math.ceil(n)
    else:
        k = math.floor(n + Fraction(1, 2))
    return k * frame


def seconds_to_fraction(seconds: float | int | str, frame: Fraction | None = None) -> Fraction:
    """Convert a user-supplied seconds value (or timecode string) to a Fraction,
    snapped to the frame grid if a frame duration is known."""
    if isinstance(seconds, str) and ":" in seconds:
        t = parse_timecode(seconds, frame)
    else:
        t = Fraction(str(seconds)).limit_denominator(1_000_000)
    return snap(t, frame) if frame else t


def parse_timecode(tc: str, frame: Fraction | None) -> Fraction:
    """Accept "HH:MM:SS:FF", "HH:MM:SS.sss", "MM:SS" or "MM:SS.sss"."""
    parts = tc.strip().split(":")
    if len(parts) == 4:
        h, m, s, f = (int(p) for p in parts)
        if not frame:
            raise ValueError("Frame-based timecode needs a known frame rate")
        return Fraction(h * 3600 + m * 60 + s) + f * frame
    if len(parts) == 3:
        h, m = int(parts[0]), int(parts[1])
        return Fraction(h * 3600 + m * 60) + Fraction(parts[2])
    if len(parts) == 2:
        return Fraction(int(parts[0]) * 60) + Fraction(parts[1])
    raise ValueError(f"Unrecognised timecode: {tc!r}")


def to_timecode(t: Fraction, frame: Fraction) -> str:
    """Human timecode HH:MM:SS:FF for a time relative to the timeline start."""
    if frame <= 0:
        return f"{float(t):.3f}s"
    fps = round(1 / frame)
    total_frames = int(snap(t, frame) / frame)
    sign = "-" if total_frames < 0 else ""
    total_frames = abs(total_frames)
    ff = total_frames % fps
    total_seconds = total_frames // fps
    return f"{sign}{total_seconds // 3600:02d}:{(total_seconds // 60) % 60:02d}:{total_seconds % 60:02d}:{ff:02d}"


def secs(t: Fraction) -> float:
    return round(float(t), 3)
