"""Background worker. Runs as its own process, separate from the dashboard.

Start it with:  python -m app.worker
It picks queued jobs from the database, so closing the browser (or the API)
does not stop any work. Jobs that were running when the worker last stopped
are queued again and resume from their last finished step.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, db, live, llm, runner, storage, youtube
from .pipeline import media, trends

HEARTBEAT_KEY = "worker"
HEARTBEAT_STALE = 12.0
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


def is_alive() -> bool:
    """A recent heartbeat from a process that still exists."""
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
    post = db.one("SELECT * FROM posts WHERE id = ?", (job["payload"]["post_id"],))
    clip = db.one("SELECT * FROM clips WHERE id = ?", (post["clip_id"],)) if post else None
    if not post or not clip:
        raise RuntimeError("The clip for this post was deleted.")
    category = youtube.CATEGORY_IDS.get((clip["options"] or {}).get("category", ""), "24")
    try:
        with storage.local_copy(clip["file"]) as video:
            video_id = youtube.upload(
                file=video, title=post["title"], description=post["description"],
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


def run_migrate(job: dict[str, Any]) -> None:
    runner.set_step(job["id"], "Moving clips to storage", 0.02)
    result = storage.migrate_local_clips(lambda p: runner.set_step(job["id"], "Moving clips to storage", p))
    runner.set_step(job["id"], f"Moved {result['moved']} clips, freed {result['freed_mb']} MB of sources", 1.0)


def run_trends(job: dict[str, Any]) -> None:
    runner.set_step(job["id"], "Reading YouTube trends", 0.3)
    trends.refresh()
    runner.set_step(job["id"], "Trends updated", 1.0)


HANDLERS = {"video": runner.process_video, "more": runner.process_more, "live": live.run_live,
            "render": runner.rerender,
            "upload": run_upload, "trends": run_trends, "migrate": run_migrate}


def execute(job: dict[str, Any]) -> None:
    job_id = job["id"]
    try:
        if job["kind"] == "auto":
            kind = runner.detect_kind(job)
            db.update("jobs", job_id, kind=kind, status="queued", step="Queued")
            return
        HANDLERS[job["kind"]](job)
        db.update("jobs", job_id, status="done", error=None)
        if job["kind"] in ("video", "live"):
            count = db.one("SELECT COUNT(*) AS n FROM clips WHERE job_id = ? AND status = 'ready'",
                           (job_id,))["n"]
            fresh = db.one("SELECT title FROM jobs WHERE id = ?", (job_id,))
            notify(f"{count} clips ready", (fresh or {}).get("title") or "Your video is finished.")
        elif job["kind"] == "more":
            fresh = db.one("SELECT title, payload FROM jobs WHERE id = ?", (job_id,)) or {}
            made = int((fresh.get("payload") or {}).get("made") or 0)
            notify(f"{made} more clips ready" if made else "No more strong moments found",
                   fresh.get("title") or "Your video")
    except runner.Cancelled:
        runner.cleanup_partial((job.get("payload") or {}).get("source_job_id") or job_id)
        row = db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
        if row and row["status"] == "stopping":
            db.update("jobs", job_id, status="done")
    except (media.MediaError, llm.LLMError, youtube.YouTubeError, storage.StorageError,
            RuntimeError) as exc:
        db.update("jobs", job_id, status="failed", error=str(exc)[:500])
    except Exception as exc:
        traceback.print_exc()
        db.update("jobs", job_id, status="failed",
                  error=f"Unexpected error: {type(exc).__name__}: {str(exc)[:300]}")


def claim(job_id: str) -> bool:
    return db.execute("UPDATE jobs SET status = 'running', updated_at = ? "
                      "WHERE id = ? AND status = 'queued'", (time.time(), job_id)) == 1


def dispatch() -> None:
    for job in db.query("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at"):
        if job["kind"] in ("video", "more"):  # both use the graphics card and the disk heavily
            busy = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'running' AND kind IN ('video', 'more')")
            if busy["n"] or not claim(job["id"]):
                continue
            heavy.submit(execute, job)
        elif job["kind"] == "live":
            if claim(job["id"]):
                threading.Thread(target=execute, args=(job,), daemon=True,
                                 name=f"live-{job['id']}").start()
        elif claim(job["id"]):
            light.submit(execute, job)


def refresh_views() -> None:
    posts = db.query("SELECT id, youtube_id FROM posts WHERE youtube_id IS NOT NULL")
    if not posts or not youtube.TOKEN_FILE.exists():
        return
    try:
        counts = youtube.view_counts([p["youtube_id"] for p in posts])
    except Exception:
        return
    for post in posts:
        if post["youtube_id"] in counts:
            db.update("posts", post["id"], views=counts[post["youtube_id"]])


def main() -> None:
    db.init()
    if is_alive():
        print("A worker is already running.")
        return
    # Anything left "running" belonged to a worker that stopped: resume it.
    db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    db.execute("UPDATE jobs SET status = 'done' WHERE status = 'stopping'")
    if storage.enabled() and storage.local_clip_count():
        queued = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'migrate' AND status IN ('queued','running')")
        if not queued["n"]:
            db.insert("jobs", {"id": db.new_id(), "kind": "migrate", "status": "queued", "step": "Queued",
                               "progress": 0, "title": "Move clips to storage", "options": {}, "payload": {},
                               "created_at": time.time(), "updated_at": time.time()})
    print(f"Worker started (pid {os.getpid()}). Data folder: {config.DATA}")
    last_views = 0.0
    last_sweep = 0.0
    while True:
        try:
            db.kv_set(HEARTBEAT_KEY, {"at": time.time(), "pid": os.getpid()})
            dispatch()
            running = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('running','queued')")["n"]
            keep_awake(running > 0)
            if time.time() - last_sweep > 3600:  # at start, then hourly
                last_sweep = time.time()
                light.submit(runner.sweep_orphans)
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
