"""Durable, per-user MT5 execution requests and audit records."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("AUREX_EXECUTION_DB", "./aurex-execution.sqlite3")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS execution_requests (
                request_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                client_order_key TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, connection_id, client_order_key)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS execution_audit (
                audit_id TEXT PRIMARY KEY,
                request_id TEXT,
                user_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _decode(value: str | None) -> Any:
        return json.loads(value) if value else None

    def by_key(self, user_id: str, connection_id: str, client_order_key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM execution_requests WHERE user_id=? AND connection_id=? AND client_order_key=?",
                (user_id, connection_id, client_order_key),
            ).fetchone()
        return self._row(row)

    def create(self, user_id: str, connection_id: str, client_order_key: str, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid4())
        now = utc_now()
        with self._db() as db:
            db.execute(
                "INSERT INTO execution_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                (request_id, user_id, connection_id, client_order_key, mode, "SUBMITTING", json.dumps(payload, separators=(",", ":")), None, now, now),
            )
        return self.get(request_id)  # type: ignore[return-value]

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM execution_requests WHERE request_id=?", (request_id,)).fetchone()
        return self._row(row)

    def update(self, request_id: str, status: str, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._db() as db:
            db.execute(
                "UPDATE execution_requests SET status=?, result_json=?, updated_at=? WHERE request_id=?",
                (status, json.dumps(result, separators=(",", ":")) if result is not None else None, utc_now(), request_id),
            )
        return self.get(request_id)

    def audit(self, request_id: str | None, user_id: str, connection_id: str, event_type: str, detail: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO execution_audit VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), request_id, user_id, connection_id, event_type, json.dumps(detail, separators=(",", ":")), utc_now()),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
        value.pop("result_json", None)
        return value
