"""Google accounts and opaque browser sessions.

Google's stable ``sub`` claim identifies an account. Session tokens are sent
only in an HttpOnly cookie; the database stores a SHA-256 digest, never the
token itself. Everyone who signs in is an ordinary user with a private workspace.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

from pymongo.errors import DuplicateKeyError

from . import db

SESSION_COOKIE = "clipper_session"
SESSION_DAYS = 30
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
    return {key: row[key] for key in ("id", "email", "name", "avatar_url", "created_at", "last_login_at")
            if key in row}


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
    row = db.find_one("users", {"google_sub": subject})
    if row is None:
        by_email = db.find_one("users", {"email": email})
        if by_email is not None and by_email.get("google_sub") not in (None, "", subject):
            raise AuthError("That email is already linked to another Google account.")
        row = by_email
    if row is None:
        try:
            row = db.insert("users", {"email": email, "name": name, "google_sub": subject,
                                      "avatar_url": avatar, "created_at": now, "last_login_at": now})
        except DuplicateKeyError:  # the same person signed in twice at once
            row = db.find_one("users", {"google_sub": subject}) or db.find_one("users", {"email": email})
            if row is None:
                raise AuthError("Sign-in could not be completed. Try again.") from None
    if row.get("last_login_at") != now:
        db.update("users", row["id"], email=email, name=name, google_sub=subject, avatar_url=avatar,
                  last_login_at=now)
        row = db.find_one("users", {"id": row["id"]})
    return public_user(row)


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
    row = db.find_one("oauth_states", {"state_hash": _digest(state)})
    db.delete("oauth_states", {"state_hash": _digest(state)})
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
    session = db.find_one("sessions", {"token_hash": _digest(token), "expires_at": {"$gt": now}})
    if not session:
        return None
    user = db.find_one("users", {"id": session["user_id"]})
    if not user:
        return None
    if now - float(session["last_seen_at"]) > 60:
        db.update_where("sessions", {"token_hash": _digest(token)}, {"last_seen_at": now})
    return public_user(user)


def delete_session(token: str | None) -> None:
    if token:
        db.delete("sessions", {"token_hash": _digest(token)})


def purge_expired() -> None:
    """Drop sessions and sign-in attempts that have run out."""
    now = time.time()
    db.delete("sessions", {"expires_at": {"$lte": now}})
    db.delete("oauth_states", {"expires_at": {"$lte": now}})
