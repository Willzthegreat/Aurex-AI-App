"""Small server-side Aurex session adapter.

This is intentionally not a login system. The existing Aurex login handler
should issue the signed token with ``issue_session_token`` and set it as an
HttpOnly, Secure cookie. The middleware then verifies that cookie and places
the authenticated identity in ``request.state.aurex_user_id``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

COOKIE_NAME = "aurex_session"


def _secret() -> bytes:
    value = os.getenv("AUREX_AUTH_SESSION_SECRET", "").strip()
    if len(value) < 32:
        raise RuntimeError("AUREX_AUTH_SESSION_SECRET must be at least 32 characters.")
    return value.encode("utf-8")


def issue_session_token(user_id: str, ttl_seconds: int = 86400) -> str:
    """Create a token for an already-authenticated Aurex user."""
    if not user_id or "." in user_id or ttl_seconds < 60:
        raise ValueError("Invalid session identity or lifetime")
    payload = f"{user_id}.{int(time.time()) + ttl_seconds}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_session_token(token: str) -> str | None:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        user_id, expiry = base64.urlsafe_b64decode(encoded + padding).decode().split(".", 1)
        if not user_id or int(expiry) <= int(time.time()):
            return None
        return user_id
    except (ValueError, TypeError, UnicodeError, RuntimeError):
        return None


class AurexSessionMiddleware(BaseHTTPMiddleware):
    """Populate request.state.aurex_user_id from a signed HttpOnly session cookie."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            user_id = verify_session_token(token)
            if user_id:
                request.state.aurex_user_id = user_id
        return await call_next(request)


def set_session_cookie(response: Response, user_id: str, ttl_seconds: int = 86400) -> None:
    response.set_cookie(COOKIE_NAME, issue_session_token(user_id, ttl_seconds), max_age=ttl_seconds,
                       httponly=True, secure=True, samesite="lax", path="/")
