"""Installed fonts: their real names, and how wide a piece of text is in one.

Final Cut Pro titles name a font by family ("Avenir Next") and face ("Bold"), which
often differ from the file name (Avenir Next.ttc), so this reads the names from the
font file itself. It also reads glyph widths, so long-form captions can be broken
into lines that actually fit the frame in the chosen font. Pure Python, no
dependencies; only .ttf/.otf/.ttc files are read.
"""

from __future__ import annotations

import functools
import os
import struct

FONT_DIRS = [
    "~/Library/Fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "~/.local/share/fonts",
    "~/.fonts",
    "/usr/share/fonts",
]
FONT_EXTS = (".ttf", ".otf", ".ttc")


def _tables(data: bytes, base: int = 0) -> dict[str, tuple[int, int]]:
    num = struct.unpack_from(">H", data, base + 4)[0]
    out = {}
    for i in range(num):
        tag, _chk, off, length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
        out[tag.decode("latin-1")] = (off, length)
    return out


def _faces(data: bytes) -> list[int]:
    """Offsets of each font's table directory (several in a .ttc collection)."""
    if data[:4] == b"ttcf":
        n = struct.unpack_from(">I", data, 8)[0]
        return list(struct.unpack_from(f">{n}I", data, 12))
    return [0]


def _names(data: bytes, tables: dict) -> dict[int, str]:
    if "name" not in tables:
        return {}
    off, _ = tables["name"]
    _fmt, count, str_off = struct.unpack_from(">HHH", data, off)
    found: dict[int, tuple[int, str]] = {}
    for i in range(count):
        pid, eid, lang, nid, length, noff = struct.unpack_from(">HHHHHH", data, off + 6 + 12 * i)
        if nid not in (1, 2, 4, 6, 16, 17):
            continue
        raw = data[off + str_off + noff: off + str_off + noff + length]
        if pid == 3 or pid == 0:
            text = raw.decode("utf-16-be", "replace")
            # prefer Windows English, then any Unicode record
            rank = 0 if (pid == 3 and lang == 0x409) else 1
        elif pid == 1 and eid == 0:
            text, rank = raw.decode("mac-roman", "replace"), 2
        else:
            continue
        if nid not in found or rank < found[nid][0]:
            found[nid] = (rank, text.strip("\x00 "))
    return {k: v for k, (_, v) in found.items()}


def read_font(path: str) -> list[dict]:
    """[{family, face, full_name, postscript, path, index}] for each face in the file."""
    with open(path, "rb") as fh:
        data = fh.read()
    out = []
    for idx, base in enumerate(_faces(data)):
        try:
            names = _names(data, _tables(data, base))
        except struct.error:
            continue
        family = names.get(16) or names.get(1)
        if not family:
            continue
        out.append({"family": family, "face": names.get(17) or names.get(2) or "Regular",
                    "full_name": names.get(4) or family, "postscript": names.get(6),
                    "path": path, "index": idx})
    return out


@functools.lru_cache(maxsize=1)
def installed() -> tuple[dict, ...]:
    seen, out = set(), []
    for d in FONT_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(FONT_EXTS):
                    continue
                p = os.path.join(root, f)
                try:
                    for face in read_font(p):
                        if face["family"].startswith("."):   # macOS hidden system fonts
                            continue
                        key = (face["family"], face["face"])
                        if key not in seen:
                            seen.add(key)
                            out.append(face)
                except (OSError, struct.error, ValueError):
                    continue
    return tuple(out)


def squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def search(query: str | None = None) -> list[dict]:
    """Installed font families matching a loose query ("avenir next", "futura"),
    each with its faces. Spaces, case and punctuation don't matter."""
    q = squash(query or "")
    fams: dict[str, dict] = {}
    for f in installed():
        hay = squash(f["family"] + f["full_name"] + (f["postscript"] or "") + os.path.basename(f["path"]))
        if q and q not in hay:
            continue
        fam = fams.setdefault(f["family"], {"family": f["family"], "faces": [], "files": []})
        if f["face"] not in fam["faces"]:
            fam["faces"].append(f["face"])
        if f["path"] not in fam["files"]:
            fam["files"].append(f["path"])
    return sorted(fams.values(), key=lambda x: (squash(x["family"]) != q, x["family"].lower()))


def resolve(family: str, face: str | None = None) -> dict | None:
    """The installed face for a family name (exact or loose), or None."""
    fams = search(family)
    if not fams:
        return None
    fam = fams[0]["family"]
    faces = [f for f in installed() if f["family"] == fam]
    want = squash(face or "regular")
    for f in faces:
        if squash(f["face"]) == want:
            return f
    for f in faces:
        if squash(f["face"]) in ("regular", "book", "roman", "normal"):
            return f
    return faces[0]


# ---- measuring text -------------------------------------------------------------

class Metrics:
    """Advance widths of one font face, for measuring a line of text."""

    def __init__(self, path: str, index: int = 0):
        with open(path, "rb") as fh:
            data = fh.read()
        base = _faces(data)[index]
        t = _tables(data, base)
        self.units = struct.unpack_from(">H", data, t["head"][0] + 18)[0]
        n_hm = struct.unpack_from(">H", data, t["hhea"][0] + 34)[0]
        hm = t["hmtx"][0]
        # hmtx holds (advance width, left side bearing) pairs
        self.advances = struct.unpack_from(f">{2 * n_hm}H", data, hm)[::2]
        self.cmap = self._cmap(data, t["cmap"][0])
        self.space = self.width_units(" ") or self.units * 0.25

    @staticmethod
    def _cmap(data: bytes, off: int) -> dict[int, int]:
        n = struct.unpack_from(">H", data, off + 2)[0]
        subs = []
        for i in range(n):
            pid, eid, soff = struct.unpack_from(">HHI", data, off + 4 + 8 * i)
            fmt = struct.unpack_from(">H", data, off + soff)[0]
            subs.append((fmt, pid, eid, off + soff))
        # prefer full Unicode (format 12), then BMP (format 4)
        for fmt, _pid, _eid, so in sorted(subs, key=lambda s: {12: 0, 4: 1}.get(s[0], 9)):
            if fmt == 12:
                ngroups = struct.unpack_from(">I", data, so + 12)[0]
                out = {}
                for g in range(ngroups):
                    a, b, gid = struct.unpack_from(">III", data, so + 16 + 12 * g)
                    for c in range(a, min(b, 0x2FFFF) + 1):
                        out[c] = gid + (c - a)
                return out
            if fmt == 4:
                segx2 = struct.unpack_from(">H", data, so + 6)[0]
                seg = segx2 // 2
                ends = struct.unpack_from(f">{seg}H", data, so + 14)
                starts = struct.unpack_from(f">{seg}H", data, so + 16 + segx2)
                deltas = struct.unpack_from(f">{seg}h", data, so + 16 + 2 * segx2)
                ro_off = so + 16 + 3 * segx2
                ranges = struct.unpack_from(f">{seg}H", data, ro_off)
                out = {}
                for i in range(seg):
                    for c in range(starts[i], ends[i] + 1):
                        if c == 0xFFFF:
                            continue
                        if ranges[i] == 0:
                            gid = (c + deltas[i]) & 0xFFFF
                        else:
                            p = ro_off + 2 * i + ranges[i] + 2 * (c - starts[i])
                            gid = struct.unpack_from(">H", data, p)[0]
                            if gid:
                                gid = (gid + deltas[i]) & 0xFFFF
                        if gid:
                            out[c] = gid
                return out
        return {}

    def width_units(self, text: str) -> float:
        total = 0.0
        last = self.advances[-1] if self.advances else self.units // 2
        for ch in text:
            gid = self.cmap.get(ord(ch))
            if gid is None:
                total += self.units * 0.55   # missing glyph: assume an average letter
            else:
                total += self.advances[gid] if gid < len(self.advances) else last
        return total

    def width(self, text: str, size: float) -> float:
        """Width of text at a font size (same units as the size, i.e. pixels)."""
        return self.width_units(text) * size / self.units


@functools.lru_cache(maxsize=16)
def metrics_for(family: str, face: str | None = None) -> Metrics | None:
    f = resolve(family, face)
    if f is None:
        return None
    try:
        return Metrics(f["path"], f["index"])
    except (OSError, KeyError, struct.error, IndexError):
        return None
