"""Cut one moment out of the source and render it in the chosen format."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

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


OLD_DEFAULT_ZOOM = 1.35  # the zoom that cut a streamer's webcam off the side of vertical clips


def plan_layout(source: Path, info: dict[str, Any], start: float, end: float,
                width: int, height: int, fmt: dict[str, Any]) -> dict[str, Any]:
    """How to fit the source into the output: "crop", "blur", "split" (webcam on top, action below) or
    "speaker" (a crop that follows the speaker's face). "auto" looks for the creator's face first."""
    src_w, src_h = info["width"], info["height"]
    if src_h and abs(src_w / src_h - width / height) < 0.05:
        return {"mode": "crop"}  # same shape: nothing to fit
    layout = fmt.get("layout", "auto")
    if layout == "blur" and float(fmt.get("zoom", 1.0)) == OLD_DEFAULT_ZOOM:
        layout = "auto"  # settings saved before "auto" existed: use the face-aware layout
    if layout == "crop":
        return {"mode": "crop"}
    if layout == "auto" and src_w and src_h:
        face = facecam.find(source, start, end, src_w, src_h)
        if face and face["kind"] == "cam" and height > width:
            return {"mode": "split", "face": face}
        if face and face["kind"] == "speaker":
            return {"mode": "speaker", "face": face}
    zoom = 1.0 if layout == "auto" else float(fmt.get("zoom", 1.0))
    return {"mode": "blur", "zoom": zoom}


def _video_filter(src_w: int, src_h: int, width: int, height: int,
                  fmt: dict[str, Any], ass_name: str, plan: dict[str, Any] | None = None) -> str:
    fps = int(fmt.get("fps", 30))
    plan = plan or {"mode": "crop" if fmt.get("layout") == "crop" else "blur", "zoom": fmt.get("zoom", 1.0)}
    if src_h and abs(src_w / src_h - width / height) < 0.05:
        plan = {"mode": "crop"}
    if plan["mode"] == "split":
        g = facecam.split_geometry(plan["face"], src_w, src_h, width, height)
        cw, ch, cx, cy = g["cam"]
        gw, gh, gx, gy = g["game"]
        chain = (
            f"[0:v]fps={fps},split=2[cam0][game0];"
            f"[cam0]crop={cw}:{ch}:{cx}:{cy},scale={width}:{g['top_h']},setsar=1[cam];"
            f"[game0]crop={gw}:{gh}:{gx}:{gy},scale={width}:{g['bottom_h']},setsar=1[game];"
            f"[cam][game]vstack=inputs=2"
        )
    elif plan["mode"] == "speaker":
        cw, ch, cx, cy = facecam.speaker_crop(plan["face"], src_w, src_h, width, height)
        chain = f"[0:v]fps={fps},crop={cw}:{ch}:{cx}:{cy},scale={width}:{height},setsar=1"
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
                title: str = "", credit: str = "") -> dict[str, Any]:
    """Render `[start, end]` of `source`. Returns file names relative to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fonts(out_dir)
    fmt = settings["format"]
    info = media.probe(source)
    start = max(start, 0.0)
    end = min(end, info["duration"]) if info["duration"] else end
    duration = round(end - start, 3)
    if duration < 1:
        raise media.MediaError("The clip is shorter than one second.")
    width, height = output_size(fmt.get("aspect", "9:16"), int(fmt.get("quality", 1080)))

    relative = [
        {"w": w["w"], "s": w["s"] - start, "e": w["e"] - start}
        for w in words if w["e"] > start and w["s"] < end
    ]
    plan = plan_layout(source, info, start, end, width, height, fmt)
    blur_vertical = height > width and plan["mode"] == "blur"
    overlay = settings.get("overlay", {})
    ass_name, video_name, thumb_name = f"{clip_id}.ass", f"{clip_id}.mp4", f"{clip_id}.jpg"
    (out_dir / ass_name).write_text(
        captions.build_ass(
            words=relative, width=width, height=height, duration=duration,
            captions=settings["captions"],
            title=title if overlay.get("hook_title", True) else "",
            credit=credit if overlay.get("credit", True) else "",
            title_all_clip=blur_vertical,
            # Split layout: the title goes just below the webcam panel, not over the face.
            title_top=facecam.CAM_SHARE + 0.02 if plan["mode"] == "split" else None,
        ),
        encoding="utf-8",
    )
    # cwd is the clip folder so the ass filter gets a bare file name
    # (Windows drive letters and spaces break ffmpeg filter arguments).
    media.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(source), "-filter_complex",
         _video_filter(info["width"], info["height"], width, height, fmt, ass_name, plan),
         "-map", "[v]", "-map", "0:a:0?", *media.video_encoder(),
         "-af", LOUDNESS, "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-movflags", "+faststart", video_name],
        cwd=out_dir, timeout=3600,
    )
    media.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.5, duration / 2):.2f}", "-i", video_name,
         "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", thumb_name],
        cwd=out_dir, timeout=120,
    )
    return {"file": video_name, "thumb": thumb_name, "width": width, "height": height,
            "duration": duration, "layout": plan["mode"]}
