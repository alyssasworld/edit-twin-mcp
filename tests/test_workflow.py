"""End-to-end test of Edit Twin through the MCP protocol, the same way Claude Code uses it.

It generates its own footage with ffmpeg (a talking-head stand-in with known pauses,
B-roll and music) and a Final Cut Pro project, so it runs anywhere — no Final Cut Pro
or speech model needed. Live app tools are only checked for a clean error off-Mac.

    pip install -e . pytest && pytest -q tests
    # or: python tests/test_workflow.py

Set EDIT_TWIN_DTD_DIR to a folder holding Apple's FCPXMLv1_13.dtd to validate against
Apple's DTD (on a Mac with Final Cut Pro it is found automatically).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))


# --------------------------------------------------------------------------- fixtures

def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_media(d: str) -> dict:
    os.makedirs(d, exist_ok=True)
    talk = os.path.join(d, "talk.mov")
    # 20 s of "speech" (a tone) with pauses at 3-5 s, 9-10.2 s and 15-17 s
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=25:d=20", "-f", "lavfi", "-i", "sine=f=330:r=48000:d=20",
           "-af", "volume='if(between(t,3,5)+between(t,9,10.2)+between(t,15,17),0,1)':eval=frame,"
                  "aformat=channel_layouts=stereo",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", talk)
    broll = os.path.join(d, "broll.mp4")
    ffmpeg("-f", "lavfi", "-i", "smptebars=s=1080x1920:r=25:d=6", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", broll)
    music = os.path.join(d, "music.m4a")
    ffmpeg("-f", "lavfi", "-i", "sine=f=220:r=48000:d=30", "-ac", "2", "-c:a", "aac", music)
    return {"talk": talk, "broll": broll, "music": music}


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>

<fcpxml version="1.13">
    <resources>
        <format id="r1" name="FFVideoFormat1080x1920p25" frameDuration="100/2500s" width="1080" height="1920" colorSpace="1-1-1 (Rec. 709)"/>
        <asset id="r2" name="talk" uid="0123456789ABCDEF0123456789ABCDEF" start="0s" duration="20s" hasVideo="1" format="r1" hasAudio="1" videoSources="1" audioSources="1" audioChannels="2" audioRate="48000">
            <media-rep kind="original-media" src="file://{talk}"/>
        </asset>
        <effect id="r3" name="Adjustment Clip" uid="FFAdjustmentEffect"/>
        <effect id="r4" name="Custom LUT" uid="FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"/>
    </resources>
    <library>
        <event name="Test event">
            <project name="{name}">
                <sequence format="r1" duration="20s" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
                    <spine>
                        <asset-clip ref="r2" offset="0s" name="talk" duration="12s" tcFormat="NDF" audioRole="dialogue">
                            {adjustment}
                            <marker start="2s" duration="100/2500s" value="intro"/>
                            {audio}
                        </asset-clip>
                        <asset-clip ref="r2" offset="12s" name="talk" start="12s" duration="8s" tcFormat="NDF" audioRole="dialogue">
                            {audio}
                        </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""

LOOK = """<video ref="r3" lane="1" offset="0s" name="Adjustment Clip" start="3600s" duration="20s" role="adjustments">
                                <filter-video ref="r4" name="warm_vlog"><param name="Amount" key="1" value="0.8"/></filter-video>
                            </video>"""
AUDIO = """<audio-channel-source srcCh="1, 2" role="dialogue.dialogue-1">
                                <adjust-loudness amount="40" uniformity="3"/>
                            </audio-channel-source>"""


def make_project(path: str, talk: str, name: str, with_look: bool = False, with_audio: bool = False) -> str:
    os.makedirs(path, exist_ok=True)
    xml = PROJECT.format(talk=talk, name=name, adjustment=LOOK if with_look else "",
                         audio=AUDIO if with_audio else "")
    if with_audio:  # a hand-finished version also has clip gain
        xml = xml.replace('audioRole="dialogue">', 'audioRole="dialogue">\n                            <adjust-volume amount="4.5dB"/>')
    with open(os.path.join(path, "Info.fcpxml"), "w") as fh:
        fh.write(xml)
    return path


def stub_transcript(work_dir: str, talk: str) -> None:
    """Fake Whisper output (file times) so the test needs no speech model.
    The 2nd sentence is a cleaner retake of the 1st; 'to new to new' is a stutter."""
    from edit_twin_mcp import transcribe as tr

    def seg(a, b, text):
        ws = text.split()
        step = (b - a) / len(ws)
        return {"start": a, "end": b, "text": text,
                "words": [{"w": w, "s": round(a + i * step, 2), "e": round(a + (i + 1) * step - 0.05, 2), "p": 0.95}
                          for i, w in enumerate(ws)]}

    data = {"engine": "stub", "file": talk, "segments": [
        seg(0.1, 2.9, "So the main idea um is"),
        seg(5.1, 8.9, "So the main idea is pretty simple"),
        seg(10.3, 14.9, "it opens the door to new to new tech"),
        seg(17.1, 19.8, "Follow for part two"),
    ]}
    cache = os.path.join(work_dir, ".transcripts")
    os.makedirs(cache, exist_ok=True)
    path = tr._cache_path(cache, talk, "default|" + tr.VERBATIM_PROMPT, None)
    with open(path, "w") as fh:
        json.dump(data, fh)


# --------------------------------------------------------------------------- the test

async def run(root: str) -> None:
    media = make_media(os.path.join(root, "media"))
    work = os.path.join(root, "work")
    os.makedirs(work, exist_ok=True)
    stub_transcript(work, media["talk"])
    raw = make_project(os.path.join(root, "My reel.fcpxmld"), media["talk"], "My reel")
    hand = make_project(os.path.join(root, "My reel (hand).fcpxmld"), media["talk"], "My reel (hand)",
                        with_look=True, with_audio=True)

    env = {**os.environ, "EDIT_TWIN_DIR": work, "PYTHONPATH": os.path.join(HERE, "..", "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "edit_twin_mcp.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            assert {"learn_from_edit", "apply_look", "find_repeats", "send_to_final_cut"} <= set(names)

            async def call(name, args=None, expect_error=False):
                res = await s.call_tool(name, args or {})
                text = next((c.text for c in res.content if c.type == "text"), "")
                assert res.is_error == expect_error, f"{name}: {text[:500]}"
                print(f"{'!!' if expect_error else '=='} {name}: {text[:160]}")
                return json.loads(text) if text[:1] in "{[" else text

            await call("open_timeline", {"path": raw})
            reps = await call("find_repeats")
            kinds = sorted(r["kind"] for r in reps["repeats"])
            assert kinds == ["retake", "stutter"], reps
            assert any(f["word"] == "um" for f in reps["fillers"])
            await call("cut_ranges", {"ranges": [[r["remove_from"], r["remove_to"]] for r in reps["repeats"]]})

            sil = await call("find_silences")
            assert sil["count"] >= 2
            before = (await call("get_timeline"))["summary"]["duration_seconds"]
            await call("remove_silences")
            after = (await call("get_timeline"))["summary"]["duration_seconds"]
            assert after < before - 2

            # Learn from a hand-finished version: look + audio chain get saved
            sent = await call("save_timeline", {"output_path": os.path.join(root, "claude cut.fcpxml")})
            rep = await call("learn_from_edit", {"your_version": hand, "claude_version": sent["saved"],
                                                 "save": True, "look_name": "warm_vlog"})
            assert "look 'warm_vlog'" in rep["saved_to_style"] and "audio chain" in rep["saved_to_style"]
            style = await call("get_style")
            assert style["default_look"] == "warm_vlog" and style["audio"]["loudness"]["amount"] == "40"

            # A fresh timeline gets the look and sound automatically
            await call("open_timeline", {"path": raw})
            await call("remove_silences")
            await call("apply_look")
            await call("apply_audio_style")
            tl = await call("get_timeline")
            first = tl["items"][0]
            adj = [c for c in first["connected"] if c.get("adjustment_clip")]
            assert len(adj) == 1 and abs(adj[0]["end"] - tl["summary"]["duration_seconds"]) < 0.05
            assert first["volume"] == "4.5dB" and first["audio"]["loudness"]["amount"] == "40"

            loud = await call("measure_loudness")
            assert loud["integrated_lufs"] is not None
            v = await call("validate_timeline")
            assert v["ok"], v
            out = await call("save_timeline", {"output_path": os.path.join(root, "final.fcpxmld")})
            assert os.path.exists(os.path.join(out["saved"], "Info.fcpxml"))
            await call("render_preview", {"output_path": os.path.join(root, "preview.mp4")})
            assert os.path.getsize(os.path.join(root, "preview.mp4")) > 1000

            await call("delete_clip", {"ref": "99"}, expect_error=True)
            if sys.platform != "darwin":
                await call("fcp_menu", {"path": ["File", "Export XML…"]}, expect_error=True)


def test_workflow():
    root = tempfile.mkdtemp(prefix="edit-twin-test-")
    try:
        asyncio.run(run(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    test_workflow()
    print("\nAll good.")
