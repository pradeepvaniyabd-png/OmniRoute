"""MT5Broker against an in-memory fake of the MetaTrader5 package API (Windows-only in reality)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

import mt5_execution_engine as mee
from config import load_settings


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_RETCODE_DONE, TRADE_RETCODE_REQUOTE, TRADE_RETCODE_INVALID_FILL = 10009, 10004, 10030
    TRADE_RETCODE_NO_CHANGES = 10025
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_SL, DEAL_REASON_TP = 4, 5
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, offset: int = 7200):
        self.offset = offset
        self.clock = time.time()
        self.positions: dict[int, SimpleNamespace] = {}
        self.deals: list[SimpleNamespace] = []
        self.requests: list[dict] = []
        self.send_script: list[int] = []
        self.terminal_alive = True
        self.initialize_calls = 0

    # session ---------------------------------------------------------------
    def initialize(self, *args, **kwargs):
        self.initialize_calls += 1
        self.terminal_alive = True
        return True

    def shutdown(self):
        return None

    def login(self, *args, **kwargs):
        return True

    def last_error(self):
        return (1, "Success")

    def account_info(self):
        return SimpleNamespace(login=123, server="Demo-Server", name="Tester", currency="USD",
                               balance=10_000.0, equity=10_000.0, profit=0.0, margin=0.0,
                               margin_free=10_000.0, margin_level=0.0, leverage=100,
                               trade_allowed=True, trade_expert=True, company="FakeBroker")

    def terminal_info(self):
        if not self.terminal_alive:
            return None
        return SimpleNamespace(connected=True, trade_allowed=True, ping_last=25_000)

    # symbols ---------------------------------------------------------------
    def symbol_info(self, name):
        if name != "XAUUSDm":
            return None
        return SimpleNamespace(name="XAUUSDm", digits=2, point=0.01, trade_tick_size=0.01,
                               trade_tick_value=1.0, trade_tick_value_loss=1.0,
                               trade_contract_size=100.0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, trade_stops_level=10, trade_freeze_level=0,
                               filling_mode=2, currency_base="XAU", currency_profit="USD",
                               trade_mode=4, description="Gold vs USD")

    def symbols_get(self, pattern):
        return (SimpleNamespace(name="XAUUSDm"), SimpleNamespace(name="XAUUSDm.pro"))

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        stamp = int(self.clock) + self.offset
        return SimpleNamespace(time=stamp, time_msc=stamp * 1000, bid=2000.0, ask=2000.2, last=0.0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                 ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
        rates = np.zeros(3, dtype=dtype)
        base = 1_762_300_800 + self.offset  # server clock = UTC + offset
        rates["time"] = [base, base + 300, base + 600]
        rates["open"], rates["high"] = [1, 2, 3], [2, 3, 4]
        rates["low"], rates["close"], rates["tick_volume"] = [0.5, 1.5, 2.5], [1.5, 2.5, 3.5], [10, 20, 30]
        return rates

    # trading ---------------------------------------------------------------
    def order_check(self, request):
        if request.get("type_filling") == self.ORDER_FILLING_FOK:
            return SimpleNamespace(retcode=self.TRADE_RETCODE_INVALID_FILL, comment="Unsupported filling mode")
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request):
        self.requests.append(dict(request))
        if self.send_script:
            code = self.send_script.pop(0)
            return SimpleNamespace(retcode=code, comment="scripted", order=0, deal=0, price=0.0, volume=0.0)
        action = request["action"]
        if action == self.TRADE_ACTION_DEAL and "position" not in request:
            ticket = 5000 + len(self.positions) + 1
            self.positions[ticket] = SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol=request["symbol"], type=request["type"],
                volume=request["volume"], price_open=request["price"], sl=request["sl"], tp=request["tp"],
                price_current=request["price"], profit=0.0, swap=0.0, time=int(self.clock) + self.offset,
                magic=request["magic"], comment=request["comment"])
            self.deals.append(SimpleNamespace(ticket=9000 + ticket, position_id=ticket,
                                              entry=self.DEAL_ENTRY_IN, type=request["type"],
                                              volume=request["volume"], price=request["price"],
                                              profit=0.0, commission=-3.5, swap=0.0, fee=0.0,
                                              reason=3, symbol=request["symbol"], time=int(self.clock),
                                              time_msc=int(self.clock) * 1000))
            return SimpleNamespace(retcode=10009, comment="done", order=ticket, deal=9000 + ticket,
                                   price=request["price"], volume=request["volume"])
        if action == self.TRADE_ACTION_SLTP:
            position = self.positions[request["position"]]
            position.sl, position.tp = request["sl"], request["tp"]
        return SimpleNamespace(retcode=10009, comment="done", order=1, deal=0, price=0.0, volume=0.0)

    def positions_get(self, symbol=None, ticket=None):
        if ticket is not None:
            return tuple(p for p in self.positions.values() if p.ticket == ticket)
        return tuple(self.positions.values())

    def orders_get(self, symbol=None):
        return ()

    def history_deals_get(self, ticket=None, position=None):
        if ticket is not None:
            return tuple(d for d in self.deals if d.ticket == ticket)
        return tuple(d for d in self.deals if d.position_id == position)

    def order_calc_profit(self, order_type, symbol, volume, price_open, price_close):
        sign = 1 if order_type == self.ORDER_TYPE_BUY else -1
        return (price_close - price_open) * 100.0 * volume * sign


@pytest.fixture
def fake(monkeypatch):
    stub = FakeMT5()
    monkeypatch.setattr(mee, "mt5", stub)
    monkeypatch.setattr(mee, "MT5_AVAILABLE", True)
    return stub


@pytest.fixture
def broker(fake, base_env, db):
    settings = load_settings({**base_env, "BROKER_MODE": "mt5", "MT5_SERVER_UTC_OFFSET": "2",
                              "DXY_FILTER": "false"})
    instance = mee.MT5Broker(settings, db)
    assert instance.connect()
    return instance


def test_unavailable_package_raises_clear_error(monkeypatch, base_env, db):
    monkeypatch.setattr(mee, "MT5_AVAILABLE", False)
    with pytest.raises(mee.BrokerUnavailableError, match="BROKER_MODE=paper"):
        mee.MT5Broker(load_settings({**base_env, "BROKER_MODE": "mt5"}), db)


def test_symbol_resolution_wildcard_picks_shortest_match(broker):
    assert broker.resolve_symbol("XAUUSD") == "XAUUSDm"
    spec = broker.get_symbol_spec("XAUUSDm")
    assert spec.stops_level == 10 and spec.tick_value == 1.0 and spec.digits == 2


def test_rates_are_converted_from_server_clock_to_utc(broker):
    frame = broker.get_rates("XAUUSDm", "M5", 3)
    assert str(frame.index[0]) == "2025-11-05 00:00:00+00:00"
    assert list(frame["volume"]) == [10.0, 20.0, 30.0]
    tick = broker.get_tick("XAUUSDm")
    assert abs(tick.time_utc.timestamp() - time.time()) < 5  # server +2h removed


def test_market_order_falls_back_to_supported_filling_and_retries_requote(broker, fake):
    fake.send_script = [fake.TRADE_RETCODE_REQUOTE]
    result = broker.place_market_order("XAUUSDm", "BUY", 0.25, sl=1990.0, tp=2030.0, comment="test")
    assert result.success and result.position_ticket == 5001
    assert fake.requests[-1]["type_filling"] == fake.ORDER_FILLING_IOC  # FOK unsupported by symbol
    assert len(fake.requests) == 2  # one requote + one fill
    assert fake.requests[-1]["price"] == 2000.2  # refreshed ask


def test_stops_level_and_spread_filters(broker):
    too_close = broker.place_market_order("XAUUSDm", "BUY", 0.1, sl=1999.95)
    assert not too_close.success and "stops level" in too_close.message
    wide = broker.place_market_order("XAUUSDm", "BUY", 0.1, max_spread_points=10)
    assert not wide.success and "spread" in wide.message


def test_modify_close_and_closed_summary(broker, fake):
    ticket = broker.place_market_order("XAUUSDm", "BUY", 0.2, sl=1990.0, tp=2030.0).position_ticket
    assert broker.modify_position(ticket, sl=1995.0).success
    assert fake.positions[ticket].sl == 1995.0
    assert broker.modify_position(ticket, sl=1995.0).message == "no change"
    positions = broker.get_positions()
    assert positions[0].direction == "BUY" and positions[0].sl == 1995.0
    assert broker.get_closed_position_summary(ticket) is None  # still open
    del fake.positions[ticket]
    fake.deals.append(SimpleNamespace(ticket=1, position_id=ticket, entry=fake.DEAL_ENTRY_OUT,
                                      type=fake.DEAL_TYPE_SELL, volume=0.2, price=2030.0, profit=600.0,
                                      commission=-3.5, swap=-1.0, fee=0.0, reason=fake.DEAL_REASON_TP,
                                      symbol="XAUUSDm", time=int(fake.clock) + fake.offset,
                                      time_msc=(int(fake.clock) + fake.offset) * 1000))
    summary = broker.get_closed_position_summary(ticket)
    assert summary.reason == "TP" and summary.direction == "BUY"
    assert summary.profit == pytest.approx(600.0 - 3.5 - 3.5 - 1.0)
    assert summary.price_close == 2030.0


def test_reconnects_when_terminal_ipc_drops(broker, fake):
    calls = fake.initialize_calls
    fake.terminal_alive = False
    assert broker.ensure_connected() is True
    assert fake.initialize_calls == calls + 1


def test_server_offset_auto_detection_uses_live_ticks(fake, base_env, db):
    settings = load_settings({**base_env, "BROKER_MODE": "mt5", "DXY_FILTER": "false"})
    instance = mee.MT5Broker(settings, db)
    instance.connect()  # first observation only records tick times
    fake.clock += 1  # market is live: the tick advanced
    instance._refresh_offset()
    assert instance.server_offset_seconds() == 7200
    assert db.kv_get(mee.MT5Broker.OFFSET_KEY) == 7200


def test_loss_per_lot_uses_broker_calculation(broker):
    assert broker.calc_loss_per_lot("XAUUSDm", "BUY", 2000.0, 1990.0) == pytest.approx(1000.0)
