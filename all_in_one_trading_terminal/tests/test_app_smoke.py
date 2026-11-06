"""Dashboard smoke test: the real app.py renders and reacts with zero exceptions (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import streamlit as st
from conftest import ROOT, FakeFeed, bullish_frame, floor_time, trend_frame
from streamlit.testing.v1 import AppTest

import background_worker
import config
import forex_factory_engine
import mt5_execution_engine
from forex_factory_engine import EconomicEvent

UTC = timezone.utc


@pytest.fixture
def offline_app(tmp_path, monkeypatch):
    env = {"BROKER_MODE": "paper", "SYMBOLS": "XAUUSD", "PAPER_DATA_SOURCES": "XAUUSD=yahoo:FAKE",
           "DATA_DIR": str(tmp_path / "data"), "LOG_DIR": str(tmp_path / "logs"), "AUTO_START_BOT": "false",
           "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHANNEL_ID": "", "TELEGRAM_ADMIN_CHAT_ID": "",
           "DXY_FILTER": "false"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()
    now = datetime.now(UTC)
    ltf = bullish_frame(floor_time(now, 5) - timedelta(minutes=5 * 24))
    feed = FakeFeed({"M5": ltf, "M15": trend_frame(now, 15), "H1": trend_frame(now, 60),
                     "H4": trend_frame(now, 240, n=60)}, mid=2000.5)
    monkeypatch.setattr(mt5_execution_engine, "PublicMarketDataFeed", lambda *args, **kwargs: feed)
    event = EconomicEvent("nfp", "Non-Farm Employment Change", "USD", "High", now + timedelta(hours=3))

    def offline_refresh(self, now=None):  # no network: serve a fixed calendar
        if not self.events():
            self.load_events([event])
        return False

    monkeypatch.setattr(forex_factory_engine.ForexFactoryEngine, "refresh_if_due", offline_refresh)
    started: list[background_worker.TradingWorker] = []
    original_start = background_worker.TradingWorker.start

    def tracking_start(self):
        started.append(self)
        original_start(self)

    monkeypatch.setattr(background_worker.TradingWorker, "start", tracking_start)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    yield app, started
    for worker in started:
        worker.stop(timeout=2)
    st.cache_resource.clear()
    config.get_settings.cache_clear()


def _assert_clean(app: AppTest) -> None:
    assert not app.exception, [e.value for e in app.exception]
    boundary = [w.value for w in app.warning if "temporarily unavailable" in str(w.value)]
    assert not boundary, boundary


def test_dashboard_renders_and_controls_work(offline_app):
    app, started = offline_app
    app.run()
    _assert_clean(app)
    assert len(started) == 1
    worker = started[0]
    assert worker.runtime.bot_enabled is False
    app.button(key="btn_start").click().run()
    _assert_clean(app)
    assert worker.runtime.bot_enabled is True
    app.button(key="btn_pause").click().run()
    _assert_clean(app)
    assert worker.runtime.bot_enabled is False
    app.toggle(key="kz_toggle").set_value(False).run()
    _assert_clean(app)
    assert worker.runtime.require_killzone is False


def test_failing_panel_degrades_gracefully(offline_app, monkeypatch):
    app, started = offline_app
    app.run()
    _assert_clean(app)

    def broken_snapshot():
        raise RuntimeError("simulated broker outage")

    monkeypatch.setattr(started[0], "get_snapshot", broken_snapshot)
    app.run()
    assert not app.exception  # the page itself never crashes
    notices = [str(w.value) for w in app.warning if "temporarily unavailable" in str(w.value)]
    assert any("Telemetry is temporarily unavailable" in notice for notice in notices), notices
    assert any("Positions is temporarily unavailable" in notice for notice in notices), notices
    assert app.button(key="btn_start") is not None  # controls keep rendering
