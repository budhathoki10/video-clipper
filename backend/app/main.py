"""Dashboard API. It only reads and writes the database and queues jobs;
all heavy work happens in the separate worker process (app.worker).

Everyone who signs in is an ordinary user with a private workspace: their own videos, clips, posts,
settings, AI keys and YouTube channel. There is no administrator and nobody can see anyone else's work.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, db, keys, llm, runner, storage, worker, youtube
from .pipeline import media, selection, trends

ACTIVE = ("queued", "running", "stopping")
MAX_ACTIVE_JOBS = 3  # videos one person may have in progress at once; there is no limit on their length


def ensure_worker() -> bool:
    """Start the background worker as a detached process if none is running."""
    if os.environ.get("CLIPPER_NO_WORKER") == "1" or worker.is_alive():
        return False
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.DETACHED_PROCESS)
    config.DATA.mkdir(parents=True, exist_ok=True)
    # Hosted: the worker writes to the same place as the server, so its messages appear on the host's
    # log page. On one computer it keeps its own log file.
    log = None if config.public_url() else open(config.DATA / "worker.log", "a", encoding="utf-8")
    subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=config.BACKEND, stdout=log,
                     stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return True


WORKER_CHECK_SECONDS = 15


async def keep_worker_running() -> None:
    """The worker is a separate process. If it stops (crash, being killed, a restart
    race), start it again so queued jobs never wait for a worker that is gone."""
    last_purge = 0.0
    while True:
        await asyncio.sleep(WORKER_CHECK_SECONDS)
        try:
            await asyncio.to_thread(ensure_worker)
            if time.time() - last_purge > 3600:  # once an hour: drop expired sessions and sign-in attempts
                last_purge = time.time()
                await asyncio.to_thread(auth.purge_expired)
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


app = FastAPI(title="Clipper", lifespan=lifespan, docs_url=None if config.public_url() else "/api/docs",
              openapi_url=None if config.public_url() else "/api/openapi.json")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

PUBLIC_API = {
    "/api/auth/google", "/api/auth/google/callback", "/api/auth/google/status", "/api/auth/logout",
    "/api/health", "/api/ping",
}


@app.middleware("http")
async def require_account(request: Request, call_next):
    """The dashboard is static, so the API is the security boundary: everyone must sign in."""
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


# ---- whose work is whose -----------------------------------------------------
LOCAL_USER = "local"  # who everything belongs to when sign-in is switched off (local development)


def _uid(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user["id"] if user else LOCAL_USER


def _mine(request: Request) -> dict[str, Any]:
    """Query filter that limits a collection to the signed-in person's own documents. Another
    person's work looks exactly like work that does not exist. With sign-in switched off (local
    development only) there is one implicit user and nothing is filtered."""
    return {"user_id": request.state.user["id"]} if getattr(request.state, "user", None) else {}


def _job_for(request: Request, job_id: str) -> dict[str, Any]:
    job = db.find_one("jobs", {"id": job_id, **_mine(request)})
    if not job:
        raise fail(404, "That job does not exist.")
    return job


def _clip_for(request: Request, clip_id: str) -> dict[str, Any]:
    clip = db.find_one("clips", {"id": clip_id, **_mine(request)})
    if not clip:
        raise fail(404, "That clip does not exist.")
    return clip


def _check_quota(request: Request) -> None:
    busy = db.count("jobs", {**_mine(request), "kind": {"$in": ["auto", "video", "more", "live"]},
                             "status": {"$in": list(ACTIVE)}})
    if busy >= MAX_ACTIVE_JOBS:
        raise fail(429, f"You already have {MAX_ACTIVE_JOBS} videos in progress. "
                        "Wait for one to finish, then add the next.")


def queue_job(kind: str, *, user_id: str, url: str | None = None, options: dict | None = None,
              payload: dict | None = None, title: str | None = None,
              job_id: str | None = None) -> dict[str, Any]:
    # A link job on a server whose worker does not take links waits for a home computer's worker.
    step = worker.WAITING_FOR_HOME if url and kind in ("auto", "video", "live") and worker.ACCEPTS == "files" else "Queued"
    job = {"id": job_id or db.new_id(), "kind": kind, "url": url, "status": "queued", "step": step,
           "progress": 0, "title": title, "options": options or {}, "payload": payload or {},
           "created_at": time.time(), "updated_at": time.time(), "user_id": user_id}
    db.insert("jobs", job)
    ensure_worker()
    return job


def _job_out(job: dict[str, Any]) -> dict[str, Any]:
    for internal in ("options", "files", "user_id", "_id"):
        job.pop(internal, None)  # settings snapshots and storage references stay on the server
    return job


def _clip_media_url(clip: dict[str, Any], key: str) -> str | None:
    return (f"/api/clips/{clip['id']}/{key}?v={int(clip.get('created_at') or 0)}"
            if clip.get(key) else None)


def _clip_duration(clip: dict[str, Any]) -> float:
    return round((clip.get("end") or 0) - (clip.get("start") or 0), 2)


def clip_out(clip: dict[str, Any]) -> dict[str, Any]:
    opts = clip.pop("options", None) or {}
    settings = opts.get("settings", {})
    clip["format"] = settings.get("format", {})
    clip["captions"] = settings.get("captions", {})
    # Files are served through the API, which sends the browser to a temporary
    # signed link when the file is held in remote storage.
    for key in ("file", "thumb"):
        clip[f"{key}_url"] = _clip_media_url(clip, key)
    clip.pop("file", None)  # internal storage references are never sent to the browser
    clip.pop("thumb", None)
    clip.pop("user_id", None)
    clip["duration"] = _clip_duration(clip)
    return clip


PageNumber = Annotated[int | None, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=50)]
ListSearch = Annotated[str, Query(max_length=200)]
RECENT_FIRST = [("created_at", -1), ("_id", -1)]


def _page_info(total: int, page: int, size: int) -> tuple[dict[str, int], int]:
    page = min(page, max(1, (total + size - 1) // size))
    return {"page": page, "page_size": size, "total": total}, (page - 1) * size


def _find_page(collection: str, filt: dict[str, Any], page: int, size: int, *,
               sort: list[tuple[str, int]] = RECENT_FIRST,
               fields: list[str] | None = None, rank: dict[str, Any] | None = None) -> dict[str, Any]:
    def read(offset: int) -> list[dict[str, Any]]:
        if rank is None:
            return db.find(collection, filt, sort=sort, limit=size, skip=offset, fields=fields)
        pipeline = [{"$match": filt}]
        if fields:
            pipeline.append({"$project": {**dict.fromkeys(fields, 1), "rank": rank}})
        else:
            pipeline.append({"$addFields": {"rank": rank}})
        pipeline.extend([{"$sort": dict(sort)}, {"$skip": offset}, {"$limit": size}])
        items = list(db.coll(collection).aggregate(pipeline))
        for item in items:
            item.pop("_id", None)
            item.pop("rank", None)
        return items

    info, offset = _page_info(db.count(collection, filt), page, size)
    items = read(offset)
    # A deletion between the count and the read can invalidate the last page.
    if offset and not items:
        info, offset = _page_info(db.count(collection, filt), page, size)
        items = read(offset)
    return {**info, "items": items}


# ---- accounts --------------------------------------------------------------
def _base_url(request: Request) -> str:
    """The address people use. Behind a hosting proxy the request's own address is internal, so
    PUBLIC_URL wins when it is set."""
    return config.public_url() or str(request.base_url).rstrip("/")


def _set_session(response: Response, token: str, request: Request) -> None:
    secure = request.url.scheme == "https" or config.public_url().startswith("https://")
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, secure=secure, samesite="lax", path="/",
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
    redirect_uri = f"{_base_url(request)}/api/auth/google/callback"
    try:
        url = auth.google_authorization_url(oauth_config, redirect_uri)
    except Exception:
        query = urlencode({"error": "Google sign-in could not be started. Try again in a moment."})
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
    return {"user": getattr(request.state, "user", None) or
            {"id": LOCAL_USER, "email": "", "name": "Local", "avatar_url": ""}}


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
def system(request: Request) -> dict[str, Any]:
    uid = _uid(request)
    settings = db.settings_for(uid)
    provider = settings["ai"]["provider"]
    secret_name = config.PROVIDER_SECRET.get(provider)
    try:
        ready = True if secret_name is None else bool(keys.effective(uid, secret_name))
    except keys.KeysError:
        ready = False
    return {
        "worker_alive": worker.is_alive(),
        "provider": provider,
        "model": settings["ai"]["models"].get(provider),
        "ai_ready": ready,
        "youtube": youtube.status(uid),
    }


@app.post("/api/system/start-worker")
def start_worker() -> dict[str, bool]:
    return {"started": ensure_worker()}


@app.post("/api/ai/test")
def ai_test(request: Request) -> dict[str, Any]:
    try:
        with llm.as_user(_uid(request)):
            return llm.ping()
    except llm.LLMError as exc:
        raise fail(502, str(exc)) from None


# ---- jobs ------------------------------------------------------------------
@app.get("/api/jobs")
def list_jobs(request: Request, kind: str | None = None, page: PageNumber = None,
              page_size: PageSize = 8, q: ListSearch = "", summary: bool = False) -> Any:
    kinds = ["auto", "video", "live"]
    filt = {**_mine(request), "kind": {"$in": [k for k in kinds if not kind or k == kind]}}
    if q.strip():
        pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
        filt["$or"] = [{field: pattern} for field in ("title", "channel", "url")]
    fields = ["id", "title", "url"] if summary else None
    result = _find_page("jobs", filt, page, page_size, fields=fields) if page is not None else None
    rows = result["items"] if result is not None else db.find("jobs", filt, sort=RECENT_FIRST, limit=200, fields=fields)
    if summary or not rows:
        return result if result is not None else rows

    ids = {"$in": [row["id"] for row in rows]}
    ready = {c["_id"]: c["count"] for c in db.coll("clips").aggregate([
        {"$match": {**_mine(request), "job_id": ids, "status": "ready"}},
        {"$group": {"_id": "$job_id", "count": {"$sum": 1}}},
    ])}
    newest_more = {c["_id"]: c["job"] for c in db.coll("jobs").aggregate([
        {"$match": {**_mine(request), "kind": "more", "payload.source_job_id": ids}},
        {"$sort": dict(RECENT_FIRST)},
        {"$group": {"_id": "$payload.source_job_id", "job": {"$first": "$$ROOT"}}},
    ])}
    for row in rows:
        row["clip_count"] = ready.get(row["id"], 0)
        item = newest_more.get(row["id"])
        if item and (item["status"] in ACTIVE or (item["status"] == "failed" and time.time() - item["created_at"] < 3600)):
            row["more"] = {"id": item["id"], "status": item["status"], "step": item["step"],
                           "progress": item["progress"], "error": item["error"]}
        _job_out(row)
    return result if result is not None else rows


@app.post("/api/jobs", status_code=201)
def create_job(body: JobCreate, request: Request) -> dict[str, Any]:
    url = body.url.strip()
    if not re.match(r"^https?://[^\s]+$", url):
        raise fail(422, "Paste a full link that starts with https://")
    options = _job_options(request, body.options)
    _require_storage()
    _check_quota(request)
    job = queue_job("auto", url=url, options=options, user_id=_uid(request))
    return _job_out(job)


def _job_options(request: Request, sent: dict[str, Any]) -> dict[str, Any]:
    """This person's settings with the options they chose for this video on top."""
    allowed = {k: config.prune_to(config.DEFAULT_SETTINGS[k], sent[k])
               for k in ("clip", "format", "captions", "overlay", "live") if isinstance(sent.get(k), dict)}
    if isinstance(sent.get("max_minutes"), (int, float)) and sent["max_minutes"] > 0:
        allowed["max_minutes"] = sent["max_minutes"]
    options = config.deep_merge(db.settings_for(_uid(request)), allowed)
    clip = options["clip"]
    if clip["min_sec"] > clip["max_sec"]:
        raise fail(422, "Minimum clip length cannot be longer than the maximum.")
    return options


def _require_storage() -> None:
    try:
        storage.require_when_hosted()
    except storage.StorageError as exc:
        raise fail(503, str(exc)) from None


# ---- upload a video file instead of pasting a link ----------------------------
# YouTube often refuses to give a hosting server its videos, so a person can upload the file. It
# arrives in parts (a few MB each, so no single request is large or slow), is checked, and then
# becomes an ordinary job that skips the download.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024
MAX_OPEN_UPLOADS = 2
UPLOAD_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


class UploadStart(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


class UploadFinish(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


def _upload_file(upload: dict[str, Any]) -> Path:
    return config.DATA / "uploads" / upload["id"] / f"source{Path(upload['name']).suffix.lower()}"


def _upload_for(request: Request, upload_id: str) -> dict[str, Any]:
    upload = db.find_one("uploads", {"id": upload_id, **_mine(request)})
    if not upload:
        raise fail(404, "That upload does not exist.")
    return upload


@app.post("/api/uploads", status_code=201)
def start_upload(body: UploadStart, request: Request) -> dict[str, Any]:
    name = Path(body.name).name
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        raise fail(422, "Upload a video file: mp4, mov, mkv, webm, m4v or avi.")
    _require_storage()
    _check_quota(request)
    if db.count("uploads", {**_mine(request), "status": "uploading"}) >= MAX_OPEN_UPLOADS:
        raise fail(429, "Two uploads are already in progress. Wait for one to finish.")
    config.ensure_dirs()
    if shutil.disk_usage(config.DATA).free < body.size + runner.MIN_FREE_BYTES:
        raise fail(507, "The server does not have enough free space for this file right now. "
                        "Try a smaller file, or try again later.")
    upload = db.insert("uploads", {"user_id": _uid(request), "name": name, "size": body.size,
                                   "received": 0, "status": "uploading", "created_at": time.time()})
    target = _upload_file(upload)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    return {"id": upload["id"], "chunk_size": UPLOAD_CHUNK_BYTES}


@app.put("/api/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, offset: int, request: Request) -> dict[str, int]:
    upload = await asyncio.to_thread(_upload_for, request, upload_id)
    if upload["status"] != "uploading":
        raise fail(409, "This upload is already finished.")
    data = await request.body()
    if not data or len(data) > MAX_CHUNK_BYTES:
        raise fail(422, "That part is empty or too large.")
    end = offset + len(data)
    if end > upload["size"]:
        raise fail(422, "More data arrived than the size of the file.")
    target = _upload_file(upload)
    if offset == upload["received"] and target.exists() and target.stat().st_size == offset:
        def append() -> None:
            with open(target, "ab") as handle:
                handle.write(data)
        await asyncio.to_thread(append)
        await asyncio.to_thread(db.update, "uploads", upload_id, received=end)
        return {"received": end}
    if end == upload["received"]:  # the same part again after a dropped reply: already stored
        return {"received": upload["received"]}
    raise fail(409, "A part arrived out of order. Start the upload again.")


@app.post("/api/uploads/{upload_id}/complete", status_code=201)
def finish_upload(upload_id: str, body: UploadFinish, request: Request) -> dict[str, Any]:
    upload = _upload_for(request, upload_id)
    if upload["status"] != "uploading":
        raise fail(409, "This upload is already finished.")
    source = _upload_file(upload)
    if upload["received"] != upload["size"] or not source.exists() or source.stat().st_size != upload["size"]:
        raise fail(409, "The upload is incomplete. Try again.")
    try:
        info = media.probe(source)
    except Exception:
        info = {}
    if not info.get("width") or not info.get("duration"):
        shutil.rmtree(source.parent, ignore_errors=True)
        db.delete("uploads", {"id": upload_id})
        raise fail(422, "That file is not a video the server can read.")
    options = _job_options(request, body.options)
    _check_quota(request)

    # Put the file where a downloaded video would be, with the details a link would have given,
    # before the job exists, so the worker can never see it half ready.
    job_id = db.new_id()
    folder = runner.job_dir(job_id)
    shutil.move(str(source), str(folder / f"source{source.suffix}"))
    title = Path(upload["name"]).stem[:100] or "Uploaded video"
    runner._save(folder / "meta.json", {
        "id": job_id, "title": title, "channel": "", "channel_handle": "", "duration": info["duration"],
        "live_status": "not_live", "thumbnail": "", "webpage_url": "", "heatmap": [], "category": "", "tags": []})
    (folder / "download.done").write_text("ok")
    job = queue_job("video", title=title, options=options, user_id=_uid(request), job_id=job_id,
                    payload={"uploaded": True})
    db.update("uploads", upload_id, status="done")
    shutil.rmtree(source.parent, ignore_errors=True)
    return _job_out(job)


class MoreClips(BaseModel):
    count: int = Field(default=3, ge=1, le=10)
    label_part: bool = False


@app.post("/api/jobs/{job_id}/more", status_code=201)
def more_clips(job_id: str, body: MoreClips, request: Request) -> dict[str, Any]:
    """Find more clips in a video that has already been clipped."""
    job = _job_for(request, job_id)
    if job["kind"] != "video":
        raise fail(404, "That video does not exist.")
    if job["status"] != "done":
        raise fail(409, "Wait until this video has finished processing.")
    kept = (job.get("files") or {}).get("artifacts") or (runner.job_dir(job_id) / "transcript.json").exists()
    if not kept:
        raise fail(409, "The working files for this video were removed, so more clips cannot be made "
                        "from it. Add the link again.")
    if db.count("jobs", {"kind": "more", "status": {"$in": list(ACTIVE)}, "payload.source_job_id": job_id}):
        raise fail(409, "More clips are already being made from this video.")
    _check_quota(request)
    created = queue_job("more", title=job["title"], user_id=_uid(request), payload={
        "source_job_id": job_id, "count": body.count, "label_part": body.label_part})
    return {"id": created["id"], "status": created["status"]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _job_out(_job_for(request, job_id))
    clips = db.find("clips", {"job_id": job_id, **_mine(request)})
    clips.sort(key=lambda c: c.get("panel_score") or c.get("ai_score") or 0, reverse=True)
    job["clips"] = [clip_out(c) for c in clips]
    return job


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request) -> dict[str, str]:
    job = _job_for(request, job_id)
    if job["status"] not in ACTIVE:
        return {"status": job["status"]}
    # A live watch stops cleanly and keeps its clips; anything else is cancelled.
    new = "stopping" if job["kind"] == "live" and job["status"] == "running" else "cancelled"
    db.update("jobs", job_id, status=new, step="Stopping" if new == "stopping" else "Cancelled")
    return {"status": new}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict[str, str]:
    job = _job_for(request, job_id)
    if job["status"] not in ("failed", "cancelled"):
        raise fail(409, "Only failed or cancelled jobs can be retried.")
    _check_quota(request)
    waits = worker.ACCEPTS == "files" and job.get("url") and job["kind"] in ("auto", "video", "live")
    db.update("jobs", job_id, status="queued", step=worker.WAITING_FOR_HOME if waits else "Queued", error=None)
    ensure_worker()
    return {"status": "queued"}


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request) -> None:
    job = _job_for(request, job_id)
    if job["status"] in ("running", "stopping"):
        raise fail(409, "Stop the job before deleting it.")
    for stored in db.find("clips", {"job_id": job_id}, fields=["file", "thumb"]):
        storage.delete(stored.get("file"))
        storage.delete(stored.get("thumb"))
    db.delete("clips", {"job_id": job_id})
    db.delete("jobs", {"id": job_id})
    runner.delete_job_files(job_id, job.get("files"))


# ---- clips -----------------------------------------------------------------
CLIP_CARD_FIELDS = ["id", "job_id", "status", "title", "start", "end", "ai_score", "panel_score",
                    "thumb", "source_channel", "created_at"]
CLIP_SCORE = {"$ifNull": ["$panel_score", {"$ifNull": ["$ai_score", -1]}]}
CLIP_DURATION = {"$subtract": [{"$ifNull": ["$end", 0]}, {"$ifNull": ["$start", 0]}]}


@app.get("/api/clips")
def list_clips(request: Request, job_id: str | None = None, page: PageNumber = None, page_size: PageSize = 12,
               q: ListSearch = "", min_score: Annotated[int, Query(ge=0, le=100)] = 0,
               sort: Literal["score", "newest", "shortest", "longest"] = "newest", ready: bool = False) -> Any:
    filt = {**_mine(request), **({"job_id": job_id} if job_id else {})}
    if ready:
        filt["status"] = "ready"
    if page is None:
        return [clip_out(c) for c in db.find("clips", filt, sort=RECENT_FIRST, limit=500)]

    base = filt.copy()
    q = q.strip()
    conditions = []
    if q:
        conditions.append({"$regexMatch": {
            "input": {"$concat": [{"$ifNull": ["$title", ""]}, " ",
                                  {"$ifNull": ["$source_channel", ""]}, " ",
                                  {"$ifNull": ["$source_title", ""]}]},
            "regex": re.escape(q), "options": "i",
        }})
    if min_score:
        conditions.append({"$gte": [CLIP_SCORE, min_score]})
    if conditions:
        filt["$expr"] = {"$and": conditions}

    ranks = {"score": CLIP_SCORE, "shortest": CLIP_DURATION, "longest": CLIP_DURATION}
    order = RECENT_FIRST if sort == "newest" else [("rank", 1 if sort == "shortest" else -1), *RECENT_FIRST]
    result = _find_page("clips", filt, page, page_size, fields=CLIP_CARD_FIELDS, sort=order, rank=ranks.get(sort))
    for clip in result["items"]:
        clip["thumb_url"] = _clip_media_url(clip, "thumb")
        clip["duration"] = _clip_duration(clip)
        for field in ("thumb", "start", "end"):
            clip.pop(field, None)
    result["unfiltered_total"] = db.count("clips", base) if conditions else result["total"]
    if job_id:
        source = db.find_one("jobs", {"id": job_id, **_mine(request)})
        result["source_title"] = (source or {}).get("title")
    return result


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: str, request: Request, include_posts: bool = True) -> dict[str, Any]:
    out = clip_out(_clip_for(request, clip_id))
    if include_posts:
        out["posts"] = [_post_out(p) for p in db.find("posts", {"clip_id": clip_id, **_mine(request)},
                                                      sort=RECENT_FIRST)]
    return out


@app.patch("/api/clips/{clip_id}")
def patch_clip(clip_id: str, body: ClipPatch, request: Request) -> dict[str, Any]:
    clip = _clip_for(request, clip_id)
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
        allowed = {k: config.prune_to(config.DEFAULT_SETTINGS[k], body.settings[k])
                   for k in ("format", "captions", "overlay") if isinstance(body.settings.get(k), dict)}
        opts["settings"] = config.deep_merge(opts["settings"], allowed)
        values["options"] = opts
    db.update("clips", clip_id, **values)
    if body.rerender:
        if clip["status"] == "rendering":
            raise fail(409, "This clip is already rendering.")
        db.update("clips", clip_id, status="rendering", created_at=time.time())
        queue_job("render", payload={"clip_id": clip_id, "rescore": body.rescore},
                  title=values.get("title") or clip["title"], user_id=_uid(request))
    return get_clip(clip_id, request)


@app.post("/api/clips/{clip_id}/hooks")
def suggest_clip_hooks(clip_id: str, request: Request) -> dict[str, list[str]]:
    """Alternative on-screen hooks for a clip, written from what is said in it."""
    clip = _clip_for(request, clip_id)
    opts = clip["options"] or {}
    spoken = ""
    try:
        runner.restore_job_files(clip["job_id"])
    except storage.StorageError:
        pass  # without the transcript the hooks are written from the title alone
    transcript = config.DATA / opts.get("words", "")
    if opts.get("words") and transcript.exists():
        words = json.loads(transcript.read_text(encoding="utf-8")).get("words", [])
        spoken = " ".join(w["w"] for w in words if clip["start"] <= w["s"] <= clip["end"])
    try:
        with llm.as_user(_uid(request)):
            hooks = selection.suggest_hooks(title=clip["title"] or "", spoken=spoken,
                                            trends=opts.get("trend_brief", ""))
    except llm.LLMError as exc:
        raise fail(502, str(exc)) from None
    if not hooks:
        raise fail(502, "The AI did not return usable hooks. Try again.")
    return {"hooks": hooks}


@app.delete("/api/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: str, request: Request) -> None:
    clip = _clip_for(request, clip_id)
    for key in ("file", "thumb"):
        if storage.is_remote(clip.get(key)):
            storage.delete(clip[key])
        elif clip.get(key):
            (config.DATA / clip[key]).unlink(missing_ok=True)
    db.delete("clips", {"id": clip_id})


def _serve_file(request: Request, clip_id: str, column: str, *, download_as: str | None = None):
    clip = _clip_for(request, clip_id)
    ref = clip.get(column)
    if not ref:
        raise fail(404, "The clip file is missing.")
    if storage.is_remote(ref):
        try:
            link = storage.url(ref, download_name=download_as)
        except storage.StorageError as exc:
            raise fail(502, str(exc)) from None
        return RedirectResponse(link, status_code=307)
    path = config.DATA / ref
    if not path.exists():
        raise fail(404, "The clip file is missing.")
    media_type = "video/mp4" if column == "file" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=download_as)


@app.get("/api/clips/{clip_id}/file")
def clip_file(clip_id: str, request: Request):
    return _serve_file(request, clip_id, "file")


@app.get("/api/clips/{clip_id}/thumb")
def clip_thumb(clip_id: str, request: Request):
    return _serve_file(request, clip_id, "thumb")


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str, request: Request):
    clip = _clip_for(request, clip_id)
    name = re.sub(r'[\\/:*?"<>|]+', "", clip["title"] or "clip").strip()[:80] or "clip"
    return _serve_file(request, clip_id, "file", download_as=f"{name}.mp4")


# ---- settings and keys -----------------------------------------------------
HIDDEN_SETTINGS = ("storage",)  # server-side configuration, not part of anyone's settings


def _public_settings(request: Request) -> dict[str, Any]:
    return {k: v for k, v in db.settings_for(_uid(request)).items() if k not in HIDDEN_SETTINGS}


def _key_status(request: Request) -> dict[str, Any]:
    try:
        return keys.status(_uid(request))
    except keys.KeysError:
        return {name: {"set": False, "hint": "", "shared": False} for name in config.PUBLIC_SECRETS}


@app.get("/api/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return {"settings": _public_settings(request), "secrets": _key_status(request)}


@app.put("/api/settings")
def put_settings(patch: dict[str, Any], request: Request) -> dict[str, Any]:
    allowed = {k: config.prune_to(config.DEFAULT_SETTINGS[k], v)
               for k, v in patch.items() if k in db.USER_SETTINGS and isinstance(v, dict)}
    merged = config.deep_merge(db.settings_for(_uid(request)), allowed)
    if merged["ai"]["provider"] not in config.PROVIDER_SECRET:
        raise fail(422, "Unknown AI provider.")
    if merged["clip"]["min_sec"] > merged["clip"]["max_sec"]:
        raise fail(422, "Minimum clip length cannot be longer than the maximum.")
    if merged["clip"]["min_sec"] < 5 or merged["clip"]["max_sec"] > 180:
        raise fail(422, "Clip length must be between 5 seconds and 3 minutes.")
    db.save_prefs(_uid(request), allowed)
    return {"settings": _public_settings(request), "secrets": _key_status(request)}


@app.put("/api/secrets")
def put_secret(body: SecretUpdate, request: Request) -> dict[str, Any]:
    """Save (or, with an empty value, remove) this person's own API key."""
    if body.name not in config.PUBLIC_SECRETS:
        raise fail(422, "That key cannot be changed here.")
    try:
        keys.put(_uid(request), body.name, body.value)
    except keys.KeysError as exc:
        raise fail(422, str(exc)) from None
    return _key_status(request)


# ---- trends ----------------------------------------------------------------
@app.get("/api/trends")
def get_trends(page: PageNumber = None, page_size: PageSize = 10) -> dict[str, Any]:
    running = db.count("jobs", {"kind": "trends", "status": {"$in": ["queued", "running"]}})
    failed = db.find("jobs", {"kind": "trends"}, sort=[("created_at", -1)], limit=1)
    data = trends.current()
    if data is not None and page is not None:
        videos = data.get("videos") or []
        info, offset = _page_info(len(videos), page, page_size)
        lengths = sorted(v["duration"] for v in videos if v.get("duration"))
        data = {**data, "videos": {**info, "items": videos[offset:offset + page_size]},
                "most_views": videos[0].get("views") if videos else None,
                "typical_length": lengths[len(lengths) // 2] if lengths else None}
    return {"data": data, "refreshing": running > 0,
            "error": (failed[0] if failed else {}).get("error")}


@app.post("/api/trends/refresh", status_code=202)
def refresh_trends(request: Request) -> dict[str, str]:
    if not db.count("jobs", {"kind": "trends", "status": {"$in": ["queued", "running"]}}):
        queue_job("trends", title="Trend refresh", user_id=_uid(request))
    return {"status": "queued"}


# ---- YouTube (each person connects their own channel) ---------------------------
@app.get("/api/youtube")
def youtube_status(request: Request) -> dict[str, Any]:
    return youtube.status(_uid(request))


@app.post("/api/youtube/connect", status_code=202)
def youtube_connect(request: Request) -> dict[str, Any]:
    try:
        url = youtube.start_connect(_uid(request), _base_url(request))
    except youtube.YouTubeError as exc:
        raise fail(409, str(exc)) from None
    return {**youtube.status(_uid(request)), "connecting": True, "redirect_to": url}


@app.get("/api/youtube/callback")
def youtube_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Where Google sends the browser back to after a channel sign-in."""
    uid = _uid(request)
    if error or not code:
        youtube.record_error(uid, "The YouTube sign-in did not finish. Press Connect YouTube again."
                             if error and error != "access_denied" else "The YouTube sign-in was cancelled.")
    else:
        try:
            youtube.finish_connect(uid, state, code)
        except youtube.YouTubeError as exc:
            youtube.record_error(uid, str(exc))
    return RedirectResponse("/settings/#youtube", status_code=303)


@app.post("/api/youtube/disconnect")
def youtube_disconnect(request: Request) -> dict[str, Any]:
    youtube.disconnect(_uid(request))
    return youtube.status(_uid(request))


# ---- posts -----------------------------------------------------------------
def _post_out(post: dict[str, Any]) -> dict[str, Any]:
    post.pop("user_id", None)
    return post


@app.get("/api/posts")
def list_posts(request: Request, page: PageNumber = None, page_size: PageSize = 20, clip_id: str | None = None,
               status: Literal["uploading", "scheduled", "published", "failed"] | None = None,
               sort: Literal["created_at", "views", "title"] = "created_at", direction: Literal["asc", "desc"] = "desc") -> Any:
    base = {**_mine(request), **({"clip_id": clip_id} if clip_id else {})}
    filt = {**base, **({"status": status} if status else {})}
    if page is not None:
        ranks = {"views": {"$ifNull": ["$views", -1]}, "title": {"$toLower": {"$ifNull": ["$title", ""]}}}
        dir_num = 1 if direction == "asc" else -1
        order = [("created_at", dir_num), ("_id", -1)] if sort == "created_at" else [("rank", dir_num), *RECENT_FIRST]
        result = _find_page("posts", filt, page, page_size, sort=order, rank=ranks.get(sort))
        result["items"] = [_post_out(post) for post in result["items"]]
        result["unfiltered_total"] = db.count("posts", base) if status else result["total"]
        return result

    posts = db.find("posts", filt, sort=RECENT_FIRST, limit=300)
    clips = {c["id"]: c for c in db.find("clips", {**_mine(request), "id": {"$in": [p["clip_id"] for p in posts]}},
                                         fields=["id", "thumb", "panel_score", "created_at"])}
    for post in posts:
        clip = clips.get(post["clip_id"], {})
        post["thumb"], post["panel_score"] = _clip_media_url(clip, "thumb"), clip.get("panel_score")
        _post_out(post)
    return posts


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
def create_post(body: PostCreate, request: Request) -> dict[str, Any]:
    uid = _uid(request)
    clip = _clip_for(request, body.clip_id)
    if clip["status"] != "ready":
        raise fail(409, "Wait until the clip has finished rendering.")
    if not youtube.status(uid)["connected"]:
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
        "id": db.new_id(), "clip_id": clip["id"], "user_id": uid, "status": "uploading",
        "title": (body.title or clip["title"] or "Clip")[:100],
        "description": _with_kind_tag(
            body.description if body.description is not None else clip["description"] or "", kind),
        "kind": kind,
        "privacy": body.privacy or db.settings_for(uid)["posting"]["privacy"],
        "publish_at": publish_at, "created_at": time.time(), "updated_at": time.time(),
    }
    db.insert("posts", post)
    queue_job("upload", payload={"post_id": post["id"]}, title=post["title"], user_id=uid)
    return _post_out(post)


@app.delete("/api/posts/{post_id}", status_code=204)
def delete_post(post_id: str, request: Request) -> None:
    """Removes the record here. It does not delete the video from YouTube."""
    db.delete("posts", {"id": post_id, **_mine(request)})


# ---- static: caption fonts and the built dashboard --------------------------
config.ensure_dirs()
if (config.BACKEND / "assets" / "fonts").is_dir():  # caption fonts, for the Settings preview
    app.mount("/fonts", StaticFiles(directory=config.BACKEND / "assets" / "fonts"), name="fonts")
if config.FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="dashboard")
