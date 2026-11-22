"""Word-level transcription with faster-whisper. GPU first, CPU fallback."""
from __future__ import annotations

import os
import site
import sys
import threading
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import media

_model = None
_model_key: tuple[str, str] | None = None
_lock = threading.Lock()  # one transcription at a time: the GPU is shared
CPU_FALLBACK_MODEL = "small"


def _add_cuda_dlls() -> None:
    """Make the pip-installed cuBLAS / cuDNN DLLs visible on Windows."""
    if os.name != "nt":
        return
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        nvidia = Path(base) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _load(name: str, device: str):
    global _model, _model_key
    if _model is not None and _model_key == (name, device):
        return _model
    from faster_whisper import WhisperModel

    if device == "cuda":
        _add_cuda_dlls()
        _model = WhisperModel(name, device="cuda", compute_type="float16")
    else:
        _model = WhisperModel(name, device="cpu", compute_type="int8")
    _model_key = (name, device)
    return _model


CHUNK_SEC = 600  # sound is transcribed ten minutes at a time, so a long video never fills the memory
QUIET_SEARCH = 15  # a chunk ends at the quietest second within this many seconds of CHUNK_SEC


def _read(path: Path, start: float, length: float) -> np.ndarray:
    """16 kHz mono samples. Decoded here and passed in, so the library's own file decoder (PyAV) is
    never involved. A WAV is read directly; anything else goes through ffmpeg."""
    if path.suffix.lower() != ".wav":
        return media.read_pcm(path, start, length)
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        handle.setpos(min(int(start * rate), handle.getnframes()))
        raw = handle.readframes(int(length * rate))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def pieces(ranges: list[tuple[float, float]], levels: np.ndarray | None = None) -> list[tuple[float, float]]:
    """Cut each (start, end) range into chunks of about CHUNK_SEC. With the loudness per second, a cut
    falls on the quietest second nearby, so a word is not split between two chunks."""
    out: list[tuple[float, float]] = []
    for start, end in ranges:
        at = start
        while end - at > CHUNK_SEC + QUIET_SEARCH:
            cut = at + CHUNK_SEC
            if levels is not None and levels.size:
                lo, hi = int(cut - QUIET_SEARCH), int(cut + QUIET_SEARCH)
                part = levels[max(lo, 0):min(hi, levels.size)]
                if part.size:
                    cut = float(max(lo, 0) + int(np.argmin(part)))
            out.append((at, cut))
            at = cut
        if end - at > 0.5:
            out.append((at, end))
    return out


def _run(model, path: Path, language: str | None, chunks: list[tuple[float, float]],
         on_progress: Callable[[float], None] | None) -> dict[str, Any]:
    words: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    total = sum(end - start for start, end in chunks) or 1.0
    done = 0.0
    for start, end in chunks:
        audio = _read(path, start, end - start)
        if audio.size:
            segments, info = model.transcribe(
                audio, language=language, word_timestamps=True, vad_filter=True,
                beam_size=2, condition_on_previous_text=False,
            )
            language = language or info.language  # keep the first chunk's language for the rest
            for seg in segments:
                text = seg.text.strip()
                if text:
                    sentences.append({"s": round(start + seg.start, 2), "e": round(start + seg.end, 2), "text": text})
                for w in seg.words or []:
                    token = w.word.strip()
                    if token:
                        words.append({"w": token, "s": round(start + w.start, 3), "e": round(start + w.end, 3)})
                if on_progress:
                    on_progress(min((done + seg.end) / total, 1.0))
        done += end - start
    return {"language": language or "", "words": words, "segments": sentences}


def transcribe(path: Path, *, model_name: str = "large-v3-turbo", language: str = "auto",
               duration: float = 0.0, ranges: list[tuple[float, float]] | None = None,
               levels: np.ndarray | None = None,
               on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Words and sentences with times in seconds of `path` (a WAV, sound or video file). `ranges`
    limits it to those (start, end) stretches; by default the whole `duration` is transcribed."""
    lang = None if language in ("", "auto") else language
    if ranges is None:
        ranges = [(0.0, duration or media.audio_duration(path))]
    chunks = pieces(ranges, levels)
    with _lock:
        if os.environ.get("CLIPPER_FORCE_CPU") != "1":
            try:
                return _run(_load(model_name, "cuda"), path, lang, chunks, on_progress)
            except Exception as exc:  # missing CUDA libraries, out of memory, no GPU
                print(f"GPU transcription unavailable ({type(exc).__name__}: {exc}); using CPU.",
                      file=sys.stderr)
                global _model
                _model = None
        return _run(_load(CPU_FALLBACK_MODEL, "cpu"), path, lang, chunks, on_progress)
