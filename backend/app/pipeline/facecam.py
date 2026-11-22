"""Find the creator's face in a clip, so a vertical clip can show it instead of cutting it off.

Two cases matter:
- a **webcam overlay** (a streamer's camera in a corner of gameplay): a small face that stays in the
  same place near an edge of the frame for the whole clip. The action then fills the vertical clip and
  the webcam floats in a rounded window in the top-left corner (picture in picture).
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
MAX_UPSCALE = 3.0  # a small webcam is enlarged at most this much, not blown up into blocks
# The webcam window of the picture-in-picture layout, as shares of the output frame.
PIP_WIDTH = 0.252  # width of the window (40% smaller than the first version, 0.42)
PIP_MAX_HEIGHT = 0.168  # the window is never taller than this
PIP_ASPECT = (0.75, 16 / 9)  # the window keeps the webcam's shape, between portrait 3:4 and 16:9
PIP_MARGIN = 0.04  # gap to the left edge
PIP_TOP = 0.045  # gap to the top edge, below the source credit line
PIP_RADIUS = 0.03  # corner radius
PIP_BORDER = 0.004  # thin light border around the window
STACK_MAX_HEIGHT = 0.4  # the webcam band of the stacked layout is at most this share of the height
STACK_ASPECT = (1.2, 16 / 9)  # the band's shape: landscape, between 6:5 and 16:9
PIP_FACE_WIDTHS = 4.0  # the window shows at most this many face widths across (head and shoulders)
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


def _action_crop(src_w: int, src_h: int, out_w: int, out_h: int, cam: tuple[float, float]) -> tuple[float, float, float, float]:
    """The part of the source that fills the whole output: the middle, moved sideways off the webcam
    when the middle would show it a second time behind the webcam window. (w, h, x, y) in pixels."""
    aspect = out_w / out_h
    gw = min(src_h * aspect, src_w)
    gh = gw / aspect
    lo, hi = cam
    middle = (src_w - gw) / 2
    options = [middle, hi, lo - gw]  # centred, just right of the webcam, just left of it
    clear = [x for x in options if 0 <= x <= src_w - gw and (x >= hi or x + gw <= lo)]
    gx = min(clear, key=lambda x: abs(x - middle)) if clear else middle
    return gw, gh, gx, (src_h - gh) / 2


def _cam_crop(face: dict[str, Any], src_w: int, src_h: int, aspect_range: tuple[float, float],
              face_widths: float = PIP_FACE_WIDTHS) -> tuple[float, float, float, float, float, float, float]:
    """The crop of the webcam to show, keeping its own shape (clamped to `aspect_range`) and the face
    inside it, never leaving the webcam box so no game shows in it. Returns
    (cw, ch, cx, cy, aspect, box_x, box_width) in source pixels."""
    box = face.get("box") or {"x": face["x"] - face["w"], "y": face["y"] - 0.6 * face["h"],
                              "w": face["w"] * 3, "h": face["h"] * 2.4}
    bx, by = _clamp(box["x"], 0, 1) * src_w, _clamp(box["y"], 0, 1) * src_h
    bw = max(min(box["w"] * src_w, src_w - bx), 2)
    bh = max(min(box["h"] * src_h, src_h - by), 2)
    aspect = _clamp(bw / bh, *aspect_range)
    # Fit the window's shape inside the webcam box, keeping the face in view.
    cw, ch = (bh * aspect, bh) if bw / bh > aspect else (bw, bw / aspect)
    # A cut-out webcam (no frame around it) gives a box far larger than the person: frame the person.
    most = face["w"] * src_w * face_widths
    if cw > most:
        cw, ch = most, most / aspect
    fx, fy = (face["x"] + face["w"] / 2) * src_w, (face["y"] + face["h"] / 2) * src_h
    cx = _clamp(fx - cw / 2, bx, bx + bw - cw)
    cy = _clamp(fy - ch * 0.4, by, by + bh - ch)
    return cw, ch, cx, cy, aspect, bx, bw


def _pad(out_w: int) -> int:
    """Room around a floating window for its border and soft shadow."""
    return max(int(out_w * 0.04), 6)


def pip_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int,
                 side: str = "left", shape: str = "rect") -> dict[str, Any]:
    """The picture-in-picture layout: the action fills the frame and the whole webcam floats in a
    small window in the top corner (`side` "left" or "right"), like the self view of a video call.
    `shape` "circle" makes the window a round bubble.

    The window keeps the webcam's own shape (between portrait 3:4 and 16:9), is PIP_WIDTH of the
    output wide and at most PIP_MAX_HEIGHT tall, and never enlarges the webcam more than MAX_UPSCALE
    times. Boxes are pixels (w, h, x, y): "cam" and "game" in the source, "window" in the output."""
    circle = shape == "circle"
    cw, ch, cx, cy, aspect, bx, bw = _cam_crop(face, src_w, src_h, (1.0, 1.0) if circle else PIP_ASPECT)
    pw = min(out_w * PIP_WIDTH * (0.85 if circle else 1.0), out_h * PIP_MAX_HEIGHT * aspect, cw * MAX_UPSCALE)
    pw, ph = _even(pw), _even(pw / aspect)
    margin = _even(out_w * PIP_MARGIN)
    top = _even(out_h * PIP_TOP)
    gw, gh, gx, gy = _action_crop(src_w, src_h, out_w, out_h, (bx, bx + bw))
    left = out_w - pw - margin if side == "right" else margin
    return {"cam": (_even(cw), _even(ch), int(cx), int(cy)),
            "game": (_even(gw), _even(gh), int(gx), int(gy)),
            "window": (pw, ph, left, top), "pad": _pad(out_w),
            "radius": pw // 2 if circle else max(int(out_w * PIP_RADIUS), 4),
            "border": max(int(out_w * PIP_BORDER), 2)}


def stack_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int,
                   share: float | None = None) -> dict[str, Any]:
    """The stacked layout: the webcam fills a band across the top, the action fills the rest.
    Boxes are pixels (w, h, x, y); "band" is the webcam band's (width, height) in the output."""
    # "split" (share given) frames the person large, like a close-up, and keeps the picture clean.
    cw, ch, cx, cy, aspect, bx, bw = _cam_crop(face, src_w, src_h, STACK_ASPECT, 3.2 if share else PIP_FACE_WIDTHS)
    band_h = _even(out_h * share) if share else _even(min(out_w / aspect, out_h * STACK_MAX_HEIGHT))
    # The crop was shaped for `aspect`; reshape it to the band so nothing is stretched.
    band_aspect = out_w / band_h
    if cw / ch > band_aspect:
        cw = ch * band_aspect
    else:
        ch = cw / band_aspect
    cx = _clamp(cx, 0, src_w - cw)
    cy = _clamp(cy, 0, src_h - ch)
    game_h = out_h - band_h
    gw, gh, gx, gy = _action_crop(src_w, src_h, out_w, game_h, (bx, bx + bw))
    return {"cam": (_even(cw), _even(ch), int(cx), int(cy)),
            "game": (_even(gw), _even(gh), int(gx), int(gy)),
            "band": (out_w, band_h), "game_size": (out_w, game_h),
            "soft": not share,  # the blurred edge is for stacked webcams; a split is left clean
            "divider": max(int(out_w * 0.006), 2) if share else 0}  # a thin white line between the halves


def card_geometry(src_w: int, src_h: int, out_w: int, out_h: int) -> dict[str, Any]:
    """The card layout: the whole source as a rounded card across the width, centred on a blurred copy."""
    pw = _even(out_w * 0.94)
    ph = _even(pw * src_h / src_w)
    return {"window": (pw, ph, (out_w - pw) // 2, _even((out_h - ph) / 2)),
            "pad": _pad(out_w), "radius": max(int(out_w * 0.03), 4), "border": max(int(out_w * PIP_BORDER), 2)}


def cards_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> dict[str, Any]:
    """Two rounded cards on a blurred background: the webcam on top, the action below it.
    Each is {"window": (w, h, x, y), ...} in the output; "cam" and "game" are source crops (w, h, x, y)."""
    cw, ch, cx, cy, aspect, bx, bw = _cam_crop(face, src_w, src_h, STACK_ASPECT)
    card_w = _even(out_w * 0.92)
    x = (out_w - card_w) // 2
    top = _even(out_h * 0.06)
    cam_h = _even(min(card_w / aspect, out_h * 0.3))
    gap = _even(out_h * 0.02)
    game_h = _even(out_h * 0.94 - top - cam_h - gap)
    gw, gh, gx, gy = _action_crop(src_w, src_h, card_w, game_h, (bx, bx + bw))
    # reshape the webcam crop to the card so nothing is stretched
    card_aspect = card_w / cam_h
    if cw / ch > card_aspect:
        cw = ch * card_aspect
    else:
        ch = cw / card_aspect
    cx, cy = _clamp(cx, 0, src_w - cw), _clamp(cy, 0, src_h - ch)
    common = {"pad": _pad(out_w), "radius": max(int(out_w * 0.03), 4), "border": max(int(out_w * PIP_BORDER), 2)}
    return {"cam": (_even(cw), _even(ch), int(cx), int(cy)), "game": (_even(gw), _even(gh), int(gx), int(gy)),
            "cam_card": {"window": (card_w, cam_h, x, top), **common},
            "game_card": {"window": (card_w, game_h, x, top + cam_h + gap), **common}}


STREAMER_ASPECT = 1.3  # the shape of the picture in the streamer layout (width / height)
STREAMER_TOP = 0.14  # gap above the picture, as a share of the output height (room for the hook title)


def streamer_geometry(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> dict[str, Any]:
    """The streamer-in-view layout: a crop of the source, a little wider than tall, that keeps the
    creator in the picture exactly as they appear in the video (a cut-out streamer stays cut out, with
    no window around them). It is shown full width over a blurred copy, with room below for captions.
    "crop" is (w, h, x, y) in the source, "window" (w, h, x, y) in the output."""
    cw = min(float(src_w), src_h * STREAMER_ASPECT)
    ch = min(float(src_h), cw / STREAMER_ASPECT)
    cw = min(cw, ch * STREAMER_ASPECT)
    fx = face["x"] + face["w"] / 2
    x = 0.0 if fx < 0.5 else src_w - cw  # start from the side the creator sits on
    # the whole face (and some shoulder room) must stay inside the crop
    left = max((face["x"] - face["w"]) * src_w, 0.0)
    right = min((face["x"] + 2 * face["w"]) * src_w, float(src_w))
    x = _clamp(x, max(right - cw, 0), min(left, src_w - cw)) if right - cw <= min(left, src_w - cw) else x
    y = _clamp((face["y"] + face["h"] / 2) * src_h - ch / 2, 0, src_h - ch)
    ww, wh = out_w, _even(out_w * ch / cw)
    return {"crop": (_even(cw), _even(ch), int(x), int(y)), "window": (ww, wh, 0, _even(out_h * STREAMER_TOP))}


def speaker_crop(face: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """A crop of the source shaped like the output, following the speaker's face sideways."""
    aspect = out_w / out_h
    ch = src_h
    cw = min(ch * aspect, src_w)
    ch = cw / aspect
    fx = (face["x"] + face["w"] / 2) * src_w
    return (_even(cw), _even(ch), int(_clamp(fx - cw / 2, 0, src_w - cw)), int((src_h - ch) / 2))
