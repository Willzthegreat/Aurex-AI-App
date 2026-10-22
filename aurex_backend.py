"""Customer-facing Aurex API for the managed MT5 connector.

The host Aurex application should mount ``app`` after its authentication
middleware has populated ``request.state.aurex_user_id``.  This module owns
the customer-to-connection mapping and never exposes connector credentials.
It deliberately has no dependency on the legacy port 8000 bridge.
"""
from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from aurex_auth import (AuthStore, AurexSessionMiddleware, client_key, clear_session_cookie,
                        normalize_email, public_user, require_user, set_session_cookie,
                        validate_password, verify_password)

SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}
STATUSES = {"PENDING_PROVISIONING", "CONNECTING", "CONNECTED", "RECONNECTING", "DISCONNECTED", "ERROR", "REVOKED"}


class ConnectionInput(BaseModel):
    provider: str = Field(pattern=r"^(EXNESS|MT5_OTHER)$")
    connection_method: str = Field(default="MT5_TERMINAL", pattern=r"^MT5_TERMINAL$")
    label: str = Field(min_length=2, max_length=80)
    account_reference: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=160)
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("login")
    @classmethod
    def numeric_login(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("MT5 login must be numeric")
        return value


class AuthInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class RegisterInput(AuthInput):
    confirm_password: str = Field(min_length=1, max_length=256)


class ConnectionStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("AUREX_CONNECTION_DB", "./aurex-connections.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                provider TEXT NOT NULL, label TEXT NOT NULL,
                account_reference TEXT NOT NULL, server_name TEXT NOT NULL,
                status TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def create(self, user_id: str, body: ConnectionInput, connection_id: str) -> dict[str, Any]:
        record = {"connection_id": connection_id, "user_id": user_id, "provider": body.provider,
                  "label": body.label, "account_reference": body.account_reference,
                  "server_name": body.server_name, "status": "PENDING_PROVISIONING",
                  "revoked": 0, "created_at": datetime.now(timezone.utc).isoformat()}
        with self._db() as db:
            db.execute("INSERT INTO connections VALUES (:connection_id,:user_id,:provider,:label,:account_reference,:server_name,:status,:revoked,:created_at)", record)
        return record

    def get_for_user(self, connection_id: str, user_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM connections WHERE connection_id=? AND user_id=?", (connection_id, user_id)).fetchone()
        return dict(row) if row else None

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM connections WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, connection_id: str, status_value: str) -> None:
        with self._db() as db:
            db.execute("UPDATE connections SET status=?, revoked=? WHERE connection_id=?", (status_value, int(status_value == "REVOKED"), connection_id))


def normalize_status(value: Any) -> str:
    raw = str(value or "").upper().strip()
    aliases = {"STARTING": "CONNECTING", "OK": "CONNECTED", "DISCONNECTED": "DISCONNECTED", "FAILED": "ERROR"}
    return aliases.get(raw, raw if raw in STATUSES else "ERROR")


def _safe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ConnectorError(502, "Connector returned invalid JSON") from exc


class ConnectorError(Exception):
    def __init__(self, http_status: int, message: str):
        self.http_status, self.message = http_status, message


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Connector redirects are not permitted")


def validate_connector_url(value: str) -> str:
    """Validate the connector origin before any bearer token is used."""
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(503, "Managed connector URL is not configured")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ConnectorError(503, "Managed connector URL is invalid") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in parsed.netloc) or "%" in parsed.netloc:
        raise ConnectorError(503, "Managed connector URL is invalid")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConnectorError(503, "Managed connector URL is invalid")
    hostname = hostname.rstrip(".").casefold()
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ConnectorError(503, "Managed connector URL is invalid") from exc
    if not ascii_hostname or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                                or not re.fullmatch(r"[a-z0-9-]+", label)
                                for label in ascii_hostname.split(".")):
        try:
            ipaddress.ip_address(ascii_hostname)
        except ValueError as exc:
            raise ConnectorError(503, "Managed connector URL is invalid") from exc
    hostname = ascii_hostname
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if parsed.scheme == "http" and not is_loopback:
        raise ConnectorError(503, "Remote managed connector URLs must use HTTPS")
    port = parsed.port
    if ":" in hostname and not hostname.startswith("["):
        authority_host = f"[{hostname}]"
    else:
        authority_host = hostname
    authority = authority_host + (f":{port}" if port is not None else "")
    return f"{parsed.scheme}://{authority}".rstrip("/")


class ConnectorClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 8.0):
        self.base_url = validate_connector_url(base_url or os.getenv("AUREX_CONNECTOR_SERVICE_URL", "http://127.0.0.1:8010"))
        self.token = token if token is not None else os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN", "").strip()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirectHandler())
        if not self.token:
            raise ConnectorError(503, "Managed connector authentication is not configured")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return _safe_json(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403): raise ConnectorError(502, "Managed connector authentication failed") from exc
            if exc.code == 404: raise ConnectorError(404, "Managed connector resource not found") from exc
            if exc.code == 409: raise ConnectorError(409, "Managed connector rejected the conflicting operation") from exc
            if exc.code in (422, 400): raise ConnectorError(422, "Managed connector rejected the request") from exc
            raise ConnectorError(503, "Managed connector market data is unavailable") from exc
        except TimeoutError as exc:
            raise ConnectorError(504, "Managed connector request timed out") from exc
        except urllib.error.URLError as exc:
            raise ConnectorError(503, "Managed connector is unavailable") from exc

    def create(self, body: dict[str, Any]) -> Any: return self.request("POST", "/v1/connections", body)
    def status(self, cid: str) -> Any: return self.request("GET", f"/v1/connections/{urllib.parse.quote(cid, safe='')}")
    def tick(self, cid: str, symbol: str) -> Any: return self.request("GET", f"/v1/connections/{cid}/tick/{urllib.parse.quote(symbol, safe='')}")
    def candles(self, cid: str, symbol: str, timeframe: str, count: int) -> Any: return self.request("GET", f"/v1/connections/{cid}/candles/{urllib.parse.quote(symbol, safe='')}/{timeframe}?count={count}")
    def revoke(self, cid: str) -> Any: return self.request("DELETE", f"/v1/connections/{cid}")


def validate_connector_configuration() -> str:
    """Validate connector configuration without making a network request."""
    configured_url = os.getenv("AUREX_CONNECTOR_SERVICE_URL", "").strip()
    production = os.getenv("AUREX_ENV", "development").strip().casefold() == "production"
    if not configured_url:
        if production:
            raise RuntimeError("Production requires AUREX_CONNECTOR_SERVICE_URL.")
        configured_url = "http://127.0.0.1:8010"
    token = os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN", "").strip()
    try:
        normalized_url = validate_connector_url(configured_url)
    except ConnectorError as exc:
        raise RuntimeError(exc.message) from exc
    if not token:
        raise RuntimeError("AUREX_CONNECTOR_SERVICE_TOKEN is not configured.")
    return normalized_url


def current_user(request: Request) -> str:
    user_id = getattr(request.state, "aurex_user_id", None)
    if not user_id:
        raise HTTPException(401, "User authentication required")
    return str(user_id)


def owned(store: ConnectionStore, cid: str, user_id: str) -> dict[str, Any]:
    try: UUID(cid)
    except ValueError: raise HTTPException(404, "Connection not found")
    record = store.get_for_user(cid, user_id)
    if not record: raise HTTPException(403, "You are not authorized to access this connection")
    if record["revoked"]: raise HTTPException(409, "Connection has been revoked")
    return record


def owned_including_revoked(store: ConnectionStore, cid: str, user_id: str) -> dict[str, Any]:
    try: UUID(cid)
    except ValueError: raise HTTPException(404, "Connection not found")
    record = store.get_for_user(cid, user_id)
    if not record: raise HTTPException(403, "You are not authorized to access this connection")
    return record


def connector_http_error(exc: ConnectorError) -> HTTPException:
    return HTTPException(exc.http_status, exc.message)


def create_app(store: ConnectionStore | None = None, client_factory: Callable[[], ConnectorClient] = ConnectorClient,
               auth_store: AuthStore | None = None) -> FastAPI:
    # Validate security-critical configuration while constructing the real app.
    # This makes invalid production settings fail before a server can accept requests.
    validate_connector_configuration()
    from aurex_auth import validate_auth_configuration
    validate_auth_configuration()
    db = store or ConnectionStore()
    accounts = auth_store or AuthStore()
    app = FastAPI(title="Aurex Managed MT5 API")
    app.add_middleware(AurexSessionMiddleware, auth_store=accounts)

    @app.post("/api/auth/register", status_code=201)
    def register(body: RegisterInput, response: Response):
        try:
            email = normalize_email(body.email)
            validate_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.password != body.confirm_password:
            raise HTTPException(422, "Passwords do not match.")
        try:
            user = accounts.create_user(email, body.password)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"user": user}

    @app.post("/api/auth/login")
    def login(body: AuthInput, request: Request, response: Response):
        try:
            email = normalize_email(body.email)
            key = client_key(request, email)
        except ValueError as exc:
            raise HTTPException(401, "Invalid email or password.") from exc
        if accounts.login_blocked(key):
            raise HTTPException(429, "Too many login attempts. Try again later.")
        row = accounts.get_user_by_email(email)
        if not row or not verify_password(row["password_hash"], body.password):
            accounts.record_login_failure(key)
            raise HTTPException(401, "Invalid email or password.")
        accounts.clear_login_failures(key)
        accounts.revoke_session(request.cookies.get("aurex_session"))
        token = accounts.create_session(row["user_id"])
        set_session_cookie(response, token)
        return {"user": public_user(row)}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        accounts.revoke_session(request.cookies.get("aurex_session"))
        clear_session_cookie(response)
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request):
        return {"user": require_user(request)}

    @app.get("/api/mt5/connections")
    def list_connections(user_id: str = Depends(current_user)): return {"connections": db.list_for_user(user_id)}

    @app.post("/api/mt5/connections", status_code=201)
    def create_connection(body: ConnectionInput, request: Request, user_id: str = Depends(current_user)):
        cid = str(uuid4())
        record = db.create(user_id, body, cid)
        try:
            result = client_factory().create({"connection_id": cid, "user_id": user_id, "provider": body.provider,
                "connection_method": body.connection_method, "label": body.label, "account_reference": body.account_reference,
                "server_name": body.server_name, "credentials": {"login": body.login, "password": body.password, "serverName": body.server_name}})
            db.update_status(cid, normalize_status(result.get("status")))
            return {**record, "status": normalize_status(result.get("status")), "message": result.get("message", "")}
        except ConnectorError as exc:
            db.update_status(cid, "ERROR")
            raise connector_http_error(exc)

    @app.get("/api/mt5/connections/{cid}")
    def get_status(cid: str, user_id: str = Depends(current_user)):
        record = owned_including_revoked(db, cid, user_id)
        if record["revoked"]:
            return {**record, "status": "REVOKED"}
        try: result = client_factory().status(cid)
        except ConnectorError as exc: raise connector_http_error(exc)
        normalized = normalize_status(result.get("status"))
        db.update_status(cid, normalized)
        return {**record, "status": normalized, "message": result.get("message", ""), "last_health_at": result.get("last_health_at"), "last_connected_at": result.get("last_connected_at")}

    @app.get("/api/mt5/connections/{cid}/tick/{symbol}")
    def get_tick(cid: str, symbol: str, user_id: str = Depends(current_user)):
        owned(db, cid, user_id)
        if not SYMBOL_RE.fullmatch(symbol): raise HTTPException(422, "Invalid symbol")
        try: result = client_factory().tick(cid, symbol)
        except ConnectorError as exc: raise connector_http_error(exc)
        if not isinstance(result, dict) or result.get("symbol") != symbol: raise HTTPException(502, "Connector returned an invalid tick")
        if any(not isinstance(result.get(key), (int, float)) or not math.isfinite(float(result[key])) for key in ("bid", "ask")): raise HTTPException(502, "Connector returned an invalid tick")
        return result

    @app.get("/api/mt5/connections/{cid}/candles/{symbol}/{timeframe}")
    def get_candles(cid: str, symbol: str, timeframe: str, count: int = Query(200, ge=1, le=3000), user_id: str = Depends(current_user)):
        owned(db, cid, user_id)
        timeframe = timeframe.upper()
        if not SYMBOL_RE.fullmatch(symbol): raise HTTPException(422, "Invalid symbol")
        if timeframe not in TIMEFRAMES: raise HTTPException(422, "Unsupported timeframe")
        try: result = client_factory().candles(cid, symbol, timeframe, count)
        except ConnectorError as exc: raise connector_http_error(exc)
        raw = result.get("candles") if isinstance(result, dict) else None
        if not isinstance(raw, list): raise HTTPException(502, "Connector returned an invalid candle response")
        clean = []
        for candle in raw:
            if not isinstance(candle, dict) or not all(key in candle for key in ("time", "open", "high", "low", "close")): continue
            try:
                values = [float(candle[key]) for key in ("open", "high", "low", "close")]
                dt = datetime.fromisoformat(str(candle["time"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError): continue
            if not all(math.isfinite(value) for value in values): continue
            clean.append({**candle, "time": dt.isoformat(), **dict(zip(("open", "high", "low", "close"), values))})
        clean.sort(key=lambda item: item["time"])
        unique = {item["time"]: item for item in clean}
        return {"symbol": symbol, "timeframe": timeframe, "count": len(unique), "candles": list(unique.values())}

    @app.delete("/api/mt5/connections/{cid}")
    def revoke(cid: str, user_id: str = Depends(current_user)):
        # Revocation is intentionally retryable: the connector may have
        # revoked credentials while failing to verify process-tree cleanup.
        # Keep the ownership check, but do not reject an already-revoked row.
        record = owned_including_revoked(db, cid, user_id)
        try: result = client_factory().revoke(cid)
        except ConnectorError as exc: raise connector_http_error(exc)
        db.update_status(cid, "REVOKED")
        return {**record, "status": "REVOKED", "message": result.get("message", "Connection revoked")}

    return app
