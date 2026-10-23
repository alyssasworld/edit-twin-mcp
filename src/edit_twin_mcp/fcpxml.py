"""Read and edit Final Cut Pro timelines through FCPXML.

Final Cut Pro's scripting interface is read-only, so the reliable way to edit
a project from outside the app is: export the project as FCPXML, change the
XML, and import the result back. This module does the middle step with
frame-accurate rational time maths, and keeps everything it doesn't
understand (effects and colour grading plugins, keyframes, metadata) intact.

Times exposed to callers are seconds from the start of the timeline.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable

from . import media
from .timecode import format_time, parse_time, secs, snap, to_timecode

PRIMARY_TYPES = {
    "asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip", "gap", "title",
    "video", "audio", "audition", "live-drawing",
}
ANCHORABLE = PRIMARY_TYPES | {"spine", "caption"}
MARKER_TAGS = {"marker", "chapter-marker", "analysis-marker", "hidden-clip-marker"}
RANGE_TAGS = {"rating", "keyword"}
FILTER_TAGS = {"filter-video", "filter-video-mask", "filter-audio"}
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
BASIC_TITLE_POSITION_KEY = "9999/999166631/999166633/1/100/101"

# Order of child elements inside a clip, per the FCPXML DTD. Lower comes first.
_RANK = {
    "param": 0, "text": 0.1, "text-style-def": 0.2,
    "note": 1, "conform-rate": 2, "timeMap": 3,
    "object-tracker": 4, "adjust-crop": 4.01, "adjust-corners": 4.02, "adjust-conform": 4.03,
    "adjust-transform": 4.04, "adjust-blend": 4.05, "adjust-stabilization": 4.06,
    "adjust-rollingShutter": 4.07, "adjust-360-transform": 4.08, "adjust-reorient": 4.09,
    "adjust-orientation": 4.10, "adjust-cinematic": 4.11, "adjust-colorConform": 4.12,
    "adjust-stereo-3D": 4.13, "adjust-volume": 4.5, "adjust-panner": 4.6,
    "mc-source": 5,
    # anchored items / inner story elements: 6
    "marker": 7, "chapter-marker": 7, "rating": 7, "keyword": 7,
    "analysis-marker": 7, "hidden-clip-marker": 7,
    "audio-channel-source": 8, "audio-role-source": 8, "sync-source": 8,
    "filter-video": 9, "filter-video-mask": 9, "filter-audio": 9.5,
    "metadata": 10, "reserved": 11,
}


def _rank(el: ET.Element) -> float:
    if el.tag in ANCHORABLE:
        return 6
    return _RANK.get(el.tag, 6)


def insert_ordered(parent: ET.Element, child: ET.Element) -> None:
    """Insert `child` where the DTD expects it (after the last sibling of
    equal or lower rank)."""
    r = _rank(child)
    pos = 0
    for i, sib in enumerate(list(parent)):
        if _rank(sib) <= r:
            pos = i + 1
    parent.insert(pos, child)


def _is_anchored(el: ET.Element) -> bool:
    return el.tag in ANCHORABLE and el.get("lane") not in (None, "", "0")


class EditError(ValueError):
    pass


@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d: dict = {"changes": self.changes}
        if self.warnings:
            d["warnings"] = self.warnings
        return d


# --------------------------------------------------------------------------- #
#  Timeline
# --------------------------------------------------------------------------- #

class Timeline:
    """One Final Cut Pro project loaded from an FCPXML file or .fcpxmld bundle."""

    def __init__(self, path: str, project: str | None = None):
        self.source_path = os.path.abspath(os.path.expanduser(path))
        self.bundle_dir: str | None = None
        xml_path = self.source_path
        if os.path.isdir(xml_path):
            if not xml_path.rstrip("/").endswith(".fcpxmld"):
                raise EditError(f"{path} is a folder, not an .fcpxml file or .fcpxmld bundle")
            self.bundle_dir = xml_path
            xml_path = os.path.join(xml_path, "Info.fcpxml")
        if not os.path.exists(xml_path):
            raise EditError(f"File not found: {xml_path}")
        self.tree = ET.parse(xml_path)
        self.root = self.tree.getroot()
        if self.root.tag != "fcpxml":
            raise EditError("This is not an FCPXML document")
        self.version = self.root.get("version", "1.0")
        self._project_index = self._choose_project(project)
        self._bind()
        self.history: list[bytes] = []
        self.redo_stack: list[bytes] = []

    # ---- loading -------------------------------------------------------- #

    def projects(self) -> list[ET.Element]:
        return list(self.root.iter("project"))

    def _choose_project(self, name: str | None) -> int:
        projects = self.projects()
        if not projects:
            raise EditError(
                "No project (timeline) found in this file. In Final Cut Pro select the "
                "project in the browser, then File > Export XML."
            )
        if name is None:
            return 0
        for i, p in enumerate(projects):
            if p.get("name") == name:
                return i
        for i, p in enumerate(projects):
            if name.lower() in (p.get("name") or "").lower():
                return i
        raise EditError(f"No project named {name!r}. Found: {[p.get('name') for p in projects]}")

    def _bind(self) -> None:
        self.project = self.projects()[self._project_index]
        self.sequence = self.project.find("sequence")
        if self.sequence is None:
            raise EditError("Project has no sequence")
        self.spine = self.sequence.find("spine")
        if self.spine is None:
            self.spine = ET.SubElement(self.sequence, "spine")
        self.resources = self.root.find("resources")
        if self.resources is None:
            self.resources = ET.Element("resources")
            self.root.insert(0, self.resources)
        self.format_el = self.resource(self.sequence.get("format"))
        fd_raw = (self.format_el.get("frameDuration") if self.format_el is not None else None) or "100/3000s"
        self.frame = parse_time(fd_raw)
        m = re.match(r"^\d+/(\d+)s$", fd_raw)
        self.timebase = int(m.group(1)) if m else self.frame.denominator
        self.origin = parse_time(self.sequence.get("tcStart"))
        first = next((c for c in self.spine if c.tag != "transition"), None)
        if first is not None and first.get("offset"):
            self.origin = parse_time(first.get("offset"))

    @property
    def version_tuple(self) -> tuple[int, int]:
        major, _, minor = self.version.partition(".")
        return int(major), int(minor or 0)

    # ---- generic helpers ---------------------------------------------- #

    def resource(self, rid: str | None) -> ET.Element | None:
        if not rid:
            return None
        for el in self.resources:
            if el.get("id") == rid:
                return el
        return None

    def _all_ids(self) -> set[str]:
        return {el.get("id") for el in self.root.iter() if el.get("id")}

    def _new_id(self, prefix: str = "r") -> str:
        ids = self._all_ids()
        n = 1
        for i in ids:
            m = re.match(rf"^{re.escape(prefix)}(\d+)$", i)
            if m:
                n = max(n, int(m.group(1)) + 1)
        while f"{prefix}{n}" in ids:
            n += 1
        return f"{prefix}{n}"

    def t(self, value: Fraction) -> str:
        return format_time(value, self.timebase)

    def snap(self, value: Fraction, mode: str = "nearest") -> Fraction:
        return snap(value, self.frame, mode)

    def abs_time(self, seconds) -> Fraction:
        """User seconds (from timeline start) -> absolute timeline time, frame snapped."""
        from .timecode import seconds_to_fraction

        return self.origin + seconds_to_fraction(seconds, self.frame)

    def rel(self, t: Fraction) -> float:
        return secs(t - self.origin)

    @staticmethod
    def off(el: ET.Element) -> Fraction:
        return parse_time(el.get("offset"))

    @staticmethod
    def start(el: ET.Element) -> Fraction:
        return parse_time(el.get("start"))

    @staticmethod
    def dur(el: ET.Element) -> Fraction:
        if el.tag == "spine" and el.get("duration") is None:
            kids = [c for c in el if c.tag != "transition"]
            if not kids:
                return Fraction(0)
            first, last = kids[0], kids[-1]
            return (parse_time(last.get("offset")) + parse_time(last.get("duration"))
                    - parse_time(first.get("offset")))
        return parse_time(el.get("duration"))

    def items(self) -> list[ET.Element]:
        """Primary storyline items (no transitions)."""
        return [c for c in self.spine if c.tag != "transition"]

    def duration(self) -> Fraction:
        items = self.items()
        if not items:
            return Fraction(0)
        last = items[-1]
        return self.off(last) + self.dur(last) - self.origin

    def item_name(self, el: ET.Element) -> str:
        if el.get("name"):
            return el.get("name")
        res = self.resource(el.get("ref"))
        if res is not None and res.get("name"):
            return res.get("name")
        return el.tag

    def media_path(self, el: ET.Element) -> str | None:
        """Local file behind an asset-clip / video / audio element, if any."""
        asset = self.resource(el.get("ref"))
        if asset is None or asset.tag != "asset":
            return None
        rep = asset.find("media-rep")
        src = rep.get("src") if rep is not None else asset.get("src")
        return media.url_to_path(src) if src else None

    def effect_names(self, el: ET.Element) -> list[str]:
        names = []
        for f in el:
            if f.tag in FILTER_TAGS:
                res = self.resource(f.get("ref"))
                names.append(f.get("name") or (res.get("name") if res is not None else f.tag))
        return names

    # ---- references ("3" = 4th storyline item, "3.1" = its 2nd connected item)

    def resolve(self, ref) -> tuple[ET.Element, ET.Element | None]:
        """Return (element, parent_spine_item_or_None)."""
        s = str(ref).strip()
        parts = s.split(".")
        items = self.items()
        try:
            idx = int(parts[0])
            item = items[idx]
        except (ValueError, IndexError):
            raise EditError(f"No storyline item {parts[0]!r}; valid indexes are 0..{len(items) - 1}")
        if len(parts) == 1:
            return item, None
        anchored = [c for c in item if _is_anchored(c)]
        try:
            child = anchored[int(parts[1])]
        except (ValueError, IndexError):
            raise EditError(f"Item {idx} has no connected item {parts[1]!r} ({len(anchored)} connected)")
        return child, item

    def item_at(self, t: Fraction, prefer_next: bool = True) -> ET.Element | None:
        items = self.items()
        for el in items:
            a, b = self.off(el), self.off(el) + self.dur(el)
            if a <= t < b:
                return el
        if items and t >= self.off(items[-1]) + self.dur(items[-1]):
            return items[-1]
        return None

    def local_time(self, el: ET.Element, t: Fraction) -> Fraction:
        """Absolute timeline time -> time inside a storyline item."""
        return self.start(el) + (t - self.off(el))

    def timeline_time(self, el: ET.Element, local: Fraction, parent: ET.Element | None = None) -> Fraction:
        if parent is None:
            return self.off(el) + (local - self.start(el))
        parent_tl = self.off(parent) + (self.off(el) - self.start(parent))
        return parent_tl + (local - self.start(el))

    # ---- undo ---------------------------------------------------------- #

    def checkpoint(self) -> None:
        self.history.append(ET.tostring(self.root))
        self.history = self.history[-50:]
        self.redo_stack.clear()

    def _restore(self, blob: bytes) -> None:
        self.root = ET.fromstring(blob)
        self.tree = ET.ElementTree(self.root)
        self._bind()

    def undo(self) -> bool:
        if not self.history:
            return False
        self.redo_stack.append(ET.tostring(self.root))
        self._restore(self.history.pop())
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.history.append(ET.tostring(self.root))
        self._restore(self.redo_stack.pop())
        return True

    # ------------------------------------------------------------------ #
    #  Reading
    # ------------------------------------------------------------------ #

    def summary(self) -> dict:
        fmt = self.format_el
        fps = round(float(1 / self.frame), 3) if self.frame else None
        return {
            "project": self.project.get("name"),
            "projects_in_file": [p.get("name") for p in self.projects()],
            "fcpxml_version": self.version,
            "format": {
                "fps": fps,
                "width": fmt.get("width") if fmt is not None else None,
                "height": fmt.get("height") if fmt is not None else None,
                "name": fmt.get("name") if fmt is not None else None,
            },
            "duration_seconds": secs(self.duration()),
            "duration_timecode": to_timecode(self.duration(), self.frame),
            "storyline_items": len(self.items()),
            "transitions": len(self.spine.findall("transition")),
            "connected_items": sum(1 for it in self.items() for c in it if _is_anchored(c)),
            "markers": len(self.list_markers()),
        }

    def describe_item(self, idx: int, el: ET.Element, detail: bool = True) -> dict:
        a = self.off(el)
        d = {
            "index": idx,
            "type": el.tag,
            "name": self.item_name(el),
            "start": self.rel(a),
            "end": self.rel(a + self.dur(el)),
            "duration": secs(self.dur(el)),
            "timecode": to_timecode(a - self.origin, self.frame),
        }
        if el.tag != "gap":
            d["source_in"] = secs(self.start(el))
        path = self.media_path(el)
        if path:
            d["media"] = os.path.basename(path)
        if el.find("timeMap") is not None:
            d["retimed"] = True
        audio = self.describe_audio(el)
        if audio.get("volume"):
            d["volume"] = audio.pop("volume")
        audio.pop("audio_effects", None)
        if audio:
            d["audio"] = audio
        fx = self.effect_names(el)
        if fx:
            d["effects"] = fx
        if el.get("enabled") == "0":
            d["disabled"] = True
        if el.tag == "title":
            d["text"] = "".join(el.find("text").itertext()) if el.find("text") is not None else ""
        if detail:
            connected = []
            for ci, c in enumerate([c for c in el if _is_anchored(c)]):
                tl = self.timeline_time(c, self.start(c), parent=el)
                cd = {
                    "ref": f"{idx}.{ci}",
                    "type": c.tag,
                    "name": self.item_name(c),
                    "lane": int(c.get("lane")),
                    "start": self.rel(tl),
                    "end": self.rel(tl + self.dur(c)),
                }
                if c.tag == "title" and c.find("text") is not None:
                    cd["text"] = "".join(c.find("text").itertext())
                cfx = self.effect_names(c)
                if cfx:
                    cd["effects"] = cfx
                if self.is_adjustment(c):
                    cd["adjustment_clip"] = True
                connected.append(cd)
            if connected:
                d["connected"] = connected
        return d

    def list_items(self, start: float | None = None, end: float | None = None) -> list[dict]:
        out = []
        for i, el in enumerate(self.items()):
            d = self.describe_item(i, el)
            if start is not None and d["end"] <= start:
                continue
            if end is not None and d["start"] >= end:
                continue
            out.append(d)
        return out

    def list_markers(self) -> list[dict]:
        out = []

        def collect(el, parent, ref):
            for m in el:
                if m.tag in ("marker", "chapter-marker"):
                    ms = parse_time(m.get("start"))
                    if not (self.start(el) <= ms < self.start(el) + self.dur(el)):
                        continue  # outside the visible part of the clip
                    tl = self.timeline_time(el, parse_time(m.get("start")), parent=parent)
                    kind = "chapter" if m.tag == "chapter-marker" else (
                        ("todo-done" if m.get("completed") == "1" else "todo")
                        if m.get("completed") is not None else "standard")
                    entry = {"time": self.rel(tl), "timecode": to_timecode(tl - self.origin, self.frame),
                             "name": m.get("value"), "kind": kind, "on": ref}
                    if m.get("note"):
                        entry["note"] = m.get("note")
                    out.append(entry)

        for i, el in enumerate(self.items()):
            collect(el, None, str(i))
            for ci, c in enumerate([c for c in el if _is_anchored(c)]):
                collect(c, el, f"{i}.{ci}")
        return sorted(out, key=lambda m: m["time"])

    # ------------------------------------------------------------------ #
    #  Core edit: ripple-delete time ranges from the primary storyline
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge(ranges: Iterable[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
        rs = sorted((a, b) for a, b in ranges if b > a)
        merged: list[list[Fraction]] = []
        for a, b in rs:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    @staticmethod
    def _subtract(a: Fraction, b: Fraction, cuts) -> list[tuple[Fraction, Fraction]]:
        keeps = [(a, b)]
        for ca, cb in cuts:
            nxt = []
            for ka, kb in keeps:
                if cb <= ka or ca >= kb:
                    nxt.append((ka, kb))
                    continue
                if ca > ka:
                    nxt.append((ka, ca))
                if cb < kb:
                    nxt.append((cb, kb))
            keeps = nxt
        return keeps

    @staticmethod
    def _overlaps(a, b, cuts) -> bool:
        return any(ca < b and cb > a for ca, cb in cuts)

    def _piece(self, el: ET.Element, sa: Fraction, sb: Fraction, orphans: list) -> ET.Element:
        """Copy of a storyline item covering timeline range [sa, sb)."""
        new = copy.deepcopy(el)
        off, st = self.off(el), self.start(el)
        nst = st + (sa - off)
        nend = nst + (sb - sa)
        if el.get("start") is not None or nst != 0:
            new.set("start", self.t(nst))
        new.set("duration", self.t(sb - sa))
        for c in list(new):
            if _is_anchored(c):
                co = self.off(c)
                if not (nst <= co < nend):
                    new.remove(c)
            elif c.tag in MARKER_TAGS:
                if not (nst <= parse_time(c.get("start")) < nend):
                    new.remove(c)
            elif c.tag in RANGE_TAGS and c.get("start") is not None:
                cs = parse_time(c.get("start"))
                ce = cs + parse_time(c.get("duration"))
                if ce <= nst or cs >= nend:
                    new.remove(c)
        return new

    def _orphans_of(self, el: ET.Element, keeps) -> list[tuple[ET.Element, Fraction]]:
        """Connected items of `el` whose anchor point is not inside any kept range."""
        off, st = self.off(el), self.start(el)
        result = []
        for c in el:
            if _is_anchored(c):
                tl = off + (self.off(c) - st)
                if not any(ka <= tl < kb for ka, kb in keeps):
                    result.append((copy.deepcopy(c), tl))
        return result

    def cut_ranges(self, ranges, connected: str = "keep", label: str = "Removed") -> Report:
        """Ripple-delete absolute timeline ranges from the primary storyline.

        connected="keep" re-attaches connected clips (B-roll, titles, music)
        whose anchor point falls inside a removed range at the cut point, so
        they survive. connected="delete" removes them, like FCP's ripple delete.
        """
        rep = Report()
        end = self.origin + self.duration()
        cuts = [(max(self.origin, self.snap(a)), min(end, self.snap(b))) for a, b in ranges]
        cuts = self._merge(cuts)

        # Retimed clips can't be cut precisely; protect them.
        for el in self.items():
            if el.find("timeMap") is not None:
                a, b = self.off(el), self.off(el) + self.dur(el)
                keeps = self._subtract(a, b, cuts)
                if keeps and keeps != [(a, b)]:
                    rep.warnings.append(
                        f"Skipped cuts inside retimed clip '{self.item_name(el)}' "
                        f"({self.rel(a)}s–{self.rel(b)}s); trim it in Final Cut Pro instead.")
                    cuts = self._merge(
                        [r for ca, cb in cuts for r in self._subtract(ca, cb, [(a, b)])])
        if not cuts:
            rep.changes.append("Nothing to remove.")
            return rep

        removed_total = sum(b - a for a, b in cuts)

        def mapped(t: Fraction) -> Fraction:
            return t - sum(min(t, cb) - ca for ca, cb in cuts if ca < t)

        new_children: list[tuple] = []
        orphans: list[tuple[ET.Element, Fraction]] = []
        removed_items = trimmed_items = 0
        kids = list(self.spine)
        for i, el in enumerate(kids):
            if el.tag == "transition":
                a = self.off(el)
                b = a + self.dur(el)
                if self._overlaps(a, b, cuts):
                    rep.warnings.append(f"Removed a transition at {self.rel(a)}s because a cut touched it.")
                    continue
                nxt = next((k for k in kids[i + 1:] if k.tag != "transition"), None)
                if nxt is None:
                    continue
                new_children.append(("T", el, self.off(nxt) - a, nxt))
                continue
            a, b = self.off(el), self.off(el) + self.dur(el)
            keeps = self._subtract(a, b, cuts)
            if keeps == [(a, b)]:
                new_children.append(("C", el, el))
                continue
            if connected == "keep":
                orphans.extend(self._orphans_of(el, keeps))
            if not keeps:
                removed_items += 1
                continue
            trimmed_items += 1
            for n, (sa, sb) in enumerate(keeps):
                new_children.append(("C", self._piece(el, sa, sb, orphans), el if n == 0 else None))

        # Rebuild the spine.
        for c in list(self.spine):
            self.spine.remove(c)
        pos = self.origin
        first_piece_offset: dict[int, Fraction] = {}
        for entry in new_children:
            if entry[0] == "C":
                _, el, original = entry
                el.set("offset", self.t(pos))
                if original is not None:
                    first_piece_offset[id(original)] = pos
                pos += self.dur(el)
        placed: dict[int, ET.Element] = {}
        for entry in new_children:
            if entry[0] == "C":
                self.spine.append(entry[1])
                if entry[2] is not None:
                    placed[id(entry[2])] = entry[1]
        for entry in new_children:
            if entry[0] != "T":
                continue
            _, tr, delta, nxt = entry
            target = placed.get(id(nxt))
            if target is None:
                continue
            tr.set("offset", self.t(first_piece_offset[id(nxt)] - delta))
            self.spine.insert(list(self.spine).index(target), tr)

        # Re-attach connected items whose anchor footage was removed.
        for child, tl in orphans:
            point = mapped(tl)
            host = self.item_at(point)
            if host is None:
                continue
            if point >= self.off(host) + self.dur(host):
                point = self.off(host) + self.dur(host) - self.frame
            child.set("offset", self.t(self.local_time(host, point)))
            insert_ordered(host, child)
        if orphans:
            rep.changes.append(f"Kept {len(orphans)} connected item(s) by re-attaching them at the cut point.")

        self._update_sequence_duration()
        rep.changes.insert(0,
            f"{label} {len(cuts)} range(s), {secs(removed_total)}s total "
            f"({trimmed_items} clip(s) shortened or split, {removed_items} removed). "
            f"New duration {secs(self.duration())}s.")
        return rep

    def _update_sequence_duration(self) -> None:
        self.sequence.set("duration", self.t(self.duration()))

    # ------------------------------------------------------------------ #
    #  Edits built on top of the core
    # ------------------------------------------------------------------ #

    def split_at(self, seconds) -> Report:
        t = self.abs_time(seconds)
        el = self.item_at(t)
        if el is None:
            raise EditError("That time is outside the timeline")
        a, b = self.off(el), self.off(el) + self.dur(el)
        if t <= a or t >= b:
            return Report(changes=[f"{self.rel(t)}s is already an edit point."])
        if el.find("timeMap") is not None:
            raise EditError("Can't split a retimed clip from XML; use the Blade tool in Final Cut Pro.")
        left = self._piece(el, a, t, [])
        right = self._piece(el, t, b, [])
        left.set("offset", self.t(a))
        right.set("offset", self.t(t))
        idx = list(self.spine).index(el)
        self.spine.remove(el)
        self.spine.insert(idx, right)
        self.spine.insert(idx, left)
        return Report(changes=[f"Split '{self.item_name(el)}' at {self.rel(t)}s."])

    def delete_item(self, ref, ripple: bool = True) -> Report:
        el, parent = self.resolve(ref)
        name = self.item_name(el)
        if parent is not None:
            parent.remove(el)
            return Report(changes=[f"Deleted connected item '{name}'."])
        a, b = self.off(el), self.off(el) + self.dur(el)
        if ripple:
            rep = self.cut_ranges([(a, b)], connected="delete", label="Deleted")
            rep.changes[0] = f"Deleted '{name}' ({secs(b - a)}s) and closed the gap. " + rep.changes[0]
            return rep
        gap = ET.Element("gap", {"name": "Gap", "offset": el.get("offset"),
                                 "start": self.t(self.start(el)), "duration": el.get("duration")})
        for c in el:
            if _is_anchored(c) or c.tag in MARKER_TAGS:
                insert_ordered(gap, copy.deepcopy(c))
        idx = list(self.spine).index(el)
        self.spine.remove(el)
        self.spine.insert(idx, gap)
        return Report(changes=[f"Replaced '{name}' with a {secs(b - a)}s gap (timing unchanged)."])

    def trim(self, ref, head: float = 0, tail: float = 0) -> Report:
        el, parent = self.resolve(ref)
        h = self.snap(Fraction(str(head)))
        tl_ = self.snap(Fraction(str(tail)))
        if h < 0 or tl_ < 0:
            raise EditError("Only shortening is supported (extending needs media handles; drag the edge in FCP).")
        if h + tl_ >= self.dur(el):
            raise EditError("That would remove the whole clip; use delete_clip instead.")
        if parent is not None:
            el.set("offset", self.t(self.off(el) + h))
            el.set("start", self.t(self.start(el) + h))
            el.set("duration", self.t(self.dur(el) - h - tl_))
            return Report(changes=[f"Trimmed connected '{self.item_name(el)}' by {secs(h)}s head, {secs(tl_)}s tail."])
        a, b = self.off(el), self.off(el) + self.dur(el)
        ranges = []
        if h:
            ranges.append((a, a + h))
        if tl_:
            ranges.append((b - tl_, b))
        rep = self.cut_ranges(ranges, label="Trimmed")
        rep.changes[0] = f"Trimmed '{self.item_name(el)}': {secs(h)}s off the start, {secs(tl_)}s off the end. " + rep.changes[0]
        return rep

    def keep_only(self, ranges_seconds: list[tuple[float, float]]) -> Report:
        keep = self._merge([(self.abs_time(a), self.abs_time(b)) for a, b in ranges_seconds])
        end = self.origin + self.duration()
        cuts = self._subtract(self.origin, end, keep)
        return self.cut_ranges(cuts, label="Removed everything outside the kept ranges:")

    def move_item(self, index: int, to_index: int) -> Report:
        items = self.items()
        if not (0 <= index < len(items)) or not (0 <= to_index < len(items)):
            raise EditError(f"Indexes must be between 0 and {len(items) - 1}")
        kids = list(self.spine)
        # remember transitions by their neighbours
        pairs = {}
        for i, c in enumerate(kids):
            if c.tag == "transition":
                prev = next((k for k in reversed(kids[:i]) if k.tag != "transition"), None)
                nxt = next((k for k in kids[i + 1:] if k.tag != "transition"), None)
                pairs[(id(prev), id(nxt))] = (c, self.off(nxt) - self.off(c) if nxt is not None else Fraction(0))
        moving = items.pop(index)
        items.insert(to_index, moving)
        for c in kids:
            self.spine.remove(c)
        pos = self.origin
        dropped = 0
        prev = None
        for el in items:
            el.set("offset", self.t(pos))
            key = (id(prev), id(el))
            if key in pairs:
                tr, delta = pairs.pop(key)
                tr.set("offset", self.t(pos - delta))
                self.spine.append(tr)
            self.spine.append(el)
            pos += self.dur(el)
            prev = el
        dropped = len(pairs)
        self._update_sequence_duration()
        rep = Report(changes=[f"Moved '{self.item_name(moving)}' from position {index} to {to_index}."])
        if dropped:
            rep.warnings.append(f"Removed {dropped} transition(s) whose neighbouring clips changed.")
        return rep

    # ---- markers --------------------------------------------------------

    def add_marker(self, seconds, name: str, kind: str = "standard", note: str | None = None) -> Report:
        t = self.abs_time(seconds)
        el = self.item_at(t)
        if el is None:
            raise EditError("That time is outside the timeline")
        local = self.local_time(el, min(t, self.off(el) + self.dur(el) - self.frame))
        tag = "chapter-marker" if kind == "chapter" else "marker"
        m = ET.Element(tag, {"start": self.t(local), "duration": self.t(self.frame), "value": name})
        if kind in ("todo", "todo-done"):
            m.set("completed", "1" if kind == "todo-done" else "0")
        if kind == "chapter":
            m.set("posterOffset", "0s")
        if note:
            m.set("note", note)
        insert_ordered(el, m)
        return Report(changes=[f"Added {kind} marker '{name}' at {self.rel(t)}s on '{self.item_name(el)}'."])

    def remove_markers(self, name_contains: str | None = None, kind: str | None = None) -> Report:
        n = 0
        for el in self.items():
            for holder in [el] + [c for c in el if _is_anchored(c)]:
                for m in list(holder):
                    if m.tag not in ("marker", "chapter-marker"):
                        continue
                    mk = "chapter" if m.tag == "chapter-marker" else ("todo" if m.get("completed") is not None else "standard")
                    if kind and kind != mk:
                        continue
                    if name_contains and name_contains.lower() not in (m.get("value") or "").lower():
                        continue
                    holder.remove(m)
                    n += 1
        return Report(changes=[f"Removed {n} marker(s)."])

    def ranges_between_markers(self, name_contains: str) -> list[tuple[float, float]]:
        """Pairs consecutive markers whose name contains the text: (in, out), (in, out)..."""
        ms = [m for m in self.list_markers() if name_contains.lower() in (m["name"] or "").lower()]
        return [(ms[i]["time"], ms[i + 1]["time"]) for i in range(0, len(ms) - 1, 2)]

    # ---- titles ----------------------------------------------------------

    def _basic_title_effect(self) -> str:
        for el in self.resources:
            if el.tag == "effect" and "Basic Title" in (el.get("uid") or ""):
                return el.get("id")
        rid = self._new_id()
        self.resources.append(ET.Element("effect", {"id": rid, "name": "Basic Title", "uid": BASIC_TITLE_UID}))
        return rid

    @staticmethod
    def _color(hex_or_name: str) -> str:
        named = {"white": "#ffffff", "black": "#000000", "yellow": "#ffd400", "red": "#ff3b30",
                 "pink": "#ff6fb5", "blue": "#2f7bff", "green": "#34c759"}
        h = named.get(hex_or_name.lower(), hex_or_name).lstrip("#")
        if len(h) == 6:
            h += "ff"
        r, g, b, a = (int(h[i:i + 2], 16) / 255 for i in range(0, 8, 2))
        return " ".join(f"{v:.4g}" for v in (r, g, b, a))

    def add_title(self, text: str, at, duration: float = 3.0, lane: int = 1, font: str = "Helvetica Neue",
                  size: float = 72, color: str = "#ffffff", bold: bool = False,
                  y_offset: float | None = None, alignment: str = "center") -> Report:
        t = self.abs_time(at)
        host = self.item_at(t)
        if host is None:
            raise EditError("That time is outside the timeline")
        rid = self._basic_title_effect()
        ts = self._new_id("ts")
        d = self.snap(Fraction(str(duration)))
        title = ET.Element("title", {
            "ref": rid, "lane": str(lane), "offset": self.t(self.local_time(host, t)),
            "name": (text[:40] + "…") if len(text) > 40 else text,
            "start": "3600s", "duration": self.t(d),
        })
        if y_offset is not None:
            ET.SubElement(title, "param", {"name": "Position", "key": BASIC_TITLE_POSITION_KEY,
                                           "value": f"0 {y_offset:g}"})
        text_el = ET.SubElement(title, "text")
        span = ET.SubElement(text_el, "text-style", {"ref": ts})
        span.text = text
        tdef = ET.SubElement(title, "text-style-def", {"id": ts})
        style = {"font": font, "fontSize": f"{size:g}", "fontFace": "Bold" if bold else "Regular",
                 "fontColor": self._color(color), "alignment": alignment}
        if bold:
            style["bold"] = "1"
        ET.SubElement(tdef, "text-style", style)
        insert_ordered(host, title)
        return Report(changes=[f"Added title “{text}” at {self.rel(t)}s for {secs(d)}s (lane {lane})."])

    def edit_title(self, ref, text: str | None = None, color: str | None = None, size: float | None = None) -> Report:
        el, _ = self.resolve(ref)
        if el.tag != "title":
            raise EditError(f"{ref} is a {el.tag}, not a title")
        if text is not None:
            spans = el.findall("./text/text-style")
            if spans:
                spans[0].text = text
                for s in spans[1:]:
                    s.text = ""
            el.set("name", text[:40])
        for tdef in el.findall("text-style-def/text-style"):
            if color:
                tdef.set("fontColor", self._color(color))
            if size:
                tdef.set("fontSize", f"{size:g}")
        return Report(changes=[f"Updated title {ref}."])

    # ---- audio / effects -------------------------------------------------

    def set_volume(self, ref, db: float) -> Report:
        el, _ = self.resolve(ref)
        if el.tag in ("title", "gap", "video", "transition"):
            raise EditError(f"A {el.tag} has no audio to adjust")
        vol = el.find("adjust-volume")
        if vol is None:
            vol = ET.Element("adjust-volume")
            insert_ordered(el, vol)
        vol.set("amount", f"{db:g}dB")
        return Report(changes=[f"Set volume of '{self.item_name(el)}' to {db:g} dB."])

    def set_enabled(self, ref, enabled: bool) -> Report:
        el, _ = self.resolve(ref)
        if enabled:
            el.attrib.pop("enabled", None)
        else:
            el.set("enabled", "0")
        return Report(changes=[f"{'Enabled' if enabled else 'Disabled'} '{self.item_name(el)}'."])

    def copy_effects(self, from_ref, to_refs: list, name_contains: str | None = None,
                     replace_existing: bool = True) -> Report:
        src, _ = self.resolve(from_ref)
        filters = [f for f in src if f.tag in FILTER_TAGS]
        if name_contains:
            filters = [f for f in filters if name_contains.lower() in
                       (f.get("name") or (self.resource(f.get("ref")).get("name") if self.resource(f.get("ref")) is not None else "")).lower()]
        if not filters:
            raise EditError(f"'{self.item_name(src)}' has no matching effects to copy")
        names = self.effect_names(src) if not name_contains else [f.get("name") or name_contains for f in filters]
        if to_refs == ["all"] or to_refs == "all":
            to_refs = [str(i) for i, el in enumerate(self.items())
                       if el.tag not in ("gap", "title") and el is not src]
        done = 0
        for r in to_refs:
            dst, _ = self.resolve(r)
            if dst is src or dst.tag in ("gap", "transition"):
                continue
            refs = {f.get("ref") for f in filters}
            if replace_existing:
                for f in list(dst):
                    if f.tag in FILTER_TAGS and f.get("ref") in refs:
                        dst.remove(f)
            else:
                existing = {f.get("ref") for f in dst if f.tag in FILTER_TAGS}
                if refs & existing:
                    continue
            for f in filters:
                insert_ordered(dst, copy.deepcopy(f))
            done += 1
        return Report(changes=[f"Copied {', '.join(names)} from '{self.item_name(src)}' to {done} item(s)."])

    def remove_effects(self, ref, name_contains: str | None = None) -> Report:
        el, _ = self.resolve(ref)
        n = 0
        for f in list(el):
            if f.tag in FILTER_TAGS:
                res = self.resource(f.get("ref"))
                nm = f.get("name") or (res.get("name") if res is not None else "")
                if name_contains and name_contains.lower() not in nm.lower():
                    continue
                el.remove(f)
                n += 1
        return Report(changes=[f"Removed {n} effect(s) from '{self.item_name(el)}'."])

    # ---- inserting media -------------------------------------------------

    def _format_for(self, info: dict) -> str | None:
        if not info.get("has_video"):
            return None
        fd, w, h = info["frame_duration"], str(info["width"]), str(info["height"])
        for el in self.resources:
            if el.tag == "format" and el.get("frameDuration") == fd and el.get("width") == w and el.get("height") == h:
                return el.get("id")
        rid = self._new_id()
        fmt = ET.Element("format", {"id": rid, "frameDuration": fd, "width": w, "height": h,
                                    "colorSpace": "1-1-1 (Rec. 709)"})
        self.resources.insert(0, fmt)
        return rid

    def _asset_for(self, path: str) -> ET.Element:
        url = media.path_to_url(path)
        for el in self.resources:
            if el.tag != "asset":
                continue
            rep = el.find("media-rep")
            src = rep.get("src") if rep is not None else el.get("src")
            if src and media.url_to_path(src) == os.path.abspath(path):
                return el
        info = media.probe(os.path.abspath(path))
        fmt_id = self._format_for(info)
        fd = parse_time(info["frame_duration"]) if info.get("has_video") else Fraction(1, 48000)
        start = Fraction(0)
        if info.get("timecode") and info.get("has_video"):
            try:
                from .timecode import parse_timecode
                start = parse_timecode(info["timecode"].replace(";", ":"), fd)
            except Exception:
                start = Fraction(0)
        dur = snap(Fraction(str(info["duration"])), fd, "floor") if info.get("has_video") else \
            Fraction(str(info["duration"])).limit_denominator(48000)
        tb = int(info["frame_duration"].split("/")[1].rstrip("s")) if info.get("has_video") else 48000
        attrs = {"id": self._new_id(), "name": info["name"], "start": format_time(start, tb),
                 "duration": format_time(dur, tb)}
        if info.get("has_video"):
            attrs.update(hasVideo="1", format=fmt_id, videoSources="1")
        if info.get("has_audio"):
            attrs.update(hasAudio="1", audioSources="1", audioChannels=str(info["audio_channels"]),
                         audioRate=str(info["audio_rate"]))
        asset = ET.Element("asset", attrs)
        if self.version_tuple >= (1, 9):
            ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": url})
        else:
            asset.set("src", url)
        self.resources.append(asset)
        return asset

    def insert_media(self, path: str, at=None, mode: str = "connect", source_in: float = 0,
                     duration: float | None = None, lane: int | None = None, role: str | None = None) -> Report:
        path = os.path.abspath(os.path.expanduser(path))
        asset = self._asset_for(path)
        a_start, a_dur = parse_time(asset.get("start")), parse_time(asset.get("duration"))
        sin = self.snap(Fraction(str(source_in)))
        if sin >= a_dur:
            raise EditError("source_in is past the end of the media")
        d = self.snap(Fraction(str(duration)), "floor") if duration else self.snap(a_dur - sin, "floor")
        d = min(d, self.snap(a_dur - sin, "floor"))
        if d <= 0:
            raise EditError("Clip would have zero length")
        clip = ET.Element("asset-clip", {"ref": asset.get("id"), "name": asset.get("name"),
                                         "start": self.t(a_start + sin), "duration": self.t(d)})
        if asset.get("format") and self.format_el is not None and asset.get("format") != self.format_el.get("id"):
            clip.set("format", asset.get("format"))
        clip.set("tcFormat", "NDF")
        if role:
            clip.set("audioRole" if asset.get("hasVideo") != "1" else "videoRole", role)
        name = asset.get("name")
        if mode == "append":
            clip.set("offset", self.t(self.origin + self.duration()))
            self.spine.append(clip)
            self._update_sequence_duration()
            return Report(changes=[f"Appended '{name}' ({secs(d)}s) to the end of the timeline."])
        if at is None:
            raise EditError("Give a time (`at`) for insert/connect")
        t = self.abs_time(at)
        if mode == "insert":
            rep = self.split_at(self.rel(t))
            items = self.items()
            idx_item = next((el for el in items if self.off(el) == t), None)
            kids = list(self.spine)
            pos = kids.index(idx_item) if idx_item is not None else len(kids)
            clip.set("offset", self.t(t))
            self.spine.insert(pos, clip)
            # ripple everything after
            for el in list(self.spine)[pos + 1:]:
                el.set("offset", self.t(self.off(el) + d))
            self._update_sequence_duration()
            return Report(changes=[f"Inserted '{name}' ({secs(d)}s) at {self.rel(t)}s; later clips moved right."])
        host = self.item_at(t)
        if host is None:
            raise EditError("That time is outside the timeline")
        if duration is None:
            # don't let a long music file or B-roll hang past the end of the edit
            remaining = self.snap(self.origin + self.duration() - t, "floor")
            if remaining > 0 and d > remaining:
                d = remaining
                clip.set("duration", self.t(d))
        if lane is None:
            lane = 1 if asset.get("hasVideo") == "1" else -1
        clip.set("lane", str(lane))
        clip.set("offset", self.t(self.local_time(host, t)))
        # attributes order: FCP doesn't care, but keep ref first for readability
        insert_ordered(host, clip)
        return Report(changes=[f"Connected '{name}' ({secs(d)}s) at {self.rel(t)}s on lane {lane}."])

    # ---- silence -----------------------------------------------------------

    def silence_ranges(self, threshold_db: float = -32, min_silence: float = 0.4,
                       padding: float | None = None, refs: list | None = None,
                       keep_after_speech: float = 0.08, keep_before_speech: float = 0.12
                       ) -> tuple[list, list[str]]:
        """Absolute timeline ranges of silence in primary-storyline clips.
        keep_after_speech = air left after a word ends (tail), keep_before_speech =
        air left before the next word starts (lead). `padding` sets both."""
        notes: list[str] = []
        cuts = []
        tail = Fraction(str(padding if padding is not None else keep_after_speech))
        lead = Fraction(str(padding if padding is not None else keep_before_speech))
        items = self.items()
        targets = [self.resolve(r)[0] for r in refs] if refs else items
        for el in targets:
            if el.tag != "asset-clip":
                if el.tag not in ("gap", "title"):
                    notes.append(f"Skipped '{self.item_name(el)}' ({el.tag}); only plain clips are analysed.")
                continue
            path = self.media_path(el)
            if not path or not os.path.exists(path):
                notes.append(f"Skipped '{self.item_name(el)}': media file not found ({path}).")
                continue
            if el.find("timeMap") is not None:
                notes.append(f"Skipped retimed clip '{self.item_name(el)}'.")
                continue
            asset = self.resource(el.get("ref"))
            a_start = parse_time(asset.get("start"))
            off, st, d = self.off(el), self.start(el), self.dur(el)
            for fs, fe in media.detect_silence(path, threshold_db, min_silence):
                s_src = a_start + Fraction(str(fs)) + (tail if fs > 0.001 else 0)
                e_src = a_start + Fraction(str(fe)) - lead
                s_src, e_src = max(s_src, st), min(e_src, st + d)
                if e_src - s_src < self.frame:
                    continue
                cuts.append((self.snap(off + (s_src - st), "ceil"), self.snap(off + (e_src - st), "floor")))
        return [(a, b) for a, b in self._merge(cuts) if b > a], notes

    # ---- transcripts -------------------------------------------------------

    def storyline_media(self) -> list[str]:
        """Distinct local media files used by plain clips in the primary storyline."""
        seen: list[str] = []
        for el in self.items():
            p = self.media_path(el) if el.tag == "asset-clip" else None
            if p and p not in seen:
                seen.append(p)
        return seen

    def map_transcript(self, file_path: str, data: dict, include_words: bool = False,
                       start: float | None = None, end: float | None = None) -> list[dict]:
        """Place a file transcript onto the timeline: only words still in the edit,
        with timeline times. A sentence that crosses a cut is split at the cut."""
        out: list[dict] = []
        for idx, el in enumerate(self.items()):
            if el.tag != "asset-clip" or self.media_path(el) != file_path or el.find("timeMap") is not None:
                continue
            asset = self.resource(el.get("ref"))
            a_start = parse_time(asset.get("start"))
            off, st, d = self.off(el), self.start(el), self.dur(el)

            def to_tl(file_t: float) -> float:
                return float(off + (a_start + Fraction(str(file_t)) - st))

            for seg in data["segments"]:
                words = seg.get("words") or []
                visible = [w for w in words
                           if st <= a_start + Fraction(str((w["s"] + w["e"]) / 2)) < st + d]
                if not words:
                    mid = a_start + Fraction(str((seg["start"] + seg["end"]) / 2))
                    if not (st <= mid < st + d):
                        continue
                    s_tl, e_tl, text = to_tl(seg["start"]), to_tl(seg["end"]), seg["text"]
                elif not visible:
                    continue
                else:
                    s_tl, e_tl = to_tl(visible[0]["s"]), to_tl(visible[-1]["e"])
                    text = " ".join(w["w"] for w in visible)
                lo, hi = float(off), float(off + d)
                s_tl, e_tl = max(lo, s_tl), min(hi, e_tl)
                o = float(self.origin)
                entry = {"start": round(s_tl - o, 2), "end": round(e_tl - o, 2), "clip": str(idx), "text": text}
                if start is not None and entry["end"] < start:
                    continue
                if end is not None and entry["start"] > end:
                    continue
                if include_words and visible:
                    entry["words"] = [[w["w"], round(max(lo, to_tl(w["s"])) - o, 2),
                                       round(min(hi, to_tl(w["e"])) - o, 2)] for w in visible]
                    low = [w["w"] for w in visible if w.get("p", 1) < 0.5]
                    if low:
                        entry["unsure_words"] = low
                out.append(entry)
        return sorted(out, key=lambda e: e["start"])

    # ---- adjustment clips / full-length connected items ---------------------

    def is_adjustment(self, el: ET.Element) -> bool:
        res = self.resource(el.get("ref"))
        uid = (res.get("uid") or "") if res is not None else ""
        return el.tag == "video" and (uid.startswith("FFAdjustment") or
                                      (el.get("role") or "").startswith("adjustments"))

    def adjustment_refs(self) -> list[str]:
        out = []
        for i, el in enumerate(self.items()):
            for ci, c in enumerate([c for c in el if _is_anchored(c)]):
                if self.is_adjustment(c):
                    out.append(f"{i}.{ci}")
        return out

    # ---- looks (an adjustment clip's stack of effects, saved for reuse) -----

    def extract_look(self, ref) -> dict:
        """Serialise an adjustment clip's effects (with the effect resources they use)
        so the same look can be dropped onto other projects."""
        el, _ = self.resolve(ref)
        filters = [f for f in el if f.tag in FILTER_TAGS]
        if not filters:
            raise EditError(f"'{self.item_name(el)}' has no effects to save")
        refs = []
        for f in filters:
            for sub in f.iter():
                r = sub.get("ref")
                if r and r not in refs and self.resource(r) is not None:
                    refs.append(r)
        resources = [ET.tostring(self.resource(r), encoding="unicode") for r in refs]
        return {
            "filters": [ET.tostring(f, encoding="unicode") for f in filters],
            "resources": resources,
            "effects": [f.get("name") or "" for f in filters],
            "fcpxml_version": self.version,
        }

    def _import_resources(self, xml_list: list[str]) -> dict[str, str]:
        """Add effect/format resources from another document, reusing any already
        present (same uid) and renumbering ids. Returns old_id -> new_id."""
        mapping: dict[str, str] = {}
        for raw in xml_list:
            res = ET.fromstring(raw)
            old = res.get("id")
            uid = res.get("uid")
            match = next((r for r in self.resources if r.tag == res.tag and uid and r.get("uid") == uid), None)
            if match is not None:
                mapping[old] = match.get("id")
                continue
            new_id = self._new_id()
            res.set("id", new_id)
            self.resources.append(res)
            mapping[old] = new_id
        return mapping

    def _adjustment_effect_id(self) -> str:
        for r in self.resources:
            if r.tag == "effect" and (r.get("uid") or "").startswith("FFAdjustment"):
                return r.get("id")
        rid = self._new_id()
        self.resources.append(ET.Element("effect", {"id": rid, "name": "Adjustment Clip",
                                                    "uid": "FFAdjustmentEffect"}))
        return rid

    def apply_look(self, look: dict, name: str, replace_existing: bool = True, lane: int | None = None) -> Report:
        """Add an Adjustment Clip carrying a saved look over the whole timeline."""
        rep = Report()
        items = self.items()
        if not items:
            raise EditError("The timeline is empty")
        if replace_existing:
            for r in reversed(self.adjustment_refs()):
                c, parent = self.resolve(r)
                if (c.get("name") or "") == name or not [f for f in c if f.tag in FILTER_TAGS]:
                    parent.remove(c)
                    rep.changes.append(f"Replaced existing adjustment clip '{c.get('name') or 'Adjustment Clip'}'.")
        mapping = self._import_resources(look["resources"])
        first = items[0]
        if lane is None:
            used = [int(c.get("lane")) for el in items for c in el if _is_anchored(c)]
            lane = max([0] + [u for u in used if u > 0]) + 1
        adj = ET.Element("video", {
            "ref": self._adjustment_effect_id(), "lane": str(lane),
            "offset": self.t(self.start(first)), "name": name,
            "start": "3600s", "duration": self.t(self.duration()), "role": "adjustments",
        })
        for raw in look["filters"]:
            f = ET.fromstring(raw)
            for sub in f.iter():
                if sub.get("ref") in mapping:
                    sub.set("ref", mapping[sub.get("ref")])
            adj.append(f)
        insert_ordered(first, adj)
        rep.changes.append(f"Added look '{name}' ({', '.join(e for e in look['effects'] if e)}) as an adjustment "
                           f"clip over the whole timeline (0s–{secs(self.duration())}s).")
        return rep

    # ---- audio style ----------------------------------------------------------

    _AUDIO_TAGS = ("adjust-volume", "adjust-panner")

    def extract_audio_style(self, ref) -> dict:
        el, _ = self.resolve(ref)
        style: dict = {"clip_tags": [], "channel_source": None, "filters": []}
        for tag in self._AUDIO_TAGS:
            c = el.find(tag)
            if c is not None:
                style["clip_tags"].append(ET.tostring(c, encoding="unicode"))
        acs = el.find("audio-channel-source")
        if acs is not None:
            acs = copy.deepcopy(acs)
            style["channel_source"] = ET.tostring(acs, encoding="unicode")
        refs = []
        for f in el.findall("filter-audio"):
            style["filters"].append(ET.tostring(f, encoding="unicode"))
            for sub in f.iter():
                if sub.get("ref") and sub.get("ref") not in refs:
                    refs.append(sub.get("ref"))
        if acs is not None:
            for sub in acs.iter():
                if sub.tag == "filter-audio" and sub.get("ref") not in refs:
                    refs.append(sub.get("ref"))
        style["resources"] = [ET.tostring(self.resource(r), encoding="unicode")
                              for r in refs if self.resource(r) is not None]
        style["summary"] = self.describe_audio(el)
        return style

    def describe_audio(self, el: ET.Element) -> dict:
        d: dict = {}
        vol = el.find("adjust-volume")
        if vol is not None:
            d["volume"] = vol.get("amount")
        acs = el.find("audio-channel-source")
        if acs is not None:
            loud = acs.find("adjust-loudness")
            if loud is not None:
                d["loudness"] = {"amount": loud.get("amount"), "uniformity": loud.get("uniformity")}
            for tag in ("adjust-noiseReduction", "adjust-humReduction", "adjust-EQ", "adjust-voiceIsolation"):
                x = acs.find(tag)
                if x is not None:
                    d[tag.replace("adjust-", "")] = dict(x.attrib) or True
            v2 = acs.find("adjust-volume")
            if v2 is not None:
                d["channel_volume"] = v2.get("amount")
        fx = [f.get("name") for f in el.findall("filter-audio")]
        if fx:
            d["audio_effects"] = fx
        return d

    def apply_audio_style(self, style: dict, refs: list | None = None) -> Report:
        """Copy a saved audio chain (clip volume, Loudness/Noise Removal enhancements,
        audio effects) onto dialogue clips."""
        targets = [self.resolve(r)[0] for r in refs] if refs else \
            [el for el in self.items() if el.tag == "asset-clip" and
             (self.resource(el.get("ref")) is not None and self.resource(el.get("ref")).get("hasAudio") == "1")]
        mapping = self._import_resources(style.get("resources", []))

        def remap(x: ET.Element) -> ET.Element:
            for sub in x.iter():
                if sub.get("ref") in mapping:
                    sub.set("ref", mapping[sub.get("ref")])
            return x

        n = 0
        for el in targets:
            for tag in self._AUDIO_TAGS:
                old = el.find(tag)
                if old is not None:
                    el.remove(old)
            for raw in style.get("clip_tags", []):
                insert_ordered(el, ET.fromstring(raw))
            for old in el.findall("filter-audio"):
                el.remove(old)
            for raw in style.get("filters", []):
                insert_ordered(el, remap(ET.fromstring(raw)))
            if style.get("channel_source"):
                for old in el.findall("audio-channel-source"):
                    el.remove(old)
                acs = remap(ET.fromstring(style["channel_source"]))
                acs.attrib.pop("start", None)
                acs.attrib.pop("duration", None)
                insert_ordered(el, acs)
            n += 1
        return Report(changes=[f"Applied audio style {style.get('summary', {})} to {n} clip(s)."])

    def find_connected(self, name_contains: str) -> str:
        for i, el in enumerate(self.items()):
            for ci, c in enumerate([c for c in el if _is_anchored(c)]):
                res = self.resource(c.get("ref"))
                hay = f"{self.item_name(c)} {res.get('name') if res is not None else ''} " + \
                      " ".join(self.effect_names(c))
                if name_contains.lower() in hay.lower():
                    return f"{i}.{ci}"
        adj = self.adjustment_refs()
        if len(adj) == 1 and any(w in name_contains.lower() for w in ("adjust", "preset", "look")):
            return adj[0]
        raise EditError(f"No connected item matching {name_contains!r}. Note: a preset applied to an "
                        f"adjustment clip is stored as its individual effects, not by the preset's name. "
                        f"Adjustment clips here: {adj or 'none'}")

    def fit_to_timeline(self, ref) -> Report:
        """Move a connected item (e.g. an Adjustment Clip with a look on it) to the
        start of the timeline and make it exactly as long as the whole edit."""
        el, parent = self.resolve(ref)
        if parent is None:
            raise EditError("Give a connected item ref like '0.1' (an adjustment clip or title above the storyline)")
        if self.media_path(el):
            res = self.resource(el.get("ref"))
            if res is not None and res.tag == "asset":
                raise EditError("That connected item is real footage, which can't be stretched; "
                                "fit_to_timeline is for adjustment clips, titles and generators.")
        first = self.items()[0]
        total = self.duration()
        parent.remove(el)
        el.set("offset", self.t(self.start(first)))
        el.set("duration", self.t(total))
        insert_ordered(first, el)
        return Report(changes=[f"'{self.item_name(el)}' now covers the whole timeline (0s–{secs(total)}s)."])

    # ---- loudness ------------------------------------------------------------

    def normalize_peaks(self, target_db: float = -2.0, per_clip: bool = False,
                        refs: list | None = None) -> Report:
        """Set clip gain so the loudest peak lands on target_db. By default one
        gain for all dialogue clips (keeps their balance); per_clip=True levels
        each clip on its own."""
        rep = Report()
        targets = [self.resolve(r)[0] for r in refs] if refs else \
            [el for el in self.items() if el.tag == "asset-clip"]
        measured = []
        for el in targets:
            path = self.media_path(el)
            asset = self.resource(el.get("ref"))
            if not path or not os.path.exists(path) or asset is None or asset.get("hasAudio") != "1":
                continue
            if el.get("enabled") == "0" or el.get("srcEnable") == "video":
                continue
            file_in = float(self.start(el) - parse_time(asset.get("start")))
            lv = media.audio_levels(path, max(0.0, file_in), float(self.dur(el)))
            if lv["peak_db"] is not None:
                measured.append((el, lv["peak_db"]))
        if not measured:
            raise EditError("No clips with readable audio to measure")
        changed = 0
        overall = max(p for _, p in measured)
        for el, peak in measured:
            gain = target_db - (peak if per_clip else overall)
            gain = round(max(-96.0, min(12.0, gain)), 1)
            if gain == 12.0 and (per_clip or not rep.warnings):
                who = f"'{self.item_name(el)}' is" if per_clip else "The audio is"
                rep.warnings.append(f"{who} very quiet; gain capped at +12 dB (Final Cut's limit), "
                                    f"so peaks will land below {target_db:g} dB. Consider Final Cut's "
                                    "Loudness / Enhance Audio or re-checking the mic level.")
            vol = el.find("adjust-volume")
            if vol is None:
                vol = ET.Element("adjust-volume")
                insert_ordered(el, vol)
            vol.set("amount", f"{gain:g}dB")
            changed += 1
        if per_clip:
            rep.changes.append(f"Levelled {changed} clip(s) so each peaks at {target_db:g} dB.")
        else:
            g = round(max(-96.0, min(12.0, target_db - overall)), 1)
            rep.changes.append(f"Loudest peak was {overall:g} dB; applied {g:+g} dB to {changed} clip(s) "
                               f"so the edit peaks at {target_db:g} dB.")
        if any(c.tag == "filter-audio" for el, _ in measured for c in el):
            rep.warnings.append("Some clips have audio effects, which can change the final peak; "
                                "check the meters in Final Cut Pro.")
        return rep

    # ---- preview -----------------------------------------------------------

    def preview_segments(self) -> list[dict]:
        segs = []
        for el in self.items():
            d = float(self.dur(el))
            path = self.media_path(el) if el.tag == "asset-clip" else None
            if path and os.path.exists(path) and el.find("timeMap") is None:
                asset = self.resource(el.get("ref"))
                file_in = float(self.start(el) - parse_time(asset.get("start")))
                segs.append({"path": path, "in": file_in, "dur": d,
                             "has_video": asset.get("hasVideo") == "1" and el.get("srcEnable") != "audio",
                             "has_audio": asset.get("hasAudio") == "1" and el.get("srcEnable") != "video"
                             and el.get("enabled") != "0"})
            else:
                segs.append({"black": True, "dur": d})
        return segs

    # ------------------------------------------------------------------ #
    #  Checks & saving
    # ------------------------------------------------------------------ #

    def check(self) -> list[str]:
        problems = []
        ids = self._all_ids()
        for el in self.root.iter():
            for attr in ("ref", "format"):
                v = el.get(attr)
                if v and el.tag not in ("text-style",) and v not in ids:
                    problems.append(f"<{el.tag}> refers to missing id {v!r}")
            if el.tag == "text-style" and el.get("ref") and el.get("ref") not in ids:
                problems.append(f"text-style refers to missing id {el.get('ref')!r}")
        pos = self.origin
        for el in self.items():
            if self.off(el) != pos:
                problems.append(f"'{self.item_name(el)}' starts at {self.rel(self.off(el))}s, expected {self.rel(pos)}s")
            if self.dur(el) <= 0:
                problems.append(f"'{self.item_name(el)}' has zero duration")
            if (self.dur(el) / self.frame).denominator != 1:
                problems.append(f"'{self.item_name(el)}' duration is not a whole number of frames")
            pos = self.off(el) + self.dur(el)
        return problems

    def to_bytes(self) -> bytes:
        ET.indent(self.tree, space="    ")
        body = ET.tostring(self.root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n").encode("utf-8")

    def save(self, out_path: str, project_name: str | None = None) -> str:
        out_path = os.path.abspath(os.path.expanduser(out_path))
        if os.path.abspath(out_path) == self.source_path:
            raise EditError("Refusing to overwrite the original export; choose a new file name.")
        original_name = self.project.get("name")
        if project_name:
            self.project.set("name", project_name)
        # Unique project uid so FCP treats it as a new project rather than a conflict.
        old_uid = self.project.attrib.pop("uid", None)
        try:
            data = self.to_bytes()
        finally:
            self.project.set("name", original_name or "")
            if old_uid:
                self.project.set("uid", old_uid)
        if out_path.endswith(".fcpxmld"):
            if os.path.exists(out_path):
                shutil.rmtree(out_path)
            if self.bundle_dir:
                shutil.copytree(self.bundle_dir, out_path)
            else:
                os.makedirs(out_path)
            with open(os.path.join(out_path, "Info.fcpxml"), "wb") as fh:
                fh.write(data)
        else:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(data)
        return out_path
