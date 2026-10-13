"""Aurex managed MT5 session supervisor.

Run this service on an always-on Windows VPS with MetaTrader 5 installed.
Each customer connection runs in a separate child process and a separate
portable terminal directory. This service intentionally exposes no order
execution routes; it is for connection lifecycle and market data only.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

DATA_DIR = Path(os.getenv("AUREX_WORKER_DATA_DIR", "./aurex-worker-data")).resolve()
CONNECTIONS_DIR = DATA_DIR / "connections"
TERMINALS_DIR = DATA_DIR / "terminals"
STATE_DIR = DATA_DIR / "state"
TERMINAL_SOURCE = Path(os.getenv("MT5_TERMINAL_PATH", r"C:\\Program Files\\MetaTrader 5\\terminal64.exe"))
SERVICE_TOKEN = os.getenv("AUREX_CONNECTOR_SERVICE_TOKEN", "").strip()
WORKER_KEY_TEXT = os.getenv("AUREX_WORKER_ENCRYPTION_KEY", "").strip()
INTERNAL_KEY = os.getenv("AUREX_WORKER_INTERNAL_KEY", "").strip()
LOCK = RLock()
PROCESSES: dict[str, subprocess.Popen] = {}
SUPERVISOR_TASK: asyncio.Task | None = None

for folder in (CONNECTIONS_DIR, TERMINALS_DIR, STATE_DIR):
    folder.mkdir(parents=True, exist_ok=True)

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def key_bytes() -> bytes:
    if not WORKER_KEY_TEXT:
        raise RuntimeError("AUREX_WORKER_ENCRYPTION_KEY is required.")
    if len(WORKER_KEY_TEXT) == 64:
        try:
            key = bytes.fromhex(WORKER_KEY_TEXT)
            if len(key) == 32:
                return key
        except ValueError:
            pass
    try:
        key = base64.b64decode(WORKER_KEY_TEXT, validate=True)
    except Exception as exc:
        raise RuntimeError("Worker encryption key must be 32 bytes encoded as 64 hex characters or base64.") from exc
    if len(key) != 32:
        raise RuntimeError("Worker encryption key must decode to exactly 32 bytes.")
    return key

def encrypt_json(value: dict[str, Any]) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key_bytes()).encrypt(nonce, json.dumps(value).encode("utf-8"), b"aurex-mt5-worker-v1")
    return "v1." + base64.b64encode(nonce).decode() + "." + base64.b64encode(ciphertext).decode()

def decrypt_json(envelope: str) -> dict[str, Any]:
    try:
        version, nonce, ciphertext = envelope.split(".", 2)
        if version != "v1":
            raise ValueError("Unknown credential envelope version")
        raw = AESGCM(key_bytes()).decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext), b"aurex-mt5-worker-v1")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Credential payload must be an object")
        return result
    except Exception as exc:
        raise RuntimeError("Could not decrypt this broker credential record.") from exc

def file_for(connection_id: str) -> Path:
    try:
        normalized = str(UUID(connection_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid connection ID.") from exc
    return CONNECTIONS_DIR / f"{normalized}.json"

def read_record(connection_id: str) -> dict[str, Any]:
    path = file_for(connection_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Broker connection not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid record")
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Broker connection record could not be read.") from exc

def write_record(record: dict[str, Any]) -> None:
    path = file_for(str(record["connection_id"]))
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)

def read_state(connection_id: str) -> dict[str, Any]:
    path = STATE_DIR / f"{UUID(connection_id)}.json"
    if not path.exists():
        return {"connection_id": connection_id, "status": "CONNECTING", "message": "Worker is starting.", "last_health_at": now()}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {"connection_id": connection_id, "status": "ERROR", "message": "Worker state file could not be read.", "last_health_at": now()}

def write_state(connection_id: str, **patch: Any) -> dict[str, Any]:
    with LOCK:
        current = read_state(connection_id)
        current.update(patch)
        current["connection_id"] = connection_id
        current["last_health_at"] = now()
        path = STATE_DIR / f"{UUID(connection_id)}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
        return current

def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def start_worker(connection_id: str) -> None:
    with LOCK:
        existing = PROCESSES.get(connection_id)
        if existing and existing.poll() is None:
            return
        record = read_record(connection_id)
        if record.get("revoked"):
            return
        port = int(record.get("local_port") or pick_free_port())
        record["local_port"] = port
        write_record(record)
        env = os.environ.copy()
        env["AUREX_SESSION_CONNECTION_ID"] = connection_id
        env["AUREX_SESSION_LOCAL_PORT"] = str(port)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--session-worker", connection_id], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, close_fds=(os.name != "nt"))
        PROCESSES[connection_id] = process
        write_state(connection_id, status="CONNECTING", message="Starting isolated MT5 terminal session.", local_port=port, pid=process.pid)

def stop_worker(connection_id: str) -> bool:
    with LOCK:
        process = PROCESSES.pop(connection_id, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)
        return process is None or process.poll() is not None

async def supervise() -> None:
    while True:
        try:
            for path in CONNECTIONS_DIR.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    connection_id = str(UUID(str(record.get("connection_id", ""))))
                    if record.get("revoked"):
                        stop_worker(connection_id)
                        continue
                    process = PROCESSES.get(connection_id)
                    if process is None or process.poll() is not None:
                        state = read_state(connection_id)
                        if state.get("status") not in ("REVOKED",):
                            start_worker(connection_id)
                except Exception as exc:
                    print("Aurex worker supervisor recovered from record error:", type(exc).__name__)
        except Exception as exc:
            print("Aurex worker supervisor error:", type(exc).__name__)
        await asyncio.sleep(5)

def verify_service_token(authorization: str | None = Header(default=None)) -> bool:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Connector service authentication is not configured.")
    supplied = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Connector service authentication failed.")
    return True

class NewConnection(BaseModel):
    connection_id: str = Field(min_length=36, max_length=36)
    user_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(pattern="^(EXNESS|MT5_OTHER)$")
    connection_method: str = Field(pattern="^MT5_TERMINAL$")
    label: str = Field(min_length=2, max_length=80)
    account_reference: str = Field(min_length=2, max_length=120)
    server_name: str = Field(min_length=2, max_length=160)
    credentials: dict[str, str]

app = FastAPI(title="Aurex Managed MT5 Connector", version="1.0.0")

@app.on_event("startup")
async def startup() -> None:
    global SUPERVISOR_TASK
    if "--session-worker" not in sys.argv:
        SUPERVISOR_TASK = asyncio.create_task(supervise())

@app.on_event("shutdown")
async def shutdown() -> None:
    if SUPERVISOR_TASK:
        SUPERVISOR_TASK.cancel()
    if "--session-worker" not in sys.argv:
        for connection_id in list(PROCESSES):
            stop_worker(connection_id)

@app.get("/health")
def health(_: bool = Depends(verify_service_token)):
    return {"status": "ok", "service": "aurex-managed-mt5-connector", "managed_sessions": len(PROCESSES), "checked_at": now()}

@app.post("/v1/connections")
def create_connection(body: NewConnection, _: bool = Depends(verify_service_token)):
    connection_id = str(UUID(body.connection_id))
    if body.connection_method != "MT5_TERMINAL":
        raise HTTPException(status_code=422, detail="This worker only supports isolated MT5 terminal sessions. API connectors must use a verified broker-specific adapter.")
    if body.provider not in ("EXNESS", "MT5_OTHER"):
        raise HTTPException(status_code=422, detail="Unsupported broker provider.")
    credentials = body.credentials
    login = str(credentials.get("login", "")).strip()
    password = str(credentials.get("password", ""))
    server_name = str(credentials.get("serverName", body.server_name)).strip()
    if not login.isdigit() or not password or not server_name:
        raise HTTPException(status_code=422, detail="A numeric MT5 login, trading password and exact server name are required.")
    if not TERMINAL_SOURCE.exists():
        raise HTTPException(status_code=503, detail="MT5_TERMINAL_PATH must point to the installed terminal64.exe on this Windows host.")
    path = file_for(connection_id)
    with LOCK:
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("user_id") != body.user_id:
                raise HTTPException(status_code=409, detail="Connection ID is already assigned to another customer.")
            if old.get("revoked"):
                raise HTTPException(status_code=409, detail="This connection ID has been revoked; create a new connection.")
            start_worker(connection_id)
            state = read_state(connection_id)
            return {"connection_id": connection_id, "status": state.get("status", "CONNECTING"), "message": state.get("message", "Connection is starting.")}
        record = {
            "connection_id": connection_id,
            "user_id": body.user_id,
            "provider": body.provider,
            "connection_method": body.connection_method,
            "label": body.label,
            "account_reference": body.account_reference,
            "server_name": server_name,
            "encrypted_credentials": encrypt_json({"login": login, "password": password, "serverName": server_name}),
            "revoked": False,
            "created_at": now(),
            "local_port": pick_free_port(),
        }
        write_record(record)
        write_state(connection_id, status="CONNECTING", message="Connection accepted; isolated terminal is starting.", local_port=record["local_port"])
        start_worker(connection_id)
    return {"connection_id": connection_id, "status": "CONNECTING", "message": "Connection accepted; isolated MT5 terminal is starting."}

@app.get("/v1/connections/{connection_id}")
def connection_status(connection_id: str, _: bool = Depends(verify_service_token)):
    record = read_record(connection_id)
    state = read_state(connection_id)
    return {"connection_id": connection_id, "status": "REVOKED" if record.get("revoked") else state.get("status", "CONNECTING"), "message": state.get("message", ""), "last_health_at": state.get("last_health_at"), "last_connected_at": state.get("last_connected_at")}

@app.delete("/v1/connections/{connection_id}")
def revoke_connection(connection_id: str, _: bool = Depends(verify_service_token)):
    record = read_record(connection_id)
    record["revoked"] = True
    record["encrypted_credentials"] = ""
    record["revoked_at"] = now()
    write_record(record)
    stopped = stop_worker(connection_id)
    write_state(connection_id, status="REVOKED", message="Connection revoked; terminal worker stopped.", worker_stopped=stopped)
    return {"ok": True, "connection_id": connection_id, "status": "REVOKED", "worker_stopped": stopped}

@app.get("/v1/connections/{connection_id}/tick/{symbol}")
def connection_tick(connection_id: str, symbol: str, _: bool = Depends(verify_service_token)):
    record = read_record(connection_id)
    if record.get("revoked"):
        raise HTTPException(status_code=410, detail="Connection has been revoked.")
    state = read_state(connection_id)
    if state.get("status") != "CONNECTED":
        raise HTTPException(status_code=503, detail="This broker session is not connected.")
    port = int(record["local_port"])
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/internal/tick/{symbol}", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=503, detail="Unable to read tick from the isolated terminal session.")

@app.get("/v1/connections/{connection_id}/candles/{symbol}/{timeframe}")
def connection_candles(connection_id: str, symbol: str, timeframe: str, count: int = 200, _: bool = Depends(verify_service_token)):
    record = read_record(connection_id)
    if record.get("revoked"):
        raise HTTPException(status_code=410, detail="Connection has been revoked.")
    state = read_state(connection_id)
    if state.get("status") != "CONNECTED":
        raise HTTPException(status_code=503, detail="This broker session is not connected.")
    port = int(record["local_port"])
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/internal/candles/{symbol}/{timeframe}?count={max(1, min(int(count), 3000))}"
        with urllib.request.urlopen(url, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=503, detail="Unable to read candles from the isolated terminal session.")

def session_worker(connection_id: str) -> None:
    import MetaTrader5 as mt5
    from fastapi import FastAPI
    connection_id = str(UUID(connection_id))
    record_path = file_for(connection_id)
    state_path = STATE_DIR / f"{connection_id}.json"
    local_port = int(os.getenv("AUREX_SESSION_LOCAL_PORT", "0"))
    terminal_target = TERMINALS_DIR / connection_id
    terminal_exe = terminal_target / TERMINAL_SOURCE.name

    def read_local_record() -> dict[str, Any]:
        return json.loads(record_path.read_text(encoding="utf-8"))

    def update(status: str, message: str, **extra: Any) -> None:
        current = {}
        if state_path.exists():
            try: current = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception: current = {}
        current.update({"connection_id": connection_id, "status": status, "message": message, "last_health_at": now(), **extra})
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, state_path)

    def ensure_terminal_copy() -> None:
        if not terminal_exe.exists():
            if terminal_target.exists():
                shutil.rmtree(terminal_target, ignore_errors=True)
            shutil.copytree(TERMINAL_SOURCE.parent, terminal_target, ignore=shutil.ignore_patterns("Logs", "Tester", "Bases"))
        if not terminal_exe.exists():
            raise RuntimeError("Copied terminal executable was not found.")

    session_app = FastAPI(title="Aurex isolated MT5 session", docs_url=None, redoc_url=None, openapi_url=None)

    def authorized_internal(x_aurex_internal_key: str | None = Header(default=None, alias="X-Aurex-Internal-Key")):
        expected = INTERNAL_KEY
        if not expected or not x_aurex_internal_key or not hmac.compare_digest(x_aurex_internal_key, expected):
            raise HTTPException(status_code=401, detail="Internal session access denied.")

    def validate_symbol(symbol: str) -> str:
        import re
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
            raise HTTPException(status_code=400, detail="Invalid symbol.")
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(status_code=404, detail=f"Symbol {symbol} is unavailable.")
        return symbol

    @session_app.get("/internal/health")
    def internal_health(_: bool = Depends(authorized_internal)):
        terminal = mt5.terminal_info()
        return {"status": "CONNECTED" if terminal and terminal.connected else "RECONNECTING", "connected": bool(terminal and terminal.connected), "checked_at": now()}

    @session_app.get("/internal/tick/{symbol}")
    def internal_tick(symbol: str, _: bool = Depends(authorized_internal)):
        validate_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise HTTPException(status_code=503, detail="No tick available.")
        return {"symbol": symbol, "bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 8), "last": tick.last, "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(), "time_msc": tick.time_msc}

    @session_app.get("/internal/candles/{symbol}/{timeframe}")
    def internal_candles(symbol: str, timeframe: str, count: int = 200, _: bool = Depends(authorized_internal)):
        import pandas as pd
        from datetime import datetime
        frames = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1}
        validate_symbol(symbol)
        frame = frames.get(timeframe.upper())
        if frame is None: raise HTTPException(status_code=400, detail="Unsupported timeframe.")
        rates = mt5.copy_rates_from_pos(symbol, frame, 0, max(1, min(int(count), 3000)))
        if rates is None or len(rates) == 0: raise HTTPException(status_code=503, detail="No candle history available.")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        candles = [{"time": row["time"].isoformat(), "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]), "tick_volume": int(row["tick_volume"]), "spread": int(row["spread"]), "real_volume": int(row["real_volume"])} for _, row in df.iterrows()]
        return {"symbol": symbol, "timeframe": timeframe.upper(), "count": len(candles), "candles": candles}

    def run_terminal() -> None:
        delay = 2
        while True:
            try:
                record = read_local_record()
                if record.get("revoked"):
                    update("REVOKED", "Connection revoked.")
                    return
                credentials = decrypt_json(record["encrypted_credentials"])
                ensure_terminal_copy()
                update("CONNECTING", "Authenticating with broker terminal.")
                login = int(str(credentials["login"]))
                ok = mt5.initialize(str(terminal_exe), login=login, password=credentials["password"], server=credentials["serverName"], timeout=60000, portable=True)
                if not ok:
                    message = f"MT5 initialization/login failed ({mt5.last_error()})."
                    update("RECONNECTING", message)
                    mt5.shutdown()
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                account = mt5.account_info()
                terminal = mt5.terminal_info()
                if account is None or terminal is None or not terminal.connected or int(account.login) != login:
                    update("RECONNECTING", "Broker login/account verification failed.")
                    mt5.shutdown()
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                update("CONNECTED", "MT5 terminal authenticated; automatic reconnect supervisor active.", last_connected_at=now(), account_login=str(account.login), broker=account.company, server=account.server)
                delay = 2
                while True:
                    current = read_local_record()
                    if current.get("revoked"):
                        mt5.shutdown()
                        update("REVOKED", "Connection revoked; terminal session stopped.")
                        return
                    terminal = mt5.terminal_info()
                    account = mt5.account_info()
                    if terminal is None or not terminal.connected or account is None or int(account.login) != login:
                        update("RECONNECTING", "Broker connection lost; automatic reconnect is in progress.")
                        mt5.shutdown()
                        break
                    update("CONNECTED", "MT5 terminal authenticated; connection healthy.", account_login=str(account.login), broker=account.company, server=account.server)
                    time.sleep(5)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                update("RECONNECTING", "Session worker encountered a recoverable error: " + type(exc).__name__)
                try: mt5.shutdown()
                except Exception: pass
                time.sleep(delay)
                delay = min(delay * 2, 60)

    import threading
    thread = threading.Thread(target=run_terminal, daemon=True)
    thread.start()
    uvicorn.run(session_app, host="127.0.0.1", port=local_port, log_level="error", access_log=False)

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--session-worker":
        session_worker(sys.argv[2])
    else:
        if not SERVICE_TOKEN:
            print("ERROR: Set AUREX_CONNECTOR_SERVICE_TOKEN before starting the service.", file=sys.stderr)
            sys.exit(2)
        if not WORKER_KEY_TEXT:
            print("ERROR: Set AUREX_WORKER_ENCRYPTION_KEY before starting the service.", file=sys.stderr)
            sys.exit(2)
        if not INTERNAL_KEY:
            print("ERROR: Set AUREX_WORKER_INTERNAL_KEY before starting the service.", file=sys.stderr)
            sys.exit(2)
        uvicorn.run(app, host=os.getenv("AUREX_CONNECTOR_HOST", "127.0.0.1"), port=int(os.getenv("AUREX_CONNECTOR_PORT", "8010")), log_level="info")
