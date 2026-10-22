"""Aurex customer authentication, sessions, and browser request protection."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "aurex_session"
CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_BODY_BYTES = 16 * 1024
SESSION_TTL_SECONDS = 86400
LOGIN_WINDOW_SECONDS = 900
LOGIN_MAX_FAILURES = 5
password_hasher = PasswordHasher()


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 320 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address.")
    return email


def validate_password(value: str) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 256:
        raise ValueError("Password must be between 12 and 256 characters.")
    return value


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    return {"id": str(row["user_id"]), "email": str(row["email"])}


class AuthStore:
    """Persistent SQLite store for users, sessions, and login throttling."""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("AUREX_AUTH_DB", "./aurex-auth.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL, created_at TEXT NOT NULL,
                revoked_at INTEGER
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT PRIMARY KEY, window_started INTEGER NOT NULL,
                failures INTEGER NOT NULL
            )""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def create_user(self, email: str, password: str) -> dict[str, str]:
        normalized = normalize_email(email)
        validate_password(password)
        record = {"user_id": str(uuid4()), "email": normalized,
                  "password_hash": password_hasher.hash(password),
                  "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with self._db() as db:
                db.execute("INSERT INTO users (user_id,email,password_hash,created_at) VALUES (?,?,?,?)",
                           (record["user_id"], record["email"], record["password_hash"], record["created_at"]))
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with this email already exists.") from exc
        return {"id": record["user_id"], "email": record["email"]}

    def get_user_by_email(self, email: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM users WHERE email=?", (normalize_email(email),)).fetchone()

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT user_id,email FROM users WHERE user_id=?", (user_id,)).fetchone()

    def create_session(self, user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._db() as db:
            db.execute("INSERT INTO sessions (token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                       (hash_token(token), user_id, now + ttl_seconds, datetime.now(timezone.utc).isoformat()))
        return token

    def user_for_session(self, token: str) -> sqlite3.Row | None:
        if not token or len(token) > 256:
            return None
        with self._db() as db:
            return db.execute("""SELECT users.user_id, users.email FROM sessions
                JOIN users ON users.user_id=sessions.user_id
                WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL AND sessions.expires_at>?""",
                             (hash_token(token), int(time.time()))).fetchone()

    def revoke_session(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                           (int(time.time()), hash_token(token)))

    def login_blocked(self, key: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT * FROM login_attempts WHERE attempt_key=?", (key,)).fetchone()
        return bool(row and row["window_started"] + LOGIN_WINDOW_SECONDS > int(time.time()) and row["failures"] >= LOGIN_MAX_FAILURES)

    def record_login_failure(self, key: str) -> None:
        now = int(time.time())
        with self._db() as db:
            row = db.execute("SELECT * FROM login_attempts WHERE attempt_key=?", (key,)).fetchone()
            if not row or row["window_started"] + LOGIN_WINDOW_SECONDS <= now:
                db.execute("INSERT OR REPLACE INTO login_attempts VALUES (?,?,?)", (key, now, 1))
            else:
                db.execute("UPDATE login_attempts SET failures=failures+1 WHERE attempt_key=?", (key,))

    def clear_login_failures(self, key: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _secure_cookie() -> bool:
    validate_auth_configuration()
    configured = os.getenv("AUREX_AUTH_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().casefold() in {"1", "true", "yes"}
    return os.getenv("AUREX_ENV", "development").casefold() == "production"


def _configured_origins() -> set[str]:
    raw = os.getenv("AUREX_TRUSTED_ORIGINS", "")
    origins: set[str] = set()
    for item in raw.split(","):
        value = item.strip().rstrip("/")
        if not value:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise RuntimeError("AUREX_TRUSTED_ORIGINS contains an invalid origin.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(ord(char) < 32 or ord(char) == 127 for char in parsed.netloc)
            or "%" in parsed.netloc
        ):
            raise RuntimeError("AUREX_TRUSTED_ORIGINS contains an invalid origin.")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise RuntimeError("AUREX_TRUSTED_ORIGINS contains an invalid origin.") from exc
        origins.add(value)
    return origins


def validate_auth_configuration() -> None:
    """Fail closed for production cookie and browser-origin configuration."""
    production = os.getenv("AUREX_ENV", "development").strip().casefold() == "production"
    configured = os.getenv("AUREX_AUTH_COOKIE_SECURE")
    if production and (configured is None or configured.strip().casefold() not in {"1", "true", "yes"}):
        raise RuntimeError("Production authentication cookies must be Secure.")
    origins = _configured_origins()
    if production:
        if not origins or any(not origin.startswith("https://") for origin in origins):
            raise RuntimeError("Production requires HTTPS AUREX_TRUSTED_ORIGINS.")


def _request_origin(request: Request, header_name: str) -> str | None:
    value = request.headers.get(header_name)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or (header_name == "origin" and parsed.path not in {"", "/"})
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in parsed.netloc)
        or "%" in parsed.netloc
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def set_session_cookie(response: Response, token: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=ttl_seconds, httponly=True,
                        secure=_secure_cookie(), samesite="lax", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


class AurexSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, auth_store: AuthStore | None = None):
        super().__init__(app)
        self.auth_store = auth_store or AuthStore()

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        validate_auth_configuration()
        raw_length = request.headers.get("content-length")
        if raw_length and raw_length.isdigit() and int(raw_length) > MAX_BODY_BYTES:
            return Response("Request body too large", status_code=413)
        if request.method in CSRF_METHODS and request.url.path.startswith("/api/"):
            configured = _configured_origins()
            cookie_authenticated = bool(request.cookies.get(COOKIE_NAME))
            raw_origin = request.headers.get("origin")
            raw_referer = request.headers.get("referer")
            origin = _request_origin(request, "origin") if raw_origin else _request_origin(request, "referer")
            if (raw_origin or raw_referer) and not origin:
                return Response("Invalid Origin or Referer", status_code=403)
            if cookie_authenticated and not origin:
                return Response("Origin or Referer is required", status_code=403)
            if origin:
                same_origin = str(request.base_url).rstrip("/")
                allowed = configured or ({same_origin} if os.getenv("AUREX_ENV", "development").casefold() != "production" else set())
                if origin not in allowed:
                    return Response("Cross-origin request rejected", status_code=403)
        token = request.cookies.get(COOKIE_NAME)
        if token:
            row = self.auth_store.user_for_session(token)
            if row:
                request.state.aurex_user_id = row["user_id"]
                request.state.aurex_user = public_user(row)
        return await call_next(request)


def client_key(request: Request, email: str) -> str:
    # X-Forwarded-For is deliberately ignored unless a trusted proxy integration is added.
    host = request.client.host if request.client else "unknown"
    return f"{host}|{normalize_email(email)}"


def require_user(request: Request) -> dict[str, str]:
    user = getattr(request.state, "aurex_user", None)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user
