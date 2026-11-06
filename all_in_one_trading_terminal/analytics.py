"""
analytics.py — Myfxbook-style performance analytics and market tools (pure functions).

Everything here works on plain trade dicts (rows of the ``trades`` table), equity-log rows
or price series, so it is fully unit-testable and shared by the web API and the dashboard.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from config import IST, SessionWindow
from database import compute_performance, parse_iso

UTC = timezone.utc
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Trade analytics
# --------------------------------------------------------------------------- #
def closed_trades(trades: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """CLOSED trades with a parseable close time, oldest first."""
    rows = [dict(t) for t in trades if t.get("status") == "CLOSED" and parse_iso(t.get("closed_at"))]
    rows.sort(key=lambda t: (parse_iso(t["closed_at"]), t.get("id") or 0))
    return rows


def _pnl(trade: Mapping[str, Any]) -> float:
    try:
        value = float(trade.get("pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(value) else value


def _group_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(t) for t in rows]
    rs = [t.get("r_multiple") for t in rows]
    stats = compute_performance(pnls, rs)
    pf = stats["profit_factor"]
    return {"trades": stats["total_trades"], "wins": stats["wins"], "losses": stats["losses"],
            "win_rate": stats["win_rate"], "pnl": stats["net_pnl"], "avg_r": stats["avg_r"],
            "profit_factor": None if isinstance(pf, float) and math.isinf(pf) else pf}


def daily_pnl(trades: Iterable[Mapping[str, Any]], tz: ZoneInfo = IST) -> list[dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in closed_trades(trades):
        buckets[parse_iso(trade["closed_at"]).astimezone(tz).date().isoformat()].append(trade)
    return [{"date": day, "pnl": round(sum(_pnl(t) for t in rows), 2), "trades": len(rows),
             "wins": sum(1 for t in rows if _pnl(t) > 0)} for day, rows in sorted(buckets.items())]


def monthly_returns(trades: Iterable[Mapping[str, Any]], starting_balance: float | None = None,
                    tz: ZoneInfo = IST) -> list[dict[str, Any]]:
    """P&L per month; return % is relative to the balance at the start of that month."""
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in closed_trades(trades):
        buckets[parse_iso(trade["closed_at"]).astimezone(tz).strftime("%Y-%m")].append(trade)
    balance = float(starting_balance) if starting_balance else None
    rows = []
    for month, items in sorted(buckets.items()):
        pnl = round(sum(_pnl(t) for t in items), 2)
        pct = round(pnl / balance * 100.0, 2) if balance else None
        rows.append({"month": month, "pnl": pnl, "trades": len(items), "return_pct": pct})
        if balance is not None:
            balance += pnl
    return rows


def breakdown(trades: Iterable[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], str | None]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in closed_trades(trades):
        groups[key(trade) or "—"].append(trade)
    rows = [{"key": name, **_group_stats(items)} for name, items in groups.items()]
    return sorted(rows, key=lambda r: r["pnl"], reverse=True)


def session_of(moment: datetime | None, windows: Sequence[SessionWindow], tz_name: str) -> str:
    if moment is None:
        return "—"
    local = moment.astimezone(ZoneInfo(tz_name)).time()
    for window in windows:
        if window.contains(local):
            return window.name
    return "OFF-SESSION"


def by_hour(trades: Iterable[Mapping[str, Any]], tz: ZoneInfo = IST) -> list[dict[str, Any]]:
    pnl = [0.0] * 24
    count = [0] * 24
    for trade in closed_trades(trades):
        opened = parse_iso(trade.get("opened_at")) or parse_iso(trade["closed_at"])
        hour = opened.astimezone(tz).hour
        pnl[hour] += _pnl(trade)
        count[hour] += 1
    return [{"hour": h, "pnl": round(pnl[h], 2), "trades": count[h]} for h in range(24)]


def by_weekday(trades: Iterable[Mapping[str, Any]], tz: ZoneInfo = IST) -> list[dict[str, Any]]:
    pnl = [0.0] * 7
    count = [0] * 7
    for trade in closed_trades(trades):
        opened = parse_iso(trade.get("opened_at")) or parse_iso(trade["closed_at"])
        day = opened.astimezone(tz).weekday()
        pnl[day] += _pnl(trade)
        count[day] += 1
    return [{"day": WEEKDAYS[d], "pnl": round(pnl[d], 2), "trades": count[d]} for d in range(7)]


def hold_time(trades: Iterable[Mapping[str, Any]]) -> dict[str, float | None]:
    minutes = []
    for trade in closed_trades(trades):
        opened, closed = parse_iso(trade.get("opened_at")), parse_iso(trade.get("closed_at"))
        if opened and closed and closed >= opened:
            minutes.append((closed - opened).total_seconds() / 60.0)
    if not minutes:
        return {"avg_minutes": None, "median_minutes": None}
    ordered = sorted(minutes)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"avg_minutes": round(sum(minutes) / len(minutes), 1), "median_minutes": round(median, 1)}


def equity_drawdown(points: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Peak-to-trough drawdown of the logged equity curve."""
    peak = None
    max_amount = max_pct = 0.0
    current_pct = 0.0
    for point in points:
        equity = point.get("equity")
        if equity is None:
            continue
        equity = float(equity)
        peak = equity if peak is None else max(peak, equity)
        amount = peak - equity
        pct = amount / peak * 100.0 if peak else 0.0
        max_amount, max_pct, current_pct = max(max_amount, amount), max(max_pct, pct), pct
    return {"max_dd_amount": round(max_amount, 2), "max_dd_pct": round(max_pct, 3),
            "current_dd_pct": round(current_pct, 3)}


def build_report(trades: Sequence[Mapping[str, Any]], equity_points: Sequence[Mapping[str, Any]],
                 starting_balance: float | None, killzones: Sequence[SessionWindow],
                 killzone_tz: str, tz: ZoneInfo = IST) -> dict[str, Any]:
    closed = closed_trades(trades)
    stats = compute_performance([_pnl(t) for t in closed], [t.get("r_multiple") for t in closed])
    if isinstance(stats["profit_factor"], float) and math.isinf(stats["profit_factor"]):
        stats["profit_factor"] = None
    stats.update(hold_time(closed))
    stats.update(equity_drawdown(equity_points))
    longs = [t for t in closed if t.get("direction") == "BUY"]
    shorts = [t for t in closed if t.get("direction") == "SELL"]
    cumulative = 0.0
    curve = []
    for trade in closed:
        cumulative += _pnl(trade)
        curve.append({"ts": parse_iso(trade["closed_at"]).isoformat(), "cum_pnl": round(cumulative, 2)})
    return {
        "stats": stats,
        "long": _group_stats(longs),
        "short": _group_stats(shorts),
        "daily": daily_pnl(closed, tz),
        "monthly": monthly_returns(closed, starting_balance, tz),
        "by_strategy": breakdown(closed, lambda t: t.get("strategy")),
        "by_symbol": breakdown(closed, lambda t: t.get("symbol")),
        "by_session": breakdown(closed, lambda t: session_of(
            parse_iso(t.get("opened_at")) or parse_iso(t.get("closed_at")), killzones, killzone_tz)),
        "by_hour": by_hour(closed, tz),
        "by_weekday": by_weekday(closed, tz),
        "cum_pnl": curve,
        "equity": [{"ts": p["ts"], "equity": p.get("equity"), "balance": p.get("balance")}
                   for p in equity_points],
    }


# --------------------------------------------------------------------------- #
# Market tools
# --------------------------------------------------------------------------- #
def correlation_matrix(closes: Mapping[str, pd.Series], bars: int = 200) -> dict[str, Any]:
    """Pearson correlation of bar returns over the common timestamps of each symbol."""
    usable = {name: series.astype(float) for name, series in closes.items()
              if series is not None and len(series) > 2}
    if len(usable) < 2:
        return {"symbols": list(usable), "matrix": [[1.0]] if usable else [], "bars": 0}
    frame = pd.DataFrame(usable).dropna()
    returns = frame.pct_change().dropna().tail(bars)
    if len(returns) < 3:
        return {"symbols": list(usable), "matrix": [], "bars": len(returns)}
    corr = returns.corr().fillna(0.0)
    symbols = list(corr.columns)
    matrix = [[round(float(corr.loc[a, b]), 3) for b in symbols] for a in symbols]
    return {"symbols": symbols, "matrix": matrix, "bars": int(len(returns))}


def volatility_row(symbol: str, h1: pd.DataFrame, pip: float, atr_ltf: float | None = None,
                   days: int = 14) -> dict[str, Any]:
    """Average daily range (ADR) from hourly bars and how much of it today has used."""
    row: dict[str, Any] = {"symbol": symbol, "adr": None, "adr_pips": None, "today_range": None,
                           "today_pips": None, "used_pct": None,
                           "atr_ltf": atr_ltf, "atr_ltf_pips": atr_ltf / pip if atr_ltf and pip else None}
    if h1 is None or h1.empty:
        return row
    daily = h1.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    if daily.empty:
        return row
    ranges = (daily["high"] - daily["low"]).astype(float)
    history = ranges.iloc[:-1].tail(days)
    today = float(ranges.iloc[-1])
    adr = float(history.mean()) if len(history) else None
    row.update({"adr": adr, "today_range": today,
                "adr_pips": adr / pip if adr and pip else None,
                "today_pips": today / pip if pip else None,
                "used_pct": round(today / adr * 100.0, 1) if adr else None})
    return row
