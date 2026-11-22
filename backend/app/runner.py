"""Job steps for a pasted video link, and the shared clip-making code.

Every step writes its result to the job folder, so a job that is interrupted
(worker restart, crash, reboot) continues from the last finished step.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from . import config, db, llm, storage
from .pipeline import media, panel, render, selection, signals, transcribe, trends


class Cancelled(Exception):
    pass


def job_dir(job_id: str) -> Path:
    path = config.JOBS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def rel(path: Path) -> str:
    return path.relative_to(config.DATA).as_posix()


MIN_FREE_BYTES = 5 * 10**9
ORPHAN_AGE_SECONDS = 600


def require_disk() -> None:
    """Refuse to start a download when the disk is nearly full."""
    free = shutil.disk_usage(config.DATA).free
    if free < MIN_FREE_BYTES:
        raise media.MediaError(
            f"There is not enough free space on this computer to download the video "
            f"({free / 1e9:.1f} GB left). Free up some space and try again.")


def cancel_aware(job_id: str, update: Callable[[float], None], every: float = 2.0) -> Callable[[float], None]:
    """A download progress callback that also notices cancellation. The check raises
    Cancelled, which stops the download instead of letting it run on after the job was
    cancelled. Checks and progress writes are limited to one per `every` seconds."""
    last = [0.0]

    def callback(fraction: float) -> None:
        now = time.time()
        if now - last[0] >= every:
            last[0] = now
            check(job_id)
            update(fraction)

    return callback


def cleanup_partial(job_id: str) -> None:
    """Remove half-downloaded files left by a cancelled or failed download."""
    folder = config.JOBS_DIR / job_id
    for pattern in ("*.part", "*.ytdl", "*.part-Frag*", "*.temp"):
        for leftover in folder.glob(pattern):
            leftover.unlink(missing_ok=True)


def sweep_orphans() -> int:
    """Delete job folders that belong to no job (for example a download that kept
    running after its job was deleted). Folders touched recently are left alone."""
    known = {row["id"] for row in db.find("jobs", fields=["id"])}
    removed = 0
    for folder in config.JOBS_DIR.iterdir():
        if not folder.is_dir() or folder.name in known:
            continue
        newest = max((f.stat().st_mtime for f in folder.rglob("*") if f.is_file()), default=folder.stat().st_mtime)
        if time.time() - newest > ORPHAN_AGE_SECONDS:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def sweep_uploads(max_age: float = 24 * 3600) -> int:
    """Remove uploads that were started and never finished, and their records."""
    root = config.DATA / "uploads"
    removed = 0
    if root.is_dir():
        for folder in root.iterdir():
            if folder.is_dir() and time.time() - folder.stat().st_mtime > max_age:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    db.delete("uploads", {"status": "uploading", "created_at": {"$lt": time.time() - max_age}})
    return removed


def set_step(job_id: str, step: str, progress: float | None = None) -> None:
    values: dict[str, Any] = {"step": step}
    if progress is not None:
        values["progress"] = round(progress, 4)
    db.update("jobs", job_id, **values)


def check(job_id: str) -> None:
    row = db.find_one("jobs", {"id": job_id})
    if not row or row["status"] in ("cancelled", "stopping"):
        raise Cancelled()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def store_outputs(job_id: str, out_dir: Path, result: dict[str, Any]) -> tuple[str, str]:
    """Move a rendered clip and its thumbnail to cloud storage. If storage is off
    or the upload fails, the clip stays on this disk and still works."""
    video, thumb = out_dir / result["file"], out_dir / result["thumb"]
    if storage.enabled():
        try:
            file_ref = storage.put(video, storage.key_for(job_id, f"clips/{video.name}"), "video/mp4")
            thumb_ref = storage.put(thumb, storage.key_for(job_id, f"clips/{thumb.name}"), "image/jpeg")
        except storage.StorageError as exc:
            if config.public_url():  # a hosted server's own disk is not a place to keep clips
                raise
            row = db.find_one("jobs", {"id": job_id}) or {}
            payload = dict(row.get("payload") or {})
            payload["storage_warning"] = f"A clip could not be saved to storage and was kept on this PC: {exc}"
            db.update("jobs", job_id, payload=payload)
            return rel(video), rel(thumb)
        video.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)
        return file_ref, thumb_ref
    return rel(video), rel(thumb)


SECTION_PAD = 3.0  # extra seconds fetched around a clip's part of a linked video


@contextlib.contextmanager
def clip_source(*, job_id: str, start: float, end: float, settings: dict[str, Any],
                local: Path | None = None, url: str | None = None) -> Iterator[tuple[Path, float]]:
    """The video a clip is rendered from, as (file, the second of the video the file starts at).
    A local file (an upload or a live recording) is used as it is. For a link, only this clip's part
    is downloaded now and removed afterwards: a long video is never downloaded whole."""
    if local is not None:
        yield local, 0.0
        return
    if not url:
        raise RuntimeError("The video for this clip is no longer available. Add it again.")
    begin = max(start - SECTION_PAD, 0.0)
    path = job_dir(job_id) / "sections" / f"{db.new_id()}.mp4"
    try:
        yield media.download_section(url, path, begin, end + SECTION_PAD,
                                     max_height=int(settings["format"]["source_quality"])), begin
    finally:
        for leftover in path.parent.glob(path.stem + ".*"):
            leftover.unlink(missing_ok=True)


def _in_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run `fn` in worker threads with the same person's settings and keys as the job."""
    context = contextvars.copy_context()
    return lambda *args: context.copy().run(fn, *args)


ARTIFACTS = ("meta.json", "transcript.json", "signals.json", "trends.json", "selection.json")
SOURCE_SUFFIXES = (".mp4", ".mkv", ".webm")


def persist_job_files(job_id: str, folder: Path, source: Path | None) -> None:
    """Keep what "more clips" and re-rendering need in storage, then clear this server's copy:
    the small working files and the downloaded video. If storage fails the folder is kept."""
    if not storage.enabled():
        return
    refs: dict[str, str] = {}
    source_ref = None
    try:
        for name in ARTIFACTS:
            if (folder / name).exists():
                refs[name] = storage.put(folder / name, storage.key_for(job_id, f"files/{name}"),
                                         "application/json")
        if source is not None and source.exists():
            source_ref = storage.put(source, storage.key_for(job_id, f"source/{source.name}"), "video/mp4")
    except storage.StorageError as exc:
        if config.public_url():
            raise
        row = db.find_one("jobs", {"id": job_id}) or {}
        db.update("jobs", job_id, payload={**(row.get("payload") or {}),
                                           "storage_warning": f"Working files stayed on this PC: {exc}"})
        return
    db.update("jobs", job_id, files={"artifacts": refs, "source": source_ref,
                                     "source_name": source.name if source is not None else None})
    shutil.rmtree(folder, ignore_errors=True)


def restore_job_files(job_id: str, *, source: bool = False) -> Path | None:
    """Bring a video's saved working files back from storage if they are not here. With
    `source=True` also return the downloaded video (local or restored), or None if there is none."""
    folder = job_dir(job_id)
    files = (db.find_one("jobs", {"id": job_id}) or {}).get("files") or {}
    for name, ref in (files.get("artifacts") or {}).items():
        if not (folder / name).exists():
            storage.get(ref, folder / name)
    if not source:
        return None
    found = [p for p in folder.glob("source.*") if p.suffix.lower() in SOURCE_SUFFIXES]
    if found:
        return found[0]
    if files.get("source"):
        return storage.get(files["source"], folder / (files.get("source_name") or "source.mp4"))
    return None


SHORTS_MAX_SEC = 180  # YouTube treats a vertical video up to 3 minutes long as a Short


def add_context(pick: dict[str, Any], *, duration: float, settings: dict[str, Any]) -> dict[str, Any]:
    """Widen a chosen moment by `clip.context_sec` on each side (within the video), so a viewer sees
    what led up to it and how it ended. A 20 s moment becomes a 30 s clip with the default 5 s.
    A vertical clip that fit in a Short is never pushed past 3 minutes by the padding."""
    context = max(float(settings["clip"].get("context_sec", 0) or 0), 0.0)
    if context <= 0 or pick.get("context_sec") is not None:
        return pick
    start = max(float(pick["start"]) - context, 0.0)
    end = float(pick["end"]) + context
    if duration:
        end = min(end, float(duration))
    if settings["format"].get("aspect") == "9:16" and pick["end"] - pick["start"] <= SHORTS_MAX_SEC < end - start:
        extra = (end - start) - SHORTS_MAX_SEC
        start, end = start + extra / 2, end - extra / 2
    return {**pick, "start": round(start, 2), "end": round(end, 2), "context_sec": context}


def credit_line(meta: dict[str, Any]) -> str:
    name = meta.get("channel_handle") or meta.get("channel") or ""
    return f"Credit: {name}" if name else ""


def describe(sel: dict[str, Any], meta: dict[str, Any], settings: dict[str, Any]) -> str:
    """Post description: AI text, source credit, hashtags."""
    posting = settings["posting"]
    names = [str(t).lstrip("#") for t in sel.get("hashtags") or []]
    # YouTube treats a vertical video of up to 3 minutes as a Short.
    if settings["format"].get("aspect") == "9:16" and sel["end"] - sel["start"] <= 180:
        names.append("shorts")
    tags = " ".join(f"#{t}" for t in dict.fromkeys(names) if t)
    lines = [sel.get("description") or ""]
    if posting.get("credit", True) and meta.get("channel"):
        lines.append(f"Clipped from: {meta.get('title', '')} by {meta['channel']}\n"
                     f"{meta.get('webpage_url', '')}")
    lines.append(f"{tags} {posting.get('hashtags', '')}".strip())
    return "\n\n".join(line for line in lines if line.strip())


def make_clip(*, job_id: str, clip_id: str, sel: dict[str, Any], source: Path | None,
              words_file: Path, meta: dict[str, Any], settings: dict[str, Any],
              trend_brief: str = "", url: str | None = None) -> None:
    """Render one clip, then score it with the audience panel. `source` is a local video file; without
    one, only this clip's part of the video at `url` is downloaded (see `clip_source`)."""
    existing = db.find_one("clips", {"id": clip_id})
    if existing and existing["status"] == "ready":
        return
    if not existing:
        owner = (db.find_one("jobs", {"id": job_id}) or {}).get("user_id")
        db.insert("clips", {
            "id": clip_id, "job_id": job_id, "user_id": owner, "status": "rendering",
            "start": sel["start"], "end": sel["end"], "title": sel["title"],
            "hook": sel.get("hook", ""), "description": describe(sel, meta, settings),
            "hashtags": sel.get("hashtags") or [], "reason": sel.get("reason", ""),
            "ai_score": sel.get("score"), "source_title": meta.get("title"),
            "source_channel": meta.get("channel"), "source_url": meta.get("webpage_url"),
            "options": {"settings": settings, "source": rel(source) if source is not None else None,
                        "url": url if source is None else None, "words": rel(words_file),
                        "credit": credit_line(meta), "trend_brief": trend_brief,
                        "category": meta.get("category", "")},
            "created_at": time.time(),
        })
    words = _load(words_file)["words"]
    out_dir = job_dir(job_id) / "clips"
    try:
        with clip_source(job_id=job_id, start=sel["start"], end=sel["end"], settings=settings,
                         local=source, url=url) as (path, offset):
            result = render.render_clip(
                source=path, out_dir=out_dir, clip_id=clip_id, start=sel["start"], end=sel["end"],
                words=words, settings=settings, title=sel.get("hook") or "", credit=credit_line(meta),
                offset=offset,
            )
    except Exception as exc:
        db.update("clips", clip_id, status="failed", error=str(exc)[:500])
        return
    file_ref, thumb_ref = store_outputs(job_id, out_dir, result)
    db.update("clips", clip_id, status="ready", error=None, file=file_ref, thumb=thumb_ref,
              width=result["width"], height=result["height"])
    score_clip(clip_id)


def recognised_words(clip: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The words speech recognition heard in the clip's video (all of its transcript), or None when the
    transcript is gone."""
    restore_job_files(clip["job_id"])
    name = (clip.get("options") or {}).get("words")
    path = config.DATA / name if name else None
    if path is None or not path.exists():
        return None
    return (_load(path) or {}).get("words", [])


def clip_words(clip: dict[str, Any]) -> list[dict[str, Any]]:
    """The words captioned in a clip: the transcript, with the person's own corrections
    (`caption_words`) in place of the stretch they edited (`caption_range`). Words outside it, for
    example after the clip was trimmed longer, come from the transcript."""
    words = recognised_words(clip) or []
    edited = clip.get("caption_words")
    if edited is None:
        return words
    lo, hi = clip.get("caption_range") or (clip["start"], clip["end"])
    kept = [w for w in words if w["e"] <= lo or w["s"] >= hi]
    return sorted(kept + edited, key=lambda w: w["s"])


def score_clip(clip_id: str) -> None:
    clip = db.find_one("clips", {"id": clip_id})
    if not clip:
        return
    opts = clip["options"]
    size = int(opts["settings"]["ai"].get("panel_size", 0))
    if size <= 0:
        return
    words = clip_words(clip)
    spoken = " ".join(w["w"] for w in words if clip["start"] <= w["s"] <= clip["end"])
    result = panel.run_panel(
        title=clip["title"], hook=clip["hook"] or "", transcript=spoken,
        duration=clip["end"] - clip["start"], channel=clip["source_channel"] or "",
        trends=opts.get("trend_brief") or trends.brief(), size=size,
    )
    if result:
        db.update("clips", clip_id, panel=result, panel_score=result["score"])


AUTO_CLIPS = 30  # "as many as found" (max_clips 0) still stops here, so one video cannot flood the list
AUTO_MIN_SCORE = 60  # and keeps only clips the AI scores at least this high
FULL_TRANSCRIPT_SEC = 2 * 3600  # longer videos: only the stretches around the strongest moments are transcribed
SPEECH_BEFORE, SPEECH_AFTER = 150.0, 105.0  # each stretch reaches this far before and after its moment


def clip_settings(clip: dict[str, Any]) -> dict[str, Any]:
    """The clip settings a job runs with. `max_clips` 0 means as many as are found: then up to
    AUTO_CLIPS clips are made, each scoring at least AUTO_MIN_SCORE."""
    if int(clip.get("max_clips") or 0) > 0:
        return clip
    return {**clip, "max_clips": AUTO_CLIPS, "min_score": max(float(clip.get("min_score") or 0), AUTO_MIN_SCORE)}


def _local_source(folder: Path) -> Path | None:
    """The video file in a job folder (an upload), if it is complete."""
    found = [p for p in folder.glob("source.*") if p.suffix.lower() in SOURCE_SUFFIXES]
    return found[0] if found and (folder / "download.done").exists() else None


def _download_sound(job_id: str, folder: Path, url: str) -> Path:
    """Only the sound of a linked video, kept until its moments are chosen."""
    found = [p for p in folder.glob("audio.*") if p.suffix.lower() not in media.NOT_MEDIA]
    if found and (folder / "audio.done").exists():
        return found[0]
    set_step(job_id, "Downloading the sound", 0.03)
    require_disk()
    sound = media.download_audio(
        url, folder,
        on_progress=cancel_aware(job_id, lambda p: set_step(job_id, "Downloading the sound", 0.03 + 0.12 * p)))
    (folder / "audio.done").write_text("ok")
    return sound


def _speech_ranges(loudness: np.ndarray, meta: dict[str, Any],
                   clip_cfg: dict[str, Any]) -> list[tuple[float, float]] | None:
    """What to transcribe: None (all of it) for a video up to FULL_TRANSCRIPT_SEC. A longer one, say an
    eleven-hour stream, is first ranked from its loudness and YouTube's replay curve; only the
    stretches around its strongest moments are transcribed, which takes minutes instead of hours."""
    if loudness.size <= FULL_TRANSCRIPT_SEC:
        return None
    rough = signals.analyze(loudness, [], meta.get("heatmap"))
    cands = signals.candidates(rough, spacing=int(SPEECH_AFTER),
                               limit=min(max(int(clip_cfg["max_clips"]) * 4, 16), 40))
    return signals.ranges_around(cands, before=SPEECH_BEFORE, after=SPEECH_AFTER, total=float(loudness.size))


def process_video(job: dict[str, Any]) -> None:
    job_id, settings = job["id"], job["options"]
    folder = job_dir(job_id)
    fmt, ai = settings["format"], settings["ai"]
    if not job.get("url") and not (folder / "meta.json").exists():  # an upload whose file is gone
        raise RuntimeError("The uploaded file is no longer on the server. Upload it again.")

    set_step(job_id, "Reading video details", 0.01)
    meta = _load(folder / "meta.json")
    if not meta:
        meta = media.fetch_info(job["url"], max_height=int(fmt["source_quality"]))
        meta.pop("stream_urls", None)
        _save(folder / "meta.json", meta)
    max_minutes = settings.get("max_minutes")
    if max_minutes:
        meta["duration"] = min(meta["duration"], max_minutes * 60) if meta["duration"] else max_minutes * 60
    db.update("jobs", job_id, title=meta["title"], channel=meta["channel"],
              duration=meta["duration"], thumbnail=meta["thumbnail"])
    check(job_id)

    url = meta.get("webpage_url") or job.get("url")
    # An upload is already here. A link is never downloaded whole: its sound is enough to find the
    # moments, and each clip's part of the picture is fetched when that clip is rendered.
    local = _local_source(folder) if not job.get("url") else None
    if not job.get("url") and local is None:
        raise RuntimeError("The uploaded file is no longer on the server. Upload it again.")
    clip_cfg = clip_settings(settings["clip"])

    picks = _load(folder / "selection.json")
    if picks is None:
        transcript = _load(folder / "transcript.json")
        sound = local
        if local is None and (not (folder / "levels.json").exists() or transcript is None):
            sound = _download_sound(job_id, folder, job["url"])
        check(job_id)

        levels = _load(folder / "levels.json")
        if levels is None:
            seconds = float(meta.get("duration") or 0) or 1.0
            step = "Listening to the whole video"
            set_step(job_id, step, 0.16)
            measured = signals.loudness_per_second(
                sound, on_progress=cancel_aware(job_id, lambda n: set_step(job_id, step, 0.16 + 0.09 * min(n / seconds, 1.0)), every=0))
            if max_minutes:
                measured = measured[:int(max_minutes * 60)]
            levels = [round(float(v), 1) for v in measured]
            _save(folder / "levels.json", levels)
        loudness = np.array(levels, dtype=np.float32)
        check(job_id)

        if transcript is None:
            ranges = _speech_ranges(loudness, meta, clip_cfg)
            last = [0.0]

            def on_progress(p: float) -> None:
                if p - last[0] >= 0.03:
                    last[0] = p
                    check(job_id)
                    set_step(job_id, "Transcribing speech", 0.26 + 0.34 * p)

            set_step(job_id, "Transcribing speech" if ranges is None else
                     "Transcribing the strongest parts", 0.26)
            transcript = transcribe.transcribe(
                sound, model_name=ai["whisper_model"], language=settings["captions"]["language"],
                duration=float(loudness.size), ranges=ranges, levels=loudness, on_progress=on_progress,
            )
            transcript["ranges"] = ranges  # None: the whole video was transcribed
            _save(folder / "transcript.json", transcript)
        check(job_id)

        set_step(job_id, "Finding the strongest moments", 0.62)
        score = signals.analyze(loudness, transcript["words"], meta.get("heatmap"))
        _save(folder / "signals.json", [round(float(v), 2) for v in np.nan_to_num(score)])
        cands = signals.candidates(
            score, spacing=int(clip_cfg["max_sec"]),
            limit=min(max(int(clip_cfg["max_clips"]) * 3, 6), 24),
        )
        set_step(job_id, "Checking what is trending", 0.65)
        trend_brief = trends.brief_for(meta)
        _save(folder / "trends.json", trend_brief)
        check(job_id)
        set_step(job_id, "AI is choosing the clips", 0.68)
        picks, warning = selection.select_clips(
            cands=cands, transcript=transcript, meta=meta, clip_cfg=clip_cfg,
            trends=trend_brief, signal=score,
        )
        picks = [add_context(pick, duration=meta["duration"], settings=settings) for pick in picks]
        for pick in picks:
            pick["id"] = db.new_id()
        _save(folder / "selection.json", picks)
        if warning:
            db.update("jobs", job_id, payload={**(job.get("payload") or {}), "warning": warning})
    for sound_file in folder.glob("audio.*"):  # the sound is not needed once the moments are chosen
        sound_file.unlink(missing_ok=True)
    check(job_id)

    total = max(len(picks), 1)
    done = [0]

    def work(pick: dict[str, Any]) -> None:
        make_clip(job_id=job_id, clip_id=pick["id"], sel=pick, source=local, url=url,
                  words_file=folder / "transcript.json", meta=meta, settings=settings,
                  trend_brief=_load(folder / "trends.json") or "")
        done[0] += 1
        set_step(job_id, f"Rendering clips ({done[0]} of {total})", 0.75 + 0.25 * done[0] / total)

    set_step(job_id, f"Rendering clips (0 of {total})", 0.75)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_in_context(work), picks))
    check(job_id)
    if not picks:
        set_step(job_id, "No moment was strong enough to clip", 1.0)
    else:
        set_step(job_id, f"{len(picks)} {'clip' if len(picks) == 1 else 'clips'} ready", 1.0)
    if storage.enabled() and storage.cfg().get("delete_source") and picks:
        if db.count("clips", {"job_id": job_id, "status": "ready"}):  # only once its clips exist
            persist_job_files(job_id, folder, local)


def process_more(job: dict[str, Any]) -> None:
    """Find more moments in a video that was already clipped: skip what was used, render
    the next best ones and add them to the same video. Needs no transcription: the
    transcript, details and audio signals from the first pass are reused."""
    payload = job["payload"] or {}
    source_id = payload["source_job_id"]
    source_job = db.find_one("jobs", {"id": source_id})
    if not source_job:
        raise RuntimeError("That video no longer exists.")
    folder = job_dir(source_id)
    restore_job_files(source_id)  # the working files are kept in storage, not on this server
    meta, transcript = _load(folder / "meta.json"), _load(folder / "transcript.json")
    if not meta or not transcript:
        raise RuntimeError("The working files for this video were removed, so more clips cannot be "
                           "made from it. Add the link again.")
    signal = np.array(_load(folder / "signals.json") or [], dtype=np.float32)
    count = int(payload.get("count") or 3)
    settings = config.deep_merge(source_job["options"], {"clip": {"max_clips": count}})
    clip_cfg = settings["clip"]
    existing = db.find("clips", {"job_id": source_id, "status": {"$ne": "failed"}}, fields=["start", "end"])
    used = [(c["start"], c["end"]) for c in existing]

    set_step(job["id"], "Looking for more moments", 0.1)
    cands = signals.candidates(signal, spacing=int(clip_cfg["max_sec"]), limit=min(max(count * 4, 8), 40))
    trend_brief = _load(folder / "trends.json") or ""
    picks, warning = selection.select_clips(
        cands=cands, transcript=transcript, meta=meta, clip_cfg=clip_cfg, trends=trend_brief,
        signal=signal if signal.size else None, exclude=used)
    if warning:  # without the AI the only option is audio peaks, which would repeat the first pass
        raise RuntimeError(warning)
    check(job["id"])
    if not picks:
        db.update("jobs", job["id"], payload={**payload, "made": 0})
        set_step(job["id"], "No other strong moments were found", 1.0)
        return

    # An upload's file is kept (here or in storage); a link has none, and each clip fetches its part.
    source = restore_job_files(source_id, source=True)
    url = meta.get("webpage_url") or source_job.get("url")
    if source is None and not url:
        raise RuntimeError("The uploaded file is no longer available, so more clips cannot be made from it. "
                           "Upload it again.")
    check(job["id"])

    picks = [add_context(pick, duration=float(meta.get("duration") or 0), settings=settings) for pick in picks]
    for index, pick in enumerate(picks):
        pick["id"] = db.new_id()
        if payload.get("label_part"):  # continue the numbering from the clips already made
            suffix = f" – Part {len(existing) + index + 1}"
            pick["title"] = pick["title"][:100 - len(suffix)].rstrip() + suffix
    done = [0]

    def work(pick: dict[str, Any]) -> None:
        make_clip(job_id=source_id, clip_id=pick["id"], sel=pick, source=source, url=url,
                  words_file=folder / "transcript.json", meta=meta, settings=settings,
                  trend_brief=trend_brief if isinstance(trend_brief, str) else "")
        done[0] += 1
        set_step(job["id"], f"Rendering clips ({done[0]} of {len(picks)})", 0.6 + 0.4 * done[0] / len(picks))

    set_step(job["id"], f"Rendering clips (0 of {len(picks)})", 0.6)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_in_context(work), picks))
    db.update("jobs", job["id"], payload={**payload, "made": len(picks)})
    set_step(job["id"], f"{len(picks)} more {'clip' if len(picks) == 1 else 'clips'} ready", 1.0)
    if storage.enabled():
        if (source_job.get("files") or {}).get("source"):  # already in storage: just clear this server
            shutil.rmtree(folder, ignore_errors=True)
        else:
            persist_job_files(source_id, folder, source)


def _rerender_input(clip: dict[str, Any], opts: dict[str, Any]) -> tuple[Path | None, str | None]:
    """What to render an edited clip from: (local file, None) or (None, link to fetch its part from)."""
    if opts.get("url"):
        return None, opts["url"]
    saved = opts.get("source") or ""
    source = restore_job_files(clip["job_id"], source=True) or (config.DATA / saved if saved else None)
    if source is not None and source.exists():
        return source, None
    if "/live/" in saved or not clip.get("source_url"):
        raise RuntimeError("The recording of this moment was removed after clipping, so it "
                           "cannot be re-rendered. Re-clip the video instead.")
    return None, clip["source_url"]  # made before parts were fetched per clip: fetch its part now


def rerender(job: dict[str, Any]) -> None:
    """Render an edited clip again (new trim, title or format)."""
    clip_id = job["payload"]["clip_id"]
    clip = db.find_one("clips", {"id": clip_id})
    if not clip:
        raise RuntimeError("That clip no longer exists.")
    opts = clip["options"]
    restore_job_files(clip["job_id"])
    source, url = _rerender_input(clip, opts)
    set_step(job["id"], "Rendering clip", 0.2)
    db.update("clips", clip_id, status="rendering")
    words = clip_words(clip)
    out_dir = job_dir(clip["job_id"]) / "clips"
    try:
        with clip_source(job_id=clip["job_id"], start=clip["start"], end=clip["end"], settings=opts["settings"],
                         local=source, url=url) as (path, offset):
            result = render.render_clip(
                source=path, out_dir=out_dir, clip_id=clip_id, start=clip["start"], end=clip["end"],
                words=words, settings=opts["settings"], title=clip["hook"] or "",
                credit=opts.get("credit", ""), offset=offset,
            )
    except Exception as exc:
        db.update("clips", clip_id, status="failed", error=str(exc)[:500])
        raise
    file_ref, thumb_ref = store_outputs(clip["job_id"], out_dir, result)
    storage.delete(clip["file"]) if clip["file"] != file_ref else None
    db.update("clips", clip_id, status="ready", error=None, width=result["width"],
              height=result["height"], file=file_ref, thumb=thumb_ref)
    if job["payload"].get("rescore"):
        set_step(job["id"], "Audience panel is watching", 0.8)
        score_clip(clip_id)
    set_step(job["id"], "Clip ready", 1.0)
    if storage.enabled():  # nothing is left behind on this server
        shutil.rmtree(job_dir(clip["job_id"]), ignore_errors=True)


def detect_kind(job: dict[str, Any]) -> str:
    """A pasted link is either a finished video or a stream that is live now."""
    info = media.fetch_info(job["url"], max_height=720)
    db.update("jobs", job["id"], title=info["title"], channel=info["channel"],
              duration=info["duration"], thumbnail=info["thumbnail"])
    if info["live_status"] == "is_upcoming":
        raise media.MediaError("This stream has not started yet. Add it again once it is live.")
    return "live" if info["live_status"] == "is_live" else "video"


def delete_job_files(job_id: str, files: dict[str, Any] | None = None) -> None:
    """Remove a video's working folder here and its saved files in storage."""
    shutil.rmtree(config.JOBS_DIR / job_id, ignore_errors=True)
    for ref in [*((files or {}).get("artifacts") or {}).values(), (files or {}).get("source")]:
        storage.delete(ref)


__all__ = ["Cancelled", "process_video", "process_more", "rerender", "detect_kind", "make_clip",
           "job_dir", "set_step", "check", "delete_job_files", "llm"]
