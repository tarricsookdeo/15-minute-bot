"""The web API: sessions are required, isolated per user, and responses carry security headers."""

import pytest
from fastapi.testclient import TestClient

import fifteen_bot.app as app_module
from fifteen_bot.sessions import SessionManager
from tests.test_sessions import FakeEngine


def _snapshot(self):
    return {"connected": True, "account_id": self.account_id, "running": False, "markets": []}


@pytest.fixture
def client(monkeypatch):
    FakeEngine.snapshot = _snapshot
    monkeypatch.setattr(app_module, "sessions", SessionManager(engine_factory=FakeEngine))
    monkeypatch.setattr(app_module, "_connect_attempts", {})
    with TestClient(app_module.app) as c:
        yield c


def connect(client, key):
    r = client.post("/api/connect", json={"api_key": key})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


def test_api_requires_a_session(client):
    for method, path in [("get", "/api/state"), ("get", "/api/session"), ("post", "/api/engine")]:
        r = client.request(method.upper(), path, headers={"X-Session": "made-up"}, json={"running": True})
        assert r.status_code == 401


def test_each_user_only_sees_their_own_session(client):
    alice, bob = connect(client, "alice-key"), connect(client, "bob-key")
    assert alice != bob
    a = client.get("/api/session", headers=alice).json()
    b = client.get("/api/session", headers=bob).json()
    assert a == b == {"connected": True, "account_id": None, "accounts": [], "onboarded": False}
    assert app_module.sessions.get(alice["X-Session"]) is not app_module.sessions.get(bob["X-Session"])
    assert len(app_module.sessions) == 2


def test_token_is_only_returned_on_connect(client):
    headers = connect(client, "alice-key")
    assert "token" not in client.get("/api/session", headers=headers).json()


def test_rejected_key(client):
    r = client.post("/api/connect", json={"api_key": "bad"})
    assert r.status_code == 401 and "didn't accept" in r.json()["detail"]


def test_connect_attempts_are_rate_limited_per_ip(client):
    codes = [client.post("/api/connect", json={"api_key": "bad"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_sign_out_ends_the_session(client):
    headers = connect(client, "alice-key")
    assert client.delete("/api/session", headers=headers).status_code == 200
    assert client.get("/api/session", headers=headers).status_code == 401


def test_websocket_pushes_state_and_rejects_unknown_sessions(client):
    headers = connect(client, "alice-key")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session": headers["X-Session"]})
        assert ws.receive_json()["type"] == "state"
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session": "made-up"})
        assert ws.receive_json()["type"] == "ended"


def test_security_headers_and_no_api_docs(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert client.get("/healthz").json()["ok"] is True
    for path in ("/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_oversized_key_is_rejected_before_reaching_public(client):
    r = client.post("/api/connect", json={"api_key": "x" * 10_000})
    assert r.status_code == 422


def test_while_restarting_the_api_asks_the_page_to_wait(client):
    headers = connect(client, "alice-key")
    app_module.sessions.ready = False  # waiting for another copy on the same database to stop
    r = client.get("/api/session", headers=headers)
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert client.post("/api/connect", json={"api_key": "bob-key"}).status_code == 503
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session": headers["X-Session"]})
        assert ws.receive_json()["type"] == "restarting"
    assert client.get("/healthz").json() == {"ok": True, "ready": False, "sessions": 1}


def test_info_says_whether_sessions_are_saved(client):
    assert client.get("/api/info").json() == {"saves_sessions": False}
