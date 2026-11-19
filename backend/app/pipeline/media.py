"""ffmpeg and yt-dlp helpers: probing, downloading, audio extraction."""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.utils import DownloadError, download_range_func

from .. import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


class Blocked(MediaError):
    """YouTube refused this computer (a bot check, a sign-in wall or no player response). Another
    computer, such as a home PC, may still be able to download the same link."""


def run(cmd: list[str], *, cwd: Path | None = None, timeout: float | None = None) -> str:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise MediaError(f"{Path(cmd[0]).name} failed: {tail or 'no error output'}")
    return proc.stdout


def probe(path: Path) -> dict[str, Any]:
    out = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ])
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    num, _, den = (stream.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3),
        "duration": float(data.get("format", {}).get("duration") or 0),
    }


def audio_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip() or 0)


def _friendly(exc: Exception) -> str:
    text = str(exc).replace("ERROR: ", "")
    proxy = config.secret("YTDLP_PROXY")
    if proxy:
        text = text.replace(proxy, "<proxy>")  # the proxy address may hold a password
    lowered = text.lower()
    if "private video" in lowered:
        return "This video is private."
    if "sign in to confirm" in lowered or "age" in lowered and "confirm" in lowered:
        return "YouTube wants a sign-in for this video (age or bot check), so it cannot be downloaded."
    if "player response" in lowered or "http error 403" in lowered or "bot" in lowered and "not a" in lowered:
        return ("YouTube is blocking this server from reading videos right now. The link is fine. "
                "Try again later, or ask whoever runs Clipper to set up a download proxy.")
    if "unavailable" in lowered:
        return "This video is unavailable."
    if "unsupported url" in lowered or "is not a valid url" in lowered:
        return "That link is not a video the downloader recognises."
    if "getaddrinfo" in lowered or "timed out" in lowered or "connection" in lowered:
        return "No network connection to YouTube. Check the internet and retry."
    return text.splitlines()[0][:300] if text else "Download failed."


COOKIE_CLIENTS = ["tv", "tv_downgraded", "web_embedded", "web_safari"]
# The clients an open-source hosted clipper (OpenShorts) measured as giving 1080p with a PO token:
# yt-dlp's own defaults plus the mobile web client, which needs the token from the helper below.
TOKEN_CLIENTS = ["default", "mweb"]


def token_helper() -> dict[str, Any]:
    """Extractor args that let yt-dlp make YouTube's proof-of-origin token with the small Node program
    baked into the server image (BGUTIL_SCRIPT_PATH). Empty when the program is not there."""
    script = os.environ.get("BGUTIL_SCRIPT_PATH", "")
    if script and Path(script).is_file():
        return {"youtubepot-bgutilscript": {"script_path": [script]}}
    return {}


def _cookie_file() -> Path | None:
    cookies = Path(config.secret("YTDLP_COOKIES_FILE") or "/etc/secrets/cookies.txt")
    return cookies if cookies.is_file() else None


def attempt_modes() -> list[str]:
    """The ways to ask YouTube, best first; a blocked answer moves on to the next one.
    token+cookies: a signed-in session and the token helper. token: the same without cookies, because
    with cookies YouTube sometimes calls a video unplayable that works anonymously. cookies: the older
    TV/embedded clients, which accept cookies without a token."""
    has_cookies, has_token = _cookie_file() is not None, bool(token_helper())
    if has_cookies and has_token:
        return ["token+cookies", "token", "cookies"]
    if has_token:
        return ["token"]
    return ["cookies", "plain"] if has_cookies else ["plain"]


def network_options(mode: str | None = None) -> dict[str, Any]:
    """Settings for servers YouTube treats as bots (see attempt_modes for `mode`). YTDLP_PROXY is a
    proxy address (a residential proxy works best). YTDLP_COOKIES_FILE, or a Render secret file named
    cookies.txt, is a Netscape-format cookies file; a copy is used because the original may be
    read-only. Nothing changes when none is set (the JavaScript runtimes below are always allowed)."""
    mode = mode or attempt_modes()[0]
    # YouTube hides its video formats behind a small JavaScript puzzle. yt-dlp solves it with Deno by
    # default; the server image has Node, so allow both. Without this the only formats found are
    # thumbnails and the download fails with "Requested format is not available".
    opts: dict[str, Any] = {"js_runtimes": {"deno": {}, "node": {}}}
    proxy = config.secret("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy
    cookies = _cookie_file()
    if cookies and mode in ("token+cookies", "cookies"):
        copy = Path(tempfile.gettempdir()) / f"clipper-cookies-{os.getpid()}.txt"
        shutil.copyfile(cookies, copy)
        opts["cookiefile"] = str(copy)
    if mode == "cookies":
        # With cookies yt-dlp may only use browser-type clients, and YouTube gives those no video files
        # without a token (only thumbnails). The TV and embedded clients accept cookies and need none.
        opts["extractor_args"] = {"youtube": {"player_client": COOKIE_CLIENTS}}
    elif mode in ("token+cookies", "token"):
        # Never skip the web page here: the token request needs it.
        opts["extractor_args"] = {**token_helper(), "youtube": {"player_client": TOKEN_CLIENTS}}
    # YTDLP_CLIENTS (comma separated) overrides the client list of every mode.
    clients = [c.strip() for c in config.secret("YTDLP_CLIENTS").split(",") if c.strip()]
    if clients:
        opts["extractor_args"] = {**opts.get("extractor_args", {}), "youtube": {"player_client": clients}}
    return opts


def _attempts(action: Callable[[dict[str, Any]], Any]) -> Any:
    """Run `action(network options)` in each mode until YouTube answers. Only an answer another mode
    may change moves on (a block, or "no format"/"unavailable", which YouTube also gives a session it
    distrusts); private, removed or no-network failures are the answer straight away."""
    modes = attempt_modes()
    for index, mode in enumerate(modes):
        try:
            return action(network_options(mode))
        except MediaError as exc:
            if index == len(modes) - 1 or not getattr(exc, "retry_other_mode", False):
                raise
            print(f"YouTube refused the '{mode}' way ({str(exc)[:80]}); trying '{modes[index + 1]}'.", flush=True)


def describe_formats(info: dict[str, Any]) -> str:
    """One line for the server log: what YouTube offered. Contains no links or secrets."""
    formats = info.get("formats") or []
    parts = [f"{f.get('format_id')}:{f.get('ext')}:{f.get('height') or f.get('format_note') or '-'}"
             f":{'v' if f.get('vcodec') not in (None, 'none') else ''}{'a' if f.get('acodec') not in (None, 'none') else ''}"
             for f in formats[:20]]
    return f"{len(formats)} formats offered" + (": " + " ".join(parts) if parts else "")


def _log_offered_formats(url: str, net: dict[str, Any]) -> None:
    """When no usable format is found, say what YouTube did offer (usually only thumbnails when its
    JavaScript puzzle could not be solved), so the cause shows in the server log."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                               "ignore_no_formats_error": True, **net}) as ydl:
            info = ydl.extract_info(url, download=False)
        print(f"yt-dlp: {describe_formats(info)}", flush=True)
    except Exception as exc:  # the diagnosis itself must never hide the real error
        print(f"yt-dlp: could not list the offered formats ({type(exc).__name__})", flush=True)


def _error(exc: Exception) -> MediaError:
    lowered = str(exc).lower()
    blocked = ("player response" in lowered or "http error 403" in lowered or "sign in to confirm" in lowered
               or ("age" in lowered and "confirm" in lowered) or ("bot" in lowered and "not a" in lowered))
    error = (Blocked if blocked else MediaError)(_friendly(exc))
    # "No format" and "unavailable" are what YouTube says to a session it distrusts, so another mode may
    # still get the video; a private video never will.
    error.retry_other_mode = blocked or "requested format is not available" in lowered or (
        "unavailable" in lowered and "private" not in lowered)
    return error


def fetch_info(url: str, *, max_height: int = 1080, net: dict[str, Any] | None = None) -> dict[str, Any]:
    if net is None:
        return _attempts(lambda options: fetch_info(url, max_height=max_height, net=options))
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": f"b[height<={max_height}]/bv*[height<={max_height}]+ba/b", **net}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        if "requested format is not available" in str(exc).lower():
            _log_offered_formats(url, net)
        raise _error(exc) from None
    thumbs = info.get("thumbnails") or []
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_handle": info.get("uploader_id") or "",
        "duration": float(info.get("duration") or 0),
        "live_status": info.get("live_status") or "not_live",
        "thumbnail": info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else ""),
        "webpage_url": info.get("webpage_url") or url,
        "heatmap": info.get("heatmap") or [],
        "category": (info.get("categories") or [""])[0],
        "tags": [str(t) for t in (info.get("tags") or [])[:8]],
        # A live stream comes either as one combined feed or as separate
        # video and audio feeds (video first).
        "stream_urls": [f["url"] for f in info.get("requested_formats") or [] if f.get("url")]
        or ([info["url"]] if info.get("url") else []),
    }


def download(url: str, dest_dir: Path, *, max_height: int = 1080,
             max_minutes: float | None = None,
             on_progress: Callable[[float], None] | None = None,
             net: dict[str, Any] | None = None) -> Path:
    if net is None:
        return _attempts(lambda options: download(url, dest_dir, max_height=max_height,
                                                  max_minutes=max_minutes, on_progress=on_progress, net=options))
    dest_dir.mkdir(parents=True, exist_ok=True)

    def hook(status: dict) -> None:
        if on_progress and status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                on_progress(min(status.get("downloaded_bytes", 0) / total, 1.0))

    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bv*+ba/b",
        "format_sort": [f"res:{max_height}", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "progress_hooks": [hook],
        "retries": 5, "fragment_retries": 5, "concurrent_fragment_downloads": 4,
        **net,
    }
    if max_minutes:
        opts["download_ranges"] = download_range_func(None, [(0, max_minutes * 60)])
        opts["force_keyframes_at_cuts"] = False
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise _error(exc) from None
    files = sorted(dest_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not files:
        raise MediaError("The download finished but no video file was produced.")
    return files[0]


def extract_audio(src: Path, wav: Path) -> Path:
    """16 kHz mono WAV: the input for both loudness analysis and transcription."""
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    return wav


@functools.lru_cache(maxsize=1)
def video_encoder() -> list[str]:
    """NVENC when the GPU supports it, otherwise x264."""
    try:
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    except (MediaError, subprocess.TimeoutExpired, FileNotFoundError):
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
