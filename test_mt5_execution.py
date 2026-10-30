import os
from pathlib import Path

from fastapi.testclient import TestClient

from aurex_auth import AuthStore
from aurex_backend import ConnectionInput, ConnectionStore, create_app


class ExecutionConnector:
    calls = []

    def status(self, cid):
        return {"status": "CONNECTED"}

    def account(self, cid):
        return {"account": {"trade_mode": 0, "trade_allowed": True, "equity": 10000}}

    def order_spec(self, cid, symbol):
        return {"symbol": symbol, "available": True, "bid": 100.0, "ask": 100.2,
                "point": 0.01, "trade_mode": 4, "volume_min": 0.01,
                "volume_max": 10.0, "volume_step": 0.01, "trade_stops_level": 10}

    def submit_order(self, cid, body):
        self.calls.append(body)
        return {"status": "FILLED", "symbol": body["symbol"], "order_ticket": 42,
                "deal_ticket": 43, "fill_price": 100.2, "executed_volume": body["volume"]}


def _client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUREX_ENABLE_DEMO_ORDER_EXECUTION", "true")
    monkeypatch.setenv("AUREX_EXECUTION_DB", str(tmp_path / "execution.sqlite3"))
    monkeypatch.setenv("AUREX_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUREX_TRUSTED_ORIGINS", "http://testserver")
    auth = AuthStore(str(tmp_path / "auth.sqlite3"))
    user = auth.create_user("trader@example.com", "correct horse battery staple")
    auth.grant_permission(user["id"], "trading.orders.execute")
    store = ConnectionStore(str(tmp_path / "connections.sqlite3"))
    body = ConnectionInput(provider="MT5_OTHER", label="Demo", account_reference="demo-1", server_name="Broker-Demo", login="1", password="secret")
    store.create(user["id"], body, "11111111-1111-1111-1111-111111111111")
    app = create_app(store=store, auth_store=auth, client_factory=ExecutionConnector)

    @app.middleware("http")
    async def test_identity(request, call_next):
        request.state.aurex_user_id = user["id"]
        return await call_next(request)

    return TestClient(app), user["id"]


def _payload(symbol="XAUUSDm", key="manual-123456789012"):
    return {"connection_id": "11111111-1111-1111-1111-111111111111", "mode": "DEMO", "symbol": symbol,
            "side": "BUY", "volume": 0.01, "client_order_key": key}


def test_exact_symbol_is_preserved_and_duplicate_request_is_idempotent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    first = client.post("/api/mt5/orders", json=_payload())
    second = client.post("/api/mt5/orders", json=_payload())
    assert first.status_code == 202, first.text
    assert first.json()["execution"]["result"]["symbol"] == "XAUUSDm"
    assert second.status_code == 202
    assert second.json()["idempotent"] is True
    assert len(ExecutionConnector.calls) == 1


def test_demo_execution_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AUREX_ENABLE_DEMO_ORDER_EXECUTION", raising=False)
    client, _ = _client(tmp_path, monkeypatch)
    # _client intentionally enables the flag, so explicitly turn it off after app construction
    monkeypatch.setenv("AUREX_ENABLE_DEMO_ORDER_EXECUTION", "false")
    response = client.post("/api/mt5/orders", json=_payload(key="manual-987654321098"))
    assert response.status_code == 423, response.text


def test_invalid_symbol_is_rejected_before_worker_submission(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.post("/api/mt5/orders", json=_payload(symbol="XAUUSDm/other"))
    assert response.status_code == 422
