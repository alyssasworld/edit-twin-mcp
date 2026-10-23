"""Speech-to-text with word timestamps, run locally.

Uses mlx-whisper on Apple Silicon Macs (fast, uses the GPU) and falls back to
faster-whisper elsewhere. The first run downloads the model (~1.5 GB for the
default). Results are cached per file, so asking again is instant.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FW_MODEL = "large-v3-turbo"


def _cache_path(cache_dir: str, path: str, model: str, language: str | None) -> str:
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{model}|{language}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"{os.path.splitext(os.path.basename(path))[0]}.{h}.json")


def _engine() -> str:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
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


def _run(path: str, language: str | None, prompt: str | None) -> tuple[list[dict], str]:
    engine = _engine()
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


# Whisper normally tidies speech up and leaves out "um"/"uh". Starting it with a
# verbatim-style prompt makes it keep fillers and repeated words, which is exactly
# what a rough cut needs to find.
VERBATIM_PROMPT = "Umm, so, uh, I was like, I was, um, thinking that, uh, you know, hmm."


def transcribe_file(path: str, cache_dir: str, language: str | None = None,
                    prompt: str | None = None, force: bool = False, verbatim: bool = True) -> dict:
    """Return {"engine", "segments": [{start, end, text, words: [{w, s, e, p}]}]}
    with times in seconds of file time."""
    os.makedirs(cache_dir, exist_ok=True)
    model_tag = os.environ.get("EDIT_TWIN_WHISPER_MODEL", "default")
    if verbatim:
        prompt = VERBATIM_PROMPT + (" " + prompt if prompt else "")
    cache = _cache_path(cache_dir, path, f"{model_tag}|{prompt or ''}", language)
    if os.path.exists(cache) and not force:
        with open(cache) as fh:
            return json.load(fh)
    segments, engine = _run(path, language, prompt)
    data = {"engine": engine, "file": path, "segments": segments}
    with open(cache, "w") as fh:
        json.dump(data, fh)
    return data
