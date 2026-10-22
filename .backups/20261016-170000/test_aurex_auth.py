import sqlite3
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aurex_auth import AuthStore
from aurex_backend import ConnectionStore, create_app


class NoConnector:
    pass


@pytest.fixture()
def auth_test_dir():
    path = Path.cwd() / ".aurex-auth-test-db"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUREX_AUTH_COOKIE_SECURE", "false")
    auth = AuthStore(str(tmp_path / "auth.sqlite3"))
    app = create_app(auth_store=auth, store=ConnectionStore(str(tmp_path / "connections.sqlite3")), client_factory=lambda: NoConnector())
    return TestClient(app), auth


def register(client, email=" Trader@Example.COM ", password="correct horse battery staple"):
    return client.post("/api/auth/register", json={"email": email, "password": password, "confirm_password": password})


def test_registration_normalizes_and_never_returns_or_stores_plaintext(auth_test_dir, monkeypatch):
    tmp_path = auth_test_dir
    client, _ = make_client(tmp_path, monkeypatch)
    response = register(client)
    assert response.status_code == 201
    assert response.json() == {"user": {"id": response.json()["user"]["id"], "email": "trader@example.com"}}
    assert "password_hash" not in response.text
    assert register(client).status_code == 409
    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored.startswith("$argon2")
    assert "correct horse battery staple" not in stored
    client.close()


def test_login_me_cookie_and_logout_revocation(auth_test_dir, monkeypatch):
    tmp_path = auth_test_dir
    client, auth = make_client(tmp_path, monkeypatch)
    register(client, "person@example.com")
    login = client.post("/api/auth/login", json={"email": "PERSON@example.com", "password": "correct horse battery staple"})
    assert login.status_code == 200
    cookie = login.cookies.get("aurex_session")
    assert cookie
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["user"]["email"] == "person@example.com"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert auth.user_for_session(cookie) is None
    client.close()


def test_unknown_and_wrong_password_are_generic_and_rate_limited(auth_test_dir, monkeypatch):
    tmp_path = auth_test_dir
    client, _ = make_client(tmp_path, monkeypatch)
    register(client, "person@example.com")
    for email in ("unknown@example.com", "person@example.com"):
        for _ in range(5):
            response = client.post("/api/auth/login", json={"email": email, "password": "wrong password"})
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid email or password."
        assert client.post("/api/auth/login", json={"email": email, "password": "wrong password"}).status_code == 429
    client.close()


def test_expired_and_tampered_sessions_fail(auth_test_dir, monkeypatch):
    tmp_path = auth_test_dir
    client, auth = make_client(tmp_path, monkeypatch)
    user = register(client, "expired@example.com").json()["user"]
    expired = auth.create_session(user["id"], ttl_seconds=-1)
    client.cookies.set("aurex_session", expired)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("aurex_session", expired[:-1] + ("a" if expired[-1] != "a" else "b"))
    assert client.get("/api/auth/me").status_code == 401
    client.close()


def test_cross_origin_state_change_is_rejected(auth_test_dir, monkeypatch):
    tmp_path = auth_test_dir
    client, _ = make_client(tmp_path, monkeypatch)
    response = client.post("/api/auth/register", headers={"Origin": "https://evil.example"}, json={
        "email": "person@example.com", "password": "correct horse battery staple", "confirm_password": "correct horse battery staple"
    })
    assert response.status_code == 403
    client.close()
