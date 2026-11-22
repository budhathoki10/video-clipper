"""A linked video is never downloaded whole: its sound is analysed, and each clip downloads only its
own part of the picture. Long videos transcribe only their strongest stretches. Streams stop at the
number of clips asked for."""
import shutil
import subprocess
import time
import wave

import numpy as np
import pytest

from app import config, db, live, llm, runner, storage
from app.pipeline import media, selection, transcribe, trends


@pytest.fixture(scope="module", autouse=True)
def database():
    config.ensure_dirs()
    db.init()


def _tone(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    "-c:a", "aac", str(path)], check=True)
    return path


def _video(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={seconds:.2f}",
                    "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds:.2f}", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def test_a_link_is_clipped_from_its_sound_and_each_clip_downloads_only_its_part(monkeypatch):
    job_id, url = "sound1", "https://youtu.be/abc"
    folder = config.JOBS_DIR / job_id
    shutil.rmtree(folder, ignore_errors=True)
    db.delete("clips", {"job_id": job_id})
    db.delete("jobs", {"id": {"$in": [job_id, "sound1-render"]}})
    meta = {"id": "abc", "title": "A long video", "channel": "Someone", "channel_handle": "@someone",
            "duration": 120.0, "live_status": "not_live", "thumbnail": "", "webpage_url": url,
            "heatmap": [], "category": "", "tags": [], "stream_urls": []}
    sections, transcribed = [], []

    def fake_audio(link, dest_dir, **kwargs):
        dest_dir.mkdir(parents=True, exist_ok=True)
        return _tone(dest_dir / "audio.m4a", 120)

    def fake_section(link, dest, start, end, **kwargs):
        sections.append((link, start, end))
        dest.parent.mkdir(parents=True, exist_ok=True)
        return _video(dest, end - start)

    def fake_transcribe(path, **kwargs):
        transcribed.append(kwargs.get("ranges"))
        words = [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.3} for i in range(240)]
        return {"language": "en", "words": words, "segments": [{"s": i, "e": i + 1.5, "text": f"line {i}"}
                                                              for i in range(0, 120, 2)]}

    monkeypatch.setattr(storage, "enabled", lambda: False)
    monkeypatch.setattr(media, "fetch_info", lambda link, **kwargs: dict(meta))
    monkeypatch.setattr(media, "download_audio", fake_audio)
    monkeypatch.setattr(media, "download_section", fake_section)
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(trends, "brief_for", lambda meta: "")
    monkeypatch.setattr(selection, "_ask", lambda *a, **k: [{
        "start": 40, "end": 60, "title": "A moment", "hook": "Wait for it", "description": "d",
        "hashtags": ["x"], "score": 90, "reason": "r"}])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"ranking": []})
    settings = config.deep_merge(config.DEFAULT_SETTINGS, {
        "ai": {"panel_size": 0}, "format": {"quality": 360, "layout": "blur"},
        "clip": {"max_clips": 1, "min_sec": 15, "max_sec": 30}})
    db.insert("jobs", {"id": job_id, "kind": "video", "url": url, "status": "running", "options": settings,
                       "payload": {}, "created_at": time.time(), "updated_at": time.time()})

    runner.process_video(db.find_one("jobs", {"id": job_id}))

    clips = db.find("clips", {"job_id": job_id})
    assert len(clips) == 1 and clips[0]["status"] == "ready", clips and clips[0].get("error")
    clip = clips[0]
    assert transcribed == [None], "a two-minute video is transcribed whole"
    assert len(sections) == 1, "one part downloaded for one clip"
    link, start, end = sections[0]
    assert link == url and abs(start - (clip["start"] - runner.SECTION_PAD)) < 0.01 \
        and abs(end - (clip["end"] + runner.SECTION_PAD)) < 0.01, "only the clip and a few seconds around it"
    assert not list(folder.glob("source.*")), "the whole video was never downloaded"
    assert not list(folder.glob("audio.*")), "the sound is removed once the moments are chosen"
    assert not list((folder / "sections").glob("*")), "the downloaded part is removed after rendering"
    assert clip["options"]["url"] == url and clip["options"]["source"] is None
    rendered = media.probe(config.DATA / clip["file"])
    assert abs(rendered["duration"] - (clip["end"] - clip["start"])) < 0.5

    # Editing the clip later fetches its part again: there is no stored copy of the whole video.
    db.insert("jobs", {"id": "sound1-render", "kind": "render", "status": "running", "options": {},
                       "payload": {"clip_id": clip["id"]}, "created_at": time.time(), "updated_at": time.time()})
    runner.rerender(db.find_one("jobs", {"id": "sound1-render"}))
    assert len(sections) == 2 and db.find_one("clips", {"id": clip["id"]})["status"] == "ready"
    db.delete("jobs", {"id": {"$in": [job_id, "sound1-render"]}})
    db.delete("clips", {"job_id": job_id})
    shutil.rmtree(folder, ignore_errors=True)


def test_a_resumed_job_reuses_the_sound_it_already_downloaded(monkeypatch, tmp_path):
    folder = tmp_path / "resume"
    folder.mkdir()
    _tone(folder / "audio.m4a", 2)
    (folder / "audio.done").write_text("ok")
    monkeypatch.setattr(media, "download_audio", lambda *a, **k: pytest.fail("the sound is already here"))
    assert runner._download_sound("resume", folder, "https://youtu.be/abc") == folder / "audio.m4a", \
        "the marker file is never taken for the sound"


def test_a_long_video_transcribes_only_around_its_strongest_moments():
    cfg = runner.clip_settings({"min_sec": 20, "max_sec": 60, "max_clips": 2, "min_score": 0})
    loudness = np.full(3 * 3600 + 100, -30.0, dtype=np.float32)
    loudness[5000:5030] = -5.0
    loudness[9000:9040] = -6.0
    ranges = runner._speech_ranges(loudness, {"heatmap": []}, cfg)
    assert ranges, "a three-hour video is not transcribed whole"
    assert sum(e - s for s, e in ranges) < loudness.size * 0.5, "far less than the whole video"
    assert any(s <= 5015 <= e for s, e in ranges) and any(s <= 9020 <= e for s, e in ranges), \
        "the loud stretches are among the parts transcribed"
    assert all(b[0] > a[1] for a, b in zip(ranges, ranges[1:])), "stretches are merged, never overlapping"
    assert runner._speech_ranges(np.zeros(3600, dtype=np.float32), {}, cfg) is None, "an hour is transcribed whole"


def test_speech_is_transcribed_in_pieces_cut_at_quiet_moments():
    levels = np.full(2000, -20.0, dtype=np.float32)
    levels[605] = -80.0  # a pause just after the ten-minute mark
    chunks = transcribe.pieces([(0.0, 1300.0)], levels)
    assert chunks[0] == (0.0, 605.0), "the first piece ends in the pause, not mid-word"
    assert chunks[-1][1] == 1300.0 and all(a[1] == b[0] for a, b in zip(chunks, chunks[1:])), "nothing skipped"
    assert all(e - s <= transcribe.CHUNK_SEC + 2 * transcribe.QUIET_SEARCH for s, e in chunks)
    assert transcribe.pieces([(100.0, 400.0), (1000.0, 1100.0)]) == [(100.0, 400.0), (1000.0, 1100.0)]


def test_times_from_each_piece_are_seconds_of_the_whole_video(tmp_path):
    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000 * 5)

    class Word:
        word, start, end = " hi", 0.5, 0.9

    class Segment:
        text, start, end, words = " hi", 0.5, 0.9, [Word()]

    class Info:
        language = "en"

    seen = []

    class Model:
        def transcribe(self, audio, language=None, **kwargs):
            seen.append((audio.size, language))
            return [Segment()], Info()

    out = transcribe._run(Model(), wav, None, [(1.0, 3.0), (3.0, 4.0)], None)
    assert seen[0] == (32000, None) and seen[1] == (16000, "en"), "each piece is read alone; its language is kept"
    assert [w["s"] for w in out["words"]] == [1.5, 3.5], "times count from the start of the video"


def test_as_many_clips_as_found_still_has_a_limit_and_a_quality_bar():
    picked = runner.clip_settings({"max_clips": 0, "min_score": 0, "min_sec": 20, "max_sec": 60})
    assert picked["max_clips"] == runner.AUTO_CLIPS and picked["min_score"] == runner.AUTO_MIN_SCORE
    chosen = {"max_clips": 4, "min_score": 10, "min_sec": 20, "max_sec": 60}
    assert runner.clip_settings(chosen) == chosen, "a number someone chose is used as it is"


def test_a_stream_stops_at_the_number_of_clips_asked_for():
    settings = config.deep_merge(config.DEFAULT_SETTINGS, {"clip": {"max_clips": 3}})
    session = live.LiveSession({"id": "cap-job", "url": "https://youtu.be/live", "options": settings})
    assert session.cap == 3 and not session.full()
    session.clips = 3
    assert session.full(), "three clips made: the stream stops on its own"
    session._build_window([], {})  # a moment queued meanwhile is not turned into a fourth clip
    assert session.windows == 0
    endless = live.LiveSession({"id": "cap-job", "url": "https://youtu.be/live",
                                "options": config.deep_merge(settings, {"clip": {"max_clips": 0}})})
    endless.clips = 500
    assert not endless.full(), "0 means as many as the stream gives"
    shutil.rmtree(config.JOBS_DIR / "cap-job", ignore_errors=True)
