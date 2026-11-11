import os
from pathlib import Path

from fastapi.testclient import TestClient

from aurex_auth import AuthStore
from aurex_backend import ConnectionInput, ConnectionStore, create_app


class ExecutionConnector:
    calls = []
    terminal_algo_disabled = False
    account_trade_allowed = True
    account_trade_expert = True
    symbol_trade_mode = 4

    def status(self, cid):
        return {"status": "CONNECTED"}

    def account(self, cid):
        return {
            "account": {
                "trade_mode": 0, "trade_allowed": self.account_trade_allowed,
                "trade_expert": self.account_trade_expert, "equity": 10000,
            },
            "terminal": {"connected": True, "trade_allowed": True, "tradeapi_disabled": self.terminal_algo_disabled},
        }

    def order_spec(self, cid, symbol):
        return {"symbol": symbol, "available": True, "bid": 100.0, "ask": 100.2,
                "point": 0.01, "trade_mode": self.symbol_trade_mode, "order_mode": 127, "volume_min": 0.01,
                "volume_max": 10.0, "volume_step": 0.01, "trade_stops_level": 10}

    def submit_order(self, cid, body):
        self.calls.append(body)
        return {"status": "FILLED", "symbol": body["symbol"], "order_ticket": 42,
                "deal_ticket": 43, "fill_price": 100.2, "executed_volume": body["volume"]}


def _client(tmp_path: Path, monkeypatch):
    ExecutionConnector.calls = []
    ExecutionConnector.terminal_algo_disabled = False
    ExecutionConnector.account_trade_allowed = True
    ExecutionConnector.account_trade_expert = True
    ExecutionConnector.symbol_trade_mode = 4
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



def test_readiness_requires_terminal_algo_permission_and_symbol_direction(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    url = "/api/mt5/connections/11111111-1111-1111-1111-111111111111/execution-readiness?mode=DEMO&symbol=XAUUSDm&side=BUY"
    response = client.get(url)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "DEMO_READY"
    assert payload["ready"] is True
    assert payload["checks"]["terminal_algo_trading"]["ok"] is True
    assert payload["checks"]["symbol_direction"]["ok"] is True


def test_readiness_fails_closed_when_terminal_algo_trading_is_disabled(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    ExecutionConnector.terminal_algo_disabled = True
    try:
        response = client.get("/api/mt5/connections/11111111-1111-1111-1111-111111111111/execution-readiness?mode=DEMO&symbol=XAUUSDm&side=BUY")
        assert response.status_code == 200
        assert response.json()["status"] == "ALGO_TRADING_DISABLED"
        assert response.json()["ready"] is False
        assert "Algo Trading" in response.json()["message"]
    finally:
        ExecutionConnector.terminal_algo_disabled = False


def test_readiness_requires_account_expert_permission(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    ExecutionConnector.account_trade_expert = False
    try:
        response = client.get("/api/mt5/connections/11111111-1111-1111-1111-111111111111/execution-readiness?mode=DEMO&symbol=XAUUSDm&side=BUY")
        assert response.status_code == 200
        assert response.json()["status"] == "EXPERT_TRADING_DISABLED"
        assert response.json()["ready"] is False
    finally:
        ExecutionConnector.account_trade_expert = True


def test_readiness_does_not_claim_ready_without_symbol_and_side(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.get("/api/mt5/connections/11111111-1111-1111-1111-111111111111/execution-readiness?mode=DEMO")
    assert response.status_code == 200
    assert response.json()["status"] == "SYMBOL_REQUIRED"
    assert response.json()["ready"] is False



def test_order_submission_is_blocked_when_terminal_algo_trading_is_disabled(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    ExecutionConnector.terminal_algo_disabled = True
    try:
        response = client.post("/api/mt5/orders", json=_payload(key="blocked-algo-123456789"))
        assert response.status_code == 409
        assert "Algo Trading is disabled" in response.text
        assert ExecutionConnector.calls == []
    finally:
        ExecutionConnector.terminal_algo_disabled = False


def test_readiness_blocks_symbol_direction_not_permitted_by_broker(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    ExecutionConnector.symbol_trade_mode = 2  # short-only
    try:
        response = client.get("/api/mt5/connections/11111111-1111-1111-1111-111111111111/execution-readiness?mode=DEMO&symbol=XAUUSDm&side=BUY")
        assert response.status_code == 200
        assert response.json()["status"] == "SYMBOL_DIRECTION_BLOCKED"
        assert response.json()["ready"] is False
    finally:
        ExecutionConnector.symbol_trade_mode = 4


def test_invalid_symbol_is_rejected_before_worker_submission(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    response = client.post("/api/mt5/orders", json=_payload(symbol="XAUUSDm/other"))
    assert response.status_code == 422
