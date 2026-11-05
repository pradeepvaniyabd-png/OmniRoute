"""End-to-end: strategy -> gating -> order -> fill -> TP1/trailing -> close -> journal + Telegram."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import FakeFeed, bullish_frame, floor_time, trend_frame

from background_worker import SingleInstanceLock, TradingWorker, WorkerLockError
from config import load_settings
from forex_factory_engine import EconomicEvent, ForexFactoryEngine
from mt5_execution_engine import PaperBroker
from telegram_broadcaster import TelegramBroadcaster

UTC = timezone.utc


class TelegramRecorder:
    def __init__(self):
        self.messages: list[dict] = []
        self.next_id = 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.messages.append(json.loads(request.content))
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})

    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]


@pytest.fixture
def rig(base_env, db, tmp_path):
    env = {**base_env, "SYMBOLS": "XAUUSD", "PAPER_DATA_SOURCES": "XAUUSD=yahoo:FAKE",
           "REQUIRE_KILLZONE": "false", "DXY_FILTER": "false", "ENABLED_STRATEGIES": "sweep_mss_fvg",
           "AUTO_START_BOT": "true", "TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHANNEL_ID": "@vip",
           "NEWS_DIGEST_ENABLED": "false"}
    settings = load_settings(env)
    last_closed_open = floor_time(datetime.now(UTC), 5) - timedelta(minutes=5)
    ltf = bullish_frame(last_closed_open - timedelta(minutes=5 * 23))
    now = datetime.now(UTC)
    feed = FakeFeed({"M5": ltf, "M15": trend_frame(now, 15), "H1": trend_frame(now, 60),
                     "H4": trend_frame(now, 240, n=60)}, mid=2000.5)
    recorder = TelegramRecorder()
    telegram = TelegramBroadcaster(settings.telegram, db, mode_label="PAPER",
                                   transport=httpx.MockTransport(recorder))
    telegram.CHANNEL_INTERVAL = telegram.PRIVATE_INTERVAL = 0.0
    news = ForexFactoryEngine(settings.news, tmp_path / "ff")
    broker = PaperBroker(settings, db, feed)
    worker = TradingWorker(settings, db, broker=broker, telegram=telegram, news=news, acquire_lock=False)
    telegram.start()
    broker.connect()
    yield worker, feed, recorder
    telegram.stop(timeout=2)


def flush(worker: TradingWorker) -> None:
    deadline = time.monotonic() + 5
    while worker.telegram.pending_count() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_full_trade_lifecycle(rig):
    worker, feed, recorder = rig
    worker._protect_cycle()
    assert worker.get_snapshot()["bot_status"] == "RUNNING"
    worker._scan_cycle()
    trades = worker.db.get_trades()
    assert len(trades) == 1, worker.db.get_recent_signals()
    trade = trades[0]
    assert (trade["status"], trade["order_type"], trade["direction"]) == ("PENDING", "LIMIT", "BUY")
    assert trade["entry_price"] == pytest.approx(2000.2) and trade["volume"] > 0
    assert worker.db.get_recent_signals()[0]["status"] == "EXECUTED"
    flush(worker)
    assert any("TRADE EXECUTED" in t and "BUY XAUUSD" in t for t in recorder.texts())

    feed.mid = 2000.0  # ask 2000.10 <= limit 2000.20 -> fill
    worker._protect_cycle()
    trade = worker.db.get_trade(trade["id"])
    assert trade["status"] == "OPEN" and trade["fill_price"] == pytest.approx(2000.1)

    feed.mid = trade["tp1"] + 0.2  # through TP1 (also > 1.5R -> trailing)
    worker._protect_cycle()
    trade = worker.db.get_trade(trade["id"])
    assert trade["tp1_hit"] == 1 and trade["be_moved"] == 1
    assert trade["sl_current"] > trade["fill_price"]
    assert trade["volume_open"] == pytest.approx(trade["volume"] / 2, abs=0.011)

    feed.mid = trade["sl_current"] - 0.5  # reverse into the protected stop
    worker._protect_cycle()
    trade = worker.db.get_trade(trade["id"])
    assert trade["status"] == "CLOSED" and trade["close_reason"] == "SL"
    assert trade["pnl"] > 0 and trade["r_multiple"] > 0
    flush(worker)
    texts = recorder.texts()
    assert any("ENTRY TRIGGERED" in t for t in texts)
    assert any("TP1 HIT" in t for t in texts)
    assert any("TRAILING STOP ACTIVATED" in t or "BREAKEVEN" in t for t in texts)
    assert any("CLOSED AT PROTECTED STOP" in t for t in texts)
    signal_card_id = trade["telegram_message_id"]
    assert signal_card_id and any(m.get("reply_parameters", {}).get("message_id") == signal_card_id
                                  for m in recorder.messages)
    assert worker.db.get_performance_stats()["wins"] == 1


def test_exposure_limit_holds_within_a_single_scan_cycle(rig):
    """Regression: two setups on one symbol in the same scan must not both execute."""
    worker, _feed, _recorder = rig
    worker._protect_cycle()
    worker._scan_cycle()  # executes the sweep setup (pending limit order)
    assert len(worker.db.get_trades()) == 1
    first = worker.get_analysis("XAUUSD").signals[0]
    twin = dataclasses.replace(first, setup="order_block", strategy="SMC Order Block Retest")
    worker.runtime.enabled_strategies.append("order_block")
    symbol = worker._broker_symbol("XAUUSD")
    spec, tick = worker.broker.get_symbol_spec(symbol), worker.broker.get_tick(symbol)
    worker._handle_signal(twin, symbol, spec, tick, datetime.now(UTC))  # no protector cycle between
    assert len(worker.db.get_trades()) == 1
    assert worker.db.get_signal(twin.signal_id)["reason"] == "max positions for symbol reached"


def test_news_blackout_blocks_new_entries(rig):
    worker, _feed, _recorder = rig
    soon = datetime.now(UTC) + timedelta(minutes=5)
    worker.news.load_events([EconomicEvent("nfp", "Non-Farm Employment Change", "USD", "High", soon)])
    worker._news_cycle()
    worker._protect_cycle()
    assert worker.get_snapshot()["bot_status"] == "NEWS LOCKED"
    worker._scan_cycle()
    assert worker.db.get_trades() == []
    signal = worker.db.get_recent_signals()[0]
    assert signal["status"] == "SKIPPED" and "news blackout" in signal["reason"]


def test_paused_bot_skips_but_can_resume(rig):
    worker, _feed, _recorder = rig
    worker.pause_bot("test")
    worker._protect_cycle()
    worker._scan_cycle()
    assert worker.db.get_trades() == []
    assert worker.db.get_recent_signals()[0]["reason"] == "bot paused"
    worker.start_bot("test")
    worker._last_analyzed.clear()  # transient skips are re-evaluated on the next bar
    worker._scan_cycle()
    assert len(worker.db.get_trades()) == 1


def test_circuit_breaker_closes_everything_and_halts(rig):
    worker, feed, recorder = rig
    worker._protect_cycle()
    feed.mid = 2000.0
    result = worker.manual_trade("XAUUSD", "BUY", 1.0, "pips", 50)
    assert result["ok"], result
    feed.mid = 1960.0  # far below the stop -> equity drop well beyond 3%
    worker.broker._positions[result["ticket"]]["sl"] = None  # simulate a missing broker stop
    worker._protect_cycle()
    snapshot = worker.get_snapshot()
    assert snapshot["drawdown"]["halted"] and snapshot["bot_status"] == "HALTED"
    trade = worker.db.get_trade_by_position(result["ticket"])
    assert trade["status"] == "CLOSED" and trade["close_reason"] == "CIRCUIT_BREAKER"
    blocked = worker.manual_trade("XAUUSD", "BUY", 1.0, "pips", 50)
    assert not blocked["ok"] and "circuit breaker" in blocked["error"].lower()
    flush(worker)
    assert any("CIRCUIT BREAKER" in t for t in recorder.texts())


def test_panic_close_and_manual_controls(rig):
    worker, feed, _recorder = rig
    worker._protect_cycle()
    feed.mid = 2000.0
    first = worker.manual_trade("XAUUSD", "BUY", 0.5, "atr", 1.5)
    assert first["ok"], first
    worker._protect_cycle()
    feed.mid = 2006.0
    worker._protect_cycle()
    breakeven = worker.move_to_breakeven(first["ticket"])
    assert breakeven["ok"] and breakeven["sl"] > 2000.0
    outcome = worker.panic_close_all("test")
    assert outcome["ok"] and outcome["closed"] == 1
    worker._protect_cycle()
    assert worker.db.get_trade_by_position(first["ticket"])["close_reason"] == "PANIC"
    assert worker.runtime.bot_enabled is False


def test_single_instance_lock(tmp_path):
    first = SingleInstanceLock(tmp_path / "worker.lock")
    first.acquire()
    with pytest.raises(WorkerLockError):
        SingleInstanceLock(tmp_path / "worker.lock").acquire()
    first.release()
    second = SingleInstanceLock(tmp_path / "worker.lock")
    second.acquire()
    second.release()
