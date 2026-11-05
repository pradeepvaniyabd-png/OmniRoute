from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import time as dtime

import pytest

from config import ConfigError, SessionWindow, load_settings, pip_size_for, symbol_value
from database import compute_performance

UTC = timezone.utc


def test_defaults_and_maps(base_env):
    settings = load_settings({**base_env, "SYMBOLS": "xauusd, btcusd",
                              "PAPER_DATA_SOURCES": "XAUUSD=yahoo:GC=F",
                              "MAX_SPREAD_POINTS": "XAUUSD=40", "TELEGRAM_CHANNEL_ID": '"@vip"'})
    assert settings.symbols == ("XAUUSD", "BTCUSD")
    assert settings.paper.data_sources["XAUUSD"] == "yahoo:GC=F"
    assert symbol_value(settings.risk.max_spread_points, "XAUUSDm", 99) == 40.0
    assert settings.telegram.channel_id == "@vip"
    assert settings.strategy.time_candle_times_ist == (dtime(20, 0), dtime(20, 45))


@pytest.mark.parametrize("overrides, message", [
    ({"RISK_PERCENT": "3", "MAX_RISK_PERCENT": "2"}, "RISK_PERCENT"),
    ({"LTF_TIMEFRAME": "H1", "MTF_TIMEFRAME": "M15"}, "LTF < MTF"),
    ({"TP1_RR": "3", "TP2_RR": "2"}, "TP2_RR"),
    ({"BROKER_MODE": "demo"}, "BROKER_MODE"),
    ({"KILLZONES": "LONDON=0200"}, "KILLZONES"),
    ({"PAPER_DATA_SOURCES": "XAUUSD=csv:file"}, "PAPER_DATA_SOURCES"),
    ({"DAILY_RESET_TIMEZONE": "Mars/Olympus"}, "time zone"),
])
def test_invalid_configuration_fails_fast(base_env, overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**base_env, **overrides})


def test_session_window_crossing_midnight():
    window = SessionWindow("ASIA", dtime(20, 0), dtime(0, 0))
    assert window.contains(dtime(23, 30)) and window.contains(dtime(20, 0))
    assert not window.contains(dtime(0, 0)) and not window.contains(dtime(12, 0))


def test_pip_sizes():
    assert pip_size_for("XAUUSD") == 0.1
    assert pip_size_for("USDJPY") == 0.01
    assert pip_size_for("EURUSD", 0.00001, 5) == pytest.approx(0.0001)
    assert pip_size_for("BTCUSDm") == 1.0


def test_compute_performance_metrics():
    stats = compute_performance([100, -50, 0, 200, -25], [1.0, -1.0, 0.0, 2.0, -0.5])
    assert stats["wins"] == 2 and stats["losses"] == 2 and stats["breakeven"] == 1
    assert stats["win_rate"] == 40.0
    assert stats["profit_factor"] == 4.0
    assert stats["net_pnl"] == 225 and stats["expectancy"] == 45
    assert stats["max_drawdown"] == 50 and stats["max_consecutive_losses"] == 1
    assert stats["avg_r"] == pytest.approx(0.3)
    assert compute_performance([])["total_trades"] == 0


def test_trade_journal_lifecycle(db):
    trade_id = db.insert_trade(mode="PAPER", symbol="XAUUSD", broker_symbol="XAUUSD", direction="BUY",
                               order_type="MARKET", strategy="Test", status="OPEN", volume=0.1,
                               entry_price=2000.0, sl_initial=1990.0, position_ticket=7,
                               confluences=["FVG", "MSS"], be_moved=False)
    assert db.get_trade_by_position(7, "PAPER")["confluences"] == ["FVG", "MSS"]
    assert len(db.get_active_trades("PAPER")) == 1
    db.update_trade(trade_id, status="CLOSED", pnl=-40.0, r_multiple=-0.4,
                    closed_at=datetime.now(UTC))
    assert db.get_active_trades() == []
    assert db.last_closed_trade("XAUUSD", "PAPER")["pnl"] == -40.0
    assert db.realized_pnl_since(datetime.now(UTC) - timedelta(hours=1)) == -40.0
    assert db.count_trades_since(datetime.now(UTC) - timedelta(hours=1), "PAPER") == 1
    assert db.get_strategy_breakdown()[0]["trades"] == 1
    db.update_trade(trade_id, bogus_column=1)  # unknown columns are ignored, not executed


def test_signals_broadcasts_kv_and_daily_stats(db):
    record = {"signal_id": "abc", "symbol": "XAUUSD", "direction": "BUY", "strategy": "S",
              "confluences": ["x"], "created_at": datetime.now(UTC)}
    assert db.insert_signal(record) is True
    assert db.insert_signal(record) is False
    db.update_signal("abc", "EXECUTED", "ok")
    assert db.get_signal("abc")["status"] == "EXECUTED"
    row = db.reserve_broadcast("NEWS_WARNING", "news:1", "@c")
    assert row and db.reserve_broadcast("NEWS_WARNING", "news:1", "@c") is None
    db.mark_broadcast(row, "SENT", 55)
    assert db.get_recent_broadcasts(1)[0]["telegram_message_id"] == 55
    db.kv_set("runtime", {"risk": 1.5})
    assert db.kv_get("runtime") == {"risk": 1.5} and db.kv_get("missing", 3) == 3
    db.upsert_daily_stats("2026-11-05", start_equity=1000.0, start_balance=1000.0)
    db.upsert_daily_stats("2026-11-05", halted=True, halt_reason="limit")
    stats = db.get_daily_stats("2026-11-05")
    assert stats["halted"] == 1 and stats["start_equity"] == 1000.0
    db.insert_log(datetime.now(UTC).isoformat(), "WARNING", "test", "hello")
    assert db.get_logs(5, "WARNING")[0]["message"] == "hello"
