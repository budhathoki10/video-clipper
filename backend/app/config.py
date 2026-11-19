"""Paths, default settings and secret handling.

Secrets live only in backend/.env. They are read through `secret()` and are
never returned by the API in full: callers get `secret_status()` instead.
"""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
ENV_FILE = BACKEND / ".env"
DATA = Path(os.environ.get("CLIPPER_DATA_DIR") or ROOT / "data")
JOBS_DIR = DATA / "jobs"
TOKENS_DIR = DATA / "tokens"
DB_PATH = DATA / "clipper.db"
FRONTEND_DIST = ROOT / "frontend" / "out"

API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("CLIPPER_PORT", "8765"))

# Keys the user manages in Settings. Storage keys are server-side configuration:
# they live in backend/.env only and are never shown or editable in the app.
PUBLIC_SECRETS = ("NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")  # each person can add their own
SECRET_NAMES = PUBLIC_SECRETS + (
    "GOOGLE_LOGIN_CLIENT_ID", "GOOGLE_LOGIN_CLIENT_SECRET",
    "BLOMP_ACCESS_KEY_ID", "BLOMP_SECRET_ACCESS_KEY",
    "MONGODB_URI", "APP_SECRET_KEY",
)
PROVIDER_SECRET = {
    "nvidia": "NVIDIA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": None,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "clip": {"min_sec": 20, "max_sec": 60, "max_clips": 5, "min_score": 0,
             "context_sec": 5},  # added before and after each moment so viewers see what led up to it
    "format": {
        "aspect": "9:16",  # 9:16 | 16:9 | 1:1
        "layout": "auto",  # auto (finds the creator's face) | blur | crop
        "zoom": 1.0,  # foreground zoom for the blur layout; 1 keeps the full width (a corner webcam stays in)
        "quality": 1080,  # short side of the output, px
        "fps": 30,
        "source_quality": 1080,  # max height downloaded
    },
    "captions": {
        "enabled": True,
        "font": "Poppins ExtraBold",
        "size": "medium",  # small | medium | large
        "color": "#FFFFFF",
        "highlight": "#FFE600",
        "position": "lower",  # lower | middle
        "words_per_line": 3,
        "uppercase": True,
        "language": "auto",
    },
    "overlay": {"hook_title": True, "credit": True},
    "ai": {
        "provider": "nvidia",
        "models": {
            "nvidia": "nvidia/nemotron-3-ultra-550b-a55b",
            "anthropic": "claude-haiku-5-5",
            "gemini": "gemini-2.5-flash",
            "ollama": "llama3.2",
        },
        "panel_size": 100,
        "whisper_model": "large-v3-turbo",
    },
    "live": {
        "sensitivity": 2.2,  # lower = more clips
        "sustain_segments": 2,  # this many segments in a row must stand out; one loud noise is not a moment
        "segment_sec": 20,
        "lookback_sec": 60,
        "cooldown_sec": 90,
        "min_score": 55,
        "quality": 720,
    },
    "trends": {
        # General searches. With match_topic on, each video's own category and
        # tags are searched as well, so advice fits whatever is being clipped.
        "queries": [
            "viral shorts this week",
            "trending shorts",
            "best moments shorts",
            "funny clips shorts",
        ],
        "match_topic": True,
        "per_query": 15,
        "refresh_hours": 24,
    },
    # Finished clips go to S3-compatible cloud storage instead of this disk.
    # The keys are secrets (backend/.env); the rest is plain configuration.
    "storage": {
        "enabled": True,
        "endpoint": "https://storage.blomp.com",
        "region": "us-east-1",
        "bucket": "",
        "prefix": "clipper",
        "delete_source": True,  # remove the downloaded source once its clips are stored
    },
    "posting": {
        "privacy": "private",  # private | unlisted | public
        "hashtags": "",  # extra tags for every post; clip tags are written by the AI
        "credit": True,
    },
}

def ensure_dirs() -> None:
    for path in (DATA, JOBS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def secret(name: str) -> str:
    """Current value of a secret: process env first, then backend/.env."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    if ENV_FILE.exists():
        return (dotenv_values(ENV_FILE).get(name) or "").strip()
    return ""


def public_url() -> str:
    """Address people use when the app is hosted (for example https://clipper.onrender.com).
    Empty when it runs on one computer."""
    # Render sets RENDER_EXTERNAL_URL itself, so a Render deploy needs no extra setting.
    return (secret("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


def mongodb_uri() -> str:
    """Connection string for the database (MongoDB Atlas or any MongoDB). Never shown or logged."""
    return secret("MONGODB_URI")


def mongodb_name() -> str:
    return secret("MONGODB_DB") or "clipper"


def platform_key(name: str) -> str:
    """A key the server owner provides for everyone. Only the NVIDIA key is shared; for any other
    provider each person adds their own."""
    return secret(name) if name == "NVIDIA_API_KEY" else ""


def google_login_oauth_client() -> dict[str, Any] | None:
    """Browser login Web OAuth configuration assembled only from backend/.env."""
    client_id = secret("GOOGLE_LOGIN_CLIENT_ID")
    client_secret = secret("GOOGLE_LOGIN_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def prune_to(defaults: Any, value: Any) -> Any:
    """Keep only the settings that exist. Anything else a client sends (unknown names, operators like
    `$where`) is dropped before it is merged or saved."""
    if isinstance(defaults, dict) and isinstance(value, dict):
        return {k: prune_to(defaults[k], v) for k, v in value.items() if k in defaults}
    return value


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out
