"""Background worker. Runs as its own process, separate from the dashboard.

Start it with:  python -m app.worker
It picks queued jobs from the database, so closing the browser (or the API)
does not stop any work. Jobs that were running when the worker last stopped
are queued again and resume from their last finished step.
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, db, live, llm, runner, storage, youtube
from .pipeline import media, trends

# Several workers, on different computers, can share one database. Each is known by its computer's
# name. A worker only takes the jobs it accepts: "all" (one computer does everything), "links" (only
# jobs made from a pasted link, for a home computer YouTube does not block) or "files" (everything
# except those, for a hosting server that YouTube blocks).
HOST = os.environ.get("CLIPPER_WORKER_HOST") or socket.gethostname()
HEARTBEAT_PREFIX = "worker:"
HEARTBEAT_KEY = f"{HEARTBEAT_PREFIX}{HOST}"
HEARTBEAT_STALE = 12.0
ORPHAN_STALE = 90.0  # a worker silent for this long is gone; the jobs it was running go back to the queue
ACCEPTS = (config.secret("CLIPPER_WORKER_ACCEPTS") or "all").lower()
WAITING_FOR_HOME = "Waiting for the home computer"
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
VIEW_REFRESH_SECONDS = 30 * 60

heavy = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video")
light = ThreadPoolExecutor(max_workers=3, thread_name_prefix="light")


def _pid_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # query limited information
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


def accepts(job: dict[str, Any]) -> bool:
    """Whether this worker may take the job (see ACCEPTS)."""
    from_link = bool(job.get("url")) and job["kind"] in ("auto", "video", "live")
    if ACCEPTS == "links":
        return from_link
    if ACCEPTS == "files":
        return not from_link
    return True


def requeue_orphans() -> int:
    """Put jobs back in the queue when the worker running them has stopped (a redeployed server, a
    computer that was switched off), whichever computer it was."""
    now = time.time()
    alive = {doc["_id"].removeprefix(HEARTBEAT_PREFIX) for doc in db.coll("kv").find(
        {"_id": {"$regex": f"^{HEARTBEAT_PREFIX}"}}) if now - (doc.get("value") or {}).get("at", 0) <= ORPHAN_STALE}
    return db.update_where("jobs", {"status": "running", "worker": {"$exists": True, "$nin": sorted(alive) + [None]}},
                           {"status": "queued", "step": "Queued", "worker": None})


def is_alive() -> bool:
    """A recent heartbeat from this computer's worker, from a process that still exists."""
    beat = db.kv_get(HEARTBEAT_KEY)
    if not beat or time.time() - beat.get("at", 0) > HEARTBEAT_STALE:
        return False
    return _pid_running(int(beat.get("pid", 0)))


def keep_awake(active: bool) -> None:
    """Stop Windows from sleeping while jobs are running."""
    if os.name == "nt":
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)


def notify(title: str, body: str) -> subprocess.Popen | None:
    """Windows toast, best effort. Text is passed through the environment."""
    if os.name != "nt":
        return None
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]>$null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$t.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($t.CreateTextNode($env:CLIPPER_TOAST_TITLE))>$null;"
        "$n.Item(1).AppendChild($t.CreateTextNode($env:CLIPPER_TOAST_BODY))>$null;"
        "$id='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    env = {**os.environ, "CLIPPER_TOAST_TITLE": title[:60], "CLIPPER_TOAST_BODY": body[:180]}
    try:
        return subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, creationflags=media.NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError:
        return None


def run_upload(job: dict[str, Any]) -> None:
    post = db.find_one("posts", {"id": job["payload"]["post_id"]})
    clip = db.find_one("clips", {"id": post["clip_id"]}) if post else None
    if not post or not clip:
        raise RuntimeError("The clip for this post was deleted.")
    category = youtube.CATEGORY_IDS.get((clip["options"] or {}).get("category", ""), "24")
    try:
        with storage.local_copy(clip["file"]) as video:
            video_id = youtube.upload(
                post["user_id"], file=video, title=post["title"], description=post["description"],
                tags=clip["hashtags"] or [], privacy=post["privacy"], publish_at=post["publish_at"],
                category_id=category,
                on_progress=lambda p: runner.set_step(job["id"], "Uploading to YouTube", p),
            )
    except Exception as exc:
        db.update("posts", post["id"], status="failed", error=str(exc)[:400])
        raise
    db.update("posts", post["id"], youtube_id=video_id, error=None,
              status="scheduled" if post["publish_at"] else "published")
    runner.set_step(job["id"], "Uploaded", 1.0)


def run_trends(job: dict[str, Any]) -> None:
    runner.set_step(job["id"], "Reading YouTube trends", 0.3)
    trends.refresh()
    runner.set_step(job["id"], "Trends updated", 1.0)


HANDLERS = {"video": runner.process_video, "more": runner.process_more, "live": live.run_live,
            "render": runner.rerender,
            "upload": run_upload, "trends": run_trends}


def execute(job: dict[str, Any]) -> None:
    """Run one job on behalf of the person who made it (their AI choice and keys)."""
    with llm.as_user(job.get("user_id")):
        _execute(job)


def _execute(job: dict[str, Any]) -> None:
    job_id = job["id"]
    try:
        if job["kind"] == "auto":
            kind = runner.detect_kind(job)
            db.update("jobs", job_id, kind=kind, status="queued", step="Queued")
            return
        HANDLERS[job["kind"]](job)
        db.update("jobs", job_id, status="done", error=None)
        if job["kind"] in ("video", "live"):
            count = db.count("clips", {"job_id": job_id, "status": "ready"})
            fresh = db.find_one("jobs", {"id": job_id})
            notify(f"{count} clips ready", (fresh or {}).get("title") or "Your video is finished.")
        elif job["kind"] == "more":
            fresh = db.find_one("jobs", {"id": job_id}) or {}
            made = int((fresh.get("payload") or {}).get("made") or 0)
            notify(f"{made} more clips ready" if made else "No more strong moments found",
                   fresh.get("title") or "Your video")
    except runner.Cancelled:
        runner.cleanup_partial((job.get("payload") or {}).get("source_job_id") or job_id)
        row = db.find_one("jobs", {"id": job_id})
        if row and row["status"] == "stopping":
            db.update("jobs", job_id, status="done")
    except (media.MediaError, llm.LLMError, youtube.YouTubeError, storage.StorageError,
            RuntimeError) as exc:
        print(f"Job {job_id} ({job['kind']}) failed: {str(exc)[:500]}", flush=True)
        db.update("jobs", job_id, status="failed", error=str(exc)[:500])
    except Exception as exc:
        print(f"Job {job_id} ({job['kind']}) crashed:", flush=True)
        traceback.print_exc()
        db.update("jobs", job_id, status="failed",
                  error=f"Unexpected error: {type(exc).__name__}: {str(exc)[:300]}")


def claim(job_id: str) -> bool:
    return db.claim_job(job_id, HOST)


def dispatch() -> None:
    for job in db.find("jobs", {"status": "queued"}, sort=[("created_at", 1)]):
        if not accepts(job):
            continue
        if job["kind"] in ("video", "more"):  # both use the graphics card and the disk heavily
            if db.count("jobs", {"status": "running", "worker": HOST, "kind": {"$in": ["video", "more"]}}) \
                    or not claim(job["id"]):
                continue
            heavy.submit(execute, job)
        elif job["kind"] == "live":
            if claim(job["id"]):
                threading.Thread(target=execute, args=(job,), daemon=True,
                                 name=f"live-{job['id']}").start()
        elif claim(job["id"]):
            light.submit(execute, job)


def refresh_views() -> None:
    """Update view counts, one person's channel at a time (each post is on its owner's channel)."""
    by_person: dict[str, list[dict[str, Any]]] = {}
    for post in db.find("posts", {"youtube_id": {"$ne": None}}, fields=["id", "youtube_id", "user_id"]):
        if post.get("user_id"):
            by_person.setdefault(post["user_id"], []).append(post)
    for user_id, posts in by_person.items():
        try:
            counts = youtube.view_counts(user_id, [p["youtube_id"] for p in posts])
        except Exception:
            continue  # this person's channel is not connected or its sign-in expired
        for post in posts:
            if post["youtube_id"] in counts:
                db.update("posts", post["id"], views=counts[post["youtube_id"]])


def main() -> None:
    db.init()
    if is_alive():
        print("A worker is already running.")
        return
    # Anything left "running" belonged to a worker that stopped: resume it.
    mine = {"$in": [None, HOST]}  # jobs this computer was running; other computers' jobs are theirs
    db.update_where("jobs", {"status": "running", "worker": mine}, {"status": "queued"})
    db.update_where("jobs", {"status": "stopping", "worker": mine}, {"status": "done"})
    requeue_orphans()
    print(f"Worker started on {HOST} (pid {os.getpid()}), takes: {ACCEPTS}. Data folder: {config.DATA}", flush=True)
    last_views = 0.0
    last_sweep = 0.0
    last_orphans = 0.0
    while True:
        try:
            db.kv_set(HEARTBEAT_KEY, {"at": time.time(), "pid": os.getpid(), "host": HOST, "accepts": ACCEPTS})
            if time.time() - last_orphans > 30:
                last_orphans = time.time()
                requeue_orphans()
            dispatch()
            running = db.count("jobs", {"status": {"$in": ["running", "queued"]}})
            keep_awake(running > 0)
            if time.time() - last_sweep > 3600:  # at start, then hourly
                last_sweep = time.time()
                light.submit(runner.sweep_orphans)
                light.submit(runner.sweep_uploads)
            if time.time() - last_views > VIEW_REFRESH_SECONDS:
                last_views = time.time()
                light.submit(refresh_views)
        except Exception:
            traceback.print_exc()
        time.sleep(1.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
