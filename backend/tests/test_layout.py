"""Vertical clips must keep the creator's face: the webcam window (picture in picture), the speaker crop
and the full-width blurred layout."""
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


def test_pip_and_speaker_crops_stay_inside_the_frame():
    face = {"x": 0.86, "y": 0.10, "w": 0.087, "h": 0.19}
    g = facecam.pip_geometry(face, 1920, 1080, 1080, 1920)
    cw, ch, cx, cy = g["cam"]
    gw, gh, gx, gy = g["game"]
    pw, ph, px, py = g["window"]
    assert 0 <= cx and cx + cw <= 1920 and 0 <= cy and cy + ch <= 1080
    assert cx <= 0.86 * 1920 and cx + cw >= (0.86 + 0.087) * 1920, "the whole face is inside the webcam crop"
    assert 0 <= gx and gx + gw <= 1920 and gh <= 1080
    assert abs(gw / gh - 1080 / 1920) < 0.01, "the action fills the whole vertical frame"
    assert px < 1080 * 0.1 and py < 1920 * 0.1, "the webcam window sits in the top-left corner"
    assert pw <= 1080 * facecam.PIP_WIDTH + 2 and ph <= 1920 * facecam.PIP_MAX_HEIGHT + 2, "a small window, not a panel"
    assert abs(pw / ph - cw / ch) < 0.05, "the window keeps the webcam's shape, nothing stretched"
    assert all(v % 2 == 0 for v in (cw, ch, gw, gh, pw, ph)), "even sizes for the encoder"
    sw, sh, sx, sy = facecam.speaker_crop({"x": 0.05, "y": 0.2, "w": 0.2, "h": 0.4}, 1920, 1080, 1080, 1920)
    assert sx == 0 and sw <= 1920 and sh <= 1080, "a speaker near the edge is followed without leaving the frame"


def test_the_action_behind_the_window_does_not_show_the_webcam_twice():
    centre_cam = {"x": 0.47, "y": 0.05, "w": 0.06, "h": 0.12, "kind": "cam",
                  "box": {"x": 0.42, "y": 0.0, "w": 0.16, "h": 0.28}}
    gw, gh, gx, gy = facecam.pip_geometry(centre_cam, 1920, 1080, 1080, 1920)["game"]
    lo, hi = 0.42 * 1920, 0.58 * 1920
    assert gx >= hi - 1 or gx + gw <= lo + 1, "the action crop moves off a webcam that sits in the middle"


@pytest.fixture()
def wide_video(tmp_path):
    source = tmp_path / "wide.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    return source


def _settings(**fmt):
    return config.deep_merge(config.DEFAULT_SETTINGS, {"format": {"quality": 360, **fmt}})


def test_a_clip_with_a_webcam_shows_it_in_a_corner_window(wide_video, tmp_path, monkeypatch):
    monkeypatch.setattr(facecam, "find", lambda *a, **k: {"x": 0.8, "y": 0.05, "w": 0.1, "h": 0.2, "kind": "cam"})
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id="pip", start=0, end=3,
                             words=[], settings=_settings(), title="Hook", credit="Credit: x")
    assert out["layout"] == "pip"
    info = media.probe(tmp_path / "out" / out["file"])
    assert (info["width"], info["height"]) == (360, 640)
    assert info["duration"] < 3.5, "the still window images do not make the clip run on"
    assert not list((tmp_path / "out").glob("*.png")), "the window images are removed after rendering"


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


def _stream_frames(cam=(0, 150, 130, 85), count=14, size=(360, 640), seed=1):
    """Frames like a stream: moving game everywhere, a still webcam box with a moving face inside."""
    import numpy as np

    rng = np.random.default_rng(seed)
    x, y, w, h = cam
    room = rng.integers(40, 200, (h, w, 3), dtype=np.uint8)
    frames = []
    for i in range(count):
        frame = rng.integers(0, 255, (*size, 3), dtype=np.uint8)  # the game changes every frame
        frame[y:y + h, x:x + w] = room
        frame[y + 25 + i % 3:y + 55 + i % 3, x + 50:x + 75] = rng.integers(0, 255, (30, 25, 3), dtype=np.uint8)
        frames.append(frame)
    return frames


def test_the_whole_webcam_box_is_found_not_just_the_face():
    frames = _stream_frames()
    face = {"x": 50 / 640, "y": 175 / 360, "w": 25 / 640, "h": 30 / 360}
    box = facecam.webcam_box(frames, face)
    assert abs(box["x"] - 0) < 0.01 and abs(box["x"] + box["w"] - 130 / 640) < 0.02, box
    assert abs(box["y"] - 150 / 360) < 0.02 and abs(box["y"] + box["h"] - 235 / 360) < 0.03, box


def test_a_small_webcam_is_not_blown_up_into_blocks(wide_video, tmp_path, monkeypatch):
    tiny = {"x": 0.02, "y": 0.05, "w": 0.03, "h": 0.06, "kind": "cam",
            "box": {"x": 0.0, "y": 0.0, "w": 0.12, "h": 0.12}}
    g = facecam.pip_geometry(tiny, 1920, 1080, 1080, 1920)
    assert g["window"][0] <= g["cam"][0] * facecam.MAX_UPSCALE + 2, "drawn smaller, not stretched"
    cw, ch, cx, cy = g["cam"]
    assert cx + cw <= 0.12 * 1920 + 2 and cy + ch <= 0.12 * 1080 + 2, "the crop stays inside the webcam"
    monkeypatch.setattr(facecam, "find", lambda *a, **k: tiny)
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id="tiny", start=0, end=3,
                             words=[], settings=_settings(), title="Hook")
    assert out["layout"] == "pip"


def test_a_section_of_the_video_renders_like_the_whole_video(wide_video, tmp_path):
    """A clip is rendered from a downloaded section that starts `offset` seconds into the video."""
    words = [{"w": "hello", "s": 101.2, "e": 101.6}, {"w": "there", "s": 101.7, "e": 102.1}]
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id="sect", start=101, end=103,
                             words=words, settings=_settings(layout="blur"), offset=100)
    assert abs(out["duration"] - 2) < 0.05
    ass = (tmp_path / "out" / "sect.ass").read_text(encoding="utf-8")
    assert "0:00:00.20" in ass, "caption times are counted from the clip start, not the section start"


CAM_FACE = {"x": 0.04, "y": 0.1, "w": 0.04, "h": 0.07, "kind": "cam",
            "box": {"x": 0.0, "y": 0.03, "w": 0.2, "h": 0.22}}


def test_the_corner_window_is_forty_percent_smaller():
    assert abs(facecam.PIP_WIDTH - 0.42 * 0.6) < 0.001 and abs(facecam.PIP_MAX_HEIGHT - 0.28 * 0.6) < 0.001


@pytest.mark.parametrize("layout,mode", [("corner", "pip"), ("corner_right", "pip"), ("bubble", "pip"),
                                         ("stack", "stack"), ("split", "stack"), ("cards", "cards"), ("streamer", "streamer"),
                                         ("card", "card"), ("auto", "pip"), ("blur", "blur")])
def test_every_layout_style_renders(wide_video, tmp_path, monkeypatch, layout, mode):
    monkeypatch.setattr(facecam, "find", lambda *a, **k: CAM_FACE)
    out = render.render_clip(source=wide_video, out_dir=tmp_path / "out", clip_id=layout, start=0, end=2,
                             words=[], settings=_settings(layout=layout), title="Hook")
    assert out["layout"] == mode
    assert (tmp_path / "out" / out["file"]).stat().st_size > 1000
    assert not list((tmp_path / "out").glob("*.png")), "helper images are cleaned up"


def test_stack_and_corner_fall_back_to_blur_without_a_webcam(wide_video, tmp_path, monkeypatch):
    monkeypatch.setattr(facecam, "find", lambda *a, **k: None)
    for layout in ("corner", "corner_right", "bubble", "stack", "split", "cards", "streamer", "speaker"):
        plan = render.plan_layout(wide_video, media.probe(wide_video), 0, 2, 360, 640, _settings(layout=layout)["format"])
        assert plan == {"mode": "blur", "zoom": 1.0}


def test_stack_geometry_fills_the_frame_without_stretching():
    g = facecam.stack_geometry(CAM_FACE, 1920, 1080, 1080, 1920)
    bw, bh = g["band"]
    gw, gh = g["game_size"]
    assert bh + gh == 1920 and bw == gw == 1080 and bh <= 1920 * facecam.STACK_MAX_HEIGHT + 2
    cw, ch, cx, cy = g["cam"]
    assert abs(cw / ch - bw / bh) < 0.03 and cx + cw <= 1920 and cy + ch <= 1080
    assert abs(g["game"][0] / g["game"][1] - gw / gh) < 0.02


def test_corner_right_bubble_split_and_cards_geometry():
    right = facecam.pip_geometry(CAM_FACE, 1920, 1080, 1080, 1920, side="right")["window"]
    assert right[2] + right[0] <= 1080 and right[2] > 1080 / 2, "the window sits in the right corner"
    bubble = facecam.pip_geometry(CAM_FACE, 1920, 1080, 1080, 1920, shape="circle")
    assert bubble["window"][0] == bubble["window"][1] and bubble["radius"] == bubble["window"][0] // 2
    split = facecam.stack_geometry(CAM_FACE, 1920, 1080, 1080, 1920, share=0.5)
    assert split["band"][1] == 960 and split["game_size"][1] == 960
    cards = facecam.cards_geometry(CAM_FACE, 1920, 1080, 1080, 1920)
    cam, game = cards["cam_card"]["window"], cards["game_card"]["window"]
    assert cam[3] + cam[1] <= game[3] and game[3] + game[1] <= 1920, "the cards do not overlap and stay inside"
    assert abs(cards["cam"][0] / cards["cam"][1] - cam[0] / cam[1]) < 0.03


def test_the_streamer_stays_in_view_without_a_window():
    cutout = {"x": 0.86, "y": 0.62, "w": 0.09, "h": 0.2, "kind": "cam"}  # a cut-out streamer, bottom right
    g = facecam.streamer_geometry(cutout, 1920, 1080, 1080, 1920)
    cw, ch, cx, cy = g["crop"]
    assert cx + cw <= 1920 and cy + ch <= 1080
    assert cx <= 0.86 * 1920 and cx + cw >= 0.95 * 1920, "the whole streamer is inside the picture"
    ww, wh, wx, wy = g["window"]
    assert ww == 1080 and abs(ww / wh - cw / ch) < 0.02 and wy + wh < 1920 * 0.7, "full width, room for captions below"
    left = facecam.streamer_geometry({"x": 0.03, "y": 0.1, "w": 0.09, "h": 0.2, "kind": "cam"}, 1920, 1080, 1080, 1920)
    assert left["crop"][2] == 0, "a streamer on the left starts the crop at the left edge"


def test_a_slow_download_helper_is_retried_not_fatal(monkeypatch):
    """The token helper (node) timing out on a busy server must not fail the whole job."""
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    monkeypatch.setattr(media, "attempt_modes", lambda: ["direct"])
    monkeypatch.setattr(media, "network_options", lambda mode: {})
    calls = {"n": 0}

    def flaky(net):
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.TimeoutExpired(["node", "generate_once.js", "--version"], 15)
        return "ok"

    assert media._attempts(flaky) == "ok" and calls["n"] == 3

    def always(net):
        raise subprocess.TimeoutExpired(["node"], 15)

    with pytest.raises(media.MediaError, match="too busy"):
        media._attempts(always)
