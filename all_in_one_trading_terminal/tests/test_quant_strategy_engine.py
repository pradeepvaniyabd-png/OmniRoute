from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from conftest import BULLISH_BARS, bullish_frame, make_frame, mirror, trend_frame

import quant_strategy_engine as q

UTC = timezone.utc
START = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)  # 02:00 New York (EST) -> London killzone
NOW = START + timedelta(minutes=5 * len(BULLISH_BARS), seconds=1)


def make_engine(settings, **overrides) -> q.QuantStrategyEngine:
    strategy = dataclasses.replace(settings.strategy, **overrides) if overrides else settings.strategy
    return q.QuantStrategyEngine(strategy, settings.risk, "M5", "M15", "H1")


@pytest.fixture
def ltf():
    return bullish_frame(START)


def test_swings_are_confirmed_fractals(ltf):
    swings = q.detect_swings(ltf, 2, 2)
    assert [(s.pos, s.kind, s.price) for s in swings] == [
        (2, "high", 2014.0), (4, "low", 2003.0), (10, "low", 1995.0),
        (14, "high", 2001.5), (18, "low", 1993.8), (21, "high", 2004.5)]
    assert all(s.confirmed_pos == s.pos + 2 for s in swings)


def test_no_lookahead_last_bars_cannot_be_swings(ltf):
    partial = ltf.iloc[:20]  # bar 18's swing low needs bars 19 and 20 to confirm
    assert 18 not in {s.pos for s in q.detect_swings(partial, 2, 2)}


def test_liquidity_sweep_requires_close_back_inside(ltf):
    swings = q.detect_swings(ltf, 2, 2)
    sweeps = q.detect_liquidity_sweeps(ltf, swings)
    assert [(s.pos, s.side, s.level, s.extreme) for s in sweeps] == [(18, "SSL", 1995.0, 1993.8)]
    assert sweeps[0].bias == "BUY"


def test_structure_bos_then_displacement_mss(ltf):
    atr = q.compute_atr(ltf, 14)
    events, trend = q.detect_market_structure(ltf, q.detect_swings(ltf, 2, 2), atr, 1.0)
    assert [(e.pos, e.direction, e.kind, e.level) for e in events] == [
        (7, "SELL", "BOS", 2003.0), (20, "BUY", "MSS", 2001.5)]
    assert events[1].displacement is True
    assert trend == 1


def test_wick_through_swing_high_is_a_sweep_not_a_bos(ltf):
    frame = ltf.copy()
    frame.iloc[20] = [1997.5, 2003.5, 1997.3, 2001.0, 400.0]  # wick above 2001.5, close below
    atr = q.compute_atr(frame, 14)
    swings = q.detect_swings(frame, 2, 2)
    events, _ = q.detect_market_structure(frame, swings, atr, 1.0)
    assert not [e for e in events if e.pos == 20]  # wick only: no structure break on bar 20
    assert any(s.pos == 20 and s.side == "BSL" for s in q.detect_liquidity_sweeps(frame, swings))
    follow_up = [e for e in events if e.direction == "BUY"]
    assert [(e.pos, e.kind) for e in follow_up] == [(21, "CHoCH")]  # bar 21 closes above: real break


def test_fvg_detection_and_fill_tracking(ltf):
    atr = q.compute_atr(ltf, 14)
    gaps = q.detect_fvgs(ltf, atr, None, 0.15)
    bullish = [g for g in gaps if g.direction == "BUY" and g.start_pos == 20][0]
    assert (bullish.bottom, bullish.top) == (1997.8, 2000.2)
    assert bullish.active and bullish.mitigated_pos is None
    bearish = [g for g in gaps if g.direction == "SELL" and g.start_pos == 8][0]
    assert (bearish.bottom, bearish.top) == (1999.0, 2001.0)
    assert bearish.filled_pos == 20  # bar 20 closes above the gap


def test_order_block_is_last_down_candle_at_impulse_origin(ltf):
    atr = q.compute_atr(ltf, 14)
    events, _ = q.detect_market_structure(ltf, q.detect_swings(ltf, 2, 2), atr, 1.0)
    blocks = q.detect_order_blocks(ltf, events, 10)
    bullish = [b for b in blocks if b.direction == "BUY"]
    assert [(b.pos, b.bottom, b.top, b.event_pos) for b in bullish] == [(17, 1995.5, 1997.5, 20)]


def test_crt_wick_raid_and_turtle_body_soup():
    start = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    wick = make_frame([(2000, 2010, 1990, 2005), (2005, 2008, 1985, 1995)], start, 240)
    setup = q.detect_crt(wick, "wick")
    assert setup is not None and (setup.direction, setup.variant, setup.target, setup.invalidation) == (
        "BUY", "CRT", 2010.0, 1985.0)
    tbs = make_frame([(2000, 2010, 1990, 2005), (2005, 2006, 1984, 1986), (1986, 1997, 1985, 1996)],
                     start, 240)
    setup = q.detect_crt(tbs, "both")
    assert setup is not None and (setup.direction, setup.variant) == ("BUY", "TBS")
    assert setup.valid_at(2000.0) and not setup.valid_at(2011.0)


def test_killzones_follow_new_york_daylight_saving(settings):
    windows, tz = settings.strategy.killzones, settings.strategy.killzone_timezone
    assert "LONDON" in q.active_killzones(datetime(2026, 7, 15, 6, 30, tzinfo=UTC), windows, tz)  # EDT
    assert "LONDON" not in q.active_killzones(datetime(2026, 11, 4, 6, 30, tzinfo=UTC), windows, tz)
    assert "LONDON" in q.active_killzones(datetime(2026, 11, 4, 7, 30, tzinfo=UTC), windows, tz)  # EST


def test_position_size_gold():
    result = q.calculate_position_size(10_000, 1.0, 2000.2, 1993.46, tick_size=0.01, tick_value=1.0,
                                       volume_min=0.01, volume_max=50, volume_step=0.01)
    assert result.valid and result.lots == 0.14
    assert result.actual_risk == pytest.approx(94.36, abs=0.01)
    assert result.actual_risk_percent <= 1.0


def test_position_size_rejects_below_min_lot_unless_override_within_cap():
    kwargs = dict(tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=100, volume_step=0.01)
    small = q.calculate_position_size(500, 0.5, 1.10000, 1.09700, **kwargs)  # $2.5 risk, $300/lot
    assert not small.valid and "below broker minimum" in small.reason
    override = q.calculate_position_size(500, 0.5, 1.10000, 1.09700, max_risk_percent=2.0,
                                         allow_min_lot=True, **kwargs)
    assert override.valid and override.lots == 0.01 and override.actual_risk == pytest.approx(3.0)


def test_position_size_uses_broker_loss_and_caps_volume():
    result = q.calculate_position_size(1_000_000, 2.0, 100.0, 99.0, tick_size=0.01, tick_value=1.0,
                                       volume_min=0.01, volume_max=5.0, volume_step=0.01,
                                       loss_per_lot=100.0)
    assert result.valid and result.lots == 5.0 and result.loss_per_lot == 100.0


def test_full_analysis_produces_a_plus_buy_limit(settings, ltf):
    engine = make_engine(settings)
    analysis = engine.analyze("XAUUSD", ltf, trend_frame(NOW, 15), trend_frame(NOW, 60), now=NOW,
                              bid=2000.4, ask=2000.6)
    signals = [s for s in analysis.signals if s.setup == "sweep_mss_fvg"]
    assert len(signals) == 1
    sig = signals[0]
    assert (sig.direction, sig.order_type, sig.entry) == ("BUY", "LIMIT", 2000.2)
    assert sig.stop_loss < 1993.8
    risk = sig.entry - sig.stop_loss
    assert (sig.tp1 - sig.entry) / risk == pytest.approx(settings.risk.tp1_rr)
    assert (sig.tp2 - sig.entry) / risk == pytest.approx(settings.risk.tp2_rr)
    assert sig.grade == "A+" and sig.score >= settings.strategy.a_plus_score
    assert {"SSL Sweep", "MSS", "FVG", "London Killzone"} <= set(sig.confluences)
    again = engine.analyze("XAUUSD", ltf, trend_frame(NOW, 15), trend_frame(NOW, 60), now=NOW,
                           bid=2000.4, ask=2000.6)
    assert again.signals[0].signal_id == sig.signal_id  # stable de-duplication key


def test_market_order_when_price_is_inside_the_fvg(settings, ltf):
    analysis = make_engine(settings).analyze("XAUUSD", ltf, None, None, now=NOW, bid=1999.3, ask=1999.5)
    sig = [s for s in analysis.signals if s.setup == "sweep_mss_fvg"][0]
    assert sig.order_type == "MARKET" and sig.entry == 1999.5


def test_counter_htf_setup_is_rejected(settings, ltf):
    downtrend = trend_frame(NOW, 60, base=2100.0, step=-1.2)
    analysis = make_engine(settings).analyze("XAUUSD", ltf, None, downtrend, now=NOW, bid=2000.4,
                                             ask=2000.6)
    assert analysis.htf_bias.direction == "BEARISH"
    assert not [s for s in analysis.signals if s.direction == "BUY"]
    assert any("against HTF" in r["reason"] for r in analysis.rejections)


def test_bearish_mirror_produces_sell(settings, ltf):
    bearish = mirror(ltf)
    down_m15 = trend_frame(NOW, 15, base=2100.0, step=-1.2)
    down_h1 = trend_frame(NOW, 60, base=2100.0, step=-1.2)
    analysis = make_engine(settings).analyze("XAUUSD", bearish, down_m15, down_h1, now=NOW,
                                             bid=1999.4, ask=1999.6)
    sig = [s for s in analysis.signals if s.setup == "sweep_mss_fvg"][0]
    assert (sig.direction, sig.order_type) == ("SELL", "LIMIT")
    assert sig.entry == pytest.approx(1999.8) and sig.stop_loss > 2006.2


def test_expired_setup_is_dropped(settings, ltf):
    later = NOW + timedelta(hours=2)
    extended = pd.concat([ltf, make_frame([(2001.0, 2001.5, 2000.6, 2001.2)] * 24,
                                          START + timedelta(minutes=5 * 24), 5)])
    analysis = make_engine(settings).analyze("XAUUSD", extended, None, None, now=later, bid=2001.0,
                                             ask=2001.2)
    assert not [s for s in analysis.signals if s.setup == "sweep_mss_fvg"]


def test_ist_time_candle_breakout(settings):
    start = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
    bars = []
    for i in range(25):
        base = 2000.0 + (0.8 if i % 2 else -0.8)
        o, c = (base - 0.5, base + 0.5) if i % 2 else (base + 0.5, base - 0.5)
        bars.append((o, max(o, c) + 1.5, min(o, c) - 1.5, c))
    bars[18:21] = [(2000.0, 2001.5, 1998.0, 2001.0), (2001.0, 2004.0, 2000.5, 2002.5),
                   (2002.5, 2003.5, 2001.0, 2003.0)]
    bars[21] = (2003.0, 2004.0, 2001.5, 2003.5)  # 14:45 closes inside the 20:00 IST range
    bars[22] = (2003.5, 2006.5, 2003.0, 2006.0)  # 14:50 closes above the range -> breakout
    bars[23] = (2006.0, 2006.8, 2005.0, 2005.5)
    bars[24] = (2005.5, 2006.0, 2004.8, 2005.2)  # 15:00 still forming
    ltf = make_frame(bars, start, 5)
    m15_bars = [(2000.0, 2001.5, 1998.5, 2000.5)] * 10 + [(2000.0, 2004.0, 1998.0, 2003.0),
                                                         (2003.0, 2006.8, 2001.5, 2005.5)]
    m15 = make_frame(m15_bars, datetime(2026, 11, 4, 12, 0, tzinfo=UTC), 15)
    assert pd.Timestamp("2026-11-04 14:30", tz="UTC") in m15.index  # 20:00 IST candle
    now = datetime(2026, 11, 4, 15, 0, 1, tzinfo=UTC)
    engine = make_engine(settings, time_candle_times_ist=(datetime.strptime("20:00", "%H:%M").time(),))
    analysis = engine.analyze("XAUUSD", ltf, m15, None, time_candle_df=m15, now=now, bid=2005.6,
                              ask=2005.8)
    signals = [s for s in analysis.signals if s.setup == "time_candle_breakout"]
    assert len(signals) == 1, analysis.rejections
    sig = signals[0]
    assert sig.direction == "BUY" and "20:00 IST" in sig.strategy
    assert sig.meta["range_high"] == 2004.0 and sig.stop_loss < 1998.0
    assert sig.order_type == "MARKET" and sig.entry == 2005.8


def test_prepare_and_closed_bars_drop_forming_candle(ltf):
    frame = q.prepare_ohlcv(ltf)
    closed = q.closed_bars(frame, "M5", frame.index[-1].to_pydatetime() + timedelta(minutes=2))
    assert len(closed) == len(frame) - 1
