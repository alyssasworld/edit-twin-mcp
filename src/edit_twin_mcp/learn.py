"""Compare Claude's rough cut with the user's hand-finished version and learn from it.

Works in source-media time, so it doesn't matter how clips were split or moved:
for every media file it computes which parts of the footage each version keeps,
then explains the differences with the transcript and calibrates pause removal
to match the user's own trimming.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter
from fractions import Fraction

from . import media
from .fcpxml import Timeline
from .timecode import parse_time


def kept_source(tl: Timeline) -> dict[str, list[tuple[float, float]]]:
    """file path -> list of kept (start, end) in seconds of file time."""
    out: dict[str, list[tuple[float, float]]] = {}
    for el in tl.items():
        if el.tag != "asset-clip" or el.find("timeMap") is not None:
            continue
        path = tl.media_path(el)
        asset = tl.resource(el.get("ref"))
        if not path or asset is None:
            continue
        s = float(tl.start(el) - parse_time(asset.get("start")))
        out.setdefault(path, []).append((s, s + float(tl.dur(el))))
    for k in out:
        out[k] = merge(out[k])
    return out


def merge(rs):
    rs = sorted(rs)
    m: list[list[float]] = []
    for a, b in rs:
        if m and a <= m[-1][1] + 1e-6:
            m[-1][1] = max(m[-1][1], b)
        else:
            m.append([a, b])
    return [(a, b) for a, b in m]


def subtract(A, B):
    out = []
    for a, b in A:
        cur = [(a, b)]
        for c, d in B:
            nxt = []
            for x, y in cur:
                if d <= x or c >= y:
                    nxt.append((x, y))
                    continue
                if c > x:
                    nxt.append((x, c))
                if d < y:
                    nxt.append((d, y))
            cur = nxt
        out += cur
    return out


def intersect(A, B):
    out = []
    for a, b in A:
        for c, d in B:
            lo, hi = max(a, c), min(b, d)
            if hi > lo:
                out.append((lo, hi))
    return out


def total(R) -> float:
    return sum(b - a for a, b in R)


def _words(transcript: dict | None) -> list[dict]:
    if not transcript:
        return []
    return [w for s in transcript["segments"] for w in (s.get("words") or [])]


def _norm(w: str) -> str:
    return re.sub(r"[^\w']+", "", w.lower())


def calibrate_silence(path: str, keep_reference, removed_truth) -> dict:
    """Grid-search pause-detection settings so that, inside `keep_reference`
    (Claude's kept footage), the predicted pauses best match what the user removed."""
    best = None
    for thr in (-28, -30, -33, -35, -40):
        sil = media.detect_silence(path, float(thr), 0.12)
        for d in (0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
            for lead in (0.03, 0.05, 0.08, 0.1, 0.15):
                for tail in (0.0, 0.03, 0.05, 0.08, 0.1):
                    pred = [(s + (tail if s > 0.001 else 0), e - lead) for s, e in sil
                            if e - s >= d and (e - lead) - (s + tail) > 0.04]
                    pred = intersect(pred, keep_reference)
                    tp = total(intersect(pred, removed_truth))
                    fp = total(pred) - tp
                    fn = total(removed_truth) - tp
                    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0
                    cand = (round(f1, 3), -fp, thr, d, lead, tail)
                    if best is None or cand > best:
                        best = cand
    f1, negfp, thr, d, lead, tail = best
    return {"threshold_db": float(thr), "min_silence": d, "keep_before_speech": lead,
            "keep_after_speech": tail, "match_score": f1}


def compare(claude: Timeline, hand: Timeline, transcripts: dict[str, dict] | None = None,
            calibrate: bool = True) -> dict:
    transcripts = transcripts or {}
    kc, kh = kept_source(claude), kept_source(hand)
    report: dict = {
        "claude_version": {"duration": round(float(claude.duration()), 2), "clips": len(claude.items())},
        "your_version": {"duration": round(float(hand.duration()), 2), "clips": len(hand.items())},
    }
    removed_all, restored_all = [], []
    calib = None
    for path in sorted(set(kc) | set(kh)):
        A, B = kc.get(path, []), kh.get(path, [])
        removed = [(a, b) for a, b in subtract(A, B) if b - a >= 0.02]
        restored = [(a, b) for a, b in subtract(B, A) if b - a >= 0.02]
        words = _words(transcripts.get(path))
        # Whisper stretches word timings over pauses, so use the audio to tell which
        # removed bits actually contained speech.
        quiet = list(media.detect_silence(path, -30.0, 0.05)) if os.path.exists(path) else None

        def voiced(a, b):
            if quiet is None:
                return [(a, b)]
            return [(x, y) for x, y in subtract([(a, b)], quiet) if y - x >= 0.06]

        def text(a, b):
            """Words that were really removed: wholly inside the cut and not one of
            Whisper's stretched timings (which swallow the pause after a word)."""
            return " ".join(w["w"] for w in words
                            if w["s"] >= a - 0.06 and w["e"] <= b + 0.06 and w["e"] - w["s"] < 1.0)

        def context(a, b):
            before = [w["w"] for w in words if (w["s"] + min(w["e"], w["s"] + 0.6)) / 2 < a][-3:]
            after = [w["w"] for w in words if w["s"] >= b - 0.06][:3]
            return " ".join(before) + " | " + " ".join(after)

        def is_pause(r):
            if text(*r):
                return False
            if quiet is not None:
                return total(voiced(*r)) < 0.25
            return r[1] - r[0] < 0.6

        small = [r for r in removed if is_pause(r)]
        content = [r for r in removed if r not in small]
        for a, b in content:
            entry = {"file": os.path.basename(path), "from": round(a, 2), "to": round(b, 2),
                     "seconds": round(b - a, 2), "words": text(a, b), "around": context(a, b)}
            if not entry["words"]:
                entry["kind"] = "hesitation / filler / breath (not in the transcript)"
            removed_all.append(entry)
        for a, b in restored:
            restored_all.append({"file": os.path.basename(path), "from": round(a, 2), "to": round(b, 2),
                                 "seconds": round(b - a, 2), "words": text(a, b)})
        report.setdefault("pause_trimming", []).append({
            "file": os.path.basename(path),
            "small_pause_trims": len(small), "seconds": round(total(small), 2)})
        if words:
            def air(K):
                lead, tail = [], []
                for a, b in K:
                    ws = [w for w in words if a <= (w["s"] + w["e"]) / 2 < b]
                    if ws:
                        lead.append(ws[0]["s"] - a)
                        tail.append(b - ws[-1]["e"])
                return (round(statistics.median(lead), 2) if lead else None,
                        round(statistics.median(tail), 2) if tail else None)
            report.setdefault("air_around_speech", []).append({
                "file": os.path.basename(path),
                "claude_before_after": air(A), "yours_before_after": air(B)})
        if calibrate and removed and os.path.exists(path) and calib is None:
            calib = calibrate_silence(path, A, removed)
    report["you_removed"] = removed_all
    report["you_restored"] = restored_all
    report["seconds_removed"] = round(sum(r["seconds"] for r in removed_all) +
                                      sum(p["seconds"] for p in report.get("pause_trimming", [])), 2)
    first_words = Counter()
    for r in removed_all:
        w = (r["words"] or "").split()
        if 0 < len(w) <= 2:
            first_words[_norm(w[0])] += 1
    report["often_removed_words"] = [w for w, n in first_words.most_common(10) if w]
    if calib:
        report["suggested_pause_settings"] = calib

    # audio: the most common audio chain on the user's dialogue clips
    chains = Counter()
    example = {}
    for i, el in enumerate(hand.items()):
        if el.tag != "asset-clip":
            continue
        d = hand.describe_audio(el)
        key = repr(sorted(d.items()))
        chains[key] += 1
        example.setdefault(key, (str(i), d))
    if chains:
        key, n = chains.most_common(1)[0]
        ref, desc = example[key]
        report["your_audio"] = {"on_clips": n, "of": len([e for e in hand.items() if e.tag == "asset-clip"]),
                                "settings": desc, "example_ref": ref}
    claude_audio = Counter(repr(sorted(claude.describe_audio(e).items())) for e in claude.items()
                           if e.tag == "asset-clip")
    if claude_audio:
        report["claude_audio"] = eval_safe(claude_audio.most_common(1)[0][0])

    # look: adjustment clips and their effects
    looks = []
    for r in hand.adjustment_refs():
        el, _ = hand.resolve(r)
        tl_start = hand.timeline_time(el, hand.start(el), parent=hand.resolve(r.split(".")[0])[0])
        looks.append({"ref": r, "effects": hand.effect_names(el),
                      "covers": [round(float(tl_start - hand.origin), 2),
                                 round(float(tl_start - hand.origin + hand.dur(el)), 2)],
                      "whole_timeline": abs(float(hand.dur(el) - hand.duration())) < 0.1})
    report["your_adjustment_clips"] = looks
    return report


def eval_safe(s: str):
    """Turn the repr(sorted(dict.items())) key back into a dict for display."""
    import ast
    try:
        return dict(ast.literal_eval(s))
    except Exception:
        return s


# ---- stutters & repeated phrases ------------------------------------------------

def find_repeats(words: list[list], max_phrase: int = 10, window_s: float = 20.0) -> list[dict]:
    """words: [[text, start, end], ...] on the timeline. Finds a phrase said twice in a
    row (a retake or stutter) and proposes removing the FIRST take, keeping the later one.

    Matches:  "to new to new tech"  ->  remove "to new"
              "and on 8th of September and on 8th of September"  ->  remove first copy
              "So the main idea ... So the main idea is" (restart)"""
    toks = [_norm(w[0]) for w in words]
    # fillers are skipped when matching phrases ("to, um, to new")

    n_words = len(words)
    found = []
    used_until = -1
    i = 0
    while i < n_words:
        if i <= used_until or not toks[i]:
            i += 1
            continue
        hit = None
        # longest phrase first; the second copy must start within a few words
        for n in range(min(max_phrase, (n_words - i) // 2), 0, -1):
            phrase = toks[i:i + n]
            if not all(phrase):
                continue
            for gap in range(0, 4 if n >= 3 else 1):
                j = i + n + gap
                if j + n > n_words:
                    break
                if toks[j:j + n] == phrase and words[j][1] - words[i][1] <= window_s:
                    if n == 1 and gap == 0 and len(phrase[0]) <= 2 and phrase[0] not in ("i", "a"):
                        continue
                    hit = (n, j)
                    break
            if hit:
                break
        if hit:
            n, j = hit
            cut_from, cut_to = words[i][1], words[j][1]
            found.append({
                "remove_from": round(cut_from, 2), "remove_to": round(cut_to, 2),
                "first_take": " ".join(w[0] for w in words[i:j]),
                "kept_take": " ".join(w[0] for w in words[j:j + n + 4]),
                "kind": "stutter" if n <= 2 and j - i <= 2 * n else "retake",
            })
            used_until = j - 1
            i = j
        else:
            i += 1
    return found


FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm"}


def find_fillers(words: list[list]) -> list[dict]:
    """Filler sounds Whisper transcribed (needs the verbatim transcript)."""
    out = []
    for w in words:
        if _norm(w[0]) in FILLERS:
            out.append({"remove_from": round(w[1], 2), "remove_to": round(w[2], 2), "word": w[0]})
    return out
