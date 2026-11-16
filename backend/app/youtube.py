"""YouTube channel connection: sign-in, upload now or scheduled, view counts.

The OAuth client ID and secret come from backend/.env. The saved user token
stays in data/tokens/ (gitignored). None is ever returned by the API.
"""
from __future__ import annotations

import os
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, db

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
# YouTube category ids by name; a clip is posted in its source video's category.
CATEGORY_IDS = {
    "Film & Animation": "1", "Autos & Vehicles": "2", "Music": "10", "Pets & Animals": "15",
    "Sports": "17", "Travel & Events": "19", "Gaming": "20", "People & Blogs": "22",
    "Comedy": "23", "Entertainment": "24", "News & Politics": "25", "Howto & Style": "26",
    "Education": "27", "Science & Technology": "28",
}
TOKEN_FILE = config.TOKENS_DIR / "youtube_token.json"
_connect_lock = threading.Lock()


class YouTubeError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


_BROWSER_NAME = "clipper-sign-in"


class _OpenAndRemember(webbrowser.BaseBrowser):
    """Opens the Google page and also keeps its address, so the dashboard can
    show a link when a background process could not put the page on screen."""

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        db.kv_set("youtube", {"auth_url": url})
        try:
            if os.name == "nt":
                os.startfile(url)  # noqa: S606 - the address comes from Google's own client library
            else:
                webbrowser.open(url)
        except Exception:
            pass
        return True


def status() -> dict[str, Any]:
    state = db.kv_get("youtube", {}) or {}
    return {
        "auth_url": state.get("auth_url", "") if _connect_lock.locked() else "",
        "client_configured": client_configured(),
        "connected": TOKEN_FILE.exists(),
        "channel": state.get("channel", "") if TOKEN_FILE.exists() else "",
        "connecting": _connect_lock.locked(),
        "error": state.get("error", ""),
    }


def client_configured() -> bool:
    return config.google_oauth_client() is not None


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        raise YouTubeError("No YouTube channel is connected. Connect one in Settings.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise YouTubeError("The YouTube sign-in expired. Connect the channel again in Settings.")
        try:
            creds.refresh(Request())
        except Exception:
            raise YouTubeError("The YouTube sign-in expired. Connect the channel again in Settings.") from None
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credentials(), cache_discovery=False)


def connect_in_background() -> None:
    """Open Google's consent page in the browser and wait for the sign-in."""
    oauth_config = config.google_oauth_client()
    if oauth_config is None:
        raise YouTubeError("Google OAuth is not configured. Add GOOGLE_CLIENT_ID and "
                           "GOOGLE_CLIENT_SECRET to backend/.env.")
    if not _connect_lock.acquire(blocking=False):
        return

    def work() -> None:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            db.kv_set("youtube", {})
            webbrowser.register(_BROWSER_NAME, None, _OpenAndRemember())
            flow = InstalledAppFlow.from_client_config(oauth_config, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, browser=_BROWSER_NAME,
                                          timeout_seconds=600,
                                          authorization_prompt_message="",
                                          success_message="Channel connected. You can close this tab.")
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            channel = ""
            try:
                items = _service().channels().list(part="snippet", mine=True).execute().get("items", [])
                channel = items[0]["snippet"]["title"] if items else ""
            except Exception:
                pass
            db.kv_set("youtube", {"channel": channel})
        except Exception as exc:
            timed_out = not str(exc) or "imed out" in str(exc)
            db.kv_set("youtube", {"error": (
                "The Google sign-in was not completed within 10 minutes. Press Connect YouTube "
                "again and finish the Google page that opens." if timed_out
                else f"Sign-in did not finish: {str(exc)[:200]}")})
        finally:
            _connect_lock.release()

    threading.Thread(target=work, daemon=True, name="youtube-connect").start()


def disconnect() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    db.kv_set("youtube", {})


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text:
        return "YouTube's daily upload quota for your Google project is used up. Try again tomorrow."
    if "uploadLimitExceeded" in text:
        return "YouTube says this channel has reached its upload limit for today."
    if "youtubeSignupRequired" in text:
        return "This Google account has no YouTube channel yet. Create one, then connect again."
    if "invalid_grant" in text:
        return "The YouTube sign-in expired. Connect the channel again in Settings."
    return f"YouTube rejected the upload: {text[:220]}"


def upload(*, file: Path, title: str, description: str, tags: list[str], privacy: str,
           publish_at: str | None, category_id: str,
           on_progress: Callable[[float], None] | None = None) -> str:
    """Upload a video. With `publish_at` (ISO time) YouTube publishes it then."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if not file.exists():
        raise YouTubeError("The clip file is missing. Render the clip again.")
    video_status: dict[str, Any] = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        when = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.astimezone()
        video_status["privacyStatus"] = "private"  # required by YouTube for scheduling
        video_status["publishAt"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": tags[:15], "categoryId": category_id},
        "status": video_status,
    }
    try:
        request = _service().videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(file), chunksize=8 * 1024 * 1024, resumable=True),
        )
        response = None
        while response is None:
            progress, response = request.next_chunk(num_retries=4)
            if progress and on_progress:
                on_progress(progress.progress())
        return response["id"]
    except HttpError as exc:
        raise YouTubeError(_explain(exc)) from None


def view_counts(video_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    service = _service()
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        items = service.videos().list(part="statistics", id=",".join(chunk)).execute().get("items", [])
        for item in items:
            out[item["id"]] = int(item["statistics"].get("viewCount", 0))
    return out
