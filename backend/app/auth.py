"""Google accounts and opaque browser sessions.

Google's stable ``sub`` claim identifies an account. Session tokens are sent
only in an HttpOnly cookie; the database stores a SHA-256 digest, never the
token itself.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

from . import db

SESSION_COOKIE = "clipper_session"
SESSION_DAYS = 30
ACTIVE_SECONDS = 15 * 60
OAUTH_STATE_SECONDS = 10 * 60
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
_log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Authentication error safe to show to the user."""


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row[key] for key in (
        "id", "email", "name", "avatar_url", "is_admin", "created_at", "last_login_at"
    ) if key in row}
    out["is_admin"] = bool(out.get("is_admin"))
    return out


def upsert_google_user(claims: dict[str, Any]) -> dict[str, Any]:
    """Create or refresh a local account from a verified Google ID token."""
    subject = str(claims.get("sub") or "").strip()
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") in (True, "true", "True", 1)
    name = " ".join(str(claims.get("name") or email.split("@", 1)[0]).strip().split())[:80]
    avatar = str(claims.get("picture") or "")[:1000]
    if not subject or not email or not verified:
        raise AuthError("Google did not return a verified email address.")

    now = time.time()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (subject,)).fetchone()
            if row is None:
                by_email = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if by_email is not None and by_email["google_sub"] not in (None, "", subject):
                    raise AuthError("That email is already linked to another Google account.")
                row = by_email
            if row is None:
                first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                user_id = db.new_id()
                conn.execute(
                    "INSERT INTO users (id, email, name, password_hash, google_sub, avatar_url, "
                    "is_admin, created_at, last_login_at) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)",
                    (user_id, email, name, subject, avatar, int(first), now, now),
                )
            else:
                user_id = row["id"]
                conn.execute(
                    "UPDATE users SET email = ?, name = ?, google_sub = ?, avatar_url = ?, "
                    "last_login_at = ? WHERE id = ?",
                    (email, name, subject, avatar, now, user_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return public_user(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def google_authorization_url(client_config: dict[str, Any], redirect_uri: str) -> str:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        client_config, scopes=GOOGLE_SCOPES, redirect_uri=redirect_uri,
        autogenerate_code_verifier=True,
    )
    # Login needs identity only. Do not merge permissions previously granted to
    # this OAuth client (for example YouTube scopes), because oauthlib correctly
    # rejects a token whose returned scope differs from the requested scope.
    url, state = flow.authorization_url(
        access_type="online", prompt="select_account",
    )
    now = time.time()
    db.insert("oauth_states", {
        "state_hash": _digest(state), "code_verifier": flow.code_verifier,
        "redirect_uri": redirect_uri, "created_at": now, "expires_at": now + OAUTH_STATE_SECONDS,
    })
    return url


def _consume_oauth_state(state: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM oauth_states WHERE state_hash = ?", (_digest(state),))
    db.execute("DELETE FROM oauth_states WHERE state_hash = ?", (_digest(state),))
    if not row or float(row["expires_at"]) <= time.time():
        raise AuthError("The Google sign-in expired. Start again.")
    return row


def google_claims(client_config: dict[str, Any], state: str, code: str) -> dict[str, Any]:
    """Exchange Google's one-time code and verify the returned identity token."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token
    from google_auth_oauthlib.flow import Flow

    stored = _consume_oauth_state(state)
    flow = Flow.from_client_config(
        client_config, scopes=GOOGLE_SCOPES, state=state,
        redirect_uri=stored["redirect_uri"], code_verifier=stored["code_verifier"],
    )
    try:
        flow.fetch_token(code=code)
        token = flow.credentials.id_token
        if not token:
            raise AuthError("Google did not return an identity token.")
        return id_token.verify_oauth2_token(
            token, GoogleRequest(), audience=flow.client_config["client_id"])
    except AuthError:
        raise
    except Exception as exc:
        # Record only the exception class. OAuth exception messages can contain
        # request details, so they must not be copied into logs or responses.
        _log.warning("Google login verification failed (%s)", type(exc).__name__)
        raise AuthError("Google sign-in could not be verified. Try again.") from None


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.insert("sessions", {
        "token_hash": _digest(token), "user_id": user_id,
        "created_at": now, "last_seen_at": now, "expires_at": now + SESSION_DAYS * 86400,
    })
    return token


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = time.time()
    row = db.one(
        "SELECT u.*, s.last_seen_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?", (_digest(token), now))
    if not row:
        return None
    if now - float(row["last_seen_at"]) > 60:
        db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, _digest(token)))
    return public_user(row)


def delete_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def stats() -> dict[str, int]:
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    db.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))
    total = db.one("SELECT COUNT(*) AS n FROM users")["n"]
    active = db.one(
        "SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE expires_at > ? AND last_seen_at >= ?",
        (now, now - ACTIVE_SECONDS),
    )["n"]
    return {"total_users": total, "active_users": active}


def list_users() -> list[dict[str, Any]]:
    cutoff = time.time() - ACTIVE_SECONDS
    return db.query(
        "SELECT u.id, u.email, u.name, u.avatar_url, u.is_admin, u.created_at, u.last_login_at, "
        "MAX(s.last_seen_at) AS last_seen_at, "
        "CASE WHEN MAX(s.last_seen_at) >= ? THEN 1 ELSE 0 END AS active "
        "FROM users u LEFT JOIN sessions s ON s.user_id = u.id AND s.expires_at > ? "
        "GROUP BY u.id ORDER BY u.created_at ASC",
        (cutoff, time.time()),
    )
