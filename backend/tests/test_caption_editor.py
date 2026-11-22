"""The caption editor: fix the words of a clip, keep their timing, render again."""
import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, runner, storage
from app.main import app
from app.pipeline import captions

HEARD = [{"w": "so", "s": 10.0, "e": 10.3}, {"w": "mister", "s": 10.4, "e": 10.8}, {"w": "beast", "s": 10.9, "e": 11.3},
         {"w": "gave", "s": 11.4, "e": 11.7}, {"w": "away", "s": 11.8, "e": 12.2}, {"w": "money", "s": 12.3, "e": 12.9}]


@pytest.fixture(scope="module", autouse=True)
def database():
    config.ensure_dirs()
    db.init()


def test_caption_lines_group_words_like_the_screen_does():
    lines = captions.lines_for(HEARD, 3)
    assert [line["text"] for line in lines] == ["so mister beast", "gave away money"]
    assert lines[0]["s"] == 10.0 and lines[0]["e"] == 11.3


def test_a_spelling_fix_keeps_the_time_of_every_word():
    lines = [{"s": 10.0, "e": 11.3, "text": "so MrBeast X"}, {"s": 11.4, "e": 12.9, "text": "gave away money"}]
    words = captions.words_from_lines(lines, HEARD)
    assert [w["w"] for w in words] == ["so", "MrBeast", "X", "gave", "away", "money"]
    assert [w["s"] for w in words] == [h["s"] for h in HEARD], "same number of words: each keeps its own time"


def test_a_line_with_a_different_number_of_words_shares_its_time_evenly():
    words = captions.words_from_lines([{"s": 10.0, "e": 12.0, "text": "one two three four five"}], HEARD)
    assert [w["w"] for w in words] == ["one", "two", "three", "four", "five"]
    assert words[0]["s"] == 10.0 and words[-1]["e"] == 12.0
    assert all(abs((w["e"] - w["s"]) - 0.4) < 0.01 for w in words), "even shares of the line's time"
    empty = [{"s": 10.0, "e": 11.0, "text": "   "}, {"s": 12.0, "e": 12.0, "text": "x"}]
    assert captions.words_from_lines(empty, HEARD) == [], "an emptied line, and a line with no time, show nothing"


def _clip_in(name, **extra):
    folder = config.JOBS_DIR / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "transcript.json").write_text(json.dumps({"words": HEARD, "segments": []}))
    settings = config.deep_merge(config.DEFAULT_SETTINGS, {})
    clip = {"id": f"{name}-c", "job_id": name, "status": "ready", "start": 9.0, "end": 14.0, "title": "T", "hook": "",
            "options": {"settings": settings, "words": f"jobs/{name}/transcript.json", "source": None,
                        "url": "https://youtu.be/x"},
            "created_at": time.time(), **extra}
    db.delete("clips", {"id": clip["id"]})
    db.insert("clips", clip)
    return clip


def _remove(*names):
    for name in names:
        db.delete("clips", {"job_id": name})
        shutil.rmtree(config.JOBS_DIR / name, ignore_errors=True)
    db.delete("jobs", {"kind": "render"})


def test_edited_words_replace_only_the_stretch_that_was_edited(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    edited = _clip_in("capwords", caption_words=[{"w": "MrBeast", "s": 10.4, "e": 11.3}], caption_range=[10.4, 11.3])
    assert [w["w"] for w in runner.clip_words(edited)] == ["so", "MrBeast", "gave", "away", "money"], \
        "the edited stretch is swapped, the rest kept"
    plain = _clip_in("capwords")
    assert [w["w"] for w in runner.clip_words(plain)] == [h["w"] for h in HEARD], "unedited: just what was heard"
    _remove("capwords")


def test_captions_can_be_read_corrected_and_reset_through_the_api(monkeypatch):
    monkeypatch.setattr(storage, "enabled", lambda: False)
    clip = _clip_in("capapi")
    with TestClient(app) as client:
        got = client.get(f"/api/clips/{clip['id']}/captions")
        assert got.status_code == 200, got.text
        lines = got.json()["lines"]
        assert [line["text"] for line in lines] == ["so mister beast", "gave away money"]
        assert lines[0]["s"] == 1.0, "times count from the start of the clip, not of the video"
        assert got.json()["edited"] is False

        late = client.put(f"/api/clips/{clip['id']}/captions", json={"lines": [{"s": 1, "e": 99, "text": "x"}]})
        assert late.status_code == 422
        long = client.put(f"/api/clips/{clip['id']}/captions", json={"lines": [{"s": 1, "e": 2, "text": "x" * 500}]})
        assert long.status_code == 422

        lines[0]["text"] = "so MrBeast X"
        saved = client.put(f"/api/clips/{clip['id']}/captions", json={"lines": lines})
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "rendering" and saved.json()["captions_edited"] is True
        assert "caption_words" not in saved.json(), "the stored words stay on the server"
        queued = db.find("jobs", {"kind": "render", "payload.clip_id": clip["id"]})
        assert len(queued) == 1 and queued[0]["payload"]["rescore"] is False

        assert client.put(f"/api/clips/{clip['id']}/captions", json={"lines": lines}).status_code == 409, \
            "not while the clip is already rendering"
        db.update("clips", clip["id"], status="ready")
        again = client.get(f"/api/clips/{clip['id']}/captions").json()
        assert again["edited"] is True and again["lines"][0]["text"] == "so MrBeast X"

        reset = client.delete(f"/api/clips/{clip['id']}/captions")
        assert reset.status_code == 200 and reset.json()["captions_edited"] is False
        db.update("clips", clip["id"], status="ready")
        assert client.get(f"/api/clips/{clip['id']}/captions").json()["lines"][0]["text"] == "so mister beast"

        live = _clip_in("caplive")
        live["options"]["source"] = "jobs/caplive/live/window_0001.mp4"
        db.update("clips", live["id"], options=live["options"])
        refused = client.get(f"/api/clips/{live['id']}/captions")
        assert refused.status_code == 409 and "live" in refused.json()["detail"]
        assert client.get("/api/clips/nobody/captions").status_code == 404
    _remove("capapi", "caplive")


def test_nobody_reads_or_changes_another_persons_captions(monkeypatch):
    monkeypatch.setenv("CLIPPER_AUTH_DISABLED", "0")
    monkeypatch.setattr(storage, "enabled", lambda: False)
    for name in ("sessions", "users"):
        db.delete(name, {})
    ann = auth.upsert_google_user({"sub": "a", "email": "ann@example.com", "email_verified": True, "name": "ann"})
    bob = auth.upsert_google_user({"sub": "b", "email": "bob@example.com", "email_verified": True, "name": "bob"})
    clip = _clip_in("capmine", user_id=ann["id"])
    theirs, mine = TestClient(app), TestClient(app)
    theirs.cookies.set(auth.SESSION_COOKIE, auth.create_session(bob["id"]))
    mine.cookies.set(auth.SESSION_COOKIE, auth.create_session(ann["id"]))
    url = f"/api/clips/{clip['id']}/captions"
    for call in (theirs.get(url), theirs.put(url, json={"lines": []}), theirs.delete(url)):
        assert call.status_code == 404, "a stranger's clip looks like a clip that does not exist"
    assert mine.get(url).status_code == 200
    for name in ("sessions", "users"):
        db.delete(name, {})
    _remove("capmine")
