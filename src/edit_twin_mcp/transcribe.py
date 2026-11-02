"""Speech-to-text with word timestamps, run locally.

Two engines:
  whisper   mlx-whisper on Apple Silicon Macs (falls back to faster-whisper elsewhere),
            prompted to transcribe verbatim. ~1.5 GB download.
  parakeet  NVIDIA Parakeet TDT 0.6B v3 via parakeet-mlx (Apple Silicon only). On the
            author's footage it caught 80% of um/uh vs Whisper's 35%, with fewer word
            errors and ~3.6x the speed (see the edit-twin-asr-benchmark repo). It takes no
            prompt, so names are fixed afterwards by `correct` (vocabulary + saved fixes).

Which engine runs (first match wins):
  1. `engine=` on a single call (e.g. compare_engines running both)
  2. EDIT_TWIN_ASR, set at install (./install.sh --engine whisper) or by a developer;
     this is a lock: the user's saved choice can't override it
  3. the user's saved choice (style.json transcription.engine, via update_style)
  4. auto: Parakeet when it's installed and runs here (Apple Silicon) and the language
     is one it knows, otherwise Whisper
Raw results are cached per file and engine, so asking again is instant; corrections are
applied when the transcript is read, so changing the vocabulary never re-transcribes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import platform
import re

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FW_MODEL = "large-v3-turbo"
PARAKEET_MODEL = os.environ.get("EDIT_TWIN_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
# 30 s chunks with 5 s overlap: on the benchmark, 120 s chunks dropped 22 s of speech at
# a chunk join and whole-file decoding was less accurate and used 8 GB of GPU memory.
PARAKEET_CHUNK_S, PARAKEET_OVERLAP_S = 30, 5
ENGINES = ("whisper", "parakeet")
CHOICES = ("auto", *ENGINES)
# Languages Parakeet TDT 0.6B v3 transcribes (ISO 639-1). Anything else goes to Whisper
# (~99 languages) when the engine is auto.
PARAKEET_LANGUAGES = frozenset(
    "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split())

_parakeet_model = None  # loaded on first use and kept, since the MCP server runs for hours


def _cache_path(cache_dir: str, path: str, model: str, language: str | None) -> str:
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{model}|{language}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"{os.path.splitext(os.path.basename(path))[0]}.{h}.json")


def _apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _norm_choice(value: str | None) -> str | None:
    name = (value or "").strip().lower() or None
    if name is not None and name not in CHOICES:
        raise ValueError(f"Unknown speech-to-text engine {value!r}; use one of {', '.join(CHOICES)}")
    return name


def _lang(language: str | None) -> str | None:
    """'en-AU', 'EN', 'en_us' -> 'en'."""
    return re.split(r"[-_]", language.strip().lower())[0] if language and language.strip() else None


def parakeet_missing() -> str | None:
    """Why Parakeet can't run here, or None if it can."""
    if not _apple_silicon():
        return "Parakeet needs an Apple Silicon Mac"
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError:
        return ("Parakeet isn't installed (re-run ./install.sh, or "
                ".venv/bin/pip install -e '.[transcribe,parakeet]')")
    return None


def choose_engine(requested: str | None = None, saved: str | None = None,
                  language: str | None = None) -> tuple[str, str]:
    """(engine, why) following the order in the module docstring."""
    lang = _lang(language)
    env = _norm_choice(os.environ.get("EDIT_TWIN_ASR"))
    for value, source in ((_norm_choice(requested), "asked for on this call"),
                          (env, "set by EDIT_TWIN_ASR"),
                          (_norm_choice(saved), "the saved choice in style.json")):
        if value and value != "auto":
            if value == "parakeet":
                if parakeet_missing():
                    raise RuntimeError(f"{parakeet_missing()}; the engine is parakeet ({source}). "
                                       "Use engine 'auto' or 'whisper'.")
                if lang and lang not in PARAKEET_LANGUAGES:
                    raise ValueError(f"Parakeet doesn't transcribe language {lang!r} (engine parakeet, "
                                     f"{source}); use engine 'auto' or 'whisper'.")
            return value, source
    missing = parakeet_missing()
    if missing:
        return "whisper", f"auto: {missing}"
    if lang and lang not in PARAKEET_LANGUAGES:
        return "whisper", f"auto: Parakeet doesn't know language {lang!r}, Whisper does"
    return "parakeet", "auto: Apple Silicon with Parakeet installed"


def resolve_engine(engine: str | None = None, saved: str | None = None,
                   language: str | None = None) -> str:
    """The engine that will run; raises with a hint if an explicit choice can't run here."""
    return choose_engine(engine, saved, language)[0]


def describe_choice(saved: str | None = None, language: str | None = None) -> str:
    """One line for logs, e.g. "Speech-to-text: parakeet (auto: Apple Silicon with ...)"."""
    try:
        name, why = choose_engine(None, saved, language)
        return f"Speech-to-text: {name} ({why})"
    except (RuntimeError, ValueError) as e:
        return f"Speech-to-text: not usable: {e}"


def _whisper_engine() -> str:
    if _apple_silicon():
        try:
            import mlx_whisper  # noqa: F401
            return "mlx"
        except ImportError:
            pass
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        pass
    raise RuntimeError(
        "No speech-to-text engine installed. In the edit-twin-mcp folder run: "
        ".venv/bin/pip install -e '.[transcribe]'  (or re-run ./install.sh)")


def _run_whisper(path: str, language: str | None, prompt: str | None) -> tuple[list[dict], str]:
    engine = _whisper_engine()
    segments: list[dict] = []
    if engine == "mlx":
        import mlx_whisper

        model = os.environ.get("EDIT_TWIN_WHISPER_MODEL", DEFAULT_MLX_MODEL)
        result = mlx_whisper.transcribe(path, path_or_hf_repo=model, word_timestamps=True,
                                        language=language, initial_prompt=prompt,
                                        condition_on_previous_text=False)
        for s in result.get("segments", []):
            segments.append({
                "start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip(),
                "words": [{"w": w["word"].strip(), "s": float(w["start"]), "e": float(w["end"]),
                           "p": round(float(w.get("probability", 1)), 2)} for w in s.get("words", [])],
            })
        return segments, f"mlx-whisper ({model})"
    from faster_whisper import WhisperModel

    model_name = os.environ.get("EDIT_TWIN_WHISPER_MODEL", DEFAULT_FW_MODEL)
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segs, _info = model.transcribe(path, word_timestamps=True, language=language,
                                   initial_prompt=prompt, condition_on_previous_text=False)
    for s in segs:
        segments.append({
            "start": float(s.start), "end": float(s.end), "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "s": float(w.start), "e": float(w.end),
                       "p": round(float(w.probability), 2)} for w in (s.words or [])],
        })
    return segments, f"faster-whisper ({model_name})"


def parakeet_words(tokens) -> list[dict]:
    """Join Parakeet's sub-word tokens (" fl", "at", "mate") into words: a piece that
    starts with a space begins a new word, any other piece continues the one before.
    A word's confidence is its least confident piece."""
    words: list[dict] = []
    for t in tokens:
        conf = float(getattr(t, "confidence", 1.0))
        if t.text.startswith(" ") or not words:
            words.append({"w": t.text.strip(), "s": float(t.start), "e": float(t.end), "p": conf})
        else:
            words[-1]["w"] += t.text
            words[-1]["e"] = float(t.end)
            words[-1]["p"] = min(words[-1]["p"], conf)
    out = []
    for w in words:
        if w["w"]:
            w["p"] = round(w["p"], 2)
            out.append(w)
    return out


def parakeet_segments(result) -> list[dict]:
    """parakeet-mlx's AlignedResult -> edit-twin segments (one per sentence)."""
    segments = []
    for sent in result.sentences:
        words = parakeet_words(sent.tokens)
        if not words:
            continue
        segments.append({"start": words[0]["s"], "end": words[-1]["e"],
                         "text": " ".join(w["w"] for w in words), "words": words})
    return segments


def _run_parakeet(path: str) -> tuple[list[dict], str]:
    global _parakeet_model
    if _parakeet_model is None:
        from parakeet_mlx import from_pretrained

        _parakeet_model = from_pretrained(PARAKEET_MODEL)
    result = _parakeet_model.transcribe(path, chunk_duration=PARAKEET_CHUNK_S,
                                        overlap_duration=PARAKEET_OVERLAP_S)
    return parakeet_segments(result), f"parakeet-mlx ({PARAKEET_MODEL})"


# Whisper normally tidies speech up and leaves out "um"/"uh". Starting it with a
# verbatim-style prompt makes it keep fillers and repeated words, which is exactly
# what a rough cut needs to find. (Parakeet keeps them without being asked.)
VERBATIM_PROMPT = "Umm, so, uh, I was like, I was, um, thinking that, uh, you know, hmm."


def transcribe_file(path: str, cache_dir: str, language: str | None = None,
                    prompt: str | None = None, force: bool = False, verbatim: bool = True,
                    engine: str | None = None, saved_engine: str | None = None) -> dict:
    """Return {"engine", "engine_choice", "segments": [{start, end, text, words: [{w, s, e, p}]}]}
    with times in seconds of file time. engine: this call's choice; saved_engine: the
    user's saved one (see choose_engine). `prompt` (names to spell) only reaches Whisper;
    for Parakeet use `correct` with a vocabulary."""
    os.makedirs(cache_dir, exist_ok=True)
    engine, why = choose_engine(engine, saved_engine, language)
    if engine == "parakeet":
        cache = _cache_path(cache_dir, path, f"parakeet|{PARAKEET_MODEL}", language)
    else:
        model_tag = os.environ.get("EDIT_TWIN_WHISPER_MODEL", "default")
        if verbatim:
            prompt = VERBATIM_PROMPT + (" " + prompt if prompt else "")
        # unchanged from before Parakeet was added, so existing Whisper caches still hit
        cache = _cache_path(cache_dir, path, f"{model_tag}|{prompt or ''}", language)
    if os.path.exists(cache) and not force:
        with open(cache) as fh:
            return {**json.load(fh), "engine_choice": why}
    if engine == "parakeet":
        segments, label = _run_parakeet(path)  # Parakeet detects the language itself
    else:
        segments, label = _run_whisper(path, language, prompt)
    data = {"engine": label, "file": path, "segments": segments}
    with open(cache, "w") as fh:
        json.dump(data, fh)
    return {**data, "engine_choice": why}


# --------------------------------------------------------------------------- #
#  Corrections: names (vocabulary) and remembered fixes, applied after transcription
# --------------------------------------------------------------------------- #

def _squash(text: str) -> str:
    """Letters and digits only, lowercase: 'Pop Mart' and 'popmart,' both -> 'popmart'."""
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def _edges(text: str) -> tuple[str, str]:
    """Punctuation around a word, e.g. '"Navier,' -> ('"', ',')."""
    return re.match(r"^\W*", text).group(0), re.search(r"\W*$", text).group(0)


def fix_vocabulary(words: list[list], terms) -> tuple[list[list], list[dict]]:
    """Respell near-misses of the user's names and jargon ("navyer" -> "Navier",
    "pop mart" -> "Popmart"). words: [[word, start, end, ...], ...].

    Guards against replacing real words: a run of 1-3 words is only replaced when it's
    the term with different spacing/capitals, or (for terms of 5+ letters) a close
    spelling (similarity >= 0.8; "never" vs "Navier" is 0.73) with the same first letter and word count. Fillers are never touched,
    and timings are kept: a run that becomes one word spans the run."""
    from .learn import FILLERS, _norm

    table = []
    for t in terms or []:
        t = " ".join(str(t).split())
        if _squash(t):
            table.append((t, _squash(t), len(t.split())))
    if not table:
        return words, []
    out, changes, i = [], [], 0
    while i < len(words):
        done = False
        for n in (3, 2, 1):
            run = words[i:i + n]
            if len(run) < n or any(_norm(w[0]) in FILLERS for w in run):
                continue
            got = _squash("".join(w[0] for w in run))
            plain = " ".join(re.sub(r"^\W+|\W+$", "", w[0]) for w in run)
            for term, key, nwords in table:
                if not got or n > nwords + 1:
                    continue
                exact = got == key
                # a close spelling only counts word-for-word; joining or splitting words
                # ("pop mart" -> "Popmart") needs the exact letters
                close = (n == nwords and len(key) >= 5 and got[0] == key[0]
                         and difflib.SequenceMatcher(None, got, key).ratio() >= 0.8)
                if not (exact or close) or plain == term:
                    continue
                lead, _ = _edges(run[0][0])
                _, tail = _edges(run[-1][0])
                parts = term.split()
                if len(parts) == n:
                    parts[0] = lead + parts[0]  # two steps: with one word, both ends are parts[0]
                    parts[-1] = parts[-1] + tail
                    new = [[p, *w[1:]] for p, w in zip(parts, run)]
                else:
                    new = [[lead + term + tail, run[0][1], run[-1][2], *run[-1][3:]]]
                out += new
                changes.append({"from": " ".join(w[0] for w in run), "to": " ".join(w[0] for w in new),
                                "at": round(float(run[0][1]), 2)})
                i += n
                done = True
                break
            if done:
                break
        if not done:
            out.append(list(words[i]))
            i += 1
    return out, changes


def correct(data: dict, vocabulary=None, fixes: dict | None = None) -> dict:
    """A copy of a transcript with remembered fixes ({"cloud": "Claude"}) and then
    vocabulary near-misses applied. Adds "corrections": what changed and where."""
    from .captions import apply_fixes

    if not vocabulary and not fixes:
        return data
    segs, changes = [], []
    for seg in data.get("segments", []):
        words = seg.get("words") or []
        if not words:
            segs.append(seg)
            continue
        rows = [[w["w"], w["s"], w["e"], w.get("p", 1)] for w in words]
        fixed = apply_fixes(rows, fixes)
        if fixed != rows:
            before = {(round(r[1], 2), r[0]) for r in rows}
            changes += [{"from": "(fix)", "to": r[0], "at": round(r[1], 2)}
                        for r in fixed if (round(r[1], 2), r[0]) not in before]
        fixed, ch = fix_vocabulary(fixed, vocabulary)
        changes += ch
        new = [{"w": r[0], "s": r[1], "e": r[2], "p": r[3] if len(r) > 3 else 1} for r in fixed]
        segs.append({**seg, "words": new, "text": " ".join(w["w"] for w in new)})
    return {**data, "segments": segs, "corrections": changes}
