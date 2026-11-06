from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import load_settings
from mt5_execution_engine import (
    AccountInfo,
    BrokerAdapter,
    DailyDrawdownGuard,
    ManagedTradeState,
    PaperBroker,
    PositionInfo,
    PositionProtector,
    SymbolSpec,
    Tick,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
GOLD = SymbolSpec(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0, contract_size=100.0,
                  volume_min=0.01, volume_max=50.0, volume_step=0.01)


@pytest.fixture
def paper(settings, db, fake_feed):
    broker = PaperBroker(settings, db, fake_feed)
    broker.connect()
    return broker


def test_market_buy_fills_at_ask_and_take_profit_closes(paper, fake_feed):
    fake_feed.mid = 2000.0  # spread 20 pts -> 1999.90 / 2000.10
    result = paper.place_market_order("XAUUSD", "BUY", 0.10, sl=1990.0, tp=2010.0)
    assert result.success and result.price == pytest.approx(2000.10)
    account = paper.get_account()
    assert account.margin == pytest.approx(2000.10 * 100 * 0.10 / 100, rel=1e-6)
    fake_feed.mid = 2010.2
    assert paper.get_positions() == []
    summary = paper.get_closed_position_summary(result.position_ticket)
    assert summary.reason == "TP" and summary.price_close == 2010.0
    assert summary.profit == pytest.approx((2010.0 - 2000.10) * 100 * 0.10, abs=0.01)
    assert paper.get_account().balance == pytest.approx(10_000 + summary.profit, abs=0.01)


def test_stop_loss_fills_at_stop_when_price_is_close(paper, fake_feed):
    fake_feed.mid = 2000.0
    ticket = paper.place_market_order("XAUUSD", "BUY", 0.10, sl=1990.0).position_ticket
    fake_feed.mid = 1989.95  # bid 1989.85 <= SL, within one spread
    paper.process_market()
    summary = paper.get_closed_position_summary(ticket)
    assert summary.reason == "SL" and summary.price_close == 1990.0
    assert summary.profit == pytest.approx(-101.0, abs=0.01)


def test_buy_limit_fills_and_keeps_order_ticket(paper, fake_feed):
    fake_feed.mid = 2000.0
    order = paper.place_pending_order("XAUUSD", "BUY", "LIMIT", 1995.0, 0.10, sl=1990.0, tp=2010.0)
    assert order.success and paper.get_pending_orders()[0].price == 1995.0
    fake_feed.mid = 1994.9  # ask 1995.00
    positions = paper.get_positions()
    assert len(positions) == 1 and positions[0].ticket == order.order_ticket
    assert positions[0].price_open == 1995.0 and paper.get_pending_orders() == []


def test_invalid_orders_are_rejected(paper, fake_feed):
    fake_feed.mid = 2000.0
    assert not paper.place_market_order("XAUUSD", "BUY", 0.10, sl=2001.0).success  # SL above bid
    assert not paper.place_pending_order("XAUUSD", "BUY", "LIMIT", 2005.0, 0.1).success  # above ask
    assert not paper.place_market_order("XAUUSD", "BUY", 6.0).success  # 12k margin > 10k equity
    assert not paper.place_market_order("XAUUSD", "BUY", 0.001).success  # below min lot


def test_partial_close_and_state_persists_across_restart(settings, db, fake_feed):
    fake_feed.mid = 2000.0
    broker = PaperBroker(settings, db, fake_feed)
    ticket = broker.place_market_order("XAUUSD", "SELL", 0.10, sl=2010.0, tp=1980.0).position_ticket
    fake_feed.mid = 1995.0
    assert broker.close_position(ticket, 0.05).success
    restored = PaperBroker(settings, db, fake_feed)
    positions = restored.get_positions()
    assert len(positions) == 1 and positions[0].volume == pytest.approx(0.05)
    assert restored.get_account().balance > settings.paper.starting_balance


def test_quote_currency_conversion_for_usdjpy(base_env, db, fake_feed):
    settings = load_settings({**base_env, "SYMBOLS": "USDJPY"})
    broker = PaperBroker(settings, db, fake_feed)
    fake_feed.mid = 150.5
    profit = broker.calc_profit("USDJPY", "BUY", 1.0, 150.00, 150.50)
    assert profit == pytest.approx(0.5 * 100_000 / 150.5, rel=1e-6)


def test_validate_stops_sides():
    assert BrokerAdapter.validate_stops("BUY", 1990, 2010, 2000, 2000.2, GOLD) is None
    assert "below bid" in BrokerAdapter.validate_stops("BUY", 2001, None, 2000, 2000.2, GOLD)
    assert "above ask" in BrokerAdapter.validate_stops("SELL", 1999, None, 2000, 2000.2, GOLD)


# ------------------------------------------------------------------ protector --
def _position(sl: float | None, price: float, direction: str = "BUY", volume: float = 0.10) -> PositionInfo:
    entry = 2000.0
    return PositionInfo(1, 1, "XAUUSD", direction, volume, entry, sl, None, price, 0.0, 0.0, NOW, 770077, "")


def _tick(bid: float, spread: float = 0.2) -> Tick:
    return Tick("XAUUSD", NOW, bid, bid + spread)


def test_breakeven_at_one_r(settings):
    protector = PositionProtector(settings.risk)
    state = ManagedTradeState(1, "XAUUSD", "BUY", 2000.0, 1990.0, 2015.0, 2030.0)
    decision = protector.evaluate(state, _position(1990.0, 2010.0), _tick(2010.0), GOLD, atr=4.0)
    assert decision.events == ["BREAKEVEN"] and decision.mark_be
    assert decision.new_sl == pytest.approx(2000.30)  # 30-point buffer beats 1.2x spread
    assert decision.partial_close_volume is None


def test_tp1_partial_and_trailing_after_1_5r(settings):
    protector = PositionProtector(settings.risk)
    state = ManagedTradeState(1, "XAUUSD", "BUY", 2000.0, 1990.0, 2015.0, 2030.0)
    decision = protector.evaluate(state, _position(1990.0, 2015.0), _tick(2015.0), GOLD, atr=4.0)
    assert "TP1_HIT" in decision.events and decision.partial_close_volume == pytest.approx(0.05)
    assert "TRAILING_ON" in decision.events and decision.new_sl == pytest.approx(2009.0)
    assert decision.mark_be and decision.mark_trailing


def test_stops_never_loosen_and_respect_min_step(settings):
    protector = PositionProtector(settings.risk)
    state = ManagedTradeState(1, "XAUUSD", "BUY", 2000.0, 1990.0, None, None, be_moved=True,
                              trailing_active=True)
    tighter = protector.evaluate(state, _position(2012.0, 2016.0), _tick(2016.0), GOLD, atr=4.0)
    assert tighter.new_sl is None  # candidate 2010 would loosen the 2012 stop
    tiny = protector.evaluate(state, _position(2008.9, 2015.0), _tick(2015.0), GOLD, atr=4.0)
    assert tiny.new_sl is None  # +0.1 improvement < 0.1 x ATR minimum step


def test_sell_side_breakeven_mirrors(settings):
    protector = PositionProtector(settings.risk)
    state = ManagedTradeState(1, "XAUUSD", "SELL", 2000.0, 2010.0, None, None)
    decision = protector.evaluate(state, _position(2010.0, 1989.8, "SELL"), _tick(1989.6), GOLD, atr=4.0)
    assert decision.events == ["BREAKEVEN"] and decision.new_sl == pytest.approx(1999.70)


def test_stops_level_clamps_new_stop(settings):
    protector = PositionProtector(settings.risk)
    wide = SymbolSpec(**{**GOLD.__dict__, "stops_level": 1000})  # 10.00 minimum distance
    state = ManagedTradeState(1, "XAUUSD", "BUY", 2000.0, 1990.0, None, None)
    decision = protector.evaluate(state, _position(1990.0, 2010.0), _tick(2010.0), wide, atr=4.0)
    assert decision.new_sl == pytest.approx(2000.0)


# ------------------------------------------------------------- drawdown guard --
def _account(equity: float, balance: float = 10_000.0) -> AccountInfo:
    return AccountInfo(1, "PAPER", "t", "USD", balance, equity, equity - balance, 0.0, equity, 0.0, 100, True)


def test_daily_drawdown_breach_halt_and_reset(settings, db):
    guard = DailyDrawdownGuard(settings.risk, db)
    first = guard.update(_account(10_000.0), NOW)
    assert first.start_equity == 10_000.0 and not first.breached
    warning = guard.update(_account(9_800.0), NOW + timedelta(minutes=5))
    assert warning.drawdown_pct == pytest.approx(2.0) and not warning.breached
    breach = guard.update(_account(9_690.0), NOW + timedelta(minutes=10))
    assert breach.breached and breach.utilization == 1.0
    guard.halt(breach.trading_day, "test")
    assert guard.update(_account(9_690.0), NOW + timedelta(minutes=11)).halted
    tomorrow = guard.update(_account(9_690.0, 9_690.0), NOW + timedelta(days=1))
    assert not tomorrow.halted and tomorrow.start_equity == 9_690.0 and tomorrow.drawdown_pct == 0.0
