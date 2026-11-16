"""Each person's own API keys, and the tokens that connect their YouTube channel.

They are encrypted before they reach the database, with a key that is only on the server
(APP_SECRET_KEY). The database alone is useless to anyone who reads it. Nothing here is ever
returned in full: callers get a short hint (the last four characters) at most.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from . import config, db


class KeysError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def _fernet():
    from cryptography.fernet import Fernet

    material = config.secret("APP_SECRET_KEY")
    if not material:
        if config.public_url():
            raise KeysError("This server is not set up to store keys yet. The owner must set "
                            "APP_SECRET_KEY in the server settings.")
        # One computer, no server settings: make a key once and keep it beside the app's data.
        path = config.DATA / ".app_secret"
        config.DATA.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode(), encoding="utf-8")
        material = path.read_text(encoding="utf-8").strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise KeysError("A saved key could not be read. Add it again in Settings.") from None


# ---- API keys ------------------------------------------------------------------------------------
def _doc_id(user_id: str, name: str) -> str:
    return f"{user_id}:{name}"


def put(user_id: str, name: str, value: str) -> None:
    if name not in config.PUBLIC_SECRETS:
        raise KeysError("That key cannot be changed here.")
    value = value.strip()
    if not value:
        db.delete("user_keys", {"_id": _doc_id(user_id, name)})
        return
    if any(ch in value for ch in ("\r", "\n", '"', "'", " ")):
        raise KeysError("That key contains characters that are not allowed.")
    db.coll("user_keys").replace_one(
        {"_id": _doc_id(user_id, name)},
        {"_id": _doc_id(user_id, name), "user_id": user_id, "name": name, "value": encrypt(value),
         "hint": f"…{value[-4:]}" if len(value) >= 12 else ""},
        upsert=True)


def own(user_id: str | None, name: str) -> str:
    """The key this person saved, or an empty string."""
    if not user_id:
        return ""
    doc = db.coll("user_keys").find_one({"_id": _doc_id(user_id, name)})
    return decrypt(doc["value"]) if doc else ""


def effective(user_id: str | None, name: str) -> str:
    """The key to use for this person: their own, or (NVIDIA only) the one the server provides."""
    return own(user_id, name) or config.platform_key(name)


def status(user_id: str | None) -> dict[str, dict[str, Any]]:
    """What the Settings page shows: whether a key is set, whether it is the shared one, and a hint."""
    saved = {d["name"]: d for d in db.coll("user_keys").find({"user_id": user_id})} if user_id else {}
    out: dict[str, dict[str, Any]] = {}
    for name in config.PUBLIC_SECRETS:
        mine = saved.get(name)
        shared = not mine and bool(config.platform_key(name))
        out[name] = {"set": bool(mine) or shared, "hint": (mine or {}).get("hint", ""), "shared": shared}
    return out


# ---- YouTube connection (one per person) ---------------------------------------------------------
def save_youtube(user_id: str, token_json: str, channel: str = "") -> None:
    db.coll("youtube").replace_one(
        {"_id": user_id}, {"_id": user_id, "user_id": user_id, "token": encrypt(token_json),
                           "channel": channel}, upsert=True)


def load_youtube(user_id: str) -> tuple[str, str] | None:
    doc = db.coll("youtube").find_one({"_id": user_id})
    return (decrypt(doc["token"]), doc.get("channel", "")) if doc else None


def youtube_channel(user_id: str) -> str | None:
    """The connected channel's name, or None when this person has not connected one."""
    doc = db.coll("youtube").find_one({"_id": user_id}, {"channel": 1})
    return (doc.get("channel") or "Your channel") if doc else None


def delete_youtube(user_id: str) -> None:
    db.delete("youtube", {"_id": user_id})
