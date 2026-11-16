"""MongoDB storage.

Everything the app remembers lives in one MongoDB database, reached through MONGODB_URI: jobs,
clips, posts, accounts, sessions, each person's settings, keys and YouTube connection. Nothing is
kept on the server's own disk, so the server can be replaced at any time. Finished clips and
downloaded videos are in the S3-compatible storage (see storage.py); here is only their address.

Every document has a string `id` (also its `_id`). The API and the worker are separate processes
that share this database.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from . import config


class DatabaseError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


_client: Any = None
_lock = threading.Lock()


def _make_client() -> Any:
    if os.environ.get("CLIPPER_MONGO_MOCK") == "1":  # tests: an in-memory stand-in, no server
        import mongomock

        return mongomock.MongoClient()
    uri = config.mongodb_uri()
    if not uri:
        raise DatabaseError("The database is not set up. Add MONGODB_URI to backend/.env "
                            "(or to the server's environment) and restart.")
    from pymongo import MongoClient

    return MongoClient(uri, serverSelectionTimeoutMS=10000, appname="clipper")


def database() -> Any:
    global _client
    with _lock:
        if _client is None:
            _client = _make_client()
    return _client[config.mongodb_name()]


def reset_connection() -> None:
    """Forget the connection (tests, and a changed MONGODB_URI)."""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


def coll(name: str) -> Any:
    return database()[name]


def init() -> None:
    """Create the indexes the app relies on. Safe to run on every start."""
    d = database()
    d.jobs.create_index([("user_id", 1), ("created_at", -1)])
    d.jobs.create_index([("status", 1), ("created_at", 1)])
    d.clips.create_index([("job_id", 1)])
    d.clips.create_index([("user_id", 1), ("created_at", -1)])
    d.posts.create_index([("user_id", 1), ("created_at", -1)])
    d.users.create_index([("email", 1)], unique=True)
    d.users.create_index([("google_sub", 1)], unique=True, sparse=True)
    d.sessions.create_index([("token_hash", 1)], unique=True)
    d.sessions.create_index([("user_id", 1)])
    d.oauth_states.create_index([("state_hash", 1)], unique=True)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is not None:
        doc.pop("_id", None)
    return doc


def find(name: str, filt: dict[str, Any] | None = None, *, sort: list[tuple[str, int]] | None = None,
         limit: int = 0, fields: list[str] | None = None) -> list[dict[str, Any]]:
    cursor = coll(name).find(filt or {}, {f: 1 for f in fields} if fields else None)
    if sort:
        cursor = cursor.sort(sort)
    if limit:
        cursor = cursor.limit(limit)
    return [_clean(doc) for doc in cursor]


def find_one(name: str, filt: dict[str, Any]) -> dict[str, Any] | None:
    return _clean(coll(name).find_one(filt))


def count(name: str, filt: dict[str, Any] | None = None) -> int:
    return coll(name).count_documents(filt or {})


# Fields code reads without checking they exist. A new document starts with them empty, the way a
# table row would, so `clip["error"]` is None rather than missing.
COLUMNS = {
    "jobs": ("url", "step", "progress", "title", "channel", "duration", "thumbnail", "error", "options",
             "payload", "files", "user_id"),
    "clips": ("user_id", "start", "end", "title", "hook", "description", "hashtags", "reason", "ai_score",
              "panel_score", "panel", "file", "thumb", "width", "height", "source_title", "source_channel",
              "source_url", "options", "error"),
    "posts": ("user_id", "title", "description", "privacy", "publish_at", "youtube_id", "views", "error",
              "kind"),
}


def insert(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc.setdefault("id", new_id())
    for column in COLUMNS.get(name, ()):
        doc.setdefault(column, None)
    coll(name).insert_one({**doc, "_id": doc["id"]})
    return doc


def update(collection: str, row_id: str, /, **values: Any) -> None:
    if collection in ("jobs", "posts"):
        values.setdefault("updated_at", time.time())
    coll(collection).update_one({"_id": row_id}, {"$set": values})


def update_where(name: str, filt: dict[str, Any], values: dict[str, Any]) -> int:
    return coll(name).update_many(filt, {"$set": values}).modified_count


def delete(name: str, filt: dict[str, Any]) -> int:
    return coll(name).delete_many(filt).deleted_count


def claim_job(job_id: str) -> bool:
    """Take a queued job for this worker. Only one caller can win."""
    now = time.time()
    return coll("jobs").find_one_and_update(
        {"_id": job_id, "status": "queued"}, {"$set": {"status": "running", "updated_at": now}}) is not None


# ---- small key/value store (settings, worker heartbeat, trends) ---------------------------------
def kv_get(key: str, default: Any = None) -> Any:
    doc = coll("kv").find_one({"_id": key})
    return doc["value"] if doc else default


def kv_set(key: str, value: Any) -> None:
    coll("kv").replace_one({"_id": key}, {"_id": key, "value": value}, upsert=True)


def kv_delete(key: str) -> None:
    coll("kv").delete_one({"_id": key})


# ---- settings -----------------------------------------------------------------------------------
# App-wide defaults come from config.DEFAULT_SETTINGS. Each person's own choices are saved under
# their id and layered on top. Storage is server configuration and is never per person.
USER_SETTINGS = ("clip", "format", "captions", "overlay", "live", "ai", "posting")


def get_settings() -> dict[str, Any]:
    return config.deep_merge(config.DEFAULT_SETTINGS, kv_get("settings", {}))


def settings_for(user_id: str | None) -> dict[str, Any]:
    base = get_settings()
    if not user_id:
        return base
    return config.deep_merge(base, kv_get(f"prefs:{user_id}", {}) or {})


def save_prefs(user_id: str, patch: dict[str, Any]) -> None:
    kv_set(f"prefs:{user_id}", config.deep_merge(kv_get(f"prefs:{user_id}", {}) or {}, patch))
