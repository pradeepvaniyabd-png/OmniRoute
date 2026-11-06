"""Web backend: auth, CSRF guards, trading round-trip, data endpoints, WebSocket feed (offline)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeFeed, bullish_frame, floor_time, trend_frame
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from background_worker import TradingWorker
from config import ConfigError, load_settings
from forex_factory_engine import EconomicEvent, ForexFactoryEngine
from mt5_execution_engine import PaperBroker
from server import SESSION_COOKIE, SessionSigner, create_app, origin_allowed, to_jsonable
from telegram_broadcaster import TelegramBroadcaster

UTC = timezone.utc
PASSWORD = "s3cret-pass"
TRADE = {"symbol": "xauusd", "direction": "buy", "risk_percent": 0.5, "sl_mode": "pips", "sl_value": 50}


@pytest.fixture
def web(base_env, db, tmp_path):
    env = {**base_env, "PAPER_DATA_SOURCES": "XAUUSD=yahoo:FAKE", "REQUIRE_KILLZONE": "false",
           "DXY_FILTER": "false", "ENABLED_STRATEGIES": "sweep_mss_fvg", "AUTO_START_BOT": "false",
           "NEWS_DIGEST_ENABLED": "false", "UI_REFRESH_SECONDS": "0.5", "CHART_REFRESH_SECONDS": "1",
           "WEB_PASSWORD": PASSWORD, "WEB_SESSION_SECRET": "unit-test-secret-" + "x" * 32}
    settings = load_settings(env)
    now = datetime.now(UTC)
    ltf = bullish_frame(floor_time(now, 5) - timedelta(minutes=5 * 24))
    feed = FakeFeed({"M5": ltf, "M15": trend_frame(now, 15), "H1": trend_frame(now, 60),
                     "H4": trend_frame(now, 240, n=60)}, mid=2000.5)
    news = ForexFactoryEngine(settings.news, tmp_path / "ff")
    news.load_events([EconomicEvent("nfp", "Non-Farm Employment Change", "USD", "High", now + timedelta(hours=3))])
    telegram = TelegramBroadcaster(settings.telegram, db, mode_label="PAPER")
    broker = PaperBroker(settings, db, feed)
    worker = TradingWorker(settings, db, broker=broker, telegram=telegram, news=news, acquire_lock=False)
    broker.connect()
    worker._protect_cycle()
    worker._scan_cycle()
    app = create_app(settings, worker=worker, start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, worker, feed


def login(client: TestClient, password: str = PASSWORD):
    return client.post("/api/login", json={"password": password})


def test_session_signer_rejects_tampering_expiry_and_password_change():
    signer = SessionSigner(b"k" * 32, "pw", 60)
    token = signer.issue()
    assert signer.verify(token)
    expires, nonce, sig = token.split(".")
    assert not signer.verify(f"{int(expires) + 999}.{nonce}.{sig}")
    assert not signer.verify(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not signer.verify("garbage") and not signer.verify(None)
    assert not SessionSigner(b"k" * 32, "new-password", 60).verify(token)
    assert not SessionSigner(b"k" * 32, "pw", -1).verify(SessionSigner(b"k" * 32, "pw", -1).issue())
    signer.revoke(token)
    assert not signer.verify(token)


def test_origin_check_and_json_sanitizer():
    assert origin_allowed({"host": "127.0.0.1:8800"})
    assert origin_allowed({"host": "127.0.0.1:8800", "origin": "http://127.0.0.1:8800"})
    assert not origin_allowed({"host": "127.0.0.1:8800", "origin": "http://evil.example"})
    assert not origin_allowed({"host": "127.0.0.1:8800", "origin": "null"})
    data = to_jsonable({"a": float("nan"), "b": [float("inf"), 1.5], "c": datetime(2026, 11, 6, tzinfo=UTC), "d": {1, 2}})
    assert data["a"] is None and data["b"] == [None, 1.5] and data["c"].startswith("2026-11-06")
    assert sorted(data["d"]) == [1, 2]


def test_remote_bind_requires_password(base_env):
    with pytest.raises(ConfigError):
        load_settings({**base_env, "WEB_HOST": "0.0.0.0"})
    assert load_settings({**base_env, "WEB_HOST": "0.0.0.0", "WEB_PASSWORD": "pw"}).web.auth_enabled
    assert load_settings({**base_env, "WEB_HOST": "0.0.0.0", "WEB_ALLOW_INSECURE_REMOTE": "true"})


def test_auth_gate_lockout_and_logout(web):
    client, worker, _feed = web
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/session").json() == {"auth_enabled": True, "authenticated": False}
    denied = client.get("/api/snapshot")
    assert denied.status_code == 401 and denied.json() == {"ok": False, "error": "login required"}
    page = client.get("/trade", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/trade"
    assert "Sign in" in client.get("/login").text
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    locked = login(client)
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    client.app.state.ctx.throttle.success("testclient")
    ok = login(client)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    cookie_header = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=strict" in cookie_header
    token = client.cookies.get(SESSION_COOKIE)
    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/snapshot").status_code == 200
    assert client.get("/trade").status_code == 200 and 'id="app"' in client.get("/trade").text
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.post("/api/logout", json={}).json()["ok"] is True
    assert client.get("/api/snapshot").status_code == 401
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/api/snapshot").status_code == 401  # revoked on logout


def test_write_guards_block_csrf_and_bad_payloads(web):
    client, worker, _feed = web
    login(client)
    assert client.post("/api/bot/start", content="x", headers={"content-type": "text/plain"}).status_code == 415
    blocked = client.post("/api/bot/start", json={}, headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403 and worker.runtime.bot_enabled is False
    assert client.post("/api/bot/start", json={}, headers={"origin": "http://testserver"}).status_code == 200
    assert worker.runtime.bot_enabled is True
    assert client.post("/api/bot/start", json={"unexpected": 1}).status_code == 422
    assert client.post("/api/bot/panic", json={}).status_code == 422
    panic = client.post("/api/bot/panic", json={"confirm": True})
    assert panic.status_code == 200 and panic.json()["ok"] and worker.runtime.bot_enabled is False
    assert client.post("/api/settings/risk", json={"risk_percent": 25}).status_code == 422
    assert client.post("/api/settings/risk", json={"risk_percent": 0.75}).json()["risk_percent"] == 0.75
    assert client.post("/api/settings/strategies", json={"strategies": ["nope"]}).status_code == 422
    updated = client.post("/api/settings/strategies", json={"strategies": ["order_block", "sweep_mss_fvg"]})
    assert set(updated.json()["strategies"]) == {"order_block", "sweep_mss_fvg"}
    assert client.post("/api/settings/execution", json={"mode": "signal_only"}).json()["execution_mode"] == "signal_only"
    assert client.post("/api/settings/killzone", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/api/settings/news-pause", json={"enabled": False}).json()["enabled"] is False


def test_manual_trading_round_trip(web):
    client, worker, feed = web
    login(client)
    feed.mid = 2000.0
    worker._protect_cycle()
    preview = client.post("/api/trade/preview", json=TRADE).json()
    assert preview["ok"] and preview["lots"] > 0 and preview["direction"] == "BUY"
    assert client.post("/api/trade/preview", json={**TRADE, "symbol": "DOGEUSD"}).status_code == 404
    assert client.post("/api/trade/preview", json={**TRADE, "risk_percent": -1}).status_code == 422
    placed = client.post("/api/trade/place", json=TRADE)
    assert placed.status_code == 200 and placed.json()["ok"], placed.json()
    ticket = placed.json()["ticket"]
    worker._protect_cycle()
    positions = client.get("/api/snapshot").json()["positions"]
    assert [p["ticket"] for p in positions] == [ticket] and positions[0]["canonical"] == "XAUUSD"
    assert client.post(f"/api/positions/{ticket}/modify", json={"sl": 1996.0}).json()["ok"]
    assert client.post(f"/api/positions/{ticket}/breakeven", json={}).status_code == 400  # not in profit yet
    assert client.post(f"/api/positions/{ticket}/close", json={}).json()["ok"]
    worker._protect_cycle()
    closed = client.get("/api/trades", params={"status": "closed"}).json()["items"]
    assert closed[0]["close_reason"] == "MANUAL"
    pending = client.post("/api/trade/place", json={**TRADE, "order_kind": "limit", "entry_price": 1990}).json()
    assert pending["ok"] and pending["pending"]
    assert client.post(f"/api/orders/{pending['order_ticket']}/cancel", json={}).json()["ok"]
    missing = client.post("/api/orders/999999/cancel", json={})
    assert missing.status_code == 400 and missing.json()["ok"] is False and missing.json()["error"]
    titles = [e["title"] for e in client.get("/api/events").json()["items"]]
    assert "Manual BUY XAUUSD" in titles and any(t.startswith("Order cancelled") for t in titles)


def test_data_endpoints_and_headers(web):
    client, _worker, _feed = web
    login(client)
    boot = client.get("/api/bootstrap").json()
    assert boot["symbols"] == ["XAUUSD"] and len(boot["strategies"]) == 3 and boot["auth_enabled"] is True
    chart = client.get("/api/chart", params={"symbol": "XAUUSD", "tf": "M5", "bars": 120}).json()
    assert chart["candles"] and chart["digits"] == 2 and chart["timeframe"] == "M5"
    assert client.get("/api/chart", params={"symbol": "XAUUSD", "tf": "X9"}).status_code == 422
    assert client.get("/api/chart", params={"symbol": "EURUSD"}).status_code == 404
    report = client.get("/api/analytics").json()
    assert "stats" in report and report["mode"] == "PAPER"
    calendar = client.get("/api/calendar").json()
    assert calendar["events"][0]["title"] == "Non-Farm Employment Change"
    assert calendar["events"][0]["affected"] == ["XAUUSD"]
    assert "stats" in client.get("/api/telegram").json()
    assert isinstance(client.get("/api/logs", params={"level": "INFO"}).json()["items"], list)
    assert client.get("/api/logs", params={"level": "LOUD"}).status_code == 422
    assert client.get("/api/tools/specs").json()["symbols"][0]["available"] is True
    assert client.get("/api/tools/correlation", params={"tf": "H1", "bars": 50}).json()["symbols"] == ["XAUUSD"]
    assert client.get("/api/tools/volatility").json()["items"][0]["symbol"] == "XAUUSD"
    assert "XAUUSD" in client.get("/api/sparklines").json()["series"]
    assert client.get("/api/does-not-exist").status_code == 404
    response = client.get("/api/snapshot")
    assert response.headers["x-frame-options"] == "DENY" and response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    reset = client.post("/api/paper/reset", json={"confirm": True, "balance": 2500})
    assert reset.json()["ok"] and reset.json()["balance"] == 2500


def test_internal_errors_are_sanitized(web, monkeypatch):
    client, worker, _feed = web
    login(client)

    def explode():
        raise RuntimeError("secret detail at /home/user/private.py")

    monkeypatch.setattr(worker, "get_snapshot", explode)
    response = client.get("/api/snapshot")
    assert response.status_code == 500
    assert "secret" not in response.text and "Traceback" not in response.text and "/home/" not in response.text
    assert response.json()["ok"] is False


def test_websocket_feed(web):
    client, worker, _feed = web
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert denied.value.code == 4401
    login(client)
    with pytest.raises(WebSocketDisconnect) as cross_site:
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert cross_site.value.code == 1008
    worker._emit("bot", "Unit test event")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        first = ws.receive_json()
        assert first["type"] == "snapshot" and "account" in first["data"]
        ws.send_json({"op": "chart", "symbol": "XAUUSD", "tf": "M5", "bars": 120})
        ws.send_json({"op": "chart", "symbol": "NOPE"})
        ws.send_json({"op": "ping"})
        seen: dict[str, dict] = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not {"chart", "pong", "error", "events"} <= seen.keys():
            message = ws.receive_json()
            seen.setdefault(message["type"], message)
        assert seen["chart"]["data"]["candles"] and seen["chart"]["data"]["symbol"] == "XAUUSD"
        assert "watchlist" in seen["error"]["error"]
        assert any(e["title"] == "Unit test event" for e in seen["events"]["items"])
        while time.monotonic() < deadline:
            message = ws.receive_json()
            if message["type"] == "chart_tick":
                assert message["data"]["candle"]["time"] == seen["chart"]["data"]["candles"][-1]["time"]
                break
        else:
            pytest.fail("no incremental chart_tick received")
