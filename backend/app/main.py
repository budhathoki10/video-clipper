"""Dashboard API. It only reads and writes the database and queues jobs;
all heavy work happens in the separate worker process (app.worker)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, db, llm, runner, storage, worker, youtube
from .pipeline import selection, trends

ACTIVE = ("queued", "running", "stopping")


def ensure_worker() -> bool:
    """Start the background worker as a detached process if none is running."""
    if os.environ.get("CLIPPER_NO_WORKER") == "1" or worker.is_alive():
        return False
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.DETACHED_PROCESS)
    log = open(config.DATA / "worker.log", "a", encoding="utf-8")
    subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=config.BACKEND, stdout=log,
                     stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return True


WORKER_CHECK_SECONDS = 15


async def keep_worker_running() -> None:
    """The worker is a separate process. If it stops (crash, being killed, a restart
    race), start it again so queued jobs never wait for a worker that is gone."""
    while True:
        await asyncio.sleep(WORKER_CHECK_SECONDS)
        try:
            await asyncio.to_thread(ensure_worker)
        except Exception:
            pass  # try again on the next check


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    ensure_worker()
    watcher = asyncio.create_task(keep_worker_running())
    yield
    watcher.cancel()


app = FastAPI(title="Clipper", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

PUBLIC_API = {
    "/api/auth/google", "/api/auth/google/callback", "/api/auth/google/status", "/api/auth/logout",
    "/api/health", "/api/ping", "/api/docs", "/api/openapi.json",
}


@app.middleware("http")
async def require_account(request: Request, call_next):
    """The dashboard is static, so the API is the security boundary."""
    path = request.url.path.rstrip("/") or "/"
    protected = path.startswith("/api/") and path not in PUBLIC_API
    if protected and request.method != "OPTIONS" and os.environ.get("CLIPPER_AUTH_DISABLED") != "1":
        user = auth.user_for_token(request.cookies.get(auth.SESSION_COOKIE))
        if not user:
            return JSONResponse({"detail": "Sign in to continue."}, status_code=401)
        request.state.user = user
    return await call_next(request)


# ---- models ----------------------------------------------------------------
class JobCreate(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    options: dict[str, Any] = Field(default_factory=dict)


class ClipPatch(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    hook: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=4900)
    hashtags: list[str] | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)
    settings: dict[str, Any] | None = None
    rerender: bool = False
    rescore: bool = False


class SecretUpdate(BaseModel):
    name: str
    value: str = Field(max_length=400)


class PostCreate(BaseModel):
    clip_id: str
    mode: Literal["now", "schedule"] = "now"
    publish_at: str | None = None
    privacy: Literal["private", "unlisted", "public"] | None = None
    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4900)
    kind: Literal["short", "video"] | None = None  # None: whatever the clip's shape is


def fail(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def queue_job(kind: str, *, url: str | None = None, options: dict | None = None,
              payload: dict | None = None, title: str | None = None) -> dict[str, Any]:
    job = {"id": db.new_id(), "kind": kind, "url": url, "status": "queued", "step": "Queued",
           "progress": 0, "title": title, "options": options or {}, "payload": payload or {},
           "created_at": time.time(), "updated_at": time.time()}
    db.insert("jobs", job)
    ensure_worker()
    return job


def clip_out(clip: dict[str, Any]) -> dict[str, Any]:
    opts = clip.pop("options", None) or {}
    settings = opts.get("settings", {})
    clip["format"] = settings.get("format", {})
    clip["captions"] = settings.get("captions", {})
    # Files are served through the API, which sends the browser to a temporary
    # signed link when the file is held in remote storage.
    for key in ("file", "thumb"):
        clip[f"{key}_url"] = (f"/api/clips/{clip['id']}/{key}?v={int(clip.get('created_at') or 0)}"
                              if clip.get(key) else None)
    clip.pop("file", None)  # internal storage references are never sent to the browser
    clip.pop("thumb", None)
    clip["duration"] = round((clip.get("end") or 0) - (clip.get("start") or 0), 2)
    return clip


# ---- accounts --------------------------------------------------------------
def _set_session(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/",
    )


@app.get("/api/auth/google/status")
def google_auth_status() -> dict[str, bool]:
    return {"configured": config.google_login_oauth_client() is not None}


@app.get("/api/auth/google")
def google_auth_start(request: Request):
    oauth_config = config.google_login_oauth_client()
    if oauth_config is None:
        query = urlencode({"error": "Google sign-in is not configured yet."})
        return RedirectResponse(f"/login/?{query}", status_code=303)
    redirect_uri = str(request.url_for("google_auth_callback"))
    try:
        url = auth.google_authorization_url(oauth_config, redirect_uri)
    except Exception:
        query = urlencode({"error": "Google sign-in could not be started. Check Google OAuth in backend/.env."})
        return RedirectResponse(f"/login/?{query}", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/api/auth/google/callback")
def google_auth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    oauth_config = config.google_login_oauth_client()
    if error:
        message = "Google sign-in was cancelled." if error == "access_denied" else "Google sign-in did not finish."
        return RedirectResponse(f"/login/?{urlencode({'error': message})}", status_code=303)
    if oauth_config is None or not state or not code:
        return RedirectResponse(
            f"/login/?{urlencode({'error': 'Google sign-in returned an incomplete response.'})}",
            status_code=303,
        )
    try:
        claims = auth.google_claims(oauth_config, state, code)
        user = auth.upsert_google_user(claims)
    except auth.AuthError as exc:
        return RedirectResponse(f"/login/?{urlencode({'error': str(exc)})}", status_code=303)
    response = RedirectResponse("/dashboard/", status_code=303)
    _set_session(response, auth.create_session(user["id"]), request)
    return response


@app.post("/api/auth/logout", status_code=204)
def logout_account(request: Request, response: Response) -> None:
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="lax")


@app.get("/api/auth/me")
def current_account(request: Request) -> dict[str, Any]:
    user = request.state.user
    return {"user": user, "stats": auth.stats() if user["is_admin"] else None}


@app.get("/api/users")
def users(request: Request) -> dict[str, Any]:
    if not request.state.user["is_admin"]:
        raise fail(403, "Only the administrator can view users.")
    return {"stats": auth.stats(), "users": auth.list_users()}


# ---- system ----------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.api_route("/api/ping", methods=["GET", "HEAD"])
async def ping() -> dict[str, bool]:
    """For an external scheduler (cron) that keeps a hosted instance awake. Public, does no
    work: no database, no worker check, nothing that could be slow or leak state."""
    return {"ok": True}


@app.get("/api/system")
def system() -> dict[str, Any]:
    settings = db.get_settings()
    provider = settings["ai"]["provider"]
    secret_name = config.PROVIDER_SECRET.get(provider)
    return {
        "worker_alive": worker.is_alive(),
        "provider": provider,
        "model": settings["ai"]["models"].get(provider),
        "ai_ready": True if secret_name is None else bool(config.secret(secret_name)),
        "youtube": youtube.status(),
    }


@app.post("/api/system/start-worker")
def start_worker() -> dict[str, bool]:
    return {"started": ensure_worker()}


@app.post("/api/ai/test")
def ai_test() -> dict[str, Any]:
    try:
        return llm.ping()
    except llm.LLMError as exc:
        raise fail(502, str(exc)) from None


# ---- jobs ------------------------------------------------------------------
@app.get("/api/jobs")
def list_jobs(kind: str | None = None) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT j.*, (SELECT COUNT(*) FROM clips c WHERE c.job_id = j.id AND c.status = 'ready') "
        "AS clip_count FROM jobs j WHERE j.kind IN ('auto','video','live') "
        "ORDER BY j.created_at DESC LIMIT 200")
    newest_more: dict[str, dict[str, Any]] = {}
    for item in db.query("SELECT id, status, step, progress, error, payload, created_at FROM jobs "
                         "WHERE kind = 'more' ORDER BY created_at"):
        source = (item["payload"] or {}).get("source_job_id")
        if source:
            newest_more[source] = item  # later rows replace earlier ones
    for row in rows:
        row.pop("options", None)
        item = newest_more.get(row["id"])
        if item and (item["status"] in ACTIVE or (item["status"] == "failed" and time.time() - item["created_at"] < 3600)):
            row["more"] = {"id": item["id"], "status": item["status"], "step": item["step"],
                           "progress": item["progress"], "error": item["error"]}
    return [r for r in rows if not kind or r["kind"] == kind]


@app.post("/api/jobs", status_code=201)
def create_job(body: JobCreate) -> dict[str, Any]:
    url = body.url.strip()
    if not re.match(r"^https?://[^\s]+$", url):
        raise fail(422, "Paste a full link that starts with https://")
    allowed = {k: body.options[k] for k in ("clip", "format", "captions", "overlay", "live", "max_minutes")
               if k in body.options}
    options = config.deep_merge(db.get_settings(), allowed)
    clip = options["clip"]
    if clip["min_sec"] > clip["max_sec"]:
        raise fail(422, "Minimum clip length cannot be longer than the maximum.")
    job = queue_job("auto", url=url, options=options)
    job.pop("options")
    return job


class MoreClips(BaseModel):
    count: int = Field(default=3, ge=1, le=10)
    label_part: bool = False


@app.post("/api/jobs/{job_id}/more", status_code=201)
def more_clips(job_id: str, body: MoreClips) -> dict[str, Any]:
    """Find more clips in a video that has already been clipped."""
    job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job or job["kind"] != "video":
        raise fail(404, "That video does not exist.")
    if job["status"] != "done":
        raise fail(409, "Wait until this video has finished processing.")
    folder = config.JOBS_DIR / job_id
    if not (folder / "transcript.json").exists() or not (folder / "meta.json").exists():
        raise fail(409, "The working files for this video were removed, so more clips cannot be made "
                        "from it. Add the link again.")
    for running in db.query("SELECT payload FROM jobs WHERE kind = 'more' AND status IN ('queued','running','stopping')"):
        if (running["payload"] or {}).get("source_job_id") == job_id:
            raise fail(409, "More clips are already being made from this video.")
    created = queue_job("more", title=job["title"], payload={
        "source_job_id": job_id, "count": body.count, "label_part": body.label_part})
    return {"id": created["id"], "status": created["status"]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise fail(404, "That job does not exist.")
    job.pop("options", None)
    job["clips"] = [clip_out(c) for c in db.query(
        "SELECT * FROM clips WHERE job_id = ? ORDER BY COALESCE(panel_score, ai_score, 0) DESC", (job_id,))]
    return job


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict[str, str]:
    job = db.one("SELECT kind, status FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise fail(404, "That job does not exist.")
    if job["status"] not in ACTIVE:
        return {"status": job["status"]}
    # A live watch stops cleanly and keeps its clips; anything else is cancelled.
    new = "stopping" if job["kind"] == "live" and job["status"] == "running" else "cancelled"
    db.update("jobs", job_id, status=new, step="Stopping" if new == "stopping" else "Cancelled")
    return {"status": new}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, str]:
    job = db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise fail(404, "That job does not exist.")
    if job["status"] not in ("failed", "cancelled"):
        raise fail(409, "Only failed or cancelled jobs can be retried.")
    db.update("jobs", job_id, status="queued", step="Queued", error=None)
    ensure_worker()
    return {"status": "queued"}


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    job = db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise fail(404, "That job does not exist.")
    if job["status"] in ("running", "stopping"):
        raise fail(409, "Stop the job before deleting it.")
    for stored in db.query("SELECT file, thumb FROM clips WHERE job_id = ?", (job_id,)):
        storage.delete(stored["file"])
        storage.delete(stored["thumb"])
    db.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    runner.delete_job_files(job_id)


# ---- clips -----------------------------------------------------------------
@app.get("/api/clips")
def list_clips(job_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM clips"
    params: tuple = ()
    if job_id:
        sql, params = sql + " WHERE job_id = ?", (job_id,)
    return [clip_out(c) for c in db.query(sql + " ORDER BY created_at DESC LIMIT 500", params)]


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: str) -> dict[str, Any]:
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    out = clip_out(clip)
    out["posts"] = db.query("SELECT * FROM posts WHERE clip_id = ? ORDER BY created_at DESC", (clip_id,))
    return out


@app.patch("/api/clips/{clip_id}")
def patch_clip(clip_id: str, body: ClipPatch) -> dict[str, Any]:
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    values: dict[str, Any] = {
        k: v for k, v in body.model_dump(include={"title", "hook", "description", "hashtags"}).items()
        if v is not None
    }
    start = clip["start"] if body.start is None else body.start
    end = clip["end"] if body.end is None else body.end
    if end - start < 3:
        raise fail(422, "A clip must be at least 3 seconds long.")
    if end - start > 180:
        raise fail(422, "A clip can be at most 3 minutes long.")
    values.update(start=start, end=end)
    if body.settings:
        opts = clip["options"]
        allowed = {k: body.settings[k] for k in ("format", "captions", "overlay") if k in body.settings}
        opts["settings"] = config.deep_merge(opts["settings"], allowed)
        values["options"] = opts
    db.update("clips", clip_id, **values)
    if body.rerender:
        if clip["status"] == "rendering":
            raise fail(409, "This clip is already rendering.")
        db.update("clips", clip_id, status="rendering", created_at=time.time())
        queue_job("render", payload={"clip_id": clip_id, "rescore": body.rescore},
                  title=values.get("title") or clip["title"])
    return get_clip(clip_id)


@app.post("/api/clips/{clip_id}/hooks")
def suggest_clip_hooks(clip_id: str) -> dict[str, list[str]]:
    """Alternative on-screen hooks for a clip, written from what is said in it."""
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    opts = clip["options"] or {}
    spoken = ""
    transcript = config.DATA / opts.get("words", "")
    if opts.get("words") and transcript.exists():
        words = json.loads(transcript.read_text(encoding="utf-8")).get("words", [])
        spoken = " ".join(w["w"] for w in words if clip["start"] <= w["s"] <= clip["end"])
    try:
        hooks = selection.suggest_hooks(title=clip["title"] or "", spoken=spoken,
                                        trends=opts.get("trend_brief", ""))
    except llm.LLMError as exc:
        raise fail(502, str(exc)) from None
    if not hooks:
        raise fail(502, "The AI did not return usable hooks. Try again.")
    return {"hooks": hooks}


@app.delete("/api/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: str) -> None:
    clip = db.one("SELECT file, thumb FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    for key in ("file", "thumb"):
        if storage.is_remote(clip[key]):
            storage.delete(clip[key])
        elif clip[key]:
            (config.DATA / clip[key]).unlink(missing_ok=True)
    db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))


def _serve_file(clip_id: str, column: str, *, download_as: str | None = None):
    clip = db.one(f"SELECT title, {column} AS ref FROM clips WHERE id = ?", (clip_id,))
    if not clip or not clip["ref"]:
        raise fail(404, "The clip file is missing.")
    if storage.is_remote(clip["ref"]):
        try:
            link = storage.url(clip["ref"], download_name=download_as)
        except storage.StorageError as exc:
            raise fail(502, str(exc)) from None
        return RedirectResponse(link, status_code=307)
    path = config.DATA / clip["ref"]
    if not path.exists():
        raise fail(404, "The clip file is missing.")
    media_type = "video/mp4" if column == "file" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=download_as)


@app.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str):
    return _serve_file(clip_id, "file")


@app.get("/api/clips/{clip_id}/thumb")
def clip_thumb(clip_id: str):
    return _serve_file(clip_id, "thumb")


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str):
    clip = db.one("SELECT title FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    name = re.sub(r'[\\/:*?"<>|]+', "", clip["title"] or "clip").strip()[:80] or "clip"
    return _serve_file(clip_id, "file", download_as=f"{name}.mp4")


@app.post("/api/storage/migrate", status_code=202)
def storage_migrate() -> dict[str, Any]:
    if not storage.enabled():
        raise fail(409, "Storage is not set up. Add the storage keys and bucket to backend/.env.")
    running = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'migrate' AND status IN ('queued','running')")
    if not running["n"]:
        queue_job("migrate", title="Move clips to cloud storage")
    return {"clips": storage.local_clip_count()}


@app.post("/api/storage/test")
def storage_test() -> dict[str, Any]:
    try:
        return storage.check()
    except storage.StorageError as exc:
        raise fail(502, str(exc)) from None


# ---- settings and secrets --------------------------------------------------
HIDDEN_SETTINGS = ("storage",)  # server-side configuration, not part of the user's settings


def _public_settings() -> dict[str, Any]:
    return {k: v for k, v in db.get_settings().items() if k not in HIDDEN_SETTINGS}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": _public_settings(), "secrets": config.secret_status()}


@app.put("/api/settings")
def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
    allowed = {k: v for k, v in patch.items()
               if k in config.DEFAULT_SETTINGS and k not in HIDDEN_SETTINGS and isinstance(v, dict)}
    merged = config.deep_merge(db.get_settings(), allowed)
    if merged["ai"]["provider"] not in config.PROVIDER_SECRET:
        raise fail(422, "Unknown AI provider.")
    if merged["clip"]["min_sec"] > merged["clip"]["max_sec"]:
        raise fail(422, "Minimum clip length cannot be longer than the maximum.")
    if merged["clip"]["min_sec"] < 5 or merged["clip"]["max_sec"] > 180:
        raise fail(422, "Clip length must be between 5 seconds and 3 minutes.")
    db.save_settings(allowed)
    return {"settings": _public_settings(), "secrets": config.secret_status()}


@app.put("/api/secrets")
def put_secret(body: SecretUpdate) -> dict[str, Any]:
    if body.name not in config.PUBLIC_SECRETS:
        raise fail(422, "That key cannot be changed here.")
    try:
        config.set_secret(body.name, body.value)
    except ValueError as exc:
        raise fail(422, str(exc)) from None
    return config.secret_status()


# ---- trends ----------------------------------------------------------------
@app.get("/api/trends")
def get_trends() -> dict[str, Any]:
    running = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'trends' AND status IN ('queued','running')")
    failed = db.one("SELECT error FROM jobs WHERE kind = 'trends' ORDER BY created_at DESC LIMIT 1")
    return {"data": trends.current(), "refreshing": running["n"] > 0,
            "error": (failed or {}).get("error")}


@app.post("/api/trends/refresh", status_code=202)
def refresh_trends() -> dict[str, str]:
    running = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'trends' AND status IN ('queued','running')")
    if not running["n"]:
        queue_job("trends", title="Trend refresh")
    return {"status": "queued"}


# ---- YouTube ---------------------------------------------------------------
@app.get("/api/youtube")
def youtube_status() -> dict[str, Any]:
    return youtube.status()


@app.post("/api/youtube/connect", status_code=202)
def youtube_connect() -> dict[str, Any]:
    try:
        youtube.connect_in_background()
    except youtube.YouTubeError as exc:
        raise fail(409, str(exc)) from None
    return {**youtube.status(), "connecting": True}


@app.post("/api/youtube/disconnect")
def youtube_disconnect() -> dict[str, Any]:
    youtube.disconnect()
    return youtube.status()


# ---- posts -----------------------------------------------------------------
@app.get("/api/posts")
def list_posts() -> list[dict[str, Any]]:
    return db.query(
        "SELECT p.*, c.thumb AS thumb, c.panel_score AS panel_score FROM posts p "
        "LEFT JOIN clips c ON c.id = p.clip_id ORDER BY p.created_at DESC LIMIT 300")


SHORT_MAX_SEC = 180


def clip_kind(clip: dict[str, Any]) -> str:
    """What YouTube will make of this file: a vertical or square video of up to
    3 minutes is a Short; anything else is a regular video."""
    width, height = clip.get("width"), clip.get("height")
    seconds = (clip.get("end") or 0) - (clip.get("start") or 0)
    if width and height and height >= width and seconds <= SHORT_MAX_SEC:
        return "short"
    return "video" if width and height else "short"


def _check_kind(clip: dict[str, Any], wanted: str | None) -> str:
    actual = clip_kind(clip)
    if wanted is None or wanted == actual:
        return actual
    if wanted == "short":
        raise fail(409, "YouTube only makes a Short from a vertical or square clip of up to 3 minutes. "
                        "Render this clip as 9:16 and keep it within 3 minutes, or post it as a video.")
    raise fail(409, "YouTube turns a vertical clip of up to 3 minutes into a Short. "
                    "Render this clip as 16:9 to post it as a regular video, or post it as a Short.")


def _with_kind_tag(description: str, kind: str) -> str:
    """A Short carries #Shorts; a regular video must not."""
    without = re.sub(r"\s*#shorts\b", "", description, flags=re.IGNORECASE).rstrip()
    return f"{without}\n\n#Shorts" if kind == "short" else without


@app.post("/api/posts", status_code=201)
def create_post(body: PostCreate) -> dict[str, Any]:
    clip = db.one("SELECT * FROM clips WHERE id = ?", (body.clip_id,))
    if not clip:
        raise fail(404, "That clip does not exist.")
    if clip["status"] != "ready":
        raise fail(409, "Wait until the clip has finished rendering.")
    if not youtube.status()["connected"]:
        raise fail(409, "Connect your YouTube channel in Settings before posting.")
    kind = _check_kind(clip, body.kind)
    publish_at = None
    if body.mode == "schedule":
        if not body.publish_at:
            raise fail(422, "Pick a date and time to schedule the post.")
        try:
            when = datetime.fromisoformat(body.publish_at.replace("Z", "+00:00"))
        except ValueError:
            raise fail(422, "The schedule time is not a valid date.") from None
        if when.tzinfo is None:
            when = when.astimezone()
        if when <= datetime.now(timezone.utc):
            raise fail(422, "The schedule time must be in the future.")
        publish_at = when.astimezone(timezone.utc).isoformat()
    post = {
        "id": db.new_id(), "clip_id": clip["id"], "status": "uploading",
        "title": (body.title or clip["title"] or "Clip")[:100],
        "description": _with_kind_tag(
            body.description if body.description is not None else clip["description"] or "", kind),
        "kind": kind,
        "privacy": body.privacy or db.get_settings()["posting"]["privacy"],
        "publish_at": publish_at, "created_at": time.time(), "updated_at": time.time(),
    }
    db.insert("posts", post)
    queue_job("upload", payload={"post_id": post["id"]}, title=post["title"])
    return post


@app.delete("/api/posts/{post_id}", status_code=204)
def delete_post(post_id: str) -> None:
    """Removes the record here. It does not delete the video from YouTube."""
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))


# ---- static: clip files and the built dashboard ----------------------------
config.ensure_dirs()
app.mount("/media", StaticFiles(directory=config.JOBS_DIR), name="media")
if (config.BACKEND / "assets" / "fonts").is_dir():  # caption fonts, for the Settings preview
    app.mount("/fonts", StaticFiles(directory=config.BACKEND / "assets" / "fonts"), name="fonts")
if config.FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="dashboard")
