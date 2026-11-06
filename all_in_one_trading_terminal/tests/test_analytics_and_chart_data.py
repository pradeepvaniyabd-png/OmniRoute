from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

import numpy as np
import pandas as pd
import pytest
from conftest import BULLISH_BARS, bullish_frame, trend_frame

import analytics as an
from chart_data import build_chart_payload, epoch_seconds
from config import SessionWindow
from quant_strategy_engine import QuantStrategyEngine

UTC = timezone.utc


def trade(tid, pnl, closed, opened=None, direction="BUY", strategy="Sweep", symbol="XAUUSD", r=None,
          status="CLOSED"):
    return {"id": tid, "pnl": pnl, "closed_at": closed.isoformat() if closed else None,
            "opened_at": (opened or (closed - timedelta(minutes=30))).isoformat() if closed else None,
            "direction": direction, "strategy": strategy, "symbol": symbol, "r_multiple": r, "status": status}


TRADES = [
    trade(1, 100.0, datetime(2026, 11, 4, 10, 0, tzinfo=UTC), r=1.0),
    trade(2, -40.0, datetime(2026, 11, 4, 20, 0, tzinfo=UTC), direction="SELL", strategy="OB", r=-0.4),
    trade(3, 60.0, datetime(2026, 12, 1, 9, 0, tzinfo=UTC), symbol="EURUSD", r=0.6),
    trade(4, 999.0, None, status="OPEN"),
]


def test_daily_pnl_uses_ist_calendar_days():
    days = an.daily_pnl(TRADES)
    assert [(d["date"], d["pnl"]) for d in days] == [("2026-11-04", 100.0), ("2026-11-05", -40.0),
                                                     ("2026-12-01", 60.0)]


def test_monthly_returns_compound_from_start_balance():
    months = an.monthly_returns(TRADES, starting_balance=1000.0)
    assert months[0] == {"month": "2026-11", "pnl": 60.0, "trades": 2, "return_pct": 6.0}
    assert months[1]["return_pct"] == pytest.approx(60 / 1060 * 100, abs=0.01)


def test_breakdowns_long_short_and_time_buckets():
    by_strategy = {row["key"]: row for row in an.breakdown(TRADES, lambda t: t["strategy"])}
    assert by_strategy["Sweep"]["trades"] == 2 and by_strategy["Sweep"]["pnl"] == 160.0
    assert by_strategy["OB"]["win_rate"] == 0.0
    hours = an.by_hour(TRADES)
    assert len(hours) == 24 and sum(h["trades"] for h in hours) == 3
    weekdays = {row["day"]: row for row in an.by_weekday(TRADES)}
    assert weekdays["Wed"]["trades"] >= 1
    windows = (SessionWindow("LONDON", dtime(2, 0), dtime(5, 0)),)
    assert an.session_of(datetime(2026, 11, 4, 8, 0, tzinfo=UTC), windows, "America/New_York") == "LONDON"
    assert an.session_of(datetime(2026, 11, 4, 18, 0, tzinfo=UTC), windows, "America/New_York") == "OFF-SESSION"
    assert an.hold_time(TRADES) == {"avg_minutes": 30.0, "median_minutes": 30.0}


def test_equity_drawdown_and_report():
    points = [{"ts": "a", "equity": 100.0, "balance": 100.0}, {"ts": "b", "equity": 120.0, "balance": 100.0},
              {"ts": "c", "equity": 90.0, "balance": 100.0}, {"ts": "d", "equity": 110.0, "balance": 110.0}]
    dd = an.equity_drawdown(points)
    assert dd["max_dd_amount"] == 30.0 and dd["max_dd_pct"] == 25.0
    assert dd["current_dd_pct"] == pytest.approx(8.333, abs=0.001)
    report = an.build_report(TRADES, points, 1000.0, (), "America/New_York")
    assert report["stats"]["total_trades"] == 3 and report["long"]["trades"] == 2
    assert report["short"]["pnl"] == -40.0 and report["cum_pnl"][-1]["cum_pnl"] == 120.0
    json.dumps(report)  # API-serialisable


def test_correlation_matrix_detects_direction():
    index = pd.date_range("2026-11-01", periods=120, freq="1h", tz="UTC")
    base = pd.Series(100 + np.cumsum(np.sin(np.arange(120) / 3.0)), index=index)
    result = an.correlation_matrix({"A": base, "B": base * 2 + 5, "C": 400 - base})
    matrix = dict(zip(result["symbols"], result["matrix"]))
    assert matrix["A"][1] == pytest.approx(1.0, abs=1e-6)
    assert matrix["A"][2] < -0.9
    assert an.correlation_matrix({"A": base})["matrix"] == [[1.0]]


def test_volatility_row_adr_and_usage():
    index = pd.date_range("2026-11-01", periods=24 * 16, freq="1h", tz="UTC")
    day = np.repeat(np.arange(16), 24)
    mid = 2000.0 + np.zeros(len(index))
    span = np.where(day == 15, 5.0, 10.0)  # every day 20 wide, today 10 wide
    frame = pd.DataFrame({"open": mid, "high": mid + span, "low": mid - span, "close": mid}, index=index)
    row = an.volatility_row("XAUUSD", frame, pip=0.1, atr_ltf=2.0)
    assert row["adr"] == pytest.approx(20.0) and row["today_range"] == pytest.approx(10.0)
    assert row["used_pct"] == 50.0 and row["adr_pips"] == pytest.approx(200.0)
    assert row["atr_ltf_pips"] == pytest.approx(20.0)


def test_chart_payload_contains_zones_markers_signals_and_levels(settings):
    start = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
    now = start + timedelta(minutes=5 * len(BULLISH_BARS), seconds=1)
    ltf = bullish_frame(start)
    engine = QuantStrategyEngine(settings.strategy, settings.risk, "M5", "M15", "H1")
    analysis = engine.analyze("XAUUSD", ltf, trend_frame(now, 15), trend_frame(now, 60), now=now,
                              bid=2000.4, ask=2000.6)
    payload = build_chart_payload(
        ltf, analysis.features, analysis, symbol="XAUUSD", timeframe="M5", digits=2, ema_period=50,
        session_windows=settings.strategy.session_ranges, session_tz=settings.strategy.killzone_timezone,
        positions=[{"direction": "BUY", "volume": 0.1, "price_open": 2000.2, "sl": 1993.5, "tp": 2020.3,
                    "tp1": 2010.3, "ticket": 7}],
        orders=[{"price": 1999.0, "direction": "BUY", "kind": "LIMIT", "ticket": 8}], now=now)
    assert len(payload["candles"]) == len(BULLISH_BARS)
    assert payload["candles"][0]["time"] == epoch_seconds(ltf.index)[0]
    fvg = [z for z in payload["zones"] if z["kind"] == "FVG" and z["direction"] == "BUY"]
    assert fvg and fvg[-1]["top"] == 2000.2 and fvg[-1]["bottom"] == 1997.8
    texts = {m["text"] for m in payload["markers"]}
    assert "SSL" in texts and "MSS↑" in texts and any(t.startswith("BUY A") for t in texts)
    assert payload["signals"][0]["setup"] == "sweep_mss_fvg"
    assert {lvl["kind"] for lvl in payload["levels"]} == {"entry", "sl", "tp", "tp1", "order"}
    assert [m["time"] for m in payload["markers"]] == sorted(m["time"] for m in payload["markers"])
    json.dumps(payload)


def test_chart_payload_empty_frame(settings):
    payload = build_chart_payload(pd.DataFrame(), None, None, symbol="X", timeframe="M5", digits=2,
                                  ema_period=50, session_windows=(), session_tz="UTC")
    assert payload["candles"] == [] and payload["last"] is None
