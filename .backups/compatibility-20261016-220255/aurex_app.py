"""Runnable Aurex MT5 application entry point.

The real Aurex login handler should set the signed session cookie from
``aurex_auth.set_session_cookie`` after authenticating a user. The mounted
routes then use that verified identity for connection ownership.
"""
from aurex_backend import app

__all__ = ["app"]
