"""End-to-end checks for the clipper.

Run from the backend folder:

    .venv\\Scripts\\python -m pytest                    # everything
    .venv\\Scripts\\python -m pytest -m "not network"   # offline checks only (about 20 s)

The full run downloads the first minutes of a real YouTube video, transcribes
it, lets the AI pick clips, renders them with captions and runs the audience
panel. Finished clips are saved to cloud storage like in the app; a copy is
downloaded to data/test-run/e2e-clips so you can watch them.

Change the video or how much of it is used:

    set CLIPPER_TEST_URL=https://youtu.be/...
    set CLIPPER_TEST_MINUTES=6
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, llm, runner, storage
from app.main import app
from app.pipeline import captions, media, render, signals

ROOT = Path(__file__).resolve().parents[2]
TEST_URL = os.environ.get("CLIPPER_TEST_URL", "https://youtu.be/q6fgVVmdpgY")
TEST_MINUTES = float(os.environ.get("CLIPPER_TEST_MINUTES", "6"))
WORDS = [{"w": w, "s": 0.5 + i * 0.4, "e": 0.8 + i * 0.4}
         for i, w in enumerate("No way he actually jumped off the roof. That is insane!".split())]


@pytest.fixture(scope="session", autouse=True)
def database():
    config.ensure_dirs()
    db.init()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


# ---- offline ----------------------------------------------------------------
def test_caption_file_highlights_each_spoken_word():
    ass = captions.build_ass(words=WORDS, width=1080, height=1920, duration=6.0,
                             captions=config.DEFAULT_SETTINGS["captions"],
                             title="He jumped off the roof", credit="Credit: @someone")
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    caption_events = [e for e in events if ",Caption," in e]
    assert len(caption_events) == len(WORDS), "one caption event per spoken word"
    assert all("\\c&H0000E6FF&" in e for e in caption_events), "active word is highlighted"
    assert any(",Title," in e for e in events) and any(",Credit," in e for e in events)
    first = re.sub(r"\{[^}]*\}", "", caption_events[0].rsplit(",,", 1)[-1])
    assert first == "NO WAY HE", first
    assert all("\\t(0,90," in e for e in caption_events), "the spoken word pops"
    assert "ROOF." not in ass and "INSANE!" in ass, "full stops dropped, exclamation marks kept"
    assert "Style: Caption,Poppins ExtraBold," in ass, "bundled font is the default"


@pytest.mark.parametrize("aspect,quality,expected", [
    ("9:16", 1080, (1080, 1920)), ("16:9", 1080, (1920, 1080)),
    ("1:1", 720, (720, 720)), ("9:16", 2160, (2160, 3840)),
])
def test_output_sizes(aspect, quality, expected):
    assert render.output_size(aspect, quality) == expected


def test_signals_find_the_loud_moment(tmp_path):
    wav = tmp_path / "tone.wav"
    rate = 16000
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for second in range(90):
            level = 20000 if 40 <= second < 46 else 900
            handle.writeframes(b"".join(
                struct.pack("<h", int(level * math.sin(2 * math.pi * 220 * n / rate)))
                for n in range(rate)))
    score = signals.analyze(wav, words=[])
    picks = signals.candidates(score, spacing=30, limit=3)
    best = max(picks, key=lambda p: p["signal"])
    assert 36 <= best["peak"] <= 48, f"loud burst at 40-46 s, found peak at {best['peak']}"


@pytest.mark.parametrize("aspect,layout,size", [
    ("9:16", "blur", (720, 1280)), ("9:16", "crop", (720, 1280)), ("16:9", "blur", (1280, 720)),
])
def test_render_makes_a_captioned_clip(tmp_path, aspect, layout, size):
    source = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    settings = config.deep_merge(config.DEFAULT_SETTINGS, {
        "format": {"aspect": aspect, "layout": layout, "quality": 720}})
    result = render.render_clip(source=source, out_dir=tmp_path / "clips", clip_id="demo",
                                start=1.0, end=7.0, words=WORDS, settings=settings,
                                title="He jumped off the roof", credit="Credit: @someone")
    info = media.probe(tmp_path / "clips" / result["file"])
    assert (info["width"], info["height"]) == size
    assert abs(info["duration"] - 6.0) < 0.3
    assert (tmp_path / "clips" / result["thumb"]).stat().st_size > 1000


def test_api_never_returns_a_key(client):
    key = config.secret("NVIDIA_API_KEY")
    assert key, "NVIDIA_API_KEY is missing from backend/.env"
    secrets = [v for v in (config.secret(n) for n in config.SECRET_NAMES) if v]
    for path in ("/api/settings", "/api/system", "/api/jobs", "/api/clips", "/api/trends",
                 "/api/youtube"):
        response = client.get(path)
        assert response.status_code == 200, path
        for value in secrets:
            assert value not in response.text, f"{path} leaked a secret key"
    status = client.get("/api/settings").json()["secrets"]["NVIDIA_API_KEY"]
    assert status == {"set": True, "hint": "", "shared": True}, "the shared key is never hinted at"


def test_no_key_in_source_files():
    keys = [k for k in (config.secret(name) for name in config.SECRET_NAMES) if k]
    assert keys, "no key found in backend/.env"
    offenders = []
    for folder in (ROOT / "backend" / "app", ROOT / "backend" / "tests", ROOT / "frontend", ROOT / "scripts"):
        for path in folder.rglob("*"):
            if not path.is_file() or any(p in path.parts for p in ("node_modules", ".next", "out", "__pycache__")):
                continue
            if path.suffix.lower() in (".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".css", ".md", ".ps1", ".html"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(key in text for key in keys):
                    offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"API key text found in: {offenders}"
    ignored = subprocess.run(["git", "check-ignore", "backend/.env"], cwd=ROOT, capture_output=True, text=True)
    assert ignored.stdout.strip() == "backend/.env", "backend/.env must be ignored by git"


def test_api_rejects_bad_input(client):
    assert client.post("/api/jobs", json={"url": "not a link"}).status_code == 422
    assert client.get("/api/jobs/does-not-exist").status_code == 404
    bad = client.put("/api/settings", json={"clip": {"min_sec": 90, "max_sec": 30}})
    assert bad.status_code == 422 and "longer" in bad.json()["detail"]
    assert client.post("/api/posts", json={"clip_id": "missing"}).status_code == 404


def test_accounts_protect_the_app(monkeypatch):
    from app import auth, youtube

    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    monkeypatch.setattr(config, "google_login_oauth_client", lambda: {"web": {"client_id": "test"}})
    monkeypatch.setattr(auth, "google_authorization_url",
                        lambda source, redirect: "https://accounts.google.test/authorize")
    db.delete("sessions", {})
    db.delete("oauth_states", {})
    db.delete("users", {})

    with TestClient(app) as owner, TestClient(app) as member:
        assert owner.get("/api/jobs").status_code == 401
        start = owner.get("/api/auth/google", follow_redirects=False)
        assert start.status_code == 303 and start.headers["location"].startswith("https://accounts.google.test")

        monkeypatch.setattr(auth, "google_claims", lambda source, state, code: {
            "sub": "google-owner", "email": "owner@example.com", "email_verified": True,
            "name": "Owner", "picture": "https://example.com/owner.jpg",
        })
        created = owner.get("/api/auth/google/callback?state=owner-state&code=owner-code",
                            follow_redirects=False)
        assert created.status_code == 303 and created.headers["location"] == "/dashboard/"
        assert "HttpOnly" in created.headers["set-cookie"]
        assert owner.get("/api/auth/me").json()["user"]["email"] == "owner@example.com"
        assert owner.get("/api/auth/me").status_code == 200

        monkeypatch.setattr(auth, "google_claims", lambda source, state, code: {
            "sub": "google-editor", "email": "editor@example.com", "email_verified": True,
            "name": "Editor", "picture": "",
        })
        second = member.get("/api/auth/google/callback?state=editor-state&code=editor-code",
                            follow_redirects=False)
        assert second.status_code == 303
        me = member.get("/api/auth/me").json()["user"]
        assert me["email"] == "editor@example.com" and "is_admin" not in me, "everyone is an ordinary user"
        assert db.count("users") == 2

        assert owner.post("/api/auth/logout").status_code == 204
        assert owner.get("/api/auth/me").status_code == 401

    db.delete("sessions", {})
    db.delete("oauth_states", {})
    db.delete("users", {})


def test_google_login_does_not_merge_youtube_permissions():
    from urllib.parse import parse_qs, urlparse

    from app import auth

    client_config = {
        "installed": {
            "client_id": "login-test.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    url = auth.google_authorization_url(
        client_config, "http://127.0.0.1:8765/api/auth/google/callback")
    query = parse_qs(urlparse(url).query)
    assert "include_granted_scopes" not in query
    assert set(query["scope"][0].split()) == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
    db.delete("oauth_states", {})


def test_job_can_be_queued_and_cancelled(client):
    created = client.post("/api/jobs", json={"url": "https://youtu.be/q6fgVVmdpgY"})
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/stop").json()["status"] == "cancelled"
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204


# ---- network ----------------------------------------------------------------
@pytest.mark.network
def test_ai_provider_answers():
    result = llm.ping()
    print(f"\n  AI: {result['provider']} / {result['model']} answered in {result['seconds']} s")
    assert result["ok"]


@pytest.mark.network
def test_full_pipeline_on_a_real_video():
    job_id = "e2e"
    for old in db.find("clips", {"job_id": job_id}):
        storage.delete(old["file"])
        storage.delete(old["thumb"])
    db.delete("clips", {"job_id": job_id})
    db.delete("jobs", {"id": job_id})
    shutil.rmtree(config.JOBS_DIR / job_id, ignore_errors=True)
    watch = config.DATA / "e2e-clips"
    shutil.rmtree(watch, ignore_errors=True)
    watch.mkdir(parents=True, exist_ok=True)
    options = config.deep_merge(config.DEFAULT_SETTINGS, {
        "clip": {"min_sec": 15, "max_sec": 45, "max_clips": 2},
        "format": {"aspect": "9:16", "layout": "blur", "quality": 720, "source_quality": 720},
        "ai": {"panel_size": 25},
    })
    options["max_minutes"] = TEST_MINUTES
    db.insert("jobs", {"id": job_id, "kind": "video", "url": TEST_URL, "status": "running",
                       "options": options, "payload": {}, "created_at": time.time(),
                       "updated_at": time.time()})
    started = time.time()
    runner.process_video(db.find_one("jobs", {"id": job_id}))
    job = db.find_one("jobs", {"id": job_id})
    clips = db.find("clips", {"job_id": job_id}, sort=[("start", 1)])
    print(f"\n  Source: {job['title']} ({job['channel']}), first {TEST_MINUTES:g} min, "
          f"{time.time() - started:.0f} s total")
    if (job["payload"] or {}).get("warning"):
        print(f"  WARNING: {job['payload']['warning']}")

    assert clips, "the pipeline produced no clips"
    assert len(clips) <= 2
    if storage.enabled():  # a processed video leaves nothing on this server
        assert not (config.JOBS_DIR / job_id).exists(), "the working folder was not cleared"
        files = job["files"]
        assert files and files["source"] and files["artifacts"], "the video and its working files were not stored"
        assert runner.restore_job_files(job_id, source=True).stat().st_size > 100_000, "the stored video is unusable"
    transcript = runner.job_dir(job_id) / "transcript.json"
    runner.restore_job_files(job_id)
    assert transcript.exists()
    for clip in clips:
        assert clip["status"] == "ready", clip["error"]
        if storage.enabled():
            assert storage.is_remote(clip["file"]) and storage.is_remote(clip["thumb"]), \
                "the clip was not saved to cloud storage"
        local_mp4 = config.JOBS_DIR / job_id / "clips" / f"{clip['id']}.mp4"
        assert not local_mp4.exists() or not storage.enabled(), "a copy was left on this disk"
        with storage.local_copy(clip["file"]) as downloaded:
            file = watch / f"{clip['id']}.mp4"
            shutil.copy(downloaded, file)
        info = media.probe(file)
        length = clip["end"] - clip["start"]
        panel = clip["panel"] or {}
        print(f"  Clip {clip['start']:.0f}-{clip['end']:.0f}s  AI {clip['ai_score']:.0f}  "
              f"panel {panel.get('score', 'n/a')} ({panel.get('viewers', 0)} viewers)  "
              f"\"{clip['title']}\"\n    {file}")
        assert (info["width"], info["height"]) == (720, 1280)
        assert 14 <= length <= 46 and abs(info["duration"] - length) < 0.5
        spoken = [w for w in json.loads(transcript.read_text(encoding="utf-8"))["words"]
                  if clip["start"] <= w["s"] <= clip["end"]]
        assert len(spoken) > 5, "there is no speech in the clip to caption"
        with storage.local_copy(clip["thumb"]) as thumb:
            assert thumb.stat().st_size > 1000
        assert clip["title"] and clip["description"]
    assert not (job["payload"] or {}).get("warning"), "the AI did not pick the clips"
    assert any(c["panel_score"] is not None for c in clips), "the audience panel did not score any clip"


# ---- posting, with a stand-in for YouTube -----------------------------------
# A real upload needs your Google sign-in, so these checks replace the YouTube
# service with a recorder and verify everything this app sends and stores.
class _FakeYouTube:
    def __init__(self):
        self.body = None

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.body = body
        return self

    def next_chunk(self, num_retries=0):
        return None, {"id": "yt_video_123"}


def _ready_clip(tmp_path, clip_id):
    video = config.JOBS_DIR / "posting" / "clips" / f"{clip_id}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00" * 2048)
    db.delete("clips", {"id": clip_id})
    db.delete("posts", {"clip_id": clip_id})  # leftovers from earlier runs
    db.insert("clips", {"id": clip_id, "job_id": "posting", "status": "ready", "start": 0, "end": 30,
                        "title": "Test clip", "description": "A test.", "hashtags": ["clips"],
                        "file": video.relative_to(config.DATA).as_posix(), "options": {"settings": {}},
                        "created_at": time.time()})
    return video


def test_upload_sends_the_right_request(tmp_path, monkeypatch):
    from app import youtube

    fake = _FakeYouTube()
    monkeypatch.setattr(youtube, "_service", lambda user_id: fake)
    video = _ready_clip(tmp_path, "upl1")

    assert youtube.upload("u1", file=video, title="T" * 150, description="d", tags=["clips"],
                          privacy="public", publish_at=None, category_id="20") == "yt_video_123"
    assert fake.body["status"]["privacyStatus"] == "public"
    assert "publishAt" not in fake.body["status"]
    assert len(fake.body["snippet"]["title"]) == 100 and fake.body["snippet"]["categoryId"] == "20"

    youtube.upload("u1", file=video, title="Later", description="d", tags=[], privacy="public",
                   publish_at="2030-01-02T10:00:00+05:45", category_id="20")
    assert fake.body["status"]["privacyStatus"] == "private", "YouTube requires private for scheduling"
    assert fake.body["status"]["publishAt"] == "2030-01-02T04:15:00.000Z", "schedule is sent in UTC"

    with pytest.raises(youtube.YouTubeError):
        youtube.upload("u1", file=tmp_path / "missing.mp4", title="x", description="", tags=[],
                       privacy="public", publish_at=None, category_id="20")


def test_post_now_and_schedule_through_the_app(client, tmp_path, monkeypatch):
    from app import worker, youtube

    _ready_clip(tmp_path, "upl2")
    not_connected = client.post("/api/posts", json={"clip_id": "upl2"})
    assert not_connected.status_code == 409 and "Connect your YouTube channel" in not_connected.json()["detail"]

    connected = {"client_configured": True, "connected": True, "channel": "Test", "connecting": False, "error": ""}
    monkeypatch.setattr(youtube, "status", lambda user_id: connected)
    monkeypatch.setattr(youtube, "_service", lambda user_id: _FakeYouTube())
    past = client.post("/api/posts", json={"clip_id": "upl2", "mode": "schedule", "publish_at": "2020-01-01T00:00:00Z"})
    assert past.status_code == 422 and "future" in past.json()["detail"]

    for body, final in (({"mode": "now", "privacy": "public"}, "published"),
                        ({"mode": "schedule", "publish_at": "2031-05-01T12:00:00Z"}, "scheduled")):
        post = client.post("/api/posts", json={"clip_id": "upl2", **body})
        assert post.status_code == 201 and post.json()["status"] == "uploading"
        job = db.find("jobs", {"kind": "upload"}, sort=[("created_at", -1)], limit=1)[0]
        assert job["payload"]["post_id"] == post.json()["id"] and job["status"] == "queued"
        worker.execute({**job, "status": "running"})      # what the background worker does
        saved = db.find_one("posts", {"id": post.json()["id"]})
        assert saved["status"] == final and saved["youtube_id"] == "yt_video_123"
        assert db.find_one("jobs", {"id": job["id"]})["status"] == "done"
    assert len([p for p in client.get("/api/posts").json() if p["clip_id"] == "upl2"]) == 2


def test_post_kind_follows_what_youtube_will_do(client, tmp_path, monkeypatch):
    from app import youtube

    monkeypatch.setattr(youtube, "status", lambda user_id: {"connected": True})
    _ready_clip(tmp_path, "kind1")
    cases = [  # width, height, seconds, kind asked for, expected HTTP status
        (1080, 1920, 30, None, 201), (1080, 1920, 30, "short", 201), (1080, 1920, 30, "video", 409),
        (1920, 1080, 30, None, 201), (1920, 1080, 30, "video", 201), (1920, 1080, 30, "short", 409),
        (1080, 1920, 200, "short", 409), (1080, 1920, 200, "video", 201),
    ]
    for width, height, seconds, kind, expected in cases:
        db.update("clips", "kind1", width=width, height=height, start=0, end=seconds)
        body = {"clip_id": "kind1", "mode": "now", "privacy": "private",
                "description": "Great moment #shorts #funny"}
        if kind:
            body["kind"] = kind
        response = client.post("/api/posts", json=body)
        assert response.status_code == expected, (width, height, seconds, kind, response.text)
        if expected == 201:
            post = response.json()
            actual = "short" if height >= width and seconds <= 180 else "video"
            assert post["kind"] == actual
            assert ("#Shorts" in post["description"]) == (actual == "short"), post["description"]
            assert post["description"].lower().count("#shorts") <= 1


def test_hook_suggestions_are_short_clean_and_unique(client, monkeypatch):
    from app.pipeline import selection

    words_file = config.JOBS_DIR / "hooks" / "transcript.json"
    words_file.parent.mkdir(parents=True, exist_ok=True)
    words_file.write_text('{"words": [{"w": "He", "s": 1, "e": 1.2}, {"w": "missed", "s": 1.3, "e": 1.6}]}')
    db.delete("clips", {"id": "hook1"})
    db.insert("clips", {"id": "hook1", "job_id": "hooks", "status": "ready", "start": 0, "end": 20,
                        "title": "A miss", "options": {"words": words_file.relative_to(config.DATA).as_posix(),
                                                       "settings": {}}, "created_at": time.time()})
    seen = {}

    def fake(system, user, **kwargs):
        seen["prompt"] = user
        return {"hooks": ['"He Missed From Two Metres"', "he missed from two metres", "Why Did Everyone Go Silent?",
                          "A hook that is far too long to ever fit on the screen", "", "Nobody Saw This Coming"]}

    monkeypatch.setattr(llm, "complete_json", fake)
    hooks = client.post("/api/clips/hook1/hooks").json()["hooks"]
    assert hooks == ["He Missed From Two Metres", "Why Did Everyone Go Silent?", "Nobody Saw This Coming"]
    assert "He missed" in seen["prompt"] and "curiosity gap" in seen["prompt"]
    assert client.post("/api/clips/nope/hooks").status_code == 404


def test_failed_upload_is_reported_clearly(client, tmp_path, monkeypatch):
    from app import worker, youtube

    _ready_clip(tmp_path, "upl3")
    monkeypatch.setattr(youtube, "status", lambda user_id: {"connected": True})

    def quota(*_, **__):
        raise youtube.YouTubeError(youtube._explain(Exception("quotaExceeded")))

    monkeypatch.setattr(youtube, "upload", quota)
    post = client.post("/api/posts", json={"clip_id": "upl3"}).json()
    job = db.find("jobs", {"kind": "upload"}, sort=[("created_at", -1)], limit=1)[0]
    worker.execute({**job, "status": "running"})
    saved = db.find_one("posts", {"id": post["id"]})
    assert saved["status"] == "failed" and "daily upload limit" in saved["error"]
    assert db.find_one("jobs", {"id": job["id"]})["status"] == "failed"


def test_youtube_oauth_requires_env_configuration(client, monkeypatch):
    monkeypatch.setattr(config, "google_login_oauth_client", lambda: None)
    assert client.post("/api/youtube/connect").status_code == 409
    assert client.post("/api/youtube/client", json={"json_text": "not allowed"}).status_code in (404, 405)


@pytest.mark.skipif(os.name != "nt", reason="Windows notifications")
def test_windows_notification_is_sent():
    from app import worker

    proc = worker.notify("Clipper test", "This is the test notification. Clips ready.")
    assert proc is not None, "powershell.exe could not be started"
    _, errors = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"toast script failed: {errors.decode(errors='replace')[:300]}"


def test_worker_heartbeat_ignores_a_dead_process():
    from app import worker

    db.kv_set(worker.HEARTBEAT_KEY, {"at": time.time(), "pid": os.getpid()})
    assert worker.is_alive()
    db.kv_set(worker.HEARTBEAT_KEY, {"at": time.time(), "pid": 999999})
    assert not worker.is_alive(), "a fresh heartbeat from a process that no longer exists is not alive"
    db.kv_set(worker.HEARTBEAT_KEY, {"at": time.time() - 60, "pid": os.getpid()})
    assert not worker.is_alive(), "an old heartbeat is not alive"


# ---- cloud storage ------------------------------------------------------------
def test_cloud_clips_play_and_download_through_signed_links(client, monkeypatch):
    seen = {}

    def fake_url(ref, download_name=None):
        seen["ref"], seen["name"] = ref, download_name
        return "https://signed.example/clip?sig=1"

    monkeypatch.setattr(storage, "url", fake_url)
    db.delete("clips", {"id": {"$in": ["cloud1", "local1"]}})
    db.insert("clips", {"id": "cloud1", "job_id": "x", "status": "ready", "start": 0, "end": 20,
                        "title": 'My: "best" clip', "file": "s3:clipper/x/clips/cloud1.mp4",
                        "thumb": "s3:clipper/x/clips/cloud1.jpg", "options": {"settings": {}},
                        "created_at": time.time()})
    detail = client.get("/api/clips/cloud1").json()
    assert detail["file_url"].startswith("/api/clips/cloud1/file")
    assert "s3:" not in str(detail["file_url"]) + str(detail["thumb_url"])

    play = client.get("/api/clips/cloud1/file", follow_redirects=False)
    assert play.status_code == 307 and play.headers["location"] == "https://signed.example/clip?sig=1"
    assert seen["ref"] == "s3:clipper/x/clips/cloud1.mp4" and seen["name"] is None
    download = client.get("/api/clips/cloud1/download", follow_redirects=False)
    assert download.status_code == 307 and seen["name"] == "My best clip.mp4"

    local = config.JOBS_DIR / "x" / "clips" / "local1.mp4"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"0" * 4096)
    db.insert("clips", {"id": "local1", "job_id": "x", "status": "ready", "start": 0, "end": 20,
                        "title": "Local", "file": local.relative_to(config.DATA).as_posix(),
                        "options": {"settings": {}}, "created_at": time.time()})
    assert client.get("/api/clips/local1/file").content == b"0" * 4096
    assert client.get("/api/clips/nope/file").status_code == 404


def test_rendered_clip_moves_to_the_cloud_or_stays_local_on_failure(tmp_path, monkeypatch):
    db.delete("jobs", {"id": "st1"})
    db.insert("jobs", {"id": "st1", "kind": "video", "status": "running", "options": {},
                       "payload": {}, "created_at": time.time(), "updated_at": time.time()})
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "key_for", lambda job, name: f"clipper/{job}/{name}")

    def make():
        out = config.JOBS_DIR / "st1" / "clips"
        out.mkdir(parents=True, exist_ok=True)
        (out / "a.mp4").write_bytes(b"v" * 100)
        (out / "a.jpg").write_bytes(b"t" * 100)
        return out, {"file": "a.mp4", "thumb": "a.jpg"}

    monkeypatch.setattr(storage, "put", lambda path, key, ctype: "s3:" + key)
    out, result = make()
    assert runner.store_outputs("st1", out, result) == ("s3:clipper/st1/clips/a.mp4", "s3:clipper/st1/clips/a.jpg")
    assert not (out / "a.mp4").exists() and not (out / "a.jpg").exists(), "local copies are removed"

    def broken(path, key, ctype):
        raise storage.StorageError("Could not reach the storage service.")

    monkeypatch.setattr(storage, "put", broken)
    out, result = make()
    file_ref, thumb_ref = runner.store_outputs("st1", out, result)
    assert not storage.is_remote(file_ref) and (config.DATA / file_ref).exists(), "kept locally when the upload fails"
    warning = db.find_one("jobs", {"id": "st1"})["payload"]["storage_warning"]
    assert "kept on this PC" in warning and "Could not reach the storage service" in warning


def test_storage_stays_server_side_and_is_not_shown_to_users(client):
    access, secret = storage.keys()
    assert access and secret, "BLOMP_ACCESS_KEY_ID / BLOMP_SECRET_ACCESS_KEY are missing from backend/.env"
    everything = ""
    for path in ("/api/settings", "/api/system", "/api/clips", "/api/jobs", "/api/trends"):
        everything += client.get(path).text
    data = client.get("/api/settings").json()
    assert "storage" not in data["settings"], "storage settings must not be shown to users"
    assert not any(name.startswith("BLOMP") for name in data["secrets"])
    assert "disk_free_gb" not in client.get("/api/system").text
    for hidden in (secret, access, storage.cfg()["bucket"], "blomp", "s3:"):
        assert hidden.lower() not in everything.lower(), f"{hidden[:6]}... is visible to users"
    assert client.put("/api/secrets", json={"name": "BLOMP_SECRET_ACCESS_KEY", "value": "x" * 20}).status_code == 422
    before = storage.cfg()["bucket"]
    client.put("/api/settings", json={"storage": {"bucket": "someone-elses-bucket"}})
    assert storage.cfg()["bucket"] == before, "users cannot change where clips are stored"


@pytest.mark.network
def test_blomp_round_trip():
    assert storage.enabled(), "cloud storage is not configured (keys and bucket in backend/.env)"
    result = storage.check()
    print(f"\n  Blomp bucket reachable, test file written and read back in {result['seconds']} s")
    sample = Path(config.DATA / "tmp" / "roundtrip.mp4")
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(os.urandom(1_500_000))
    ref = storage.put(sample, storage.key_for("roundtrip", "clips/roundtrip.mp4"), "video/mp4")
    try:
        import httpx

        link = storage.url(ref)
        part = httpx.get(link, headers={"Range": "bytes=0-99"}, timeout=30)
        assert part.status_code == 206 and len(part.content) == 100, "video seeking needs range requests"
        with storage.local_copy(ref) as copy:
            assert copy.read_bytes() == sample.read_bytes(), "downloaded file differs from the original"
        assert not copy.exists(), "the temporary download was not removed"
    finally:
        storage.delete(ref)
        sample.unlink(missing_ok=True)
    assert httpx.get(link, timeout=30).status_code in (403, 404), "the file was not deleted from the cloud"


def test_server_restarts_a_worker_that_has_died(monkeypatch):
    import asyncio

    from app import main

    calls = []
    monkeypatch.setattr(main, "ensure_worker", lambda: calls.append(1) or True)
    monkeypatch.setattr(main, "WORKER_CHECK_SECONDS", 0.01)

    async def run():
        task = asyncio.create_task(main.keep_worker_running())
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 3, "the server must keep checking that the worker is running"


# ---- more clips from a video that was already clipped -----------------------------------------
def _clipped_video(job_id, tmp_path, seconds=70):
    """A finished video job with its saved working files and one existing clip at 10-30 s."""
    folder = config.JOBS_DIR / job_id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    words = [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(seconds * 2)]
    segments = [{"s": i, "e": i + 1.5, "text": f"line {i}"} for i in range(0, seconds, 2)]
    (folder / "transcript.json").write_text(json.dumps({"words": words, "segments": segments}))
    (folder / "meta.json").write_text(json.dumps({"title": "Long video", "channel": "Some channel",
                                                  "duration": seconds, "webpage_url": "https://example.com/v"}))
    (folder / "signals.json").write_text(json.dumps([0.0] * seconds))
    settings = config.deep_merge(config.DEFAULT_SETTINGS, {"ai": {"panel_size": 0}, "format": {"quality": 360}})
    db.delete("clips", {"job_id": job_id})
    db.delete("jobs", {"id": job_id})
    db.insert("jobs", {"id": job_id, "kind": "video", "url": "https://example.com/v", "status": "done",
                       "title": "Long video", "options": settings, "payload": {},
                       "created_at": time.time(), "updated_at": time.time()})
    db.insert("clips", {"id": f"{job_id}-c1", "job_id": job_id, "status": "ready", "start": 10, "end": 30,
                        "title": "First clip", "options": {"settings": settings}, "created_at": time.time()})
    return folder


def test_more_clips_endpoint_rules(client, tmp_path):
    db.delete("jobs", {"kind": "more"})  # requests left over from earlier runs
    _clipped_video("more1", tmp_path)
    ok = client.post("/api/jobs/more1/more", json={"count": 2})
    assert ok.status_code == 201
    queued = db.find_one("jobs", {"id": ok.json()["id"]})
    assert queued["kind"] == "more" and queued["payload"]["source_job_id"] == "more1" and queued["payload"]["count"] == 2
    again = client.post("/api/jobs/more1/more", json={})
    assert again.status_code == 409 and "already being made" in again.json()["detail"]
    listed = next(j for j in client.get("/api/jobs").json() if j["id"] == "more1")
    assert listed["more"]["status"] == "queued", "the video row shows the running request"

    assert client.post("/api/jobs/nope/more", json={}).status_code == 404
    assert client.post("/api/jobs/more1/more", json={"count": 0}).status_code == 422
    assert client.post("/api/jobs/more1/more", json={"count": 11}).status_code == 422
    _clipped_video("more2", tmp_path)
    db.update("jobs", "more2", status="running")
    assert client.post("/api/jobs/more2/more", json={}).status_code == 409
    db.update("jobs", "more2", status="done")
    (config.JOBS_DIR / "more2" / "transcript.json").unlink()
    gone = client.post("/api/jobs/more2/more", json={})
    assert gone.status_code == 409 and "working files" in gone.json()["detail"]


def test_more_clips_skip_used_moments_and_continue_the_numbering(tmp_path, monkeypatch):
    from app.pipeline import selection

    folder = _clipped_video("more3", tmp_path)
    source = folder / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=70",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=70", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    monkeypatch.setattr(storage, "enabled", lambda: False)
    monkeypatch.setattr(media, "download", lambda *a, **k: pytest.fail("the source is on disk, no download"))

    def pick(start, end, score, title):
        return {"start": start, "end": end, "title": title, "hook": "A hook", "description": "d",
                "hashtags": ["x"], "score": score, "reason": "r"}

    monkeypatch.setattr(selection, "_ask", lambda *a, **k: [
        pick(12, 28, 95, "Same moment again"), pick(45, 62, 70, "A fresh moment")])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"ranking": []})
    db.delete("jobs", {"id": "more3-job"})
    db.insert("jobs", {"id": "more3-job", "kind": "more", "status": "running", "title": "Long video",
                       "options": {}, "created_at": time.time(), "updated_at": time.time(),
                       "payload": {"source_job_id": "more3", "count": 3, "label_part": True}})
    runner.process_more(db.find_one("jobs", {"id": "more3-job"}))

    clips = db.find("clips", {"job_id": "more3"}, sort=[("created_at", 1)])
    new = [c for c in clips if c["id"] != "more3-c1"]
    assert len(new) == 1, "the overlapping moment is skipped, only the new one is made"
    assert new[0]["start"] >= 44 and new[0]["status"] == "ready"
    assert not (new[0]["start"] < 30 and new[0]["end"] > 10), "no overlap with the clip that already exists"
    assert new[0]["title"].endswith(" – Part 2"), new[0]["title"]
    assert (config.DATA / new[0]["file"]).exists() and source.exists()
    assert db.find_one("jobs", {"id": "more3-job"})["payload"]["made"] == 1


def test_more_clips_without_ai_does_not_repeat_the_first_pass(tmp_path, monkeypatch):
    from app.pipeline import selection

    _clipped_video("more4", tmp_path)

    def broken(*args, **kwargs):
        raise llm.LLMError("The AI is unavailable.")

    monkeypatch.setattr(selection, "_ask", broken)
    db.delete("jobs", {"id": "more4-job"})
    db.insert("jobs", {"id": "more4-job", "kind": "more", "status": "running", "options": {},
                       "created_at": time.time(), "updated_at": time.time(),
                       "payload": {"source_job_id": "more4", "count": 2}})
    with pytest.raises(RuntimeError, match="AI is unavailable"):
        runner.process_more(db.find_one("jobs", {"id": "more4-job"}))
    assert len(db.find("clips", {"job_id": "more4"})) == 1


# ---- disk safety ----------------------------------------------------------------------------
def test_cancelling_stops_a_running_download(monkeypatch):
    db.delete("jobs", {"id": "dl1"})
    db.insert("jobs", {"id": "dl1", "kind": "video", "status": "running", "options": {}, "payload": {},
                       "created_at": time.time(), "updated_at": time.time()})
    updates = []
    callback = runner.cancel_aware("dl1", updates.append, every=0)
    callback(0.1)
    assert updates == [0.1], "while the job runs, progress is reported"
    db.update("jobs", "dl1", status="cancelled")
    with pytest.raises(runner.Cancelled):
        callback(0.2)
    assert updates == [0.1], "no more progress once the job was cancelled"

    folder = config.JOBS_DIR / "dl1"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("source.f299.mp4.part", "source.f140.m4a.ytdl", "keep.json"):
        (folder / name).write_bytes(b"x" * 10)
    runner.cleanup_partial("dl1")
    assert [p.name for p in folder.iterdir()] == ["keep.json"], "partial downloads are removed, other files kept"


def test_folders_without_a_job_are_swept_but_recent_ones_are_kept():
    import os as _os

    db.delete("jobs", {"id": "sweep-known"})
    db.insert("jobs", {"id": "sweep-known", "kind": "video", "status": "done", "options": {}, "payload": {},
                       "created_at": time.time(), "updated_at": time.time()})
    for name in ("sweep-known", "sweep-old", "sweep-new"):
        folder = config.JOBS_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "source.mp4.part").write_bytes(b"x" * 100)
    old = time.time() - 3 * 3600
    for path in (config.JOBS_DIR / "sweep-old", config.JOBS_DIR / "sweep-old" / "source.mp4.part",
                 config.JOBS_DIR / "sweep-known", config.JOBS_DIR / "sweep-known" / "source.mp4.part"):
        _os.utime(path, (old, old))
    runner.sweep_orphans()
    assert not (config.JOBS_DIR / "sweep-old").exists(), "an old folder with no job is removed"
    assert (config.JOBS_DIR / "sweep-new").exists(), "a folder touched recently might still be in use"
    assert (config.JOBS_DIR / "sweep-known").exists(), "a folder that belongs to a job is never removed"


def test_downloads_are_refused_when_the_disk_is_nearly_full(monkeypatch):
    import collections

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(500 * 10**9, 497 * 10**9, 3 * 10**9))
    with pytest.raises(media.MediaError, match="not enough free space"):
        runner.require_disk()
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(500 * 10**9, 400 * 10**9, 100 * 10**9))
    runner.require_disk()


def test_the_source_download_is_deleted_once_more_clips_are_stored(tmp_path, monkeypatch):
    from app.pipeline import selection

    folder = _clipped_video("more5", tmp_path)
    source = folder / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=70",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=70", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda path, key, ctype: "s3:" + key)
    monkeypatch.setattr(storage, "key_for", lambda job, name: f"test/{job}/{name}")
    monkeypatch.setattr(selection, "_ask", lambda *a, **k: [{
        "start": 45, "end": 62, "title": "Fresh", "hook": "h", "description": "d", "hashtags": [], "score": 70, "reason": "r"}])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"ranking": []})
    db.delete("jobs", {"id": "more5-job"})
    db.insert("jobs", {"id": "more5-job", "kind": "more", "status": "running", "options": {},
                       "created_at": time.time(), "updated_at": time.time(),
                       "payload": {"source_job_id": "more5", "count": 1}})
    runner.process_more(db.find_one("jobs", {"id": "more5-job"}))
    new = [c for c in db.find("clips", {"job_id": "more5"}) if c["id"] != "more5-c1"]
    assert len(new) == 1 and new[0]["file"].startswith("s3:"), "the new clip was stored remotely"
    assert not source.exists(), "the downloaded source must not be left on the disk"


def test_ping_route_is_public_and_does_no_work(monkeypatch):
    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/jobs").status_code == 401, "the rest of the API still needs an account"
        for method in ("get", "head"):
            response = getattr(anonymous, method)("/api/ping")
            assert response.status_code == 200, method
        assert anonymous.get("/api/ping").json() == {"ok": True}, "no state is returned"


# ---- accounts: everyone is an ordinary user -----------------------------------------------------
def _person(email, sub):
    from app import auth

    return auth.upsert_google_user({"sub": sub, "email": email, "email_verified": True, "name": email})


def _browser(person):
    from app import auth

    visitor = TestClient(app)
    visitor.cookies.set(auth.SESSION_COOKIE, auth.create_session(person["id"]))
    return visitor


def _wipe_people():
    for name in ("sessions", "users", "user_keys", "youtube", "oauth_states"):
        db.delete(name, {})
    db.delete("kv", {"_id": {"$regex": "^(prefs|youtube-connect):"}})


def test_public_address_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://clipper-abc.onrender.com")
    assert config.public_url() == "https://clipper-abc.onrender.com"
    monkeypatch.setenv("PUBLIC_URL", "https://clips.example.com/")
    assert config.public_url() == "https://clips.example.com", "an explicit PUBLIC_URL (custom domain) wins"


def test_anyone_can_sign_in_and_nobody_is_an_administrator(monkeypatch):
    from app import auth

    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    _wipe_people()
    monkeypatch.setenv("PUBLIC_URL", "https://clipper.example.com")
    first, second = _person("first@example.com", "s1"), _person("second@example.com", "s2")
    assert "is_admin" not in first and "is_admin" not in second, "there are no roles"
    assert first["id"] != second["id"]
    again = _person("first@example.com", "s1")
    assert again["id"] == first["id"], "signing in again finds the same account"
    with pytest.raises(auth.AuthError):  # an email cannot be taken over by a different Google account
        _person("first@example.com", "someone-else")
    me = _browser(first).get("/api/auth/me").json()
    assert me["user"]["email"] == "first@example.com" and "stats" not in me
    assert _browser(first).get("/api/users").status_code in (404, 405), "no page lists the other users"
    assert TestClient(app).get("/api/jobs").status_code == 401, "everyone must sign in"
    _wipe_people()


def test_people_see_only_their_own_work(monkeypatch):
    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    _wipe_people()
    ann, bob, cat = _person("ann@example.com", "a"), _person("bob@example.com", "b"), _person("cat@example.com", "c")
    for who, person in (("ann", ann), ("bob", bob)):
        db.delete("jobs", {"id": f"{who}-job"})
        db.delete("clips", {"id": f"{who}-clip"})
        db.insert("jobs", {"id": f"{who}-job", "kind": "video", "status": "done", "title": who,
                           "options": {}, "payload": {}, "created_at": time.time(), "updated_at": time.time(),
                           "user_id": person["id"], "files": {"artifacts": {"x": "s3:secret/ref"}}})
        db.insert("clips", {"id": f"{who}-clip", "job_id": f"{who}-job", "user_id": person["id"],
                            "status": "ready", "start": 0, "end": 20, "title": who, "options": {},
                            "created_at": time.time(), "file": "s3:private/key.mp4"})
    ann_c, bob_c, cat_c = _browser(ann), _browser(bob), _browser(cat)

    assert {j["id"] for j in ann_c.get("/api/jobs").json()} == {"ann-job"}
    assert {j["id"] for j in bob_c.get("/api/jobs").json()} == {"bob-job"}
    assert cat_c.get("/api/jobs").json() == [] and cat_c.get("/api/clips").json() == []
    assert [c["id"] for c in ann_c.get("/api/clips").json()] == ["ann-clip"]
    for call in (ann_c.get("/api/jobs/bob-job"), ann_c.delete("/api/jobs/bob-job"),
                 ann_c.post("/api/jobs/bob-job/stop"), ann_c.post("/api/jobs/bob-job/more", json={"count": 1}),
                 ann_c.get("/api/clips/bob-clip"), ann_c.get("/api/clips/bob-clip/file"),
                 ann_c.patch("/api/clips/bob-clip", json={"title": "mine now"}),
                 ann_c.post("/api/clips/bob-clip/hooks"), ann_c.delete("/api/clips/bob-clip"),
                 ann_c.get("/api/clips/bob-clip/download"), ann_c.post("/api/posts", json={"clip_id": "bob-clip"})):
        assert call.status_code == 404, "someone else's work looks like it does not exist"
    assert db.find_one("clips", {"id": "bob-clip"})["title"] == "bob"
    own = ann_c.get("/api/jobs/ann-job").json()
    assert "files" not in own and "options" not in own and "user_id" not in own, "internals stay on the server"
    assert "s3:" not in ann_c.get("/api/clips").text, "storage addresses never reach the browser"

    # Settings are per person, and so is the choice of AI engine.
    base = bob_c.get("/api/settings").json()["settings"]
    assert ann_c.put("/api/settings", json={"clip": {"max_sec": 45}, "ai": {"provider": "gemini"}}).status_code == 200
    mine = ann_c.get("/api/settings").json()["settings"]
    assert mine["clip"]["max_sec"] == 45 and mine["ai"]["provider"] == "gemini"
    ann_c.put("/api/settings", json={"clip": {"$where": "1", "unknown": 1}, "evil": {"a": 1}})
    stored_prefs = db.kv_get(f"prefs:{ann['id']}")
    assert "$where" not in str(stored_prefs) and "unknown" not in str(stored_prefs) and "evil" not in stored_prefs,         "only real settings are saved"
    theirs = bob_c.get("/api/settings").json()["settings"]
    assert theirs["clip"]["max_sec"] == base["clip"]["max_sec"] and theirs["ai"]["provider"] == base["ai"]["provider"]
    assert "storage" not in mine, "storage is server configuration"

    # A long video is welcome: nothing shortens it. A person may have three in progress at once.
    made = []
    for n in range(3):
        response = ann_c.post("/api/jobs", json={"url": f"https://example.com/watch?v={n}",
                                                 "options": {"max_minutes": 600}})
        assert response.status_code == 201, response.text
        made.append(response.json()["id"])
    assert db.find_one("jobs", {"id": made[0]})["options"]["max_minutes"] == 600, "no length cap"
    assert db.find_one("jobs", {"id": made[0]})["user_id"] == ann["id"]
    assert ann_c.post("/api/jobs", json={"url": "https://example.com/watch?v=9"}).status_code == 429
    assert bob_c.post("/api/jobs", json={"url": "https://example.com/watch?v=9"}).status_code == 201, \
        "one person's queue does not block another's"

    for job in db.find("jobs", {"user_id": {"$in": [ann["id"], bob["id"]]}}):
        db.delete("jobs", {"id": job["id"]})
    db.delete("clips", {"id": {"$in": ["ann-clip", "bob-clip"]}})
    _wipe_people()


def test_a_hosted_server_refuses_work_it_could_not_store(monkeypatch):
    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    _wipe_people()
    ann = _person("ann@example.com", "a")
    monkeypatch.setenv("PUBLIC_URL", "https://clipper.example.com")
    monkeypatch.setattr(storage, "enabled", lambda: False)
    response = _browser(ann).post("/api/jobs", json={"url": "https://example.com/watch?v=1"})
    assert response.status_code == 503 and "store" in response.json()["detail"]
    _wipe_people()


def test_each_person_has_their_own_keys_and_the_nvidia_key_is_shared(monkeypatch):
    from app import keys

    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    monkeypatch.setenv("APP_SECRET_KEY", "a-test-secret-for-encrypting-keys")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-the-shared-platform-key-0001")
    _wipe_people()
    ann, bob = _person("ann@example.com", "a"), _person("bob@example.com", "b")
    ann_c, bob_c = _browser(ann), _browser(bob)

    shared = bob_c.get("/api/settings").json()["secrets"]
    assert shared["NVIDIA_API_KEY"] == {"set": True, "hint": "", "shared": True}
    assert shared["ANTHROPIC_API_KEY"]["set"] is False, "other providers need the person's own key"

    saved = ann_c.put("/api/secrets", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-ann-own-key-abcd1234"})
    assert saved.status_code == 200 and "sk-ant-ann-own-key" not in saved.text, "a key is never sent back"
    assert saved.json()["ANTHROPIC_API_KEY"] == {"set": True, "hint": "…1234", "shared": False}
    assert bob_c.get("/api/settings").json()["secrets"]["ANTHROPIC_API_KEY"]["set"] is False
    stored = db.coll("user_keys").find_one({"user_id": ann["id"]})
    assert "sk-ant-ann-own-key" not in str(stored), "keys are encrypted in the database"
    assert keys.own(ann["id"], "ANTHROPIC_API_KEY") == "sk-ant-ann-own-key-abcd1234"
    assert ann_c.put("/api/secrets", json={"name": "GOOGLE_LOGIN_CLIENT_SECRET", "value": "x"}).status_code == 422

    # The AI call uses the right key for the right person.
    ann_c.put("/api/settings", json={"ai": {"provider": "anthropic"}})
    with llm.as_user(ann["id"]):
        assert llm._provider()[0] == "anthropic" and llm._provider()[2] == "sk-ant-ann-own-key-abcd1234"
    bob_c.put("/api/settings", json={"ai": {"provider": "anthropic"}})
    with llm.as_user(bob["id"]), pytest.raises(llm.LLMError, match="Add your own"):
        llm._provider()
    bob_c.put("/api/settings", json={"ai": {"provider": "nvidia"}})
    with llm.as_user(bob["id"]):
        assert llm._provider()[2] == "nvapi-the-shared-platform-key-0001"
    ann_c.put("/api/secrets", json={"name": "NVIDIA_API_KEY", "value": "nvapi-ann-own-nvidia-key-9999"})
    ann_c.put("/api/settings", json={"ai": {"provider": "nvidia"}})
    with llm.as_user(ann["id"]):
        assert llm._provider()[2] == "nvapi-ann-own-nvidia-key-9999", "their own key beats the shared one"

    assert ann_c.put("/api/secrets", json={"name": "ANTHROPIC_API_KEY", "value": ""}).json()["ANTHROPIC_API_KEY"]["set"] is False

    monkeypatch.delenv("APP_SECRET_KEY")
    monkeypatch.setenv("PUBLIC_URL", "https://clipper.example.com")
    with pytest.raises(keys.KeysError, match="APP_SECRET_KEY"):
        keys.put(ann["id"], "GEMINI_API_KEY", "g" * 20)
    _wipe_people()


def test_each_person_connects_and_posts_to_their_own_youtube_channel(monkeypatch):
    from google_auth_oauthlib.flow import Flow

    from app import keys, worker, youtube

    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    monkeypatch.setenv("APP_SECRET_KEY", "a-test-secret-for-encrypting-keys")
    monkeypatch.setenv("PUBLIC_URL", "https://clipper.example.com")
    monkeypatch.setenv("GOOGLE_LOGIN_CLIENT_ID", "web-client.example")
    monkeypatch.setenv("GOOGLE_LOGIN_CLIENT_SECRET", "web-secret-for-test")
    _wipe_people()
    ann, bob = _person("ann@example.com", "a"), _person("bob@example.com", "b")
    ann_c, bob_c = _browser(ann), _browser(bob)

    assert ann_c.get("/api/youtube").json()["connected"] is False
    start = ann_c.post("/api/youtube/connect")
    assert start.status_code == 202
    url = start.json()["redirect_to"]
    assert "redirect_uri=https%3A%2F%2Fclipper.example.com%2Fapi%2Fyoutube%2Fcallback" in url and "code_challenge=" in url
    state = db.kv_get(f"youtube-connect:{ann['id']}")["state"]
    assert ann_c.get("/api/youtube").json()["connecting"] is True
    assert bob_c.get("/api/youtube").json()["connecting"] is False, "one person's sign-in is not another's"

    # Another person cannot finish ann's sign-in, and a wrong state is refused.
    refused = bob_c.get(f"/api/youtube/callback?code=x&state={state}", follow_redirects=False)
    assert refused.status_code == 303 and keys.load_youtube(bob["id"]) is None
    assert "expired" in bob_c.get("/api/youtube").json()["error"]

    class Credentials:
        def to_json(self):
            return '{"token": "ann-secret-token", "refresh_token": "r"}'

    seen = {}

    def fake_fetch(self, code=None, **kwargs):
        seen["code"], seen["verifier"] = code, self.code_verifier

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch)
    monkeypatch.setattr(Flow, "credentials", property(lambda self: Credentials()))
    monkeypatch.setattr(youtube, "_service", lambda user_id: (_ for _ in ()).throw(RuntimeError("offline")))
    done = ann_c.get(f"/api/youtube/callback?code=the-code&state={state}", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/settings/#youtube"
    assert seen["code"] == "the-code" and seen["verifier"], "the code is exchanged with the stored PKCE verifier"
    assert ann_c.get("/api/youtube").json()["connected"] is True
    assert bob_c.get("/api/youtube").json()["connected"] is False
    assert "ann-secret-token" not in str(db.coll("youtube").find_one({"user_id": ann["id"]})), "token is encrypted"
    assert "ann-secret-token" not in ann_c.get("/api/youtube").text

    # Posting uses the poster's own channel; someone with none connected cannot post.
    db.delete("clips", {"id": "yt-clip"})
    db.insert("clips", {"id": "yt-clip", "job_id": "x", "user_id": ann["id"], "status": "ready", "start": 0,
                        "end": 30, "width": 1080, "height": 1920, "title": "T", "options": {"settings": {}},
                        "created_at": time.time(), "file": "jobs/x/clip.mp4"})
    db.insert("clips", {"id": "yt-clip-bob", "job_id": "y", "user_id": bob["id"], "status": "ready", "start": 0,
                        "end": 30, "width": 1080, "height": 1920, "title": "T", "options": {"settings": {}},
                        "created_at": time.time()})
    assert bob_c.post("/api/posts", json={"clip_id": "yt-clip-bob"}).status_code == 409, "bob has no channel"
    assert bob_c.post("/api/posts", json={"clip_id": "yt-clip"}).status_code == 404, "and cannot use ann's clip"
    post = ann_c.post("/api/posts", json={"clip_id": "yt-clip", "mode": "now"})
    assert post.status_code == 201 and "user_id" not in post.json()
    used = {}

    def fake_upload(user_id, **kwargs):
        used["user"] = user_id
        return "yt123"

    monkeypatch.setattr(youtube, "upload", fake_upload)
    monkeypatch.setattr(storage, "local_copy", lambda ref: __import__("contextlib").nullcontext(Path("clip.mp4")))
    job = db.find("jobs", {"kind": "upload"}, sort=[("created_at", -1)], limit=1)[0]
    assert job["user_id"] == ann["id"]
    worker.execute({**job, "status": "running"})
    assert used["user"] == ann["id"], "the upload goes to the channel of the person who posted"
    assert [p["id"] for p in ann_c.get("/api/posts").json()] == [post.json()["id"]]
    assert bob_c.get("/api/posts").json() == []

    disconnected = ann_c.post("/api/youtube/disconnect").json()
    assert disconnected["connected"] is False and keys.load_youtube(ann["id"]) is None
    for doc in ("yt-clip", "yt-clip-bob"):
        db.delete("clips", {"id": doc})
    db.delete("posts", {"clip_id": "yt-clip"})
    _wipe_people()


def test_working_files_and_the_source_video_are_kept_in_storage_not_on_the_server(tmp_path, monkeypatch):
    bucket = {}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "key_for", lambda job, name: f"t/{job}/{name}")
    monkeypatch.setattr(storage, "put", lambda path, key, ctype: bucket.setdefault(key, Path(path).read_bytes()) and "s3:" + key)
    monkeypatch.setattr(storage, "get", lambda ref, dest: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                           dest.write_bytes(bucket[ref.removeprefix("s3:")]), dest)[2])
    deleted = []
    monkeypatch.setattr(storage, "delete", lambda ref: deleted.append(ref))

    db.delete("jobs", {"id": "keep1"})
    db.insert("jobs", {"id": "keep1", "kind": "video", "status": "done", "options": {}, "payload": {},
                       "created_at": time.time(), "updated_at": time.time(), "user_id": "u"})
    folder = runner.job_dir("keep1")
    for name in ("meta.json", "transcript.json", "signals.json"):
        (folder / name).write_text('{"name": "%s"}' % name)
    source = folder / "source.mp4"
    source.write_bytes(b"\x01" * 5000)

    runner.persist_job_files("keep1", folder, source)
    assert not folder.exists(), "nothing is left on the server after a video is processed"
    files = db.find_one("jobs", {"id": "keep1"})["files"]
    assert set(files["artifacts"]) == {"meta.json", "transcript.json", "signals.json"} and files["source"]

    got = runner.restore_job_files("keep1", source=True)
    assert got.read_bytes() == b"\x01" * 5000, "the downloaded video comes back from storage when needed"
    assert (runner.job_dir("keep1") / "transcript.json").read_text() == '{"name": "transcript.json"}'
    runner.delete_job_files("keep1", files)
    assert not (config.JOBS_DIR / "keep1").exists()
    assert set(deleted) == {*files["artifacts"].values(), files["source"]}, "deleting a video deletes its stored files"
    db.delete("jobs", {"id": "keep1"})
