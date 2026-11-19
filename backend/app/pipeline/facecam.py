"""Find the creator's face in a clip, so a vertical clip can show it instead of cutting it off.

Two cases matter:
- a **webcam overlay** (a streamer's camera in a corner of gameplay): a small face that stays in the
  same place near an edge of the frame for the whole clip. The clip is then split: the webcam on top,
  the action below.
- a **speaker** filling much of the frame (podcast, talking head): the vertical crop follows the face
  instead of the centre.

Faces in the content itself (people in a game or a film) move with the camera or sit near the middle,
so they are not taken for a webcam. Detection uses OpenCV's YuNet model (assets/models, MIT licence).
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from . import media

MODEL = Path(__file__).resolve().parents[2] / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_WIDTH = 640
SAMPLES = 10
MIN_SHARE = 0.6  # the face must be found in the same place in this share of the sampled frames
CAM_MAX_WIDTH = 0.2  # a webcam face is at most this share of the frame width
SPEAKER_MIN_WIDTH = 0.12  # a speaker's face is at least this share of the frame width
EDGE_BAND = 0.3  # a webcam overlay sits within this share of the frame from an edge
CAM_SHARE = 0.38  # the webcam panel's share of a split clip's height
CAM_FACE_WIDTHS = 2.0  # the webcam crop is this many face widths wide (a typical overlay)


@functools.lru_cache(maxsize=1)
def _detector() -> Any:
    import cv2  # imported here: only rendering a vertical clip needs it

    return cv2.FaceDetectorYN.create(str(MODEL), "", (SAMPLE_WIDTH, SAMPLE_WIDTH), 0.75, 0.3, 20)


def _frames(source: Path, start: float, end: float, width: int, height: int) -> list[np.ndarray]:
    """Evenly spaced frames from [start, end], small, as BGR arrays."""
    h = max(int(round(SAMPLE_WIDTH * height / width / 2) * 2), 2)
    span = max(end - start, 0.1)
    out = []
    for i in range(SAMPLES):
        at = start + span * (i + 0.5) / SAMPLES
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(source), "-frames:v", "1",
             "-vf", f"scale={SAMPLE_WIDTH}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True, timeout=60, creationflags=media.NO_WINDOW)
        if proc.returncode == 0 and len(proc.stdout) == SAMPLE_WIDTH * h * 3:
            out.append(np.frombuffer(proc.stdout, dtype=np.uint8).reshape(h, SAMPLE_WIDTH, 3))
    return out


def detect(frames: list[np.ndarray]) -> list[list[tuple[float, float, float, float, float]]]:
    """Faces per frame as (x, y, w, h, score), each a share of the frame size."""
    found = []
    detector = _detector()
    for frame in frames:
        h, w = frame.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(frame)
        found.append([(float(f[0]) / w, float(f[1]) / h, float(f[2]) / w, float(f[3]) / h, float(f[14]))
                      for f in (faces if faces is not None else [])])
    return found


def steady_face(per_frame: list[list[tuple[float, float, float, float, float]]]) -> dict[str, Any] | None:
    """The face that stays in one place through the clip, or None. Returns its box (shares of the frame)
    and its kind: "cam" (a small face near an edge) or "speaker" (a large face anywhere)."""
    frames = len(per_frame)
    if frames == 0:
        return None
    best: tuple[int, float, list[tuple[float, float, float, float, float]]] | None = None
    for faces in per_frame:
        for face in faces:
            cx, cy, fw = face[0] + face[2] / 2, face[1] + face[3] / 2, face[2]
            matches = []
            for other in per_frame:
                near = [o for o in other
                        if abs(o[0] + o[2] / 2 - cx) <= max(0.03, 0.35 * fw)
                        and abs(o[1] + o[3] / 2 - cy) <= max(0.04, 0.35 * face[3])
                        and 0.7 <= o[2] / fw <= 1.4]
                if near:
                    matches.append(max(near, key=lambda o: o[4]))
            score = sum(m[4] for m in matches)
            if best is None or (len(matches), score) > (best[0], best[1]):
                best = (len(matches), score, matches)
    if best is None or best[0] < max(3, int(np.ceil(MIN_SHARE * frames))):
        return None
    box = np.median(np.array([m[:4] for m in best[2]]), axis=0)
    x, y, w, h = (float(v) for v in box)
    cx, cy = x + w / 2, y + h / 2
    near_edge = min(cx, 1 - cx) < EDGE_BAND or min(cy, 1 - cy) < EDGE_BAND
    if w <= CAM_MAX_WIDTH and near_edge:
        kind = "cam"
    elif w >= SPEAKER_MIN_WIDTH:
        kind = "speaker"
    else:
        return None  # a small face in the middle is part of the content, not the creator's camera
    return {"x": x, "y": y, "w": w, "h": h, "kind": kind}


def find(source: Path, start: float, end: float, width: int, height: int) -> dict[str, Any] | None:
    """The creator's face in this part of the source, or None (also when detection is not possible)."""
    try:
        return steady_face(detect(_frames(source, start, end, width, height)))
    except Exception as exc:  # a missing model or library must never stop a clip from rendering
        print(f"Face detection skipped: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return None


def _even(value: float) -> int:
    return max(int(round(value / 2) * 2), 2)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def split_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int,
                   cam_share: float = CAM_SHARE) -> dict[str, tuple[int, int, int, int] | int]:
    """Crops for the split layout: the webcam (around the face) on top, the action (the centre of the
    frame) below. Returns source-pixel boxes (w, h, x, y) and the panel heights."""
    top_h = _even(out_h * cam_share)
    bottom_h = out_h - top_h
    # Webcam crop: about two face widths wide, shaped like the top panel, face a little above centre.
    aspect = out_w / top_h
    cw = _clamp(face["w"] * src_w * CAM_FACE_WIDTHS, 32, src_w)
    ch = cw / aspect
    if ch > src_h:
        ch, cw = src_h, src_h * aspect
    fx, fy = (face["x"] + face["w"] / 2) * src_w, (face["y"] + face["h"] / 2) * src_h
    cx = _clamp(fx - cw / 2, 0, src_w - cw)
    cy = _clamp(fy - ch * 0.45, 0, src_h - ch)
    # Action crop: the middle of the frame, shaped like the bottom panel.
    gaspect = out_w / bottom_h
    gh = src_h
    gw = min(gh * gaspect, src_w)
    gh = gw / gaspect
    gx, gy = (src_w - gw) / 2, (src_h - gh) / 2
    return {"cam": (_even(cw), _even(ch), int(cx), int(cy)),
            "game": (_even(gw), _even(gh), int(gx), int(gy)),
            "top_h": top_h, "bottom_h": bottom_h}


def speaker_crop(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """A crop of the source shaped like the output, following the speaker's face sideways."""
    aspect = out_w / out_h
    ch = src_h
    cw = min(ch * aspect, src_w)
    ch = cw / aspect
    fx = (face["x"] + face["w"] / 2) * src_w
    return (_even(cw), _even(ch), int(_clamp(fx - cw / 2, 0, src_w - cw)), int((src_h - ch) / 2))
