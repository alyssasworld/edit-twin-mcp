"""Engine choice, Parakeet output and transcript corrections, without any speech model."""

import os
import tempfile
from types import SimpleNamespace as NS

import pytest

from edit_twin_mcp import transcribe as tr


def tok(text, start, end, conf=1.0):
    return NS(text=text, start=start, end=end, confidence=conf)


def test_parakeet_pieces_join_into_words():
    words = tr.parakeet_words([tok(" my", 0.0, 0.2), tok(" fl", 0.3, 0.4, 0.9), tok("at", 0.4, 0.5, 0.4),
                               tok("mate", 0.5, 0.8), tok(" um", 1.0, 1.2)])
    assert [w["w"] for w in words] == ["my", "flatmate", "um"]
    assert words[1]["s"] == 0.3 and words[1]["e"] == 0.8
    assert words[1]["p"] == 0.4  # least confident piece, so captions flag it as unsure


def test_parakeet_sentences_become_segments():
    result = NS(sentences=[NS(tokens=[tok(" So,", 0.1, 0.3), tok(" um", 0.4, 0.6), tok(" yes.", 0.7, 0.9)]),
                           NS(tokens=[])])
    segs = tr.parakeet_segments(result)
    assert len(segs) == 1
    assert segs[0]["text"] == "So, um yes." and segs[0]["start"] == 0.1 and segs[0]["end"] == 0.9


@pytest.fixture
def mac(monkeypatch):
    """An Apple Silicon Mac with Parakeet installed, no EDIT_TWIN_ASR."""
    monkeypatch.delenv("EDIT_TWIN_ASR", raising=False)
    monkeypatch.setattr(tr, "parakeet_missing", lambda: None)
    return monkeypatch


def test_auto_picks_parakeet_where_it_runs_and_whisper_for_other_languages(mac):
    assert tr.resolve_engine() == "parakeet"
    assert tr.resolve_engine(saved="auto", language="en-AU") == "parakeet"
    name, why = tr.choose_engine(language="ja")
    assert name == "whisper" and "'ja'" in why


def test_auto_falls_back_to_whisper_without_parakeet(mac):
    mac.setattr(tr, "parakeet_missing", lambda: "Parakeet needs an Apple Silicon Mac")
    name, why = tr.choose_engine()
    assert name == "whisper" and "Apple Silicon" in why


def test_user_can_opt_out_and_install_lock_wins(mac):
    assert tr.resolve_engine(saved="whisper") == "whisper"  # the user's own choice
    mac.setenv("EDIT_TWIN_ASR", "whisper")  # set by ./install.sh --engine whisper
    name, why = tr.choose_engine(saved="parakeet")
    assert name == "whisper" and "EDIT_TWIN_ASR" in why
    assert tr.resolve_engine("parakeet", saved="whisper") == "parakeet"  # one explicit call
    mac.setenv("EDIT_TWIN_ASR", "auto")  # auto isn't a lock
    assert tr.resolve_engine(saved="whisper") == "whisper"


def test_explicit_choices_that_cant_run_fail_clearly(mac):
    with pytest.raises(ValueError, match="Unknown"):
        tr.resolve_engine("nope")
    with pytest.raises(ValueError, match="language 'ja'"):
        tr.resolve_engine(saved="parakeet", language="ja")
    mac.setattr(tr, "parakeet_missing", lambda: "Parakeet isn't installed")
    with pytest.raises(RuntimeError, match="isn't installed"):
        tr.resolve_engine("parakeet")
    assert "not usable" in tr.describe_choice(saved="parakeet")
    assert tr.describe_choice().startswith("Speech-to-text: whisper (auto:")


def test_engines_cache_separately_and_old_whisper_cache_still_hits(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "choose_engine", lambda e=None, s=None, l=None: (e or "whisper", "test"))
    monkeypatch.setattr(tr, "_run_whisper", lambda p, l, pr: (calls.append("whisper") or [], "w"))
    monkeypatch.setattr(tr, "_run_parakeet", lambda p: (calls.append("parakeet") or [], "p"))
    with tempfile.TemporaryDirectory() as d:
        audio = os.path.join(d, "a.wav")
        open(audio, "wb").write(b"x")
        cache = os.path.join(d, "cache")
        tr.transcribe_file(audio, cache, engine="whisper")
        tr.transcribe_file(audio, cache, engine="parakeet")
        tr.transcribe_file(audio, cache, engine="whisper")
        tr.transcribe_file(audio, cache, engine="parakeet")
        assert calls == ["whisper", "parakeet"]
        # the key Whisper caches were written under before Parakeet existed
        old = tr._cache_path(cache, audio, "default|" + tr.VERBATIM_PROMPT, None)
        assert os.path.exists(old)


def rows(text, step=0.3):
    return [[w, round(i * step, 2), round(i * step + 0.25, 2)] for i, w in enumerate(text.split())]


def test_vocabulary_fixes_near_misses_and_spacing():
    words, changes = tr.fix_vocabulary(rows("the navyer equations, at pop mart today"), ["Navier", "Popmart"])
    assert [w[0] for w in words] == ["the", "Navier", "equations,", "at", "Popmart", "today"]
    assert words[4][1:3] == [1.2, 1.75]  # "pop mart" -> one word spanning both
    assert [c["to"] for c in changes] == ["Navier", "Popmart"]


def test_vocabulary_keeps_punctuation_and_case_fixes():
    words, _ = tr.fix_vocabulary(rows('"navier," she said. Popmart.'), ["Navier", "Popmart"])
    assert words[0][0] == '"Navier,"' and words[3][0] == "Popmart."


def test_vocabulary_leaves_real_words_and_fillers_alone():
    text = "I never said um that pop art is a navy idea"
    words, changes = tr.fix_vocabulary(rows(text), ["Navier", "Popmart", "Umami"])
    assert changes == [] and [w[0] for w in words] == text.split()


def test_correct_applies_fixes_then_vocabulary_and_rebuilds_text():
    data = {"engine": "x", "segments": [{"start": 0, "end": 2, "text": "", "words": [
        {"w": "cloud", "s": 0.0, "e": 0.3, "p": 0.9}, {"w": "code", "s": 0.3, "e": 0.6, "p": 0.9},
        {"w": "at", "s": 0.7, "e": 0.8, "p": 1}, {"w": "navyer.", "s": 0.9, "e": 1.2, "p": 0.3}]}]}
    out = tr.correct(data, ["Navier"], {"cloud code": "Claude Code"})
    seg = out["segments"][0]
    assert seg["text"] == "Claude Code at Navier."
    assert [w["s"] for w in seg["words"]] == [0.0, 0.3, 0.7, 0.9]
    assert seg["words"][3]["p"] == 0.3
    assert len(out["corrections"]) == 3
    assert tr.correct(data, [], None) is data  # nothing to do: untouched
