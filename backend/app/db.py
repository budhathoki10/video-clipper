"""SQLite storage. One short-lived connection per call; WAL so the API and
the worker process can read and write at the same time."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from . import config

JSON_COLS = {"options", "payload", "hashtags", "panel", "value"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT, status TEXT NOT NULL,
  step TEXT, progress REAL DEFAULT 0, title TEXT, channel TEXT, duration REAL,
  thumbnail TEXT, error TEXT, options TEXT, payload TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
  start REAL, end REAL, title TEXT, hook TEXT, description TEXT, hashtags TEXT,
  reason TEXT, ai_score REAL, panel_score REAL, panel TEXT,
  file TEXT, thumb TEXT, width INTEGER, height INTEGER,
  source_title TEXT, source_channel TEXT, source_url TEXT,
  options TEXT, error TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, status TEXT NOT NULL,
  title TEXT, description TEXT, privacy TEXT, publish_at TEXT,
  youtube_id TEXT, views INTEGER, error TEXT, created_at REAL, updated_at REAL, kind TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  google_sub TEXT, avatar_url TEXT,
  created_at REAL NOT NULL, last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state_hash TEXT PRIMARY KEY, code_verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_clips_job ON clips(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_seen_at, expires_at);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Databases made before posts had a kind (short or video) get the column.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
        if "kind" not in columns:
            conn.execute("ALTER TABLE posts ADD COLUMN kind TEXT")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "google_sub" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        if "avatar_url" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub)")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in JSON_COLS & out.keys():
        if out[key] is not None:
            try:
                out[key] = json.loads(out[key])
            except (TypeError, ValueError):
                pass
    return out


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    return {
        k: json.dumps(v) if k in JSON_COLS and v is not None and not isinstance(v, str) else v
        for k, v in values.items()
    }


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [_decode(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    conn = connect()
    try:
        with conn:
            return conn.execute(sql, params).rowcount
    finally:
        conn.close()


def insert(table: str, values: dict[str, Any]) -> None:
    values = _encode(values)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))


def update(table: str, row_id: str, **values: Any) -> None:
    if table in ("jobs", "posts"):
        values.setdefault("updated_at", time.time())
    values = _encode(values)
    sets = ", ".join(f"{k} = ?" for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))


def kv_get(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_settings() -> dict[str, Any]:
    return config.deep_merge(config.DEFAULT_SETTINGS, kv_get("settings", {}))


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    merged = config.deep_merge(kv_get("settings", {}), patch)
    kv_set("settings", merged)
    return get_settings()
