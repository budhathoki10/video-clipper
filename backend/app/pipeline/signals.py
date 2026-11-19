"""Free, local signals that mark where something happens in a video.

Per second we measure loudness, sudden loudness jumps and speaking speed, then
add YouTube's "most replayed" curve when it exists. Peaks of the combined score
become candidate moments for the AI to judge.
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np

SILENCE_DB = -80.0


def loudness_per_second(wav: Path) -> np.ndarray:
    """RMS level in dB for each second of a 16-bit mono WAV, read in chunks."""
    levels: list[float] = []
    with wave.open(str(wav), "rb") as handle:
        rate = handle.getframerate()
        while True:
            raw = handle.readframes(rate)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            levels.append(20 * np.log10(rms) if rms > 1e-4 else SILENCE_DB)
    return np.array(levels, dtype=np.float32)


def _robust_z(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    median = np.median(values)
    mad = np.median(np.abs(values - median)) or 1.0
    return np.clip((values - median) / (1.4826 * mad), -3, 6)


def _smooth(values: np.ndarray, width: int) -> np.ndarray:
    if values.size < width:
        return values
    return np.convolve(values, np.ones(width) / width, mode="same")


def analyze(wav: Path, words: list[dict[str, Any]],
            heatmap: list[dict[str, Any]] | None = None) -> np.ndarray:
    """Interest score per second (higher = more likely a clip-worthy moment)."""
    db = loudness_per_second(wav)
    n = db.size
    if n == 0:
        return db
    voiced = np.where(db > SILENCE_DB + 1, db, np.median(db))
    loud = _robust_z(_smooth(voiced, 3))

    # A jump: this moment is much louder than the 10 seconds before it.
    before = np.concatenate([np.full(10, voiced[0]), voiced])[:n]
    jump = _robust_z(_smooth(voiced, 2) - _smooth(before, 10))

    rate = np.zeros(n, dtype=np.float32)
    for word in words:
        second = int(word["s"])
        if 0 <= second < n:
            rate[second] += 1
    pace = _robust_z(_smooth(rate, 5))

    score = 0.5 * loud + 0.35 * jump + 0.3 * pace
    if heatmap:
        heat = np.zeros(n, dtype=np.float32)
        for point in heatmap:
            a, b = int(point["start_time"]), int(point["end_time"]) + 1
            heat[max(a, 0):min(b, n)] = float(point["value"])
        score = score + 2.0 * _robust_z(heat)
    return _smooth(score, 5).astype(np.float32)


SUSTAIN_SEC = 15  # moments are ranked by how excited a stretch this long is, not by its loudest second


def candidates(score: np.ndarray, *, spacing: int, limit: int,
               sustain: int = SUSTAIN_SEC) -> list[dict[str, float]]:
    """Top moments, at least `spacing` seconds apart. They are ranked by the average over `sustain`
    seconds, so excitement that holds wins over one sudden noise (a cough, a bang, a random shout)."""
    if score.size == 0:
        return []
    remaining = _smooth(score, sustain) if sustain > 1 else score.copy()
    picks: list[dict[str, float]] = []
    floor = float(np.min(score)) - 1.0
    for _ in range(limit):
        peak = int(np.argmax(remaining))
        if remaining[peak] <= floor:
            break
        picks.append({"peak": float(peak), "signal": round(float(score[peak]), 2)})
        remaining[max(peak - spacing, 0):peak + spacing + 1] = floor
    return sorted(picks, key=lambda p: p["peak"])
