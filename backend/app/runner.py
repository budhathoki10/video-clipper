"""Job steps for a pasted video link, and the shared clip-making code.

Every step writes its result to the job folder, so a job that is interrupted
(worker restart, crash, reboot) continues from the last finished step.
"""
from __future__ import annotations

import contextvars
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

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


def fetch_source_again(clip: dict[str, Any], opts: dict[str, Any]) -> Path:
    """Download the source video again for a clip whose copy was removed."""
    if "/live/" in opts["source"] or not clip.get("source_url"):
        raise RuntimeError("The recording of this moment was removed after clipping, so it "
                           "cannot be re-rendered. Re-clip the video instead.")
    folder = job_dir(clip["job_id"])
    path = media.download(clip["source_url"], folder,
                          max_height=int(opts["settings"]["format"]["source_quality"]))
    opts["source"] = rel(path)
    db.update("clips", clip["id"], options=opts)
    return path


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


def make_clip(*, job_id: str, clip_id: str, sel: dict[str, Any], source: Path,
              words_file: Path, meta: dict[str, Any], settings: dict[str, Any],
              trend_brief: str = "") -> None:
    """Render one clip, then score it with the audience panel."""
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
            "options": {"settings": settings, "source": rel(source), "words": rel(words_file),
                        "credit": credit_line(meta), "trend_brief": trend_brief,
                        "category": meta.get("category", "")},
            "created_at": time.time(),
        })
    words = _load(words_file)["words"]
    out_dir = job_dir(job_id) / "clips"
    try:
        result = render.render_clip(
            source=source, out_dir=out_dir, clip_id=clip_id, start=sel["start"], end=sel["end"],
            words=words, settings=settings, title=sel.get("hook") or "", credit=credit_line(meta),
        )
    except Exception as exc:
        db.update("clips", clip_id, status="failed", error=str(exc)[:500])
        return
    file_ref, thumb_ref = store_outputs(job_id, out_dir, result)
    db.update("clips", clip_id, status="ready", error=None, file=file_ref, thumb=thumb_ref,
              width=result["width"], height=result["height"])
    score_clip(clip_id)


def score_clip(clip_id: str) -> None:
    clip = db.find_one("clips", {"id": clip_id})
    if not clip:
        return
    opts = clip["options"]
    size = int(opts["settings"]["ai"].get("panel_size", 0))
    if size <= 0:
        return
    restore_job_files(clip["job_id"])
    words = _load(config.DATA / opts["words"])["words"]
    spoken = " ".join(w["w"] for w in words if clip["start"] <= w["s"] <= clip["end"])
    result = panel.run_panel(
        title=clip["title"], hook=clip["hook"] or "", transcript=spoken,
        duration=clip["end"] - clip["start"], channel=clip["source_channel"] or "",
        trends=opts.get("trend_brief") or trends.brief(), size=size,
    )
    if result:
        db.update("clips", clip_id, panel=result, panel_score=result["score"])


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

    found = [p for p in folder.glob("source.*") if p.suffix.lower() in SOURCE_SUFFIXES]
    if found and (folder / "download.done").exists():
        source = found[0]
    elif not job.get("url"):  # an uploaded file that is no longer here cannot be fetched again
        raise RuntimeError("The uploaded file is no longer on the server. Upload it again.")
    else:
        set_step(job_id, "Downloading video", 0.03)
        require_disk()
        source = media.download(
            job["url"], folder, max_height=int(fmt["source_quality"]), max_minutes=max_minutes,
            on_progress=cancel_aware(job_id, lambda p: set_step(job_id, "Downloading video", 0.03 + 0.27 * p)),
        )
        (folder / "download.done").write_text("ok")
    check(job_id)

    wav = folder / "audio.wav"
    transcript = _load(folder / "transcript.json")
    if not transcript:
        set_step(job_id, "Preparing audio", 0.31)
        media.extract_audio(source, wav)
        duration = media.audio_duration(wav)
        last = [0.0]

        def on_progress(p: float) -> None:
            if p - last[0] >= 0.03:
                last[0] = p
                set_step(job_id, "Transcribing speech", 0.33 + 0.27 * p)

        set_step(job_id, "Transcribing speech", 0.33)
        transcript = transcribe.transcribe(
            wav, model_name=ai["whisper_model"], language=settings["captions"]["language"],
            duration=duration, on_progress=on_progress,
        )
        _save(folder / "transcript.json", transcript)
    check(job_id)

    picks = _load(folder / "selection.json")
    if picks is None:
        set_step(job_id, "Finding the strongest moments", 0.62)
        if not wav.exists():
            media.extract_audio(source, wav)
        score = signals.analyze(wav, transcript["words"], meta.get("heatmap"))
        _save(folder / "signals.json", [round(float(v), 2) for v in np.nan_to_num(score)])
        clip_cfg = settings["clip"]
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
        for pick in picks:
            pick["id"] = db.new_id()
        _save(folder / "selection.json", picks)
        if warning:
            db.update("jobs", job_id, payload={"warning": warning})
    wav.unlink(missing_ok=True)
    check(job_id)

    total = max(len(picks), 1)
    done = [0]

    def work(pick: dict[str, Any]) -> None:
        make_clip(job_id=job_id, clip_id=pick["id"], sel=pick, source=source,
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
            persist_job_files(job_id, folder, source)


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

    source = restore_job_files(source_id, source=True)  # a local copy, or the one kept in storage
    if source is None:
        set_step(job["id"], "Downloading the video again", 0.25)
        require_disk()
        source = media.download(
            meta.get("webpage_url") or source_job["url"], folder,
            max_height=int(settings["format"]["source_quality"]), max_minutes=settings.get("max_minutes"),
            on_progress=cancel_aware(job["id"], lambda p: set_step(job["id"], "Downloading the video again", 0.25 + 0.3 * p)))
    check(job["id"])

    for index, pick in enumerate(picks):
        pick["id"] = db.new_id()
        if payload.get("label_part"):  # continue the numbering from the clips already made
            suffix = f" – Part {len(existing) + index + 1}"
            pick["title"] = pick["title"][:100 - len(suffix)].rstrip() + suffix
    done = [0]

    def work(pick: dict[str, Any]) -> None:
        make_clip(job_id=source_id, clip_id=pick["id"], sel=pick, source=source,
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


def rerender(job: dict[str, Any]) -> None:
    """Render an edited clip again (new trim, title or format)."""
    clip_id = job["payload"]["clip_id"]
    clip = db.find_one("clips", {"id": clip_id})
    if not clip:
        raise RuntimeError("That clip no longer exists.")
    opts = clip["options"]
    restore_job_files(clip["job_id"])
    source = restore_job_files(clip["job_id"], source=True) or config.DATA / opts["source"]
    if not source.exists():
        source = fetch_source_again(clip, opts)
    set_step(job["id"], "Rendering clip", 0.2)
    db.update("clips", clip_id, status="rendering")
    words = _load(config.DATA / opts["words"])["words"]
    out_dir = job_dir(clip["job_id"]) / "clips"
    try:
        result = render.render_clip(
            source=source, out_dir=out_dir, clip_id=clip_id, start=clip["start"], end=clip["end"],
            words=words, settings=opts["settings"], title=clip["hook"] or "",
            credit=opts.get("credit", ""),
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
