"""Small in-memory authentication boundary for taxonomy mutations."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

_SESSION_COOKIE = "trader_admin_session"
_ADMIN_USERNAME = "admin"


@dataclass(frozen=True)
class AdminSession:
    session_id: str
    csrf_token: str
    expires_at: float
    last_seen_at: float


class AdminSessionStore:
    """Process-local sessions; intentionally lost when the backend restarts."""

    def __init__(self, *, password: str, session_ttl_seconds: int, login_window_seconds: int, login_max_attempts: int) -> None:
        self._password = password
        self._session_ttl_seconds = session_ttl_seconds
        self._login_window_seconds = login_window_seconds
        self._login_max_attempts = login_max_attempts
        self._sessions: dict[str, AdminSession] = {}
        self._failed_logins: dict[str, list[float]] = {}

    def login(self, *, username: str, password: str, source: str) -> AdminSession | None:
        now = time.monotonic()
        attempts = [at for at in self._failed_logins.get(source, []) if now - at < self._login_window_seconds]
        if len(attempts) >= self._login_max_attempts:
            self._failed_logins[source] = attempts
            return None
        valid_username = hmac.compare_digest(username, _ADMIN_USERNAME)
        valid_password = hmac.compare_digest(password, self._password)
        valid = valid_username and valid_password
        if not valid:
            attempts.append(now)
            self._failed_logins[source] = attempts
            return None
        self._failed_logins.pop(source, None)
        session = AdminSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + self._session_ttl_seconds,
            last_seen_at=now,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> AdminSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        now = time.monotonic()
        if session is None or session.expires_at <= now:
            self._sessions.pop(session_id, None)
            return None
        return session

    def logout(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)


def session_cookie_name() -> str:
    return _SESSION_COOKIE


def require_admin_session(*, request: Request, store: AdminSessionStore) -> AdminSession:
    session = store.get(request.cookies.get(_SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_session_required")
    return session


def require_csrf(*, request: Request, session: AdminSession, allowed_origin: str) -> None:
    origin = request.headers.get("origin") or request.headers.get("referer", "").rstrip("/")
    if not origin or origin.rstrip("/") != allowed_origin.rstrip("/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="untrusted_request_origin")
    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_token_required")
