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
PUBLIC_SECRETS = ("NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
SECRET_NAMES = PUBLIC_SECRETS + (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    "GOOGLE_LOGIN_CLIENT_ID", "GOOGLE_LOGIN_CLIENT_SECRET",
    "BLOMP_ACCESS_KEY_ID", "BLOMP_SECRET_ACCESS_KEY",
)
PROVIDER_SECRET = {
    "nvidia": "NVIDIA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": None,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "clip": {"min_sec": 20, "max_sec": 60, "max_clips": 5, "min_score": 0},
    "format": {
        "aspect": "9:16",  # 9:16 | 16:9 | 1:1
        "layout": "blur",  # blur | crop
        "zoom": 1.35,  # foreground zoom for the blur layout
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

_env_lock = threading.Lock()


def ensure_dirs() -> None:
    for path in (DATA, JOBS_DIR, TOKENS_DIR):
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
    Empty when it runs on one computer. Hosted mode closes sign-up unless ALLOWED_EMAILS is set."""
    # Render sets RENDER_EXTERNAL_URL itself, so a Render deploy needs no extra setting.
    return (secret("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


def allowed_emails() -> set[str]:
    """Emails that may sign in. Empty means no list is set."""
    raw = secret("ALLOWED_EMAILS").replace(";", ",")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def set_secret(name: str, value: str) -> None:
    """Write one secret to backend/.env, replacing the existing line."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret: {name}")
    value = value.strip()
    if any(ch in value for ch in "\r\n\"'"):
        raise ValueError("Key contains characters that are not allowed.")
    with _env_lock:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        replaced = False
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == name:
                lines[i] = f"{name}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{name}={value}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.pop(name, None)


def secret_status() -> dict[str, dict[str, Any]]:
    """Safe view of secrets for the UI: whether set, and the last 4 characters."""
    out = {}
    for name in PUBLIC_SECRETS:
        value = secret(name)
        out[name] = {"set": bool(value), "hint": f"…{value[-4:]}" if len(value) >= 12 else ""}
    return out


def google_oauth_client() -> dict[str, Any] | None:
    """YouTube Desktop OAuth configuration assembled only from backend/.env."""
    client_id = secret("GOOGLE_CLIENT_ID")
    client_secret = secret("GOOGLE_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


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


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out
