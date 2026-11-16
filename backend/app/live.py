"""Watch a live stream from start to finish and cut clips while it runs.

ffmpeg records the stream in short segments. Each finished segment is measured
(loudness, speaking speed, excited phrases) against the stream's own baseline.
When a segment stands out, the last minute or so is stitched together, the AI
trims it, and the clip is rendered while recording continues. Old segments are
deleted, so disk use stays flat however long the stream is.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from . import db, runner, storage
from .pipeline import media, selection, signals, transcribe, trends

HYPE = re.compile(
    r"no way|oh my god|\bomg\b|what the|let'?s go|holy|insane|crazy|\bwtf\b|oh no|"
    r"are you kidding|\bbro\b|what\?!|nooo+|yes+!", re.IGNORECASE)
WARMUP_SEGMENTS = 5
HISTORY = 90  # segments kept for the baseline


def _start_recorder(stream_urls: list[str], seg_dir: Path, seconds: int, start_number: int) -> subprocess.Popen:
    inputs: list[str] = []
    for url in stream_urls[:2]:
        inputs += ["-i", url]
    # Two feeds: video from the first, audio from the second.
    maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(stream_urls) > 1 else ["-map", "0"]
    return subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", *inputs, *maps, "-c", "copy", "-f", "segment",
         "-segment_time", str(seconds), "-reset_timestamps", "1",
         "-segment_start_number", str(start_number), "-segment_format", "mpegts",
         str(seg_dir / "seg_%06d.ts")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=media.NO_WINDOW,
    )


def _index(path: Path) -> int:
    return int(path.stem.split("_")[1])


def _z(value: float, past: list[float]) -> float:
    if len(past) < WARMUP_SEGMENTS:
        return 0.0
    arr = np.array(past)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median))) or 1.0
    return max(min((value - median) / (1.4826 * mad), 6.0), -3.0)


class LiveSession:
    def __init__(self, job: dict[str, Any]) -> None:
        self.job_id = job["id"]
        self.url = job["url"]
        self.settings = job["options"]
        self.cfg = self.settings["live"]
        self.folder = runner.job_dir(self.job_id)
        self.seg_dir = self.folder / "segments"
        self.win_dir = self.folder / "live"
        self.history: list[dict[str, Any]] = []
        self.watched = 0.0
        self.last_trigger = -1e9
        self.pending: dict[str, Any] | None = None
        self.clips = 0
        self.windows = 0
        self.meta: dict[str, Any] = {}
        self.trend_brief = ""
        self.pool = ThreadPoolExecutor(max_workers=1)

    # -- measuring -----------------------------------------------------------
    def _measure(self, seg: Path) -> dict[str, Any]:
        wav = seg.with_suffix(".wav")
        media.extract_audio(seg, wav)
        levels = signals.loudness_per_second(wav)
        duration = float(levels.size) or float(self.cfg["segment_sec"])
        loud = float(np.percentile(levels, 85)) if levels.size else signals.SILENCE_DB
        text = transcribe.transcribe(
            wav, model_name=self.settings["ai"]["whisper_model"],
            language=self.settings["captions"]["language"], duration=duration)
        wav.unlink(missing_ok=True)
        rate = len(text["words"]) / max(duration, 1)
        hype = len(HYPE.findall(" ".join(s["text"] for s in text["segments"])))
        score = (0.6 * _z(loud, [h["loud"] for h in self.history])
                 + 0.4 * _z(rate, [h["rate"] for h in self.history])
                 + 0.5 * min(hype, 3))
        entry = {"idx": _index(seg), "path": seg, "t0": self.watched, "dur": duration,
                 "loud": loud, "rate": rate, "score": round(score, 2),
                 "words": text["words"], "segments": text["segments"]}
        self.watched += duration
        return entry

    # -- clipping ------------------------------------------------------------
    def _build_window(self, entries: list[dict[str, Any]], peak: dict[str, Any]) -> None:
        self.windows += 1
        self.win_dir.mkdir(exist_ok=True)
        base = self.win_dir / f"window_{self.windows:04d}"
        listing = base.with_suffix(".txt")
        listing.write_text("".join(f"file '{e['path'].as_posix()}'\n" for e in entries),
                           encoding="utf-8")
        source = base.with_suffix(".mp4")
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-c", "copy", "-movflags", "+faststart", str(source)], timeout=600)
        listing.unlink(missing_ok=True)

        origin = entries[0]["t0"]
        words = [{"w": w["w"], "s": w["s"] + e["t0"] - origin, "e": w["e"] + e["t0"] - origin}
                 for e in entries for w in e["words"]]
        sentences = [{"s": s["s"] + e["t0"] - origin, "e": s["e"] + e["t0"] - origin,
                      "text": s["text"]} for e in entries for s in e["segments"]]
        words_file = base.with_suffix(".json")
        words_file.write_text(json.dumps({"words": words, "segments": sentences}), encoding="utf-8")
        total = sum(e["dur"] for e in entries)
        clip_cfg = {**self.settings["clip"], "max_clips": 1, "min_score": self.cfg["min_score"]}
        clip_cfg["max_sec"] = min(clip_cfg["max_sec"], total)
        clip_cfg["min_sec"] = min(clip_cfg["min_sec"], clip_cfg["max_sec"])
        picks, _warning = selection.select_clips(
            cands=[{"peak": peak["t0"] - origin + peak["dur"] / 2, "signal": peak["score"]}],
            transcript={"words": words, "segments": sentences},
            meta={**self.meta, "duration": total}, clip_cfg=clip_cfg, trends=self.trend_brief,
        )
        if not picks:
            source.unlink(missing_ok=True)
            words_file.unlink(missing_ok=True)
            return
        runner.make_clip(job_id=self.job_id, clip_id=db.new_id(), sel=picks[0], source=source,
                         words_file=words_file, meta=self.meta, settings=self.settings,
                         trend_brief=self.trend_brief)
        if storage.enabled() and storage.cfg().get("delete_source"):
            source.unlink(missing_ok=True)  # the stitched window is only needed to render
        self.clips += 1

    def _handle(self, entry: dict[str, Any], final: bool = False) -> None:
        self.history.append(entry)
        if self.pending and (entry["idx"] > self.pending["idx"] or final):
            back = math.ceil(float(self.cfg["lookback_sec"]) / float(self.cfg["segment_sec"]))
            chosen = [h for h in self.history
                      if self.pending["idx"] - back <= h["idx"] <= self.pending["idx"] + 1
                      and h["path"].exists()]
            peak, self.pending = self.pending, None
            if chosen:
                # Copy now: the segment files are deleted once they age out.
                self.pool.submit(self._safe_window, chosen, peak)
        fresh = entry["t0"] - self.last_trigger >= float(self.cfg["cooldown_sec"])
        if not self.pending and fresh and len(self.history) > WARMUP_SEGMENTS \
                and entry["score"] >= float(self.cfg["sensitivity"]):
            self.pending = entry
            self.last_trigger = entry["t0"]
        self._cleanup()
        self._report()

    def _safe_window(self, entries: list[dict[str, Any]], peak: dict[str, Any]) -> None:
        try:
            self._build_window(entries, peak)
        except Exception as exc:
            db.update("jobs", self.job_id, payload={**self._payload(), "last_error": str(exc)[:300]})

    def _cleanup(self) -> None:
        back = math.ceil(float(self.cfg["lookback_sec"]) / float(self.cfg["segment_sec"])) + 8
        newest = self.history[-1]["idx"]
        for h in self.history:
            if h["idx"] < newest - back and h["path"].exists():
                h["path"].unlink(missing_ok=True)
        for h in self.history[:-HISTORY]:
            h["words"], h["segments"] = [], []
        self.history = self.history[-HISTORY * 2:]

    def _payload(self) -> dict[str, Any]:
        return {"watched_sec": round(self.watched), "clips": self.clips,
                "scores": [h["score"] for h in self.history[-60:]],
                "threshold": float(self.cfg["sensitivity"])}

    def _report(self) -> None:
        watched = time.strftime("%H:%M:%S", time.gmtime(self.watched))
        db.update("jobs", self.job_id, step=f"Watching live · {watched} watched",
                  payload=self._payload(), duration=self.watched)

    # -- main loop -----------------------------------------------------------
    def run(self) -> None:
        shutil.rmtree(self.seg_dir, ignore_errors=True)
        self.seg_dir.mkdir(parents=True)
        seconds = int(self.cfg["segment_sec"])
        runner.require_disk()
        info = media.fetch_info(self.url, max_height=int(self.cfg["quality"]))
        if info["live_status"] != "is_live" or not info["stream_urls"]:
            raise media.MediaError("This stream is not live right now.")
        self.meta = {k: v for k, v in info.items() if k != "stream_urls"}
        self.trend_brief = trends.brief_for(self.meta)
        recorder = _start_recorder(info["stream_urls"], self.seg_dir, seconds, 0)
        runner.set_step(self.job_id, "Watching live · waiting for the first segment")
        next_index, restarts, stopping = 0, 0, False
        try:
            while True:
                row = db.one("SELECT status FROM jobs WHERE id = ?", (self.job_id,))
                stopping = not row or row["status"] in ("stopping", "cancelled")
                if stopping and recorder.poll() is None:
                    recorder.terminate()
                    recorder.wait(timeout=15)
                ended = recorder.poll() is not None
                segs = sorted(self.seg_dir.glob("seg_*.ts"), key=_index)
                # The newest file is still being written unless the recorder stopped.
                ready = [s for s in segs if _index(s) >= next_index]
                if not ended:
                    ready = ready[:-1]
                for i, seg in enumerate(ready):
                    try:
                        entry = self._measure(seg)
                    except Exception:
                        next_index = _index(seg) + 1
                        continue
                    self._handle(entry, final=ended and i == len(ready) - 1)
                    next_index = entry["idx"] + 1
                if ended:
                    if stopping:
                        break
                    # The stream ended, or the stream address expired: check which.
                    try:
                        info = media.fetch_info(self.url, max_height=int(self.cfg["quality"]))
                    except media.MediaError:
                        info = {"live_status": "not_live", "stream_urls": []}
                    if info["live_status"] != "is_live" or restarts >= 40:
                        break
                    restarts += 1
                    time.sleep(3)
                    recorder = _start_recorder(info["stream_urls"], self.seg_dir, seconds, next_index)
                time.sleep(2)
        finally:
            if recorder.poll() is None:
                recorder.kill()
            self.pool.shutdown(wait=True)
            shutil.rmtree(self.seg_dir, ignore_errors=True)
        watched = time.strftime("%H:%M:%S", time.gmtime(self.watched))
        reason = "Stopped" if stopping else "Stream ended"
        db.update("jobs", self.job_id, progress=1.0, payload=self._payload(),
                  step=f"{reason} · {watched} watched · {self.clips} clips")


def run_live(job: dict[str, Any]) -> None:
    LiveSession(job).run()
