"""End-to-end: strategy -> gating -> order -> fill -> TP1/trailing -> close -> journal + Telegram."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
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
    snapshot = worker.get_snapshot()
    assert snapshot["bot_status"] == "RUNNING"
    quote = snapshot["symbols"]["XAUUSD"]  # market-watch quotes refresh from the protector loop
    assert quote["bid"] == pytest.approx(2000.4) and quote["ask"] == pytest.approx(2000.6)
    assert quote["spread_points"] == pytest.approx(20.0) and quote["digits"] == 2
    assert snapshot["drawdown"]["realized_today"] == 0.0
    worker._scan_cycle()
    assert "change_24h_pct" in worker.get_snapshot()["symbols"]["XAUUSD"]
    trades = worker.db.get_trades()
    assert len(trades) == 1, worker.db.get_recent_signals()
    trade = trades[0]
    assert (trade["status"], trade["order_type"], trade["direction"]) == ("PENDING", "LIMIT", "BUY")
    assert trade["entry_price"] == pytest.approx(2000.2) and trade["volume"] > 0
    assert worker.db.get_recent_signals()[0]["status"] == "EXECUTED"
    flush(worker)
    assert any("ORDER PLACED" in t and "BUY XAUUSD" in t and "(Limit order)" in t for t in recorder.texts())

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
    assert worker.get_snapshot()["drawdown"]["realized_today"] == pytest.approx(trade["pnl"])
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


def test_change_24h_from_ltf_closes():
    index = pd.date_range("2026-11-04 00:00", periods=300, freq="5min", tz="UTC")
    closes = pd.Series(range(300), index=index, dtype=float) + 100.0
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes})
    expected = (399.0 / 111.0 - 1.0) * 100.0  # close exactly 24h (288 bars) before the last bar
    assert TradingWorker._change_24h(frame) == pytest.approx(expected, abs=1e-3)
    assert TradingWorker._change_24h(frame.tail(10)) is None


def test_single_instance_lock(tmp_path):
    first = SingleInstanceLock(tmp_path / "worker.lock")
    first.acquire()
    with pytest.raises(WorkerLockError):
        SingleInstanceLock(tmp_path / "worker.lock").acquire()
    first.release()
    second = SingleInstanceLock(tmp_path / "worker.lock")
    second.acquire()
    second.release()


def test_manual_limit_stop_orders_modify_cancel_and_event_feed(rig):
    worker, feed, recorder = rig
    feed.mid = 2000.0
    worker._protect_cycle()
    assert any(e["title"] == "Broker connected" for e in worker.events_since(0))
    wrong_side = worker.preview_manual_trade("XAUUSD", "BUY", 0.5, "pips", 50, order_kind="LIMIT",
                                             entry_price=2005.0)
    assert not wrong_side["ok"] and "below" in wrong_side["error"]
    no_price = worker.preview_manual_trade("XAUUSD", "SELL", 0.5, "pips", 50, order_kind="STOP")
    assert not no_price["ok"] and "price" in no_price["error"]
    assert not worker.preview_manual_trade("XAUUSD", "HOLD", 0.5, "pips", 50)["ok"]
    preview = worker.preview_manual_trade("XAUUSD", "BUY", 0.5, "pips", 50, order_kind="LIMIT",
                                          entry_price=1995.0)
    assert preview["ok"] and preview["entry"] == 1995.0 and preview["sl"] == pytest.approx(1990.0)
    assert preview["market_price"] == pytest.approx(2000.1) and preview["order_kind"] == "LIMIT"

    limit = worker.manual_trade("XAUUSD", "BUY", 0.5, "pips", 50, order_kind="LIMIT", entry_price=1995.0)
    assert limit["ok"] and limit["pending"], limit
    journal = worker.db.get_trade_by_order(limit["order_ticket"])
    assert journal["status"] == "PENDING" and journal["order_type"] == "LIMIT"
    worker._protect_cycle()
    assert worker.db.get_trade_by_order(limit["order_ticket"])["status"] == "PENDING"
    assert worker.cancel_order_manual(limit["order_ticket"])["ok"]
    assert worker.db.get_trade_by_order(limit["order_ticket"])["status"] == "CANCELLED"
    assert not worker.cancel_order_manual(limit["order_ticket"])["ok"]  # already gone

    stop = worker.manual_trade("XAUUSD", "SELL", 0.5, "pips", 50, order_kind="STOP", entry_price=1990.0)
    assert stop["ok"], stop
    feed.mid = 1989.0
    worker._protect_cycle()
    filled = worker.db.get_trade_by_order(stop["order_ticket"])
    assert filled["status"] == "OPEN" and filled["position_ticket"]
    ticket = int(filled["position_ticket"])
    assert worker.modify_position_manual(ticket, sl=1994.0, tp=1970.0)["ok"]
    position = next(p for p in worker.broker.get_positions() if p.ticket == ticket)
    assert position.sl == pytest.approx(1994.0) and position.tp == pytest.approx(1970.0)
    updated = worker.db.get_trade(filled["id"])
    assert updated["sl_current"] == pytest.approx(1994.0) and updated["tp2"] == pytest.approx(1970.0)
    assert not worker.modify_position_manual(ticket)["ok"]
    assert not worker.modify_position_manual(ticket, sl=-1.0)["ok"]

    titles = [e["title"] for e in worker.events_since(0)]
    assert any(t.startswith("BUY LIMIT placed") for t in titles)
    assert any(t.startswith("Order cancelled") for t in titles)
    assert any(t.startswith("Order filled") for t in titles)
    assert any(t.startswith("Stops updated") for t in titles)
    last = worker.last_event_id
    assert worker.events_since(last) == [] and worker.get_snapshot()["last_event_id"] == last
    assert [e["id"] for e in worker.events_since(0, limit=2)] == [last - 1, last]
    flush(worker)
    texts = recorder.texts()
    assert any("ORDER PLACED" in text and "(Stop order)" in text for text in texts), texts
    assert any("ENTRY TRIGGERED" in text for text in texts), texts


def test_web_tools_specs_sparklines_correlation_volatility(rig):
    worker, _feed, _recorder = rig
    worker._protect_cycle()
    worker._scan_cycle()
    specs = worker.symbol_specs()
    row = specs["symbols"][0]
    assert specs["account_currency"] and specs["leverage"] == 100
    assert row["available"] and row["symbol"] == "XAUUSD" and row["digits"] == 2
    assert row["pip"] == pytest.approx(0.1) and row["pip_value"] > 0
    assert row["margin_per_lot"] == pytest.approx(row["notional_per_lot"] / 100)
    assert len(worker.sparklines()["XAUUSD"]) > 2
    corr = worker.correlation("H1", 50)
    assert corr["symbols"] == ["XAUUSD"] and corr["matrix"] == [[1.0]] and corr["timeframe"] == "H1"
    assert worker.correlation("H1", 50) is corr  # cached
    vol = worker.volatility()
    assert vol[0]["symbol"] == "XAUUSD" and vol[0]["digits"] == 2 and vol[0]["atr_ltf"] is not None
    json.dumps({"specs": specs, "corr": corr, "vol": vol})


def test_paper_account_reset(rig):
    worker, feed, _recorder = rig
    feed.mid = 2000.0
    worker._protect_cycle()
    opened = worker.manual_trade("XAUUSD", "BUY", 0.5, "atr", 1.5)
    assert opened["ok"], opened
    worker._protect_cycle()
    with pytest.raises(TypeError):
        worker.reset_paper_account(1000.0, True, "extra")  # signature guard
    assert not worker.reset_paper_account(-5)["ok"]
    reset = worker.reset_paper_account(5000.0)
    assert reset["ok"] and reset["balance"] == pytest.approx(5000.0)
    assert worker.broker.get_positions() == []
    assert worker.db.get_trade_by_position(opened["ticket"])["status"] == "CANCELLED"
    worker._protect_cycle()
    snapshot = worker.get_snapshot()
    assert snapshot["account"]["balance"] == pytest.approx(5000.0)
    assert snapshot["drawdown"]["drawdown_pct"] == 0.0 and not snapshot["drawdown"]["halted"]
    wiped = worker.reset_paper_account(clear_history=True)
    assert wiped["ok"] and wiped["trades"] >= 1 and worker.db.get_trades() == []
    assert any(e["title"] == "Paper account reset" for e in worker.events_since(0))
