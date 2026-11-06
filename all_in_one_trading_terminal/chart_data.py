"""
chart_data.py — JSON chart payloads for the web terminal (TradingView Lightweight Charts).

Times are UTC epoch seconds; the browser renders them in the trader's time zone. Zones (FVG,
order blocks) and session ranges are sent as boxes so the client can draw real rectangles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pandas as pd

from config import SessionWindow
from quant_strategy_engine import (
    FeatureSet,
    MarketAnalysis,
    closed_bars,
    compute_ema,
    compute_session_ranges,
)

UTC = timezone.utc


def epoch_seconds(index: pd.DatetimeIndex) -> list[int]:
    utc = pd.DatetimeIndex(index).tz_convert("UTC")
    return [int(v) for v in (utc - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)]


def _f(value: Any, digits: int) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, max(digits + 1, 2))


def build_chart_payload(df: pd.DataFrame, features: FeatureSet | None, analysis: MarketAnalysis | None, *,
                        symbol: str, timeframe: str, digits: int, ema_period: int,
                        session_windows: Sequence[SessionWindow], session_tz: str,
                        positions: Sequence[Mapping[str, Any]] = (),
                        orders: Sequence[Mapping[str, Any]] = (), now: datetime | None = None,
                        max_fvg: int = 6, max_ob: int = 4) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    payload: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe, "digits": int(digits),
                               "candles": [], "volume": [], "ema": [], "zones": [], "sessions": [],
                               "markers": [], "levels": [], "signals": [], "last": None}
    if df is None or df.empty:
        return payload
    times = epoch_seconds(df.index)
    time_of = dict(zip(df.index, times))
    opens, highs = df["open"].to_numpy(float), df["high"].to_numpy(float)
    lows, closes = df["low"].to_numpy(float), df["close"].to_numpy(float)
    volumes = df["volume"].to_numpy(float) if "volume" in df else [0.0] * len(df)
    payload["candles"] = [{"time": t, "open": _f(o, digits), "high": _f(h, digits), "low": _f(lo, digits),
                           "close": _f(c, digits)}
                          for t, o, h, lo, c in zip(times, opens, highs, lows, closes)]
    if float(sum(volumes)) > 0:
        payload["volume"] = [{"time": t, "value": float(v), "up": bool(c >= o)}
                             for t, v, o, c in zip(times, volumes, opens, closes)]
    if len(closes) > 5:
        payload["ema"] = [{"time": t, "value": _f(v, digits)}
                          for t, v in zip(times, compute_ema(closes, ema_period))]
    payload["last"] = _f(closes[-1], digits)

    def feat_time(pos: int | None) -> int | None:
        if features is None or pos is None or pos >= len(features.df):
            return None
        return time_of.get(features.df.index[pos])

    if features is not None:
        reference = float(closes[-1])
        atr = features.atr_now

        def near(level: float) -> bool:
            return atr <= 0 or abs(level - reference) <= 12 * atr

        for gap in [g for g in features.fvgs if g.active and near(g.ce)][-max_fvg:]:
            start = feat_time(gap.start_pos)
            if start is not None:
                payload["zones"].append({"kind": "FVG", "direction": gap.direction, "from": start, "to": None,
                                         "top": _f(gap.top, digits), "bottom": _f(gap.bottom, digits),
                                         "mitigated": gap.mitigated_pos is not None})
        for block in [b for b in features.order_blocks if b.active and near((b.top + b.bottom) / 2)][-max_ob:]:
            start = feat_time(block.pos)
            if start is not None:
                payload["zones"].append({"kind": "OB", "direction": block.direction, "from": start, "to": None,
                                         "top": _f(block.top, digits), "bottom": _f(block.bottom, digits),
                                         "mitigated": block.mitigated_pos is not None})
        markers = []
        for sweep in features.sweeps[-8:]:
            t = feat_time(sweep.pos)
            if t is not None:
                bearish = sweep.side == "BSL"
                markers.append({"time": t, "position": "aboveBar" if bearish else "belowBar",
                                "shape": "arrowDown" if bearish else "arrowUp",
                                "color": "#f6465d" if bearish else "#0ecb81", "text": sweep.side})
        for event in features.events[-8:]:
            t = feat_time(event.pos)
            if t is not None:
                bullish = event.direction == "BUY"
                markers.append({"time": t, "position": "belowBar" if bullish else "aboveBar", "shape": "circle",
                                "color": "#38bdf8" if bullish else "#f472b6",
                                "text": f"{event.kind}{'↑' if bullish else '↓'}"})
        payload["markers"] = markers

    closed = closed_bars(df, timeframe, now)
    for session in compute_session_ranges(closed, session_windows, session_tz, now):
        if session.start < df.index[0]:
            continue
        payload["sessions"].append({"name": session.name, "from": time_of.get(session.start) or epoch_seconds(
            pd.DatetimeIndex([session.start]))[0], "to": epoch_seconds(pd.DatetimeIndex([session.end]))[0],
            "high": _f(session.high, digits), "low": _f(session.low, digits), "complete": session.complete})

    if analysis is not None:
        for sig in analysis.signals[:3]:
            anchor = time_of.get(pd.Timestamp(sig.anchor_time))
            payload["signals"].append({
                "direction": sig.direction, "setup": sig.setup, "strategy": sig.strategy, "grade": sig.grade,
                "score": sig.score, "order_type": sig.order_type, "entry": _f(sig.entry, digits),
                "stop_loss": _f(sig.stop_loss, digits), "tp1": _f(sig.tp1, digits), "tp2": _f(sig.tp2, digits),
                "anchor": anchor, "confluences": list(sig.confluences)})
            if anchor is not None:
                buy = sig.direction == "BUY"
                payload["markers"].append({"time": anchor, "position": "belowBar" if buy else "aboveBar",
                                           "shape": "arrowUp" if buy else "arrowDown",
                                           "color": "#0ecb81" if buy else "#f6465d",
                                           "text": f"{sig.direction} {sig.grade} {sig.score:.1f}"})
    payload["markers"].sort(key=lambda m: m["time"])

    for position in positions:
        buy = position.get("direction") == "BUY"
        side = "BUY" if buy else "SELL"
        for kind, price, title in (("entry", position.get("price_open"), f"{side} {position.get('volume', 0):g}"),
                                   ("sl", position.get("sl"), "SL"), ("tp1", position.get("tp1"), "TP1"),
                                   ("tp", position.get("tp"), "TP")):
            if price:
                payload["levels"].append({"kind": kind, "price": _f(price, digits), "title": title,
                                          "ticket": position.get("ticket")})
    for order in orders:
        if order.get("price"):
            payload["levels"].append({"kind": "order", "price": _f(order["price"], digits),
                                      "title": f"{order.get('direction')} {order.get('kind')}",
                                      "ticket": order.get("ticket")})
    return payload
