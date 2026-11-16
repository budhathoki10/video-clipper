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


def _run(model, wav: Path, language: str | None, duration: float,
         on_progress: Callable[[float], None] | None) -> dict[str, Any]:
    # Decode the 16 kHz mono WAV ourselves and pass samples in, so the
    # library's own file decoder (PyAV) is never involved.
    with wave.open(str(wav), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    del raw
    segments, info = model.transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True,
        beam_size=2, condition_on_previous_text=False,
    )
    words: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            sentences.append({"s": round(seg.start, 2), "e": round(seg.end, 2), "text": text})
        for w in seg.words or []:
            token = w.word.strip()
            if token:
                words.append({"w": token, "s": round(w.start, 3), "e": round(w.end, 3)})
        if on_progress and duration:
            on_progress(min(seg.end / duration, 1.0))
    return {"language": info.language, "words": words, "segments": sentences}


def transcribe(wav: Path, *, model_name: str = "large-v3-turbo", language: str = "auto",
               duration: float = 0.0,
               on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    lang = None if language in ("", "auto") else language
    with _lock:
        if os.environ.get("CLIPPER_FORCE_CPU") != "1":
            try:
                return _run(_load(model_name, "cuda"), wav, lang, duration, on_progress)
            except Exception as exc:  # missing CUDA libraries, out of memory, no GPU
                print(f"GPU transcription unavailable ({type(exc).__name__}: {exc}); using CPU.",
                      file=sys.stderr)
                global _model
                _model = None
        return _run(_load(CPU_FALLBACK_MODEL, "cpu"), wav, lang, duration, on_progress)
