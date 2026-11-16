"""YouTube channel connection: each person connects their own channel, then posts to it.

Sign-in is a normal web redirect: the person goes to Google, allows uploads, and Google sends the
browser back to /api/youtube/callback on this app. The one-time state and PKCE verifier are kept in
the database for 10 minutes. The channel token is encrypted before it is stored (see keys.py) and
is never returned by the API.

The OAuth client ID and secret are the server's (backend/.env or the host's environment). Uploads
count against that Google project's daily quota, which is shared by everyone who uses the app.
"""
from __future__ import annotations

import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, db, keys

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
# YouTube category ids by name; a clip is posted in its source video's category.
CATEGORY_IDS = {
    "Film & Animation": "1", "Autos & Vehicles": "2", "Music": "10", "Pets & Animals": "15",
    "Sports": "17", "Travel & Events": "19", "Gaming": "20", "People & Blogs": "22",
    "Comedy": "23", "Entertainment": "24", "News & Politics": "25", "Howto & Style": "26",
    "Education": "27", "Science & Technology": "28",
}
CONNECT_SECONDS = 600


class YouTubeError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def _state_key(user_id: str) -> str:
    return f"youtube-connect:{user_id}"


def client_configured() -> bool:
    return config.google_login_oauth_client() is not None


def status(user_id: str) -> dict[str, Any]:
    pending = db.kv_get(_state_key(user_id), {}) or {}
    waiting = bool(pending.get("state")) and time.time() < pending.get("expires", 0)
    channel = keys.youtube_channel(user_id)
    return {
        "auth_url": pending.get("auth_url", "") if waiting else "",
        "client_configured": client_configured(),
        "connected": channel is not None,
        "channel": channel or "",
        "connecting": waiting,
        "error": pending.get("error", ""),
    }


def callback_uri(base: str) -> str:
    return f"{base.rstrip('/')}/api/youtube/callback"


def start_connect(user_id: str, base: str) -> str:
    """Step 1: the Google page to send this person's browser to."""
    from google_auth_oauthlib.flow import Flow

    oauth_config = config.google_login_oauth_client()
    if oauth_config is None:
        raise YouTubeError("Posting to YouTube is not set up on this server yet. "
                           "The server owner must add the Google OAuth client.")
    flow = Flow.from_client_config(oauth_config, SCOPES, redirect_uri=callback_uri(base))
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    db.kv_set(_state_key(user_id), {"auth_url": url, "state": state, "verifier": flow.code_verifier,
                                    "redirect_uri": callback_uri(base),
                                    "expires": time.time() + CONNECT_SECONDS})
    return url


def finish_connect(user_id: str, state: str, code: str) -> None:
    """Step 2: exchange Google's code for this person's channel token and save it."""
    from google_auth_oauthlib.flow import Flow

    saved = db.kv_get(_state_key(user_id), {}) or {}
    pending = bool(saved.get("state")) and time.time() < saved.get("expires", 0)
    if not (pending and state and hmac.compare_digest(str(saved["state"]), state)):
        raise YouTubeError("That sign-in link expired. Press Connect YouTube again.")
    oauth_config = config.google_login_oauth_client()
    if oauth_config is None:
        raise YouTubeError("Posting to YouTube is not set up on this server yet.")
    flow = Flow.from_client_config(oauth_config, SCOPES, state=state, redirect_uri=saved["redirect_uri"])
    flow.code_verifier = saved.get("verifier")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        db.kv_delete(_state_key(user_id))
        raise YouTubeError(f"Google did not accept the sign-in: {str(exc)[:160]}") from None
    token_json = flow.credentials.to_json()
    keys.save_youtube(user_id, token_json, "")
    channel = ""
    try:
        items = _service(user_id).channels().list(part="snippet", mine=True).execute().get("items", [])
        channel = items[0]["snippet"]["title"] if items else ""
    except Exception:
        pass
    keys.save_youtube(user_id, keys.load_youtube(user_id)[0], channel)
    db.kv_delete(_state_key(user_id))


def record_error(user_id: str, message: str) -> None:
    db.kv_set(_state_key(user_id), {"error": message})


def disconnect(user_id: str) -> None:
    keys.delete_youtube(user_id)
    db.kv_delete(_state_key(user_id))


def _credentials(user_id: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    saved = keys.load_youtube(user_id)
    if not saved:
        raise YouTubeError("Connect your YouTube channel in Settings before posting.")
    creds = Credentials.from_authorized_user_info(json.loads(saved[0]), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise YouTubeError("The YouTube sign-in expired. Connect the channel again in Settings.")
        try:
            creds.refresh(Request())
        except Exception:
            raise YouTubeError("The YouTube sign-in expired. Connect the channel again in Settings.") from None
        keys.save_youtube(user_id, creds.to_json(), saved[1])
    return creds


def _service(user_id: str):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credentials(user_id), cache_discovery=False)


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text:
        return "YouTube's daily upload limit for this app is used up. Try again tomorrow."
    if "uploadLimitExceeded" in text:
        return "YouTube says this channel has reached its upload limit for today."
    if "youtubeSignupRequired" in text:
        return "This Google account has no YouTube channel yet. Create one, then connect again."
    if "invalid_grant" in text:
        return "The YouTube sign-in expired. Connect the channel again in Settings."
    return f"YouTube rejected the upload: {text[:220]}"


def upload(user_id: str, *, file: Path, title: str, description: str, tags: list[str], privacy: str,
           publish_at: str | None, category_id: str,
           on_progress: Callable[[float], None] | None = None) -> str:
    """Upload a video to this person's channel. With `publish_at` (ISO time) YouTube publishes it then."""
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
        request = _service(user_id).videos().insert(
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


def view_counts(user_id: str, video_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    service = _service(user_id)
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        items = service.videos().list(part="statistics", id=",".join(chunk)).execute().get("items", [])
        for item in items:
            out[item["id"]] = int(item["statistics"].get("viewCount", 0))
    return out
