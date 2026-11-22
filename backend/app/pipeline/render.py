"""Cut one moment out of the source and render it in the chosen format."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

import numpy as np

from . import captions, facecam, media

ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1)}
FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
FONTS_COPY = "_fonts"
_fonts_lock = threading.Lock()
# Even out the volume to the level YouTube targets, so quiet and loud sources
# sound alike in the feed.
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


def _even(value: float) -> int:
    return int(round(value / 2) * 2)


def output_size(aspect: str, quality: int) -> tuple[int, int]:
    """`quality` is the short side: 1080 → 1080x1920 for 9:16, 1920x1080 for 16:9."""
    a, b = ASPECTS.get(aspect, ASPECTS["9:16"])
    if a <= b:
        return _even(quality), _even(quality * b / a)
    return _even(quality * a / b), _even(quality)


# A light sharpen after enlarging a webcam, so it looks clean rather than smeared.
SHARPEN = "unsharp=5:5:0.7:5:5:0.0"
# Layouts that look for the creator's face first; each falls back to the blurred layout without one.
# Nothing is drawn around a webcam window or card: no border, no shadow (the frame image stays clear).
FRAME_BORDER = 0.0
FRAME_SHADOW = 0.0
FACE_LAYOUTS = ("auto", "corner", "corner_right", "bubble", "stack", "split", "cards", "streamer", "speaker")
OLD_DEFAULT_ZOOM = 1.35  # the zoom that cut a streamer's webcam off the side of vertical clips


def plan_layout(source: Path, info: dict[str, Any], start: float, end: float,
                width: int, height: int, fmt: dict[str, Any]) -> dict[str, Any]:
    """How to fit the source into the output: "crop", "blur", "pip" (the action fills the frame, the
    webcam floats in a small rounded window top left), "stack" (webcam band on top, action below),
    "card" (the whole frame as a rounded card on a blurred background) or "speaker" (a crop that
    follows the speaker's face). "auto" looks for the creator's face first."""
    src_w, src_h = info["width"], info["height"]
    if src_h and abs(src_w / src_h - width / height) < 0.05:
        return {"mode": "crop"}  # same shape: nothing to fit
    layout = fmt.get("layout", "auto")
    if layout == "blur" and float(fmt.get("zoom", 1.0)) == OLD_DEFAULT_ZOOM:
        layout = "auto"  # settings saved before "auto" existed: use the face-aware layout
    if layout == "crop":
        return {"mode": "crop"}
    vertical = height > width
    if layout == "card" and src_w and src_h:
        return {"mode": "card", "geometry": facecam.card_geometry(src_w, src_h, width, height)}
    if layout in FACE_LAYOUTS and src_w and src_h:
        face = facecam.find(source, start, end, src_w, src_h, info.get("duration"))
        cam = face and face["kind"] == "cam" and vertical
        if layout in ("stack", "split") and cam:
            return {"mode": "stack", "face": face,
                    "geometry": facecam.stack_geometry(face, src_w, src_h, width, height, 0.5 if layout == "split" else None)}
        if layout == "streamer" and face and vertical:
            return {"mode": "streamer", "face": face,
                    "geometry": facecam.streamer_geometry(face, src_w, src_h, width, height)}
        if layout == "cards" and cam:
            return {"mode": "cards", "face": face, "geometry": facecam.cards_geometry(face, src_w, src_h, width, height)}
        if layout in ("auto", "corner", "corner_right", "bubble") and cam:
            return {"mode": "pip", "face": face, "geometry": facecam.pip_geometry(
                face, src_w, src_h, width, height, side="right" if layout == "corner_right" else "left",
                shape="circle" if layout == "bubble" else "rect")}
        if layout in ("auto", "speaker") and face and face["kind"] == "speaker":
            return {"mode": "speaker", "face": face}
    zoom = 1.0 if layout in FACE_LAYOUTS else float(fmt.get("zoom", 1.0))
    return {"mode": "blur", "zoom": zoom}


def pip_pad(g: dict[str, Any]) -> int:
    """Room around a floating window for its border and soft shadow."""
    return _even(g["pad"])


def _rounded(width: int, height: int, radius: float) -> np.ndarray:
    """Coverage (0-1) of a rounded rectangle filling width x height, with smooth (antialiased) corners."""
    y, x = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    dx = np.maximum(np.maximum(radius - x, x - (width - radius)), 0)
    dy = np.maximum(np.maximum(radius - y, y - (height - radius)), 0)
    return np.clip(radius + 0.5 - np.sqrt(dx * dx + dy * dy), 0, 1)


def soft_focus_mask(width: int, height: int) -> np.ndarray:
    """Gray image (0-255): sharp in the middle, fading to 0 towards the edges and corners. Used so the
    game behind a cut-out webcam is blurred while the people in the middle stay sharp."""
    y, x = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    r = np.sqrt(((x - width / 2) / (width / 2)) ** 2 + ((y - height * 0.46) / (height / 2)) ** 2)
    return (np.clip((1.3 - r) / 0.5, 0, 1) * 255).astype(np.uint8)


def pip_images(out_dir: Path, clip_id: str, g: dict[str, Any], soft: bool = False) -> list[str]:
    """Write the rounded window's mask and its frame (a thin light border over a soft shadow) as PNGs
    beside the clip, plus the soft-focus mask when `soft`. Returns their bare file names (ffmpeg runs
    in out_dir)."""
    import cv2  # bundled with the face detector

    pw, ph, _, _ = g["window"]
    radius, border, pad = g["radius"], g["border"], pip_pad(g)
    mask_name, frame_name = f"{clip_id}_mask.png", f"{clip_id}_frame.png"
    cv2.imwrite(str(out_dir / mask_name), (_rounded(pw, ph, radius) * 255).astype(np.uint8))

    fw, fh = pw + 2 * pad, ph + 2 * pad
    shadow = np.zeros((fh, fw), np.float32)
    drop = max(pad // 3, 1)  # the shadow falls slightly below the window
    shadow[pad + drop:pad + drop + ph, pad:pad + pw] = _rounded(pw, ph, radius)
    shadow = cv2.GaussianBlur(shadow, (0, 0), max(pad / 2.5, 1)) * FRAME_SHADOW
    edge = np.zeros((fh, fw), np.float32)
    edge[pad - border:pad + ph + border, pad - border:pad + pw + border] = _rounded(pw + 2 * border, ph + 2 * border,
                                                                                 radius + border)
    edge *= FRAME_BORDER
    alpha = edge + shadow * (1 - edge)  # the light border drawn over the dark shadow
    white = np.divide(edge, alpha, out=np.zeros_like(alpha), where=alpha > 0)
    rgba = np.dstack([white * 255] * 3 + [alpha * 255]).astype(np.uint8)
    cv2.imwrite(str(out_dir / frame_name), rgba)
    names = [mask_name, frame_name]
    if soft:
        soft_name = f"{clip_id}_soft.png"
        cv2.imwrite(str(out_dir / soft_name), soft_focus_mask(pw, ph))
        names.append(soft_name)
    return names


def stack_images(out_dir: Path, clip_id: str, g: dict[str, Any]) -> list[str]:
    import cv2

    name = f"{clip_id}_soft.png"
    cv2.imwrite(str(out_dir / name), soft_focus_mask(*g["band"]))
    return [name]


def _soft_cam(label: str, soft_input: int, out: str) -> str:
    """Filter pieces: the webcam picture `label` stays sharp in the middle and blurred at the edges."""
    return (f"[{label}]split=2[{out}s][{out}b];[{out}b]gblur=sigma=14,eq=brightness=-0.04[{out}bb];"
            f"[{soft_input}:v]format=gray[{out}soft];[{out}s]format=yuva420p[{out}sa];"
            f"[{out}sa][{out}soft]alphamerge[{out}sm];[{out}bb][{out}sm]overlay=0:0[{out}]")


def _video_filter(src_w: int, src_h: int, width: int, height: int,
                  fmt: dict[str, Any], ass_name: str, plan: dict[str, Any] | None = None) -> str:
    fps = int(fmt.get("fps", 30))
    plan = plan or {"mode": "crop" if fmt.get("layout") == "crop" else "blur", "zoom": fmt.get("zoom", 1.0)}
    if src_h and abs(src_w / src_h - width / height) < 0.05:
        plan = {"mode": "crop"}
    if plan["mode"] == "pip":
        # Inputs 1-3 are the window's rounded mask, its border-and-shadow frame and its soft-focus mask.
        g = plan["geometry"]
        cw, ch, cx, cy = g["cam"]
        gw, gh, gx, gy = g["game"]
        pw, ph, px, py = g["window"]
        pad = pip_pad(g)
        chain = (
            f"[0:v]fps={fps},split=2[cam0][game0];"
            f"[game0]crop={gw}:{gh}:{gx}:{gy},scale={width}:{height}:flags=lanczos,setsar=1[game];"
            f"[cam0]crop={cw}:{ch}:{cx}:{cy},scale={pw}:{ph}:flags=lanczos,{SHARPEN},setsar=1[camc];"
            f"{_soft_cam('camc', 3, 'camf')};"
            f"[camf]format=yuva420p[camy];[1:v]format=gray[mask];[camy][mask]alphamerge[cam];"
            f"[game][2:v]overlay={px - pad}:{py - pad}[framed];"
            f"[framed][cam]overlay={px}:{py}"
        )
    elif plan["mode"] == "stack":
        # Input 1 (stacked webcams only) is the soft-focus mask of the webcam band.
        g = plan["geometry"]
        cw, ch, cx, cy = g["cam"]
        gw, gh, gx, gy = g["game"]
        bw, bh = g["band"]
        sw, sh = g["game_size"]
        cam_scaled = f"crop={cw}:{ch}:{cx}:{cy},scale={bw}:{bh}:flags=lanczos,{SHARPEN},setsar=1"
        cam = (f"[cam0]{cam_scaled}[camc];{_soft_cam('camc', 1, 'camf')};[camf]format=yuv420p[cam];"
               if g.get("soft", True) else f"[cam0]{cam_scaled},format=yuv420p[cam];")
        line = g.get("divider", 0)
        divider = f",drawbox=x=0:y={bh - line // 2}:w=iw:h={line}:color=white:t=fill" if line else ""
        chain = (
            f"[0:v]fps={fps},split=2[cam0][game0];"
            f"[game0]crop={gw}:{gh}:{gx}:{gy},scale={sw}:{sh}:flags=lanczos,setsar=1,format=yuv420p[game];"
            f"{cam}[cam][game]vstack{divider}"
        )
    elif plan["mode"] == "cards":
        # Inputs 1-3: webcam card mask, frame, soft focus; inputs 4-5: action card mask, frame.
        g = plan["geometry"]
        cw, ch, cx, cy = g["cam"]
        gw, gh, gx, gy = g["game"]
        ccw, cch, ccx, ccy = g["cam_card"]["window"]
        gcw, gch, gcx, gcy = g["game_card"]["window"]
        pad = pip_pad(g["cam_card"])
        bw, bh = _even(width / 4), _even(height / 4)
        chain = (
            f"[0:v]fps={fps},split=3[bg0][cam0][game0];"
            f"[bg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.10,scale={width}:{height}[bg];"
            f"[cam0]crop={cw}:{ch}:{cx}:{cy},scale={ccw}:{cch}:flags=lanczos,{SHARPEN},setsar=1[camc];"
            f"{_soft_cam('camc', 3, 'camf')};"
            f"[camf]format=yuva420p[camy];[1:v]format=gray[m1];[camy][m1]alphamerge[cam];"
            f"[game0]crop={gw}:{gh}:{gx}:{gy},scale={gcw}:{gch}:flags=lanczos,setsar=1,format=yuva420p[gamec];"
            f"[4:v]format=gray[m2];[gamec][m2]alphamerge[game];"
            f"[bg][2:v]overlay={ccx - pad}:{ccy - pad}[b1];[b1][cam]overlay={ccx}:{ccy}[b2];"
            f"[b2][5:v]overlay={gcx - pad}:{gcy - pad}[b3];[b3][game]overlay={gcx}:{gcy}"
        )
    elif plan["mode"] == "streamer":
        g = plan["geometry"]
        cw, ch, cx, cy = g["crop"]
        ww, wh, wx, wy = g["window"]
        bw, bh = _even(width / 4), _even(height / 4)
        chain = (
            f"[0:v]fps={fps},split=2[bg0][fg0];"
            f"[bg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.10,scale={width}:{height}[bg];"
            f"[fg0]crop={cw}:{ch}:{cx}:{cy},scale={ww}:{wh}:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay={wx}:{wy}"
        )
    elif plan["mode"] == "card":
        # Inputs 1 and 2: the card's rounded mask and its border-and-shadow frame.
        g = plan["geometry"]
        pw, ph, px, py = g["window"]
        pad = pip_pad(g)
        bw, bh = _even(width / 4), _even(height / 4)
        chain = (
            f"[0:v]fps={fps},split=2[bg0][fg0];"
            f"[bg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.10,scale={width}:{height}[bg];"
            f"[fg0]scale={pw}:{ph}:flags=lanczos,format=yuva420p[fgc];"
            f"[1:v]format=gray[mask];[fgc][mask]alphamerge[fg];"
            f"[bg][2:v]overlay={px - pad}:{py - pad}[framed];"
            f"[framed][fg]overlay={px}:{py}"
        )
    elif plan["mode"] == "speaker":
        cw, ch, cx, cy = facecam.speaker_crop(plan["face"], src_w, src_h, width, height)
        chain = f"[0:v]fps={fps},crop={cw}:{ch}:{cx}:{cy},scale={width}:{height}:flags=lanczos,setsar=1"
    elif plan["mode"] == "crop":
        chain = (f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height}")
    else:
        zoom = min(max(float(plan.get("zoom", 1.0)), 1.0), 2.0)
        bw, bh = _even(width / 4), _even(height / 4)
        chain = (
            f"[0:v]fps={fps},split=2[bg0][fg0];"
            f"[bg0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.10,scale={width}:{height}[bg];"
            f"[fg0]scale=w={_even(width * zoom)}:h={height}:force_original_aspect_ratio=decrease,"
            f"crop=w='min(iw,{width})':h='min(ih,{height})'[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    return f"{chain},ass={ass_name}:fontsdir={FONTS_COPY},format=yuv420p[v]"


def _ensure_fonts(out_dir: Path) -> None:
    """Put the bundled caption fonts beside the clips. ffmpeg runs with the clip
    folder as its working directory, so the fonts path stays a bare name."""
    target = out_dir / FONTS_COPY
    with _fonts_lock:  # clips render in parallel and share this folder
        if FONTS_DIR.is_dir() and not target.exists():
            shutil.copytree(FONTS_DIR, target, ignore=shutil.ignore_patterns("*.txt"),
                            dirs_exist_ok=True)


def render_clip(*, source: Path, out_dir: Path, clip_id: str, start: float, end: float,
                words: list[dict[str, Any]], settings: dict[str, Any],
                title: str = "", credit: str = "", offset: float = 0.0) -> dict[str, Any]:
    """Render `[start, end]` (seconds of the original video) of `source`. `source` may be only a
    section of the video that begins `offset` seconds in. Returns file names relative to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts(out_dir)
    fmt = settings["format"]
    info = media.probe(source)
    start = max(start, offset, 0.0)
    if info["duration"]:
        end = min(end, offset + info["duration"])
    duration = round(end - start, 3)
    if duration < 1:
        raise media.MediaError("The clip is shorter than one second.")
    width, height = output_size(fmt.get("aspect", "9:16"), int(fmt.get("quality", 1080)))

    relative = [
        {"w": w["w"], "s": w["s"] - start, "e": w["e"] - start}
        for w in words if w["e"] > start and w["s"] < end
    ]
    local = start - offset  # where the clip starts inside the source file
    plan = plan_layout(source, info, local, local + duration, width, height, fmt)
    blur_vertical = height > width and plan["mode"] in ("blur", "card", "streamer")
    extra_inputs: list[str] = []
    title_top = None
    caption_center = None
    names: list[str] = []
    if plan["mode"] == "pip":
        _, ph, _, py = plan["geometry"]["window"]
        names = pip_images(out_dir, clip_id, plan["geometry"], soft=True)
        title_top = (py + ph) / height + 0.025  # the hook goes just below the webcam window
    elif plan["mode"] == "stack":
        g = plan["geometry"]
        names = stack_images(out_dir, clip_id, g) if g.get("soft", True) else []
        title_top = g["band"][1] / height + 0.025  # just below the webcam band
        if g.get("divider"):  # a split: the captions sit on the line between the halves, the hook below it
            caption_center = g["band"][1] / height
            title_top = caption_center + 0.06
    elif plan["mode"] == "card":
        names = pip_images(out_dir, clip_id, plan["geometry"])
    elif plan["mode"] == "cards":
        g = plan["geometry"]
        names = pip_images(out_dir, f"{clip_id}c", g["cam_card"], soft=True) + pip_images(out_dir, f"{clip_id}g", g["game_card"])
        _, cam_h, _, cam_y = g["cam_card"]["window"]
        title_top = (cam_y + cam_h) / height + 0.005
    for name in names:
        extra_inputs += ["-i", name]
    overlay = settings.get("overlay", {})
    ass_name, video_name, thumb_name = f"{clip_id}.ass", f"{clip_id}.mp4", f"{clip_id}.jpg"
    (out_dir / ass_name).write_text(
        captions.build_ass(
            words=relative, width=width, height=height, duration=duration,
            captions=settings["captions"],
            title=title if overlay.get("hook_title", True) else "",
            credit=credit if overlay.get("credit", True) else "",
            title_all_clip=blur_vertical,
            caption_center=caption_center,
            title_top=title_top,  # picture in picture: below the webcam window, not over the face
        ),
        encoding="utf-8",
    )
    # cwd is the clip folder so the ass filter gets a bare file name
    # (Windows drive letters and spaces break ffmpeg filter arguments).
    try:
        media.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{local:.3f}", "-t", f"{duration:.3f}",
             "-i", str(source), *extra_inputs, "-filter_complex",
             _video_filter(info["width"], info["height"], width, height, fmt, ass_name, plan),
             "-map", "[v]", "-map", "0:a:0?", *media.video_encoder(),
             "-af", LOUDNESS, "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-movflags", "+faststart", video_name],
            cwd=out_dir, timeout=3600,
        )
    finally:
        for name in names:
            (out_dir / name).unlink(missing_ok=True)
    media.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.5, duration / 2):.2f}", "-i", video_name,
         "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", thumb_name],
        cwd=out_dir, timeout=120,
    )
    return {"file": video_name, "thumb": thumb_name, "width": width, "height": height,
            "duration": duration, "layout": plan["mode"]}
