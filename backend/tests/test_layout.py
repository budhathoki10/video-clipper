"""Vertical clips must keep the creator's face: the webcam split, the speaker crop and the full-width
blurred layout."""
import subprocess

import pytest

from app import config
from app.pipeline import facecam, media, render

CAM = (0.86, 0.10, 0.087, 0.19, 0.9)  # a webcam face in the top-right corner, as found in a real stream


def _frames_with(face, count=10, every=1):
    return [[face] if i % every == 0 else [] for i in range(count)]


def test_a_webcam_in_a_corner_is_recognised():
    moving = [[(0.3 + 0.04 * i, 0.4, 0.03, 0.06, 0.8)] for i in range(10)]  # a game character
    per_frame = [a + b for a, b in zip(_frames_with(CAM), moving)]
    found = facecam.steady_face(per_frame)
    assert found and found["kind"] == "cam" and abs(found["x"] - 0.86) < 0.01, "the corner webcam, not the moving face"


def test_faces_that_are_part_of_the_content_are_not_a_webcam():
    assert facecam.steady_face(_frames_with((0.45, 0.4, 0.03, 0.06, 0.9))) is None, "a small face in the middle is content"
    assert facecam.steady_face(_frames_with(CAM, every=4)) is None, "a face seen in only a few frames is not steady"
    assert facecam.steady_face([[] for _ in range(10)]) is None
    speaker = facecam.steady_face(_frames_with((0.4, 0.2, 0.22, 0.4, 0.95)))
    assert speaker and speaker["kind"] == "speaker", "a large face anywhere is a speaker to follow"


def test_split_and_speaker_crops_stay_inside_the_frame():
    face = {"x": 0.86, "y": 0.10, "w": 0.087, "h": 0.19}
    g = facecam.split_geometry(face, 1920, 1080, 1080, 1920)
    cw, ch, cx, cy = g["cam"]
    gw, gh, gx, gy = g["game"]
    assert g["top_h"] + g["bottom_h"] == 1920
    assert 0 <= cx and cx + cw <= 1920 and 0 <= cy and cy + ch <= 1080
    assert cx <= 0.86 * 1920 and cx + cw >= (0.86 + 0.087) * 1920, "the whole face is inside the webcam crop"
    assert 0 <= gx and gx + gw <= 1920 and gh <= 1080
    assert all(v % 2 == 0 for v in (cw, ch, gw, gh)), "even sizes for the encoder"
    sw, sh, sx, sy = facecam.speaker_crop({"x": 0.05, "y": 0.2, "w": 0.2, "h": 0.4}, 1920, 1080, 1080, 1920)
    assert sx == 0 and sw <= 1920 and sh <= 1080, "a speaker near the edge is followed without leaving the frame"


@pytest.fixture()
def wide_video(tmp_path):
    source = tmp_path / "wide.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    return source


def _settings(**fmt):
    return config.deep_merge(config.DEFAULT_SETTINGS, {"format": {"quality": 360, **fmt}})


def test_a_clip_with_a_webcam_is_rendered_with_the_face_on_top(wide_video, tmp_path, monkeypatch):
    monkeypatch.setattr(facecam, "find", lambda *a, **k: {"x": 0.8, "y": 0.05, "w": 0.1, "h": 0.2, "kind": "cam"})
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id="split", start=0, end=3,
                             words=[], settings=_settings(), title="Hook", credit="Credit: x")
    assert out["layout"] == "split"
    info = media.probe(tmp_path / "out" / out["file"])
    assert (info["width"], info["height"]) == (360, 640)


def test_without_a_face_the_whole_frame_is_kept(wide_video, tmp_path, monkeypatch):
    monkeypatch.setattr(facecam, "find", lambda *a, **k: None)
    assert config.DEFAULT_SETTINGS["format"]["layout"] == "auto" and config.DEFAULT_SETTINGS["format"]["zoom"] == 1.0
    plan = render.plan_layout(wide_video, media.probe(wide_video), 0, 3, 360, 640, _settings()["format"])
    assert plan == {"mode": "blur", "zoom": 1.0}, "no side is cut off"
    old = render.plan_layout(wide_video, media.probe(wide_video), 0, 3, 360, 640,
                             _settings(layout="blur", zoom=1.35)["format"])
    assert old["mode"] == "blur" and old["zoom"] == 1.0, "settings saved with the old default zoom no longer crop"
    chosen = render.plan_layout(wide_video, media.probe(wide_video), 0, 3, 360, 640, _settings(layout="blur", zoom=1.5)["format"])
    assert chosen == {"mode": "blur", "zoom": 1.5}, "a zoom someone chose on purpose is kept"
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id="blur", start=0, end=3,
                             words=[], settings=_settings(), title="Hook")
    assert out["layout"] == "blur"


def test_face_detection_runs_on_real_frames(wide_video):
    found = facecam.find(wide_video, 0, 3, 640, 360)
    assert found is None, "the test pattern has no face; the model loads and returns nothing"
