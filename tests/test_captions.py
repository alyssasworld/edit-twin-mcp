"""Caption timing and line breaking, without a timeline or speech model.

    pytest -q tests/test_captions.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from edit_twin_mcp import captions  # noqa: E402
from edit_twin_mcp.fcpxml import _fcp_color_to_hex  # noqa: E402


def words(text: str, start: float = 0.0, step: float = 0.3, pause_after: dict | None = None) -> list[list]:
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append([w, round(t, 3), round(t + step - 0.05, 3)])
        t += step + (pause_after or {}).get(i, 0)
    return out


# A font name that's never installed, so widths use the estimate on any machine
WORD = {**captions.BUILTIN_STYLES["one_word"], "font": "Not Installed Font", "size": 100}
LINE = {**captions.BUILTIN_STYLES["subtitle"], "font": "Not Installed Font", "size": 50}


def no_overlaps(caps):
    return all(a["end"] <= b["start"] for a, b in zip(caps, caps[1:]))


def test_one_word_at_a_time_holds_until_next_word():
    caps = captions.build(words("this is my, um, first look!"), WORD, 1080, 10)
    assert [c["text"] for c in caps] == ["this", "is", "my", "first", "look"]
    assert caps[0]["end"] == caps[1]["start"]          # no flicker between words
    assert caps[2]["end"] == caps[3]["start"]          # held over the skipped "um"
    assert caps[-1]["end"] < 10 and no_overlaps(caps)


def test_word_mode_lets_go_after_a_pause_and_groups_words():
    ws = words("hello there friend", pause_after={0: 2.0})
    caps = captions.build(ws, WORD, 1080, 10)
    assert caps[0]["end"] < ws[1][1] - 1                # doesn't sit on screen through the pause
    caps = captions.build(words("one two three four five"), {**WORD, "max_words": 2}, 1080, 10)
    assert [c["text"] for c in caps] == ["one two", "three four", "five"]


def test_case_and_punctuation():
    caps = captions.build(words("Black cat - Gorg!"), {**WORD, "case": "lower"}, 1080, 10)
    assert [c["text"] for c in caps] == ["black", "cat", "gorg"]
    caps = captions.build(words("Black cat - Gorg!"), {**LINE, "case": "upper"}, 1920, 10)
    assert caps[0]["text"] == "BLACK CAT - GORG!"


def test_lines_fit_the_frame_and_break_at_sentences_and_pauses():
    text = ("so this is my first look at the new phone. and honestly the camera bump is huge "
            "like way bigger than last year and I kind of love it")
    ws = words(text, pause_after={16: 1.5})   # a pause after "huge"
    caps = captions.build(ws, LINE, 1920, 60)
    assert caps[0]["text"] == "so this is my first look at the new phone."
    assert all(len(c["text"]) <= 42 for c in caps)
    m = captions.Measure(LINE)
    assert all(m(c["text"]) <= 0.8 * 1920 for c in caps)
    assert any(c["text"].startswith("and I") for c in caps)   # breaks before "and", not "I kind / of"
    assert no_overlaps(caps)
    narrow = captions.build(ws, LINE, 720, 60)
    assert len(narrow) > len(caps)


def test_two_lines_only_when_one_doesnt_fit():
    ws = words("a short one. then a much longer sentence that will not fit on a single line at all")
    caps = captions.build(ws, {**LINE, "max_lines": 2}, 1920, 60)
    assert "\n" not in caps[0]["text"]
    assert any("\n" in c["text"] for c in caps[1:])


def test_fixes_keep_punctuation():
    fixed = captions.apply_fixes([["Gorge!", 0, 1], ["ok", 1, 2]], {"gorge": "gorg"})
    assert fixed[0][0] == "gorg!" and fixed[1][0] == "ok"


def test_styles_resolve_and_scale():
    saved = {"styles": {"reel words": {**WORD}}, "default": {"short": "reel words", "long": None}}
    assert captions.resolve_style(saved, None, "short")[0] == "reel words"
    assert captions.resolve_style(saved, None, "long")[0] == "subtitle"
    assert captions.resolve_style(saved, "reel", "long")[0] == "reel words"
    s = captions.concrete(captions.BUILTIN_STYLES["subtitle"], 3840, 2160)
    assert s["size"] == round(0.045 * 1080, 1) and s["y"] < 0


def test_reel_default_size_and_position():
    # matches a one-word caption placed by hand on a 2160x3840 reel in Final Cut
    s = captions.concrete(captions.BUILTIN_STYLES["one_word"], 2160, 3840)
    assert s["size"] == 69.2 and s["y"] == 335
    assert captions.title_width(2160, 3840) == 607.5


def test_srt_and_colour():
    srt = captions.to_srt([{"start": 61.5, "end": 3725.25, "text": "hi"}])
    assert srt == "1\n00:01:01,500 --> 01:02:05,250\nhi\n"
    assert _fcp_color_to_hex("1 0.913725 0.780392 1") == "#ffe9c7"


def test_lowercase_keeps_acronyms_and_names():
    style = {**WORD, "case": "lower", "keep_caps": ["YouTube", "Wi-Fi", "London"]}
    caps = captions.build(words("So AI and youtube need Wi-Fi, the London office for us"), style, 1080, 10)
    assert [c["text"] for c in caps] == ["so", "AI", "and", "YouTube", "need", "Wi-Fi",
                                         "the", "London", "office", "for", "us"]


def test_hyphenated_names_join_up():
    style = {**WORD, "case": "lower", "keep_caps": ["Wi-Fi"]}
    caps = captions.build(words("the wi fi password"), style, 1080, 10)
    assert [c["text"] for c in caps] == ["the", "Wi-Fi", "password"]


def test_numbers_and_question_marks():
    caps = captions.build(words("around 10,000 agents, right?"), {**WORD, "case": "lower"}, 1080, 10)
    assert [c["text"] for c in caps] == ["around", "10,000", "agents", "right"]
    caps = captions.build(words("around 10,000 agents, right?"),
                          {**WORD, "case": "lower", "punctuation": "questions"}, 1080, 10)
    assert [c["text"] for c in caps] == ["around", "10,000", "agents", "right?"]
    caps = captions.build(words("around 10 000 ai agents"), {**WORD, "case": "lower", "keep_caps": ["AI"]}, 1080, 10)
    assert [c["text"] for c in caps] == ["around", "10,000", "AI", "agents"]


def test_words_at_tight_cuts_stay_with_their_clip():
    from edit_twin_mcp.fcpxml import _word_in
    # Whisper starts "put" 1.5s early, soaking up the pause that was cut; it ends 0.18s
    # into the clip, so it belongs to that clip, not the cut-away pause.
    assert _word_in({"s": -1.5, "e": 0.18}, 0, 2)
    assert _word_in({"s": -0.6, "e": 0.08}, 0, 2)       # "and", 0.08s into the clip
    # "it" runs a frame past the end of its clip: it stays there and isn't repeated next clip
    assert _word_in({"s": 1.76, "e": 2.02}, 0, 2)
    assert not _word_in({"s": 1.76, "e": 2.02}, 2, 4)
    assert not _word_in({"s": -0.5, "e": -0.1}, 0, 2)  # a word that was cut out
