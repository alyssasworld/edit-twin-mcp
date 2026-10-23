"""ffprobe / ffmpeg helpers: media info, silence detection, preview renders."""

from __future__ import annotations

from fractions import Fraction
import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from urllib.parse import unquote, urlparse

# Common rates and how Final Cut Pro writes their frame duration.
_STANDARD_RATES = {
    Fraction(24000, 1001): "1001/24000s",
    Fraction(24): "100/2400s",
    Fraction(25): "100/2500s",
    Fraction(30000, 1001): "1001/30000s",
    Fraction(30): "100/3000s",
    Fraction(48): "100/4800s",
    Fraction(50): "100/5000s",
    Fraction(60000, 1001): "1001/60000s",
    Fraction(60): "100/6000s",
    Fraction(120): "100/12000s",
}


def which(tool: str) -> str | None:
    found = shutil.which(tool)
    if found:
        return found
    # Homebrew paths are often missing from the PATH of GUI-launched processes.
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = os.path.join(prefix, tool)
        if os.path.exists(candidate):
            return candidate
    return None


def require(tool: str) -> str:
    path = which(tool)
    if not path:
        raise RuntimeError(
            f"{tool} is not installed. On a Mac run: brew install ffmpeg "
            "(it provides both ffmpeg and ffprobe)."
        )
    return path


def url_to_path(src: str) -> str:
    """file:///Users/x/My%20Clip.mov -> /Users/x/My Clip.mov"""
    if src.startswith("file://"):
        return unquote(urlparse(src).path)
    return src


def path_to_url(path: str) -> str:
    from urllib.parse import quote

    return "file://" + quote(os.path.abspath(path))


def nearest_frame_duration(fps: Fraction) -> str:
    """Pick the standard FCP frame duration closest to a measured frame rate
    (phones record variable frame rate, e.g. 29.98)."""
    best = min(_STANDARD_RATES, key=lambda r: abs(float(r) - float(fps)))
    if abs(float(best) - float(fps)) < 0.6:
        return _STANDARD_RATES[best]
    inv = 1 / fps
    return f"{inv.numerator}/{inv.denominator}s"


@lru_cache(maxsize=256)
def probe(path: str) -> dict:
    """Return a compact description of a media file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Media file not found: {path}")
    out = subprocess.run(
        [require("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    fmt = data.get("format", {})
    info: dict = {
        "path": path,
        "name": os.path.splitext(os.path.basename(path))[0],
        "duration": float(fmt.get("duration") or 0),
        "has_video": False,
        "has_audio": False,
        "timecode": (fmt.get("tags") or {}).get("timecode"),
    }
    for s in data.get("streams", []):
        tags = s.get("tags") or {}
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1:
            if info["has_video"]:
                continue
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            if rate in ("0/0", "0/1"):
                rate = s.get("r_frame_rate") or "30/1"
            fps = Fraction(rate)
            info.update(
                has_video=True,
                width=int(s.get("width") or 0),
                height=int(s.get("height") or 0),
                fps=round(float(fps), 3),
                frame_duration=nearest_frame_duration(fps),
                video_codec=s.get("codec_name"),
            )
            rotation = tags.get("rotate")
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rotation = sd["rotation"]
            if rotation and abs(int(float(rotation))) in (90, 270):
                info["width"], info["height"] = info["height"], info["width"]
            info["timecode"] = info["timecode"] or tags.get("timecode")
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info.update(
                has_audio=True,
                audio_channels=int(s.get("channels") or 2),
                audio_rate=int(s.get("sample_rate") or 48000),
            )
        elif s.get("codec_type") == "data" and tags.get("timecode"):
            info["timecode"] = info["timecode"] or tags.get("timecode")
    return info


@lru_cache(maxsize=128)
def detect_silence(path: str, threshold_db: float = -35.0, min_silence: float = 0.6) -> tuple:
    """Return ((start, end), ...) of silent stretches, in seconds of file time."""
    info = probe(path)
    if not info["has_audio"]:
        return tuple()
    proc = subprocess.run(
        [require("ffmpeg"), "-hide_banner", "-nostats", "-i", path, "-vn",
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    ranges = []
    start = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            ranges.append((start, float(m.group(1))))
            start = None
    if start is not None:
        ranges.append((start, info["duration"]))
    return tuple(ranges)


def audio_levels(path: str, start: float, duration: float) -> dict:
    """Peak and mean loudness for part of a file (for checking a cut)."""
    proc = subprocess.run(
        [require("ffmpeg"), "-hide_banner", "-nostats", "-ss", str(start), "-t", str(duration),
         "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    mean = re.search(r"mean_volume:\s*(-?[\d.]+)", proc.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+)", proc.stderr)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "peak_db": float(peak.group(1)) if peak else None,
    }


def grab_frame(path: str, at: float, out_png: str, width: int = 640) -> str:
    subprocess.run(
        [require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", path,
         "-frames:v", "1", "-vf", f"scale={width}:-2", out_png],
        check=True,
    )
    return out_png


def render_segments(segments: list[dict], out_path: str, width: int, height: int, fps: float) -> str:
    """Render a rough cut: each segment is {"path", "in", "dur", "has_video", "has_audio"}
    or {"black": True, "dur"}. Used for quick previews of the primary storyline."""
    ffmpeg = require("ffmpeg")
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for i, seg in enumerate(segments):
        dur = f"{seg['dur']:.6f}"
        if seg.get("black") or not seg.get("has_video"):
            inputs += ["-f", "lavfi", "-t", dur, "-i", f"color=c=black:s={width}x{height}:r={fps}"]
            v_idx = len([x for x in inputs if x == "-i"]) - 1
            vchain = f"[{v_idx}:v]setsar=1,format=yuv420p[v{i}]"
        else:
            inputs += ["-ss", f"{seg['in']:.6f}", "-t", dur, "-i", seg["path"]]
            v_idx = len([x for x in inputs if x == "-i"]) - 1
            vchain = (
                f"[{v_idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1,format=yuv420p,"
                f"trim=duration={dur},setpts=PTS-STARTPTS[v{i}]"
            )
        if seg.get("has_audio") and not seg.get("black"):
            src_idx = v_idx
            achain = (f"[{src_idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                      f"atrim=duration={dur},apad=whole_dur={dur},asetpts=PTS-STARTPTS[a{i}]")
        else:
            inputs += ["-f", "lavfi", "-t", dur, "-i", "anullsrc=r=48000:cl=stereo"]
            a_idx = len([x for x in inputs if x == "-i"]) - 1
            achain = f"[{a_idx}:a]asetpts=PTS-STARTPTS[a{i}]"
        filters += [vchain, achain]
        labels.append(f"[v{i}][a{i}]")
    filters.append("".join(labels) + f"concat=n={len(segments)}:v=1:a=1[vout][aout]")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")
    return out_path


def measure_loudness(segments: list[tuple[str, float, float, float]]) -> dict:
    """segments: (path, file_in, duration, gain_db). Concatenates the audio with each
    clip's gain and returns EBU R128 integrated loudness and true peak."""
    ffmpeg = require("ffmpeg")
    inputs, chains, labels = [], [], []
    for i, (path, start, dur, gain) in enumerate(segments):
        inputs += ["-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", path]
        chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain}dB[a{i}]")
        labels.append(f"[a{i}]")
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(segments)}:v=0:a=1,ebur128=peak=true[out]"
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", *inputs, "-filter_complex", graph,
                           "-map", "[out]", "-f", "null", "-"], capture_output=True, text=True)
    tail = proc.stderr[proc.stderr.rfind("Summary:"):] if "Summary:" in proc.stderr else proc.stderr[-2000:]
    def grab(label):
        m = re.search(label + r":\s*(-?[\d.]+|-inf)", tail)
        return float(m.group(1)) if m and m.group(1) != "-inf" else None
    out = {"integrated_lufs": grab("I"), "true_peak_db": grab("Peak"), "loudness_range_lu": grab("LRA")}
    if out["integrated_lufs"] is not None:
        out["gain_to_reach_-14_lufs"] = round(-14 - out["integrated_lufs"], 1)
    return out
