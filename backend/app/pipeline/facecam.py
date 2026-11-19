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
SAMPLES = 14
MIN_SHARE = 0.6  # the face must be found in the same place in this share of the sampled frames
CAM_MAX_WIDTH = 0.2  # a webcam face is at most this share of the frame width
SPEAKER_MIN_WIDTH = 0.12  # a speaker's face is at least this share of the frame width
EDGE_BAND = 0.3  # a webcam overlay sits within this share of the frame from an edge
CAM_MIN_SHARE, CAM_MAX_SHARE = 0.28, 0.45  # the webcam panel's share of a split clip's height
MAX_UPSCALE = 3.0  # a webcam smaller than out_w / this is shown smaller, not blown up into blocks
STILL = 6.0  # brightness change (0-255) below which a pixel counts as still across the sampled frames


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


def _first_moving(still: np.ndarray, start: int, stop: int, step: int) -> int | None:
    """Scanning from `start` towards `stop`, the first line where moving content begins after a still
    stretch: inside a webcam the room is still, outside it the game or video moves."""
    seen_still, run = False, 0
    for pos in range(start, stop, step):
        share = float(still[pos].mean()) if 0 <= pos < still.shape[0] else 0.0
        if share >= 0.6:
            seen_still, run = True, 0
        elif share < 0.25 and seen_still:
            run += 1
            if run >= 3:
                return pos - step * 2
        else:
            run = 0
    edge = -1 if step < 0 else still.shape[0]
    if seen_still and stop == edge:  # still up to the frame edge (a noisy line or two allowed): the webcam touches it
        return 0 if step < 0 else still.shape[0]
    return None


def webcam_box(frames: list[np.ndarray], face: dict[str, Any]) -> dict[str, float]:
    """The whole webcam overlay around a webcam face, as shares of the frame. A side whose edge cannot
    be found (the room moves, or the game is still) gets a typical overlay size instead."""
    gray = np.array([f.mean(axis=2) for f in frames], dtype=np.float32)
    still = gray.std(axis=0) < STILL
    h, w = still.shape
    x0, y0 = face["x"] * w, face["y"] * h
    fw, fh = face["w"] * w, face["h"] * h
    x1, y1 = x0 + fw, y0 + fh
    rows = still[int(y0):int(y1) + 1, :]  # across the face's height, for the left and right edges
    cols = still[:, int(x0):int(x1) + 1]  # across the face's width, for the top and bottom edges
    # Scan outwards from the face's own edge: the still room right beside it is what proves we are
    # inside the webcam before the moving game begins.
    left = _first_moving(rows.T, int(x0), int(max(x0 - 6 * fw, -1)), -1)
    right = _first_moving(rows.T, int(x1), int(min(x1 + 6 * fw, w)), 1)
    top = _first_moving(cols, int(y0), int(max(y0 - 3 * fh, -1)), -1)
    bottom = _first_moving(cols, int(y1), int(min(y1 + 4 * fh, h)), 1)
    bx0 = left if left is not None else max(x0 - 1.8 * fw, 0)
    bx1 = right if right is not None else min(x1 + 1.8 * fw, w)
    # A streamer's moving body can hide the bottom (or top) edge: then the measured width and the usual
    # 16:9 webcam shape give it, as long as the face stays inside.
    usual_h = (bx1 - bx0) * 9 / 16  # the sampled frames keep the source's shape, so pixels are square
    by0 = top if top is not None else (bottom - usual_h if bottom is not None else max(y0 - 0.8 * fh, 0))
    by1 = bottom if bottom is not None else (by0 + usual_h if top is not None else min(y1 + 1.6 * fh, h))
    if top is None:  # an estimated edge must still leave the face inside; a measured one is kept
        by0 = min(by0, y0 - 0.1 * fh)
    if bottom is None:
        by1 = max(by1, y1 + 0.3 * fh)
    bx0, bx1 = max(bx0, 0), min(bx1, w)
    by0, by1 = max(by0, 0), min(by1, h)
    return {"x": bx0 / w, "y": by0 / h, "w": max(bx1 - bx0, fw) / w, "h": max(by1 - by0, fh) / h}


CONTEXT_SEC = 30  # frames are sampled this far around the clip too: a webcam stays put, the game moves


def find(source: Path, start: float, end: float, width: int, height: int,
         duration: float | None = None) -> dict[str, Any] | None:
    """The creator's face in this part of the source, or None (also when detection is not possible).
    For a webcam, "box" is the whole webcam overlay. Frames come from the clip and up to CONTEXT_SEC
    around it, so a quiet stretch of game is not mistaken for part of the webcam."""
    try:
        first = max(start - CONTEXT_SEC, 0.0)
        last = min(end + CONTEXT_SEC, duration) if duration else end
        frames = _frames(source, first, max(last, end), width, height)
        face = steady_face(detect(frames))
        if face and face["kind"] == "cam":
            face["box"] = webcam_box(frames, face)
        return face
    except Exception as exc:  # a missing model or library must never stop a clip from rendering
        print(f"Face detection skipped: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return None


def _even(value: float) -> int:
    return max(int(round(value / 2) * 2), 2)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def split_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> dict[str, Any]:
    """Crops for the split layout: the whole webcam on top, the middle of the action below.

    The webcam panel takes the webcam's own shape (28-45% of the height); the crop stays inside the
    webcam so no game shows in it. A small webcam is not enlarged more than MAX_UPSCALE times: it is
    shown smaller, centred over a blurred copy of itself. Boxes are source pixels (w, h, x, y)."""
    box = face.get("box") or {"x": face["x"] - face["w"], "y": face["y"] - 0.6 * face["h"],
                              "w": face["w"] * 3, "h": face["h"] * 2.4}
    bx, by = _clamp(box["x"], 0, 1) * src_w, _clamp(box["y"], 0, 1) * src_h
    bw = min(box["w"] * src_w, src_w - bx)
    bh = min(box["h"] * src_h, src_h - by)
    top_h = _even(_clamp(out_w / max(bw / bh, 0.1), out_h * CAM_MIN_SHARE, out_h * CAM_MAX_SHARE))
    bottom_h = out_h - top_h
    # Fit the panel's shape inside the webcam box, keeping the face in view.
    aspect = out_w / top_h
    cw, ch = (bh * aspect, bh) if bw / bh > aspect else (bw, bw / aspect)
    fx, fy = (face["x"] + face["w"] / 2) * src_w, (face["y"] + face["h"] / 2) * src_h
    cx = _clamp(fx - cw / 2, bx, bx + bw - cw)
    cy = _clamp(fy - ch * 0.45, by, by + bh - ch)
    # How wide the webcam is drawn: full width, unless that would blow it up more than MAX_UPSCALE.
    draw_w = _even(min(out_w, cw * MAX_UPSCALE))
    draw_h = _even(min(top_h, draw_w / aspect))
    # Action crop: the middle of the frame, shaped like the bottom panel.
    gaspect = out_w / bottom_h
    gw = min(src_h * gaspect, src_w)
    gh = gw / gaspect
    gx, gy = (src_w - gw) / 2, (src_h - gh) / 2
    return {"cam": (_even(cw), _even(ch), int(cx), int(cy)),
            "game": (_even(gw), _even(gh), int(gx), int(gy)),
            "top_h": top_h, "bottom_h": bottom_h, "draw": (draw_w, draw_h)}


def speaker_crop(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """A crop of the source shaped like the output, following the speaker's face sideways."""
    aspect = out_w / out_h
    ch = src_h
    cw = min(ch * aspect, src_w)
    ch = cw / aspect
    fx = (face["x"] + face["w"] / 2) * src_w
    return (_even(cw), _even(ch), int(_clamp(fx - cw / 2, 0, src_w - cw)), int((src_h - ch) / 2))
