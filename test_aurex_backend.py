import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUREX_CONNECTOR_SERVICE_URL", "http://127.0.0.1:8010")
os.environ.setdefault("AUREX_CONNECTOR_SERVICE_TOKEN", "offline-test-token")

from aurex_backend import ConnectorError, ConnectionStore, create_app, validate_connector_url


class FakeConnector:
    calls = []

    def create(self, payload):
        self.calls.append(("create", payload))
        return {"connection_id": payload["connection_id"], "status": "CONNECTING", "message": "starting"}

    def status(self, cid):
        self.calls.append(("status", cid))
        return {"connection_id": cid, "status": "CONNECTED", "message": "healthy"}

    def tick(self, cid, symbol):
        self.calls.append(("tick", cid, symbol))
        return {"symbol": symbol, "bid": 2000.1, "ask": 2000.2, "time": "2026-01-01T00:00:00+00:00"}

    def candles(self, cid, symbol, timeframe, count):
        self.calls.append(("candles", cid, symbol, timeframe, count))
        return {"symbol": symbol, "timeframe": timeframe, "candles": [
            {"time": "2026-01-01T00:02:00Z", "open": 2, "high": 3, "low": 1, "close": 2.5},
            {"time": "2026-01-01T00:01:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            {"time": "2026-01-01T00:01:00Z", "open": 99, "high": 99, "low": 99, "close": 99},
        ]}

    def revoke(self, cid):
        self.calls.append(("revoke", cid))
        return {"ok": True, "status": "REVOKED"}


@pytest.fixture()
def client():
    FakeConnector.calls = []
    test_dir = Path.cwd() / ".aurex-test-db"
    shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(ConnectionStore(str(test_dir / "connections.sqlite3")), lambda: FakeConnector())
    # This represents the existing Aurex auth middleware. The route itself
    # never accepts a browser-supplied user_id.
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.aurex_user_id = request.headers.get("x-test-user")
        return await call_next(request)
    client = TestClient(app)
    yield client
    client.close()
    shutil.rmtree(test_dir, ignore_errors=True)


def create(client, user="alice"):
    response = client.post("/api/mt5/connections", headers={"x-test-user": user}, json={
        "provider": "EXNESS", "label": "My Exness", "account_reference": "acct-1",
        "server_name": "Exness-MT5Real30", "login": "123456", "password": "secret",
    })
    assert response.status_code == 201
    return response.json()


def test_authentication_and_customer_isolation(client):
    assert client.get("/api/mt5/connections").status_code == 401
    connection = create(client)
    assert client.get(f"/api/mt5/connections/{connection['connection_id']}", headers={"x-test-user": "bob"}).status_code == 403
    listed = client.get("/api/mt5/connections", headers={"x-test-user": "bob"}).json()
    assert listed["connections"] == []


def test_selected_managed_connection_and_normalized_candles(client):
    connection = create(client)
    cid = connection["connection_id"]
    assert client.get(f"/api/mt5/connections/{cid}/tick/XAUUSDm", headers={"x-test-user": "alice"}).status_code == 200
    response = client.get(f"/api/mt5/connections/{cid}/candles/XAUUSDm/M15?count=200", headers={"x-test-user": "alice"})
    assert response.status_code == 200
    candles = response.json()["candles"]
    assert [c["time"] for c in candles] == ["2026-01-01T00:01:00+00:00", "2026-01-01T00:02:00+00:00"]
    assert ("candles", cid, "XAUUSDm", "M15", 200) in FakeConnector.calls
    assert not any("8000" in json.dumps(call) for call in FakeConnector.calls)


def test_revoke_blocks_market_data(client):
    connection = create(client)
    cid = connection["connection_id"]
    assert client.delete(f"/api/mt5/connections/{cid}", headers={"x-test-user": "alice"}).status_code == 200
    assert client.get(f"/api/mt5/connections/{cid}/tick/XAUUSDm", headers={"x-test-user": "alice"}).status_code == 409


def test_invalid_chart_parameters_are_rejected(client):
    connection = create(client)
    cid = connection["connection_id"]
    headers = {"x-test-user": "alice"}
    assert client.get(f"/api/mt5/connections/{cid}/tick/XAU%20USD", headers=headers).status_code == 422
    assert client.get(f"/api/mt5/connections/{cid}/candles/XAUUSDm/H2", headers=headers).status_code == 422


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8010",
    "http://localhost:8010",
    "http://[::1]:8010",
    "https://connector.example.com",
])
def test_connector_url_policy_allows_loopback_http_and_remote_https(url):
    assert validate_connector_url(url).startswith(("http://", "https://"))


@pytest.mark.parametrize("url", [
    "http://connector.example.com",
    "ftp://connector.example.com",
    "https://user:password@connector.example.com",
    "https://connector.example.com/path",
    "https://connector.example.com:bad",
    "not a URL",
])
def test_connector_url_policy_rejects_unsafe_or_malformed_urls(url):
    with pytest.raises(ConnectorError):
        validate_connector_url(url)


@pytest.mark.parametrize("url", [
    "https://bad%20host.example.com",
    "https://bad label.example.com",
    "https://exa_mple.example.com",
    "http://192.168.1.10:8010",
    "https://127.0.0.1%3A8010",
])
def test_connector_url_rejects_ambiguous_authorities_and_remote_http(url):
    with pytest.raises(ConnectorError):
        validate_connector_url(url)


def test_connector_url_normalizes_valid_idna_and_literal_ipv6():
    assert validate_connector_url("https://BÜCHER.example.com") == "https://xn--bcher-kva.example.com"
    assert validate_connector_url("http://[::1]:8010") == "http://[::1]:8010"


def test_connector_redirect_handler_rejects_redirects_before_following():
    from aurex_backend import _NoRedirectHandler

    with pytest.raises(urllib.error.URLError):
        _NoRedirectHandler().redirect_request(None, None, 302, "Found", {}, "https://evil.example")


@pytest.mark.parametrize("url", [
    "http://connector.example.com",
    "https://connector.example.com/path",
    "https://bad%20host.example.com",
    "https://user:password@connector.example.com",
])
def test_app_construction_rejects_invalid_connector_configuration(url, monkeypatch):
    monkeypatch.setenv("AUREX_CONNECTOR_SERVICE_URL", url)
    monkeypatch.setenv("AUREX_CONNECTOR_SERVICE_TOKEN", "offline-test-token")
    with pytest.raises(RuntimeError, match="Managed connector|Remote"):
        create_app(ConnectionStore(":memory:"), client_factory=lambda: FakeConnector())


def test_app_construction_rejects_missing_connector_token(monkeypatch):
    monkeypatch.setenv("AUREX_CONNECTOR_SERVICE_URL", "http://127.0.0.1:8010")
    monkeypatch.delenv("AUREX_CONNECTOR_SERVICE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AUREX_CONNECTOR_SERVICE_TOKEN"):
        create_app(ConnectionStore(":memory:"), client_factory=lambda: FakeConnector())


def test_helper_import_works_without_connector_secrets():
    environment = os.environ.copy()
    environment.pop("AUREX_CONNECTOR_SERVICE_URL", None)
    environment.pop("AUREX_CONNECTOR_SERVICE_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-c", "from aurex_backend import validate_connector_url; print(validate_connector_url('https://connector.example.com'))"],
        cwd=Path.cwd(), env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "https://connector.example.com"


def test_production_app_construction_rejects_missing_connector_url_and_token(monkeypatch):
    monkeypatch.setenv("AUREX_ENV", "production")
    monkeypatch.setenv("AUREX_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("AUREX_TRUSTED_ORIGINS", "https://terminal.example.com")
    monkeypatch.delenv("AUREX_CONNECTOR_SERVICE_URL", raising=False)
    monkeypatch.delenv("AUREX_CONNECTOR_SERVICE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AUREX_CONNECTOR_SERVICE_URL"):
        create_app(ConnectionStore(":memory:"), client_factory=lambda: FakeConnector())


def test_connection_store_configures_busy_timeout(client):
    store = ConnectionStore(str(Path.cwd() / ".aurex-test-db" / "busy.sqlite3"))
    with store._db() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
