"""
quant_strategy_engine.py — Institutional SMC / ICT / price-action algorithmic brain.

Pure pandas/numpy, no broker dependency, no look-ahead:
  * Swing highs/lows (Williams fractals) are only *known* ``right`` bars after they print.
  * Liquidity sweeps (BSL/SSL): wick through an untaken swing level, body closes back inside.
  * Market structure: BOS / CHoCH / MSS on a **body close** beyond the last confirmed swing,
    MSS = CHoCH with a displacement candle (body >= DISPLACEMENT_ATR x ATR).
  * Fair Value Gaps (3-candle displacement imbalance) and impulse-origin Order Blocks,
    both tracked for mitigation / invalidation.
  * Candle Range Theory (CRT wick raid) and Turtle Body Soup (failed body breakout) on the
    CRT timeframe, session/killzone windows (DST-aware), premium/discount, EMA trend,
    relative volume and an inverse/positive DXY filter.
  * Setup generators: Sweep -> MSS -> FVG (ICT model), Order-Block retest (continuation)
    and the IST time-candle breakout (20:00 / 20:45 IST candles by default).
  * Weighted confluence scoring (0-10, A+/A/B/C) and dynamic lot sizing from equity,
    risk %, stop distance and tick value.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import IST, RiskSettings, SessionWindow, StrategySettings, canonical_symbol, timeframe_delta

UTC = timezone.utc
Direction = Literal["BUY", "SELL"]
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
MIN_BARS = 20
MAX_REJECTIONS = 30


# --------------------------------------------------------------------------- #
# Data preparation & indicators
# --------------------------------------------------------------------------- #
def empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=list(OHLCV_COLUMNS), dtype="float64",
                        index=pd.DatetimeIndex([], tz="UTC", name="time"))


def prepare_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    """Validate and normalise an OHLCV frame: UTC DatetimeIndex, sorted, float64, no NaN prices."""
    if df is None or len(df) == 0:
        return empty_ohlcv()
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing columns: {missing}")
    out = df.copy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    index = pd.DatetimeIndex(out.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    out.index = index
    out.index.name = "time"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.loc[:, list(OHLCV_COLUMNS)].astype("float64")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out


def closed_bars(df: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
    """Drop the still-forming candle(s): keep bars whose close time <= ``now``."""
    if df.empty:
        return df
    return df[df.index + timeframe_delta(timeframe) <= pd.Timestamp(now)]


def compute_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    if df.empty:
        return np.array([], dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    true_range = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    return pd.Series(true_range).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()


def compute_ema(values: np.ndarray | pd.Series, span: int) -> np.ndarray:
    return pd.Series(np.asarray(values, dtype=float)).ewm(span=span, adjust=False).mean().to_numpy()


def relative_volume(volume: np.ndarray, period: int = 20) -> np.ndarray:
    series = pd.Series(np.asarray(volume, dtype=float))
    baseline = series.shift(1).rolling(period, min_periods=3).mean()
    ratio = (series / baseline).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    return ratio.to_numpy()


def volume_available(volume: np.ndarray, window: int = 50) -> bool:
    tail = np.asarray(volume, dtype=float)[-window:]
    return bool(tail.size and np.nansum(tail) > 0 and np.count_nonzero(tail) >= tail.size // 2)


# --------------------------------------------------------------------------- #
# Feature dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SwingPoint:
    pos: int
    time: pd.Timestamp
    price: float
    kind: str  # "high" | "low"
    confirmed_pos: int


@dataclass(frozen=True)
class LiquiditySweep:
    pos: int
    time: pd.Timestamp
    side: str  # "BSL" (buy-side swept -> bearish) | "SSL" (sell-side swept -> bullish)
    level: float
    swing_pos: int
    extreme: float
    close: float
    equal_level: bool

    @property
    def bias(self) -> Direction:
        return "SELL" if self.side == "BSL" else "BUY"


@dataclass(frozen=True)
class StructureEvent:
    pos: int
    time: pd.Timestamp
    direction: Direction
    kind: str  # BOS | CHoCH | MSS
    level: float
    swing_pos: int
    displacement: bool
    body_atr: float


@dataclass
class FairValueGap:
    pos: int  # third candle (gap confirmed on its close)
    start_pos: int  # displacement (middle) candle
    time: pd.Timestamp
    direction: Direction
    top: float
    bottom: float
    size_atr: float
    rel_volume: float
    mitigated_pos: int | None = None
    filled_pos: int | None = None

    @property
    def ce(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def active(self) -> bool:
        return self.filled_pos is None


@dataclass
class OrderBlock:
    pos: int
    time: pd.Timestamp
    direction: Direction
    top: float
    bottom: float
    mean_threshold: float
    event_pos: int
    mitigated_pos: int | None = None
    invalidated_pos: int | None = None

    @property
    def active(self) -> bool:
        return self.invalidated_pos is None


@dataclass(frozen=True)
class CRTSetup:
    direction: Direction
    variant: str  # CRT | TBS
    range_high: float
    range_low: float
    target: float
    invalidation: float
    range_time: pd.Timestamp
    trigger_time: pd.Timestamp

    def valid_at(self, price: float) -> bool:
        if self.direction == "BUY":
            return self.invalidation < price < self.target
        return self.target < price < self.invalidation


@dataclass(frozen=True)
class SessionRange:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp
    high: float
    low: float
    complete: bool


@dataclass(frozen=True)
class Bias:
    direction: str  # BULLISH | BEARISH | NEUTRAL
    structure: int
    ema_trend: int
    close: float
    ema: float
    last_event: str | None = None

    def aligned(self, direction: str) -> bool:
        return (self.direction == "BULLISH" and direction == "BUY") or (
            self.direction == "BEARISH" and direction == "SELL")

    def opposed(self, direction: str) -> bool:
        return (self.direction == "BEARISH" and direction == "BUY") or (
            self.direction == "BULLISH" and direction == "SELL")


NEUTRAL_BIAS = Bias("NEUTRAL", 0, 0, float("nan"), float("nan"), None)


@dataclass
class FeatureSet:
    df: pd.DataFrame
    atr: np.ndarray
    ema: np.ndarray
    rel_volume: np.ndarray
    volume_ok: bool
    swings: list[SwingPoint]
    equal_positions: frozenset[int]
    sweeps: list[LiquiditySweep]
    events: list[StructureEvent]
    trend: int
    fvgs: list[FairValueGap]
    order_blocks: list[OrderBlock]

    @property
    def atr_now(self) -> float:
        return float(self.atr[-1]) if len(self.atr) else 0.0


@dataclass
class TradeSignal:
    symbol: str
    direction: Direction
    strategy: str
    setup: str
    timeframe: str
    order_type: str  # MARKET | LIMIT
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp1_rr: float
    rr: float
    score: float
    grade: str
    confluences: list[str]
    factors: dict[str, Any]
    anchor_time: datetime
    created_at: datetime
    valid_until: datetime
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def signal_id(self) -> str:
        key = f"{self.symbol}|{self.direction}|{self.setup}|{self.anchor_time.isoformat()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]

    def to_record(self) -> dict[str, Any]:
        return {"signal_id": self.signal_id, "symbol": self.symbol, "direction": self.direction,
                "strategy": self.strategy, "setup": self.setup, "timeframe": self.timeframe,
                "order_type": self.order_type, "entry": self.entry, "stop_loss": self.stop_loss,
                "tp1": self.tp1, "tp2": self.tp2, "rr": self.rr, "score": self.score,
                "grade": self.grade, "confluences": list(self.confluences),
                "anchor_time": self.anchor_time, "valid_until": self.valid_until,
                "created_at": self.created_at}

    def to_dict(self) -> dict[str, Any]:
        data = self.to_record()
        data.update({"tp1_rr": self.tp1_rr, "factors": dict(self.factors), "meta": dict(self.meta)})
        return data


@dataclass
class MarketAnalysis:
    symbol: str
    timeframe: str
    generated_at: datetime
    features: FeatureSet | None = None
    last_bar_time: pd.Timestamp | None = None
    last_close: float | None = None
    atr: float = 0.0
    htf_bias: Bias = NEUTRAL_BIAS
    mtf_bias: Bias = NEUTRAL_BIAS
    dxy_bias: Bias | None = None
    crt: CRTSetup | None = None
    killzones_now: list[str] = field(default_factory=list)
    premium_discount: dict[str, Any] = field(default_factory=dict)
    session_ranges: list[SessionRange] = field(default_factory=list)
    signals: list[TradeSignal] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best_signal(self) -> TradeSignal | None:
        return self.signals[0] if self.signals else None

    def summary(self) -> dict[str, Any]:
        feats = self.features
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "generated_at": self.generated_at.isoformat(),
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time is not None else None,
            "last_close": self.last_close,
            "atr": self.atr,
            "ltf_trend": {1: "BULLISH", -1: "BEARISH"}.get(feats.trend if feats else 0, "NEUTRAL"),
            "htf_bias": self.htf_bias.direction,
            "mtf_bias": self.mtf_bias.direction,
            "dxy_bias": self.dxy_bias.direction if self.dxy_bias else "N/A",
            "crt": f"{self.crt.variant} {self.crt.direction}" if self.crt else "None",
            "killzones": list(self.killzones_now),
            "premium_discount": self.premium_discount.get("zone", "UNKNOWN"),
            "active_fvgs": sum(1 for g in feats.fvgs if g.active) if feats else 0,
            "active_obs": sum(1 for ob in feats.order_blocks if ob.active) if feats else 0,
            "signals": len(self.signals),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Detection algorithms (pure functions)
# --------------------------------------------------------------------------- #
def detect_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[SwingPoint]:
    """Fractal swings: strictly beyond ``left`` bars, not exceeded by ``right`` bars."""
    n = len(df)
    if n < left + right + 1:
        return []
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    times = df.index
    swings: list[SwingPoint] = []
    for i in range(left, n - right):
        high = highs[i]
        if high > highs[i - left:i].max() and high >= highs[i + 1:i + right + 1].max():
            swings.append(SwingPoint(i, times[i], float(high), "high", i + right))
        low = lows[i]
        if low < lows[i - left:i].min() and low <= lows[i + 1:i + right + 1].min():
            swings.append(SwingPoint(i, times[i], float(low), "low", i + right))
    return swings


def mark_equal_levels(swings: Sequence[SwingPoint], tolerance: float,
                      lookback: int = 6) -> frozenset[int]:
    """Positions of swings forming equal highs / equal lows (resting liquidity pools)."""
    if tolerance <= 0:
        return frozenset()
    equal: set[int] = set()
    for kind in ("high", "low"):
        points = [s for s in swings if s.kind == kind]
        for index, swing in enumerate(points):
            for previous in points[max(0, index - lookback):index]:
                if abs(previous.price - swing.price) <= tolerance:
                    equal.update((swing.pos, previous.pos))
    return frozenset(equal)


def detect_liquidity_sweeps(df: pd.DataFrame, swings: Sequence[SwingPoint],
                            equal_positions: frozenset[int] = frozenset(),
                            max_age_bars: int = 300) -> list[LiquiditySweep]:
    """A sweep = wick beyond an untaken confirmed swing level with the body closing back inside."""
    n = len(df)
    if n == 0 or not swings:
        return []
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df.index
    ordered = sorted(swings, key=lambda s: s.confirmed_pos)
    active_highs: list[SwingPoint] = []
    active_lows: list[SwingPoint] = []
    cursor = 0
    sweeps: list[LiquiditySweep] = []
    for t in range(n):
        while cursor < len(ordered) and ordered[cursor].confirmed_pos < t:
            swing = ordered[cursor]
            (active_highs if swing.kind == "high" else active_lows).append(swing)
            cursor += 1
        if active_highs:
            taken = [s for s in active_highs if highs[t] > s.price]
            if taken:
                swept = [s for s in taken if closes[t] < s.price]
                if swept:
                    best = max(swept, key=lambda s: s.price)
                    sweeps.append(LiquiditySweep(t, times[t], "BSL", best.price, best.pos,
                                                 float(highs[t]), float(closes[t]),
                                                 any(s.pos in equal_positions for s in swept)))
                taken_ids = {id(s) for s in taken}
                active_highs = [s for s in active_highs if id(s) not in taken_ids]
        if active_lows:
            taken = [s for s in active_lows if lows[t] < s.price]
            if taken:
                swept = [s for s in taken if closes[t] > s.price]
                if swept:
                    best = min(swept, key=lambda s: s.price)
                    sweeps.append(LiquiditySweep(t, times[t], "SSL", best.price, best.pos,
                                                 float(lows[t]), float(closes[t]),
                                                 any(s.pos in equal_positions for s in swept)))
                taken_ids = {id(s) for s in taken}
                active_lows = [s for s in active_lows if id(s) not in taken_ids]
        if t % 50 == 0:
            active_highs = [s for s in active_highs if t - s.pos <= max_age_bars]
            active_lows = [s for s in active_lows if t - s.pos <= max_age_bars]
    return sweeps


def detect_market_structure(df: pd.DataFrame, swings: Sequence[SwingPoint], atr: np.ndarray,
                            displacement_mult: float = 1.0) -> tuple[list[StructureEvent], int]:
    """BOS / CHoCH / MSS on body close beyond the latest confirmed (unbroken) swing."""
    n = len(df)
    if n == 0:
        return [], 0
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df.index
    ordered = sorted(swings, key=lambda s: s.confirmed_pos)
    trend = 0
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None
    cursor = 0
    events: list[StructureEvent] = []
    for t in range(n):
        while cursor < len(ordered) and ordered[cursor].confirmed_pos < t:
            swing = ordered[cursor]
            if swing.kind == "high":
                last_high = swing
            else:
                last_low = swing
            cursor += 1
        close = closes[t]
        atr_t = float(atr[t]) if t < len(atr) else 0.0
        body = abs(close - opens[t])
        body_atr = body / atr_t if atr_t > 0 else 0.0
        displaced = bool(atr_t > 0 and body >= displacement_mult * atr_t)
        if last_high is not None and close > last_high.price:
            kind = "CHoCH" if trend == -1 else "BOS"
            if kind == "CHoCH" and displaced:
                kind = "MSS"
            events.append(StructureEvent(t, times[t], "BUY", kind, last_high.price, last_high.pos,
                                         displaced, body_atr))
            trend = 1
            last_high = None
        elif last_low is not None and close < last_low.price:
            kind = "CHoCH" if trend == 1 else "BOS"
            if kind == "CHoCH" and displaced:
                kind = "MSS"
            events.append(StructureEvent(t, times[t], "SELL", kind, last_low.price, last_low.pos,
                                         displaced, body_atr))
            trend = -1
            last_low = None
    return events, trend


def detect_fvgs(df: pd.DataFrame, atr: np.ndarray, rel_volume: np.ndarray | None = None,
                min_atr: float = 0.15) -> list[FairValueGap]:
    """3-candle imbalances created by a displacement candle, with mitigation/fill tracking."""
    n = len(df)
    if n < 3:
        return []
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    rel = rel_volume if rel_volume is not None else np.ones(n)
    times = df.index
    gaps: list[FairValueGap] = []
    for i in range(2, n):
        atr_i = float(atr[i]) if i < len(atr) else 0.0
        if atr_i <= 0:
            continue
        if lows[i] > highs[i - 2] and closes[i - 1] > opens[i - 1]:
            bottom, top = highs[i - 2], lows[i]
            if top - bottom >= min_atr * atr_i:
                gaps.append(FairValueGap(i, i - 1, times[i - 1], "BUY", float(top), float(bottom),
                                         float((top - bottom) / atr_i), float(rel[i - 1])))
        elif highs[i] < lows[i - 2] and closes[i - 1] < opens[i - 1]:
            top, bottom = lows[i - 2], highs[i]
            if top - bottom >= min_atr * atr_i:
                gaps.append(FairValueGap(i, i - 1, times[i - 1], "SELL", float(top), float(bottom),
                                         float((top - bottom) / atr_i), float(rel[i - 1])))
    for gap in gaps:
        start = gap.pos + 1
        if start >= n:
            continue
        if gap.direction == "BUY":
            touches = np.nonzero(lows[start:] <= gap.top)[0]
            fills = np.nonzero(closes[start:] < gap.bottom)[0]
        else:
            touches = np.nonzero(highs[start:] >= gap.bottom)[0]
            fills = np.nonzero(closes[start:] > gap.top)[0]
        gap.mitigated_pos = int(start + touches[0]) if touches.size else None
        gap.filled_pos = int(start + fills[0]) if fills.size else None
    return gaps


def detect_order_blocks(df: pd.DataFrame, events: Sequence[StructureEvent],
                        lookback: int = 10) -> list[OrderBlock]:
    """Last opposite-colour candle at the origin of the impulse that broke structure."""
    n = len(df)
    if n == 0 or not events:
        return []
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df.index
    blocks: dict[tuple[int, str], OrderBlock] = {}
    for event in events:
        lo = max(0, min(event.swing_pos, event.pos))
        hi = event.pos
        if event.direction == "BUY":
            origin = lo + int(np.argmin(lows[lo:hi + 1]))
            j = origin
            stop = max(0, origin - lookback)
            while j >= stop and not closes[j] < opens[j]:
                j -= 1
        else:
            origin = lo + int(np.argmax(highs[lo:hi + 1]))
            j = origin
            stop = max(0, origin - lookback)
            while j >= stop and not closes[j] > opens[j]:
                j -= 1
        if j < stop:
            continue
        key = (j, event.direction)
        if key in blocks:
            continue
        block = OrderBlock(j, times[j], event.direction, float(highs[j]), float(lows[j]),
                           float((opens[j] + closes[j]) / 2.0), event.pos)
        start = event.pos + 1
        if start < n:
            if event.direction == "BUY":
                touches = np.nonzero(lows[start:] <= block.top)[0]
                breaks = np.nonzero(closes[start:] < block.bottom)[0]
            else:
                touches = np.nonzero(highs[start:] >= block.bottom)[0]
                breaks = np.nonzero(closes[start:] > block.top)[0]
            block.mitigated_pos = int(start + touches[0]) if touches.size else None
            block.invalidated_pos = int(start + breaks[0]) if breaks.size else None
        blocks[key] = block
    return sorted(blocks.values(), key=lambda b: b.event_pos)


def detect_crt(df: pd.DataFrame, mode: str = "both") -> CRTSetup | None:
    """Candle Range Theory on the last completed candles of ``df``.

    CRT (wick): candle M raids one extreme of range candle R with its wick and closes back
    inside R -> expect delivery to the opposite extreme of R.
    TBS (turtle body soup): candle B closes its *body* beyond R, then candle M closes back
    inside R (failed breakout) -> expect delivery to the opposite extreme.
    """
    n = len(df)
    if n < 2:
        return None
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    t = df.index
    found: list[CRTSetup] = []
    if mode in ("wick", "both"):
        r, m = n - 2, n - 1
        inside = lo[r] < c[m] < h[r]
        raid_low = lo[m] < lo[r] and inside
        raid_high = h[m] > h[r] and inside
        if raid_low and not raid_high:
            found.append(CRTSetup("BUY", "CRT", float(h[r]), float(lo[r]), float(h[r]),
                                  float(lo[m]), t[r], t[m]))
        elif raid_high and not raid_low:
            found.append(CRTSetup("SELL", "CRT", float(h[r]), float(lo[r]), float(lo[r]),
                                  float(h[m]), t[r], t[m]))
    if mode in ("tbs", "both") and n >= 3:
        r, b, m = n - 3, n - 2, n - 1
        back_inside = lo[r] < c[m] < h[r]
        if c[b] < lo[r] and back_inside:
            found.append(CRTSetup("BUY", "TBS", float(h[r]), float(lo[r]), float(h[r]),
                                  float(min(lo[b], lo[m])), t[r], t[m]))
        elif c[b] > h[r] and back_inside:
            found.append(CRTSetup("SELL", "TBS", float(h[r]), float(lo[r]), float(lo[r]),
                                  float(max(h[b], h[m])), t[r], t[m]))
    if not found or len({setup.direction for setup in found}) > 1:
        return None
    return found[0]


def active_killzones(moment: datetime, windows: Iterable[SessionWindow], tz_name: str) -> list[str]:
    local = moment.astimezone(ZoneInfo(tz_name)).time()
    return [window.name for window in windows if window.contains(local)]


def compute_session_ranges(df: pd.DataFrame, windows: Sequence[SessionWindow], tz_name: str,
                           now: datetime, days: int = 1) -> list[SessionRange]:
    """High/low of each session window for today and the previous ``days`` local days."""
    if df.empty or not windows:
        return []
    tz = ZoneInfo(tz_name)
    today = now.astimezone(tz).date()
    ranges: list[SessionRange] = []
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        for window in windows:
            start_local = datetime.combine(day, window.start, tzinfo=tz)
            end_day = day if window.end > window.start else day + timedelta(days=1)
            end_local = datetime.combine(end_day, window.end, tzinfo=tz)
            start_utc = pd.Timestamp(start_local.astimezone(UTC))
            end_utc = pd.Timestamp(end_local.astimezone(UTC))
            if start_utc > pd.Timestamp(now):
                continue
            mask = (df.index >= start_utc) & (df.index < end_utc)
            if not mask.any():
                continue
            segment = df.loc[mask]
            ranges.append(SessionRange(window.name, start_utc, min(end_utc, segment.index[-1]),
                                       float(segment["high"].max()), float(segment["low"].min()),
                                       pd.Timestamp(now) >= end_utc))
    return ranges


def premium_discount(swings: Sequence[SwingPoint], price: float) -> dict[str, Any]:
    highs = [s.price for s in swings if s.kind == "high"]
    lows = [s.price for s in swings if s.kind == "low"]
    if not highs or not lows:
        return {"zone": "UNKNOWN", "equilibrium": None, "range_high": None, "range_low": None}
    range_high, range_low = highs[-1], lows[-1]
    if range_high <= range_low:
        range_high, range_low = max(highs[-3:]), min(lows[-3:])
    if range_high <= range_low:
        return {"zone": "UNKNOWN", "equilibrium": None, "range_high": None, "range_low": None}
    equilibrium = (range_high + range_low) / 2.0
    return {"zone": "PREMIUM" if price > equilibrium else "DISCOUNT", "equilibrium": equilibrium,
            "range_high": range_high, "range_low": range_low}


def determine_bias(df: pd.DataFrame, ema_period: int = 50, left: int = 2, right: int = 2,
                   displacement_mult: float = 1.0, atr_period: int = 14) -> Bias:
    """Structure trend (BOS/CHoCH) confirmed or vetoed by price vs a rising/falling EMA."""
    if len(df) < MIN_BARS:
        return NEUTRAL_BIAS
    swings = detect_swings(df, left, right)
    atr = compute_atr(df, atr_period)
    events, trend = detect_market_structure(df, swings, atr, displacement_mult)
    closes = df["close"].to_numpy(dtype=float)
    ema = compute_ema(closes, ema_period)
    lookback = min(5, len(ema) - 1)
    slope = ema[-1] - ema[-1 - lookback]
    close = float(closes[-1])
    if close > ema[-1] and slope > 0:
        ema_trend = 1
    elif close < ema[-1] and slope < 0:
        ema_trend = -1
    else:
        ema_trend = 0
    if trend != 0 and ema_trend in (trend, 0):
        direction = trend
    elif trend == 0:
        direction = ema_trend
    else:
        direction = 0
    label = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[direction]
    return Bias(label, trend, ema_trend, close, float(ema[-1]), events[-1].kind if events else None)


def dxy_alignment(symbol: str, direction: str, dxy_bias: Bias | None) -> bool | None:
    """True/False when DXY confirms/contradicts the trade; None when not applicable."""
    if dxy_bias is None or dxy_bias.direction == "NEUTRAL":
        return None
    key = canonical_symbol(symbol)
    if key.startswith(("DXY", "USDX")):
        return None
    usd_base = key.startswith("USD")
    usd_quote = key[3:6] == "USD" and not usd_base
    dollar_up = dxy_bias.direction == "BULLISH"
    if usd_quote:  # XAUUSD, EURUSD, GBPUSD, BTCUSD: inverse correlation
        return (direction == "BUY" and not dollar_up) or (direction == "SELL" and dollar_up)
    if usd_base:  # USDJPY, USDCAD: positive correlation
        return (direction == "BUY" and dollar_up) or (direction == "SELL" and not dollar_up)
    return None


# --------------------------------------------------------------------------- #
# Position sizing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PositionSizeResult:
    valid: bool
    lots: float
    raw_lots: float
    risk_amount: float
    actual_risk: float
    actual_risk_percent: float
    sl_distance: float
    loss_per_lot: float
    reason: str = ""


def _step_decimals(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def calculate_position_size(equity: float, risk_percent: float, entry: float, stop_loss: float, *,
                            tick_size: float, tick_value: float, volume_min: float,
                            volume_max: float, volume_step: float,
                            loss_per_lot: float | None = None,
                            max_risk_percent: float | None = None,
                            allow_min_lot: bool = False) -> PositionSizeResult:
    """Lots = (equity x risk%) / (loss of 1 lot at the stop), floored to the volume step."""
    distance = abs(float(entry) - float(stop_loss))
    risk_amount = max(0.0, float(equity)) * float(risk_percent) / 100.0

    def invalid(reason: str, raw: float = 0.0, per_lot: float = 0.0) -> PositionSizeResult:
        return PositionSizeResult(False, 0.0, raw, risk_amount, 0.0, 0.0, distance, per_lot, reason)

    if equity <= 0:
        return invalid("account equity is zero")
    if distance <= 0:
        return invalid("stop loss equals entry")
    if loss_per_lot is None or not math.isfinite(loss_per_lot) or loss_per_lot <= 0:
        if tick_size <= 0 or tick_value <= 0:
            return invalid("symbol tick size/value unavailable")
        loss_per_lot = distance / tick_size * tick_value
    step = volume_step if volume_step > 0 else volume_min or 0.01
    raw = risk_amount / loss_per_lot
    decimals = _step_decimals(step)
    lots = round(math.floor(raw / step + 1e-9) * step, decimals)
    if lots < volume_min - 1e-12:
        min_risk_pct = volume_min * loss_per_lot / equity * 100.0
        cap = max_risk_percent if max_risk_percent is not None else risk_percent
        if allow_min_lot and min_risk_pct <= cap + 1e-9:
            lots = round(volume_min, decimals)
        else:
            return invalid(f"required {raw:.4f} lots is below broker minimum {volume_min:g} "
                           f"(min lot would risk {min_risk_pct:.2f}%)", raw, loss_per_lot)
    lots = min(lots, volume_max)
    actual = lots * loss_per_lot
    actual_pct = actual / equity * 100.0
    if max_risk_percent is not None and actual_pct > max_risk_percent + 1e-9:
        return invalid(f"risk {actual_pct:.2f}% exceeds MAX_RISK_PERCENT {max_risk_percent:g}%",
                       raw, loss_per_lot)
    return PositionSizeResult(True, lots, raw, risk_amount, actual, actual_pct, distance,
                              loss_per_lot)


# --------------------------------------------------------------------------- #
# Strategy engine
# --------------------------------------------------------------------------- #
@dataclass
class _Context:
    symbol: str
    feats: FeatureSet
    now: datetime
    bid: float
    ask: float
    htf_bias: Bias
    mtf_bias: Bias
    dxy_bias: Bias | None
    crt: CRTSetup | None
    crt_available: bool
    pd_zone: str
    analysis: MarketAnalysis

    def reject(self, setup: str, direction: str, reason: str) -> None:
        if len(self.analysis.rejections) < MAX_REJECTIONS:
            self.analysis.rejections.append({"setup": setup, "direction": direction,
                                             "reason": reason, "time": self.now.isoformat()})


class QuantStrategyEngine:
    """Multi-timeframe SMC/ICT confluence engine."""

    SETUP_NAMES = {
        "sweep_mss_fvg": "SMC Liquidity Sweep + MSS + FVG",
        "order_block": "SMC Order Block Retest",
        "time_candle_breakout": "IST Time-Candle Breakout",
    }

    def __init__(self, strategy: StrategySettings, risk: RiskSettings, ltf: str = "M5",
                 mtf: str = "M15", htf: str = "H1"):
        self.cfg = strategy
        self.risk = risk
        self.ltf = ltf
        self.mtf = mtf
        self.htf = htf

    # ---------------------------------------------------------------- features --
    def detect_features(self, df: pd.DataFrame) -> FeatureSet:
        """Swings, sweeps, structure, FVGs and OBs for any (already closed) frame."""
        frame = prepare_ohlcv(df)
        atr = compute_atr(frame, self.cfg.atr_period)
        closes = frame["close"].to_numpy(dtype=float)
        ema = compute_ema(closes, self.cfg.ema_period) if len(closes) else np.array([])
        volume = frame["volume"].to_numpy(dtype=float)
        rel = relative_volume(volume) if len(volume) else np.array([])
        swings = detect_swings(frame, self.cfg.swing_left, self.cfg.swing_right)
        tolerance = 0.1 * float(atr[-1]) if len(atr) else 0.0
        equal = mark_equal_levels(swings, tolerance)
        sweeps = detect_liquidity_sweeps(frame, swings, equal)
        events, trend = detect_market_structure(frame, swings, atr, self.cfg.displacement_atr)
        fvgs = detect_fvgs(frame, atr, rel, self.cfg.fvg_min_atr)
        blocks = detect_order_blocks(frame, events, self.cfg.ob_lookback_bars)
        return FeatureSet(frame, atr, ema, rel, volume_available(volume), swings, equal, sweeps,
                          events, trend, fvgs, blocks)

    def bias_for(self, df: pd.DataFrame | None, timeframe: str, now: datetime) -> Bias:
        if df is None:
            return NEUTRAL_BIAS
        frame = closed_bars(prepare_ohlcv(df), timeframe, now)
        return determine_bias(frame, self.cfg.ema_period, self.cfg.swing_left, self.cfg.swing_right,
                              self.cfg.displacement_atr, self.cfg.atr_period)

    # ---------------------------------------------------------------- analysis --
    def analyze(self, symbol: str, ltf_df: pd.DataFrame, mtf_df: pd.DataFrame | None = None,
                htf_df: pd.DataFrame | None = None, *, crt_df: pd.DataFrame | None = None,
                time_candle_df: pd.DataFrame | None = None, dxy_df: pd.DataFrame | None = None,
                now: datetime | None = None, bid: float | None = None,
                ask: float | None = None) -> MarketAnalysis:
        now = now or datetime.now(UTC)
        analysis = MarketAnalysis(symbol=symbol, timeframe=self.ltf, generated_at=now)
        ltf = closed_bars(prepare_ohlcv(ltf_df), self.ltf, now)
        if len(ltf) < MIN_BARS:
            analysis.notes.append(f"insufficient {self.ltf} data ({len(ltf)} closed bars)")
            return analysis
        feats = self.detect_features(ltf)
        last_close = float(ltf["close"].iloc[-1])
        bid = float(bid) if bid else last_close
        ask = float(ask) if ask else last_close
        analysis.features = feats
        analysis.last_bar_time = ltf.index[-1]
        analysis.last_close = last_close
        analysis.atr = feats.atr_now
        analysis.htf_bias = self.bias_for(htf_df, self.htf, now)
        analysis.mtf_bias = self.bias_for(mtf_df, self.mtf, now)
        if self.cfg.dxy_filter and dxy_df is not None:
            analysis.dxy_bias = self.bias_for(dxy_df, self.htf, now)
            if analysis.dxy_bias is NEUTRAL_BIAS and len(dxy_df) < MIN_BARS:
                analysis.notes.append("DXY data insufficient")
        elif self.cfg.dxy_filter:
            analysis.notes.append("DXY feed unavailable — filter skipped")
        crt_available = False
        if crt_df is not None:
            crt_frame = closed_bars(prepare_ohlcv(crt_df), self.cfg.crt_timeframe, now)
            crt_available = len(crt_frame) >= 2
            crt = detect_crt(crt_frame, self.cfg.crt_mode)
            mid = (bid + ask) / 2.0
            analysis.crt = crt if crt is not None and crt.valid_at(mid) else None
        analysis.killzones_now = active_killzones(now, self.cfg.killzones, self.cfg.killzone_timezone)
        if htf_df is not None:
            htf_closed = closed_bars(prepare_ohlcv(htf_df), self.htf, now)
            analysis.premium_discount = premium_discount(
                detect_swings(htf_closed, self.cfg.swing_left, self.cfg.swing_right), last_close)
        else:
            analysis.premium_discount = {"zone": "UNKNOWN"}
        analysis.session_ranges = compute_session_ranges(ltf, self.cfg.session_ranges,
                                                         self.cfg.killzone_timezone, now)
        ctx = _Context(symbol, feats, now, bid, ask, analysis.htf_bias, analysis.mtf_bias,
                       analysis.dxy_bias, analysis.crt, crt_available,
                       analysis.premium_discount.get("zone", "UNKNOWN"), analysis)
        candidates: list[TradeSignal] = []
        if "sweep_mss_fvg" in self.cfg.enabled:
            candidates += self._setup_sweep_mss_fvg(ctx)
        if "order_block" in self.cfg.enabled:
            candidates += self._setup_order_block(ctx)
        if "time_candle_breakout" in self.cfg.enabled:
            candidates += self._setup_time_candle(ctx, time_candle_df)
        analysis.signals = sorted(candidates, key=lambda s: s.score, reverse=True)
        return analysis

    # ------------------------------------------------------------ generators --
    def _setup_sweep_mss_fvg(self, ctx: _Context) -> list[TradeSignal]:
        feats, cfg = ctx.feats, self.cfg
        n = len(feats.df)
        start = max(0, n - cfg.setup_lookback_bars)
        lows = feats.df["low"].to_numpy(dtype=float)
        highs = feats.df["high"].to_numpy(dtype=float)
        out: list[TradeSignal] = []
        for direction in ("BUY", "SELL"):
            side = "SSL" if direction == "BUY" else "BSL"
            sweeps = [s for s in feats.sweeps if s.side == side and s.pos >= start]
            if not sweeps:
                continue
            sweep = sweeps[-1]
            shifts = [e for e in feats.events if e.direction == direction
                      and sweep.pos <= e.pos <= sweep.pos + cfg.mss_max_bars]
            if not shifts:
                ctx.reject("sweep_mss_fvg", direction, f"{side} sweep without structure shift")
                continue
            shift = shifts[0]
            gaps = [g for g in feats.fvgs if g.direction == direction and g.active
                    and sweep.pos <= g.start_pos <= shift.pos + 1]
            if not gaps:
                ctx.reject("sweep_mss_fvg", direction, "no unfilled FVG in the displacement leg")
                continue
            gap = max(gaps, key=lambda g: g.start_pos)
            buffer = cfg.sl_buffer_atr * feats.atr_now
            if direction == "BUY":
                entry = gap.top if cfg.fvg_entry == "top" else gap.ce
                stop = sweep.extreme - buffer
                invalidated = sweep.pos + 1 < n and lows[sweep.pos + 1:].min() <= stop
            else:
                entry = gap.bottom if cfg.fvg_entry == "top" else gap.ce
                stop = sweep.extreme + buffer
                invalidated = sweep.pos + 1 < n and highs[sweep.pos + 1:].max() >= stop
            if invalidated:
                ctx.reject("sweep_mss_fvg", direction, "price traded through the sweep extreme")
                continue
            leg_volume = float(np.max(feats.rel_volume[sweep.pos:shift.pos + 1]))
            ob_overlap = any(ob.direction == direction and ob.active and ob.bottom <= gap.top
                             and ob.top >= gap.bottom for ob in feats.order_blocks)
            core = {
                "liquidity_sweep": (True, 1.5, f"{side} Sweep"),
                "structure_break": (True, 1.0, shift.kind),
                "reversal_shift": (shift.kind != "BOS", 0.5, "Trend Reversal"),
                "displacement": (shift.displacement, 0.5, "Displacement"),
                "fvg": (True, 1.0, "FVG"),
                "order_block": (ob_overlap, 0.5, "OB Confluence"),
                "volume": (leg_volume >= cfg.volume_spike_mult if feats.volume_ok else None, 0.5,
                           "Volume Spike"),
                "equal_liquidity": (sweep.equal_level, 0.5,
                                    "EQL Pool" if direction == "BUY" else "EQH Pool"),
            }
            meta = {"sweep_level": sweep.level, "sweep_extreme": sweep.extreme,
                    "sweep_time": sweep.time.isoformat(), "structure_level": shift.level,
                    "fvg_top": gap.top, "fvg_bottom": gap.bottom}
            signal = self._build_signal(ctx, direction, "sweep_mss_fvg", entry, stop, shift.pos,
                                        core, meta)
            if signal:
                out.append(signal)
        return out

    def _setup_order_block(self, ctx: _Context) -> list[TradeSignal]:
        feats, cfg = ctx.feats, self.cfg
        n = len(feats.df)
        start = max(0, n - cfg.setup_lookback_bars)
        out: list[TradeSignal] = []
        for direction in ("BUY", "SELL"):
            blocks = [ob for ob in feats.order_blocks if ob.direction == direction and ob.active
                      and ob.event_pos >= start]
            if not blocks:
                continue
            block = max(blocks, key=lambda ob: ob.event_pos)
            if block.mitigated_pos is not None and block.mitigated_pos < n - 2:
                ctx.reject("order_block", direction, "order block already mitigated")
                continue
            if not ctx.htf_bias.aligned(direction):
                ctx.reject("order_block", direction, "OB continuation needs HTF alignment")
                continue
            event = next((e for e in feats.events if e.pos == block.event_pos
                          and e.direction == direction), None)
            if event is None:
                continue
            buffer = cfg.sl_buffer_atr * feats.atr_now
            if direction == "BUY":
                entry = block.top if cfg.ob_entry == "proximal" else block.mean_threshold
                stop = block.bottom - buffer
            else:
                entry = block.bottom if cfg.ob_entry == "proximal" else block.mean_threshold
                stop = block.top + buffer
            leg_fvg = any(g.direction == direction and block.pos <= g.start_pos <= block.event_pos + 1
                          for g in feats.fvgs)
            leg_volume = float(np.max(feats.rel_volume[block.pos:block.event_pos + 1]))
            core = {
                "structure_break": (True, 1.0, event.kind),
                "order_block": (True, 1.5, "Order Block"),
                "fvg": (leg_fvg, 1.0, "FVG in Impulse"),
                "displacement": (event.displacement, 0.5, "Displacement"),
                "volume": (leg_volume >= cfg.volume_spike_mult if feats.volume_ok else None, 0.5,
                           "Volume Spike"),
            }
            meta = {"ob_top": block.top, "ob_bottom": block.bottom,
                    "ob_time": block.time.isoformat(), "structure_level": event.level}
            signal = self._build_signal(ctx, direction, "order_block", entry, stop,
                                        block.event_pos, core, meta)
            if signal:
                out.append(signal)
        return out

    def _setup_time_candle(self, ctx: _Context, ref_df: pd.DataFrame | None) -> list[TradeSignal]:
        feats, cfg = ctx.feats, self.cfg
        timeframe = cfg.time_candle_timeframe
        if ref_df is not None:
            reference = closed_bars(prepare_ohlcv(ref_df), timeframe, ctx.now)
        elif timeframe == self.ltf:
            reference = feats.df
        else:
            return []
        if reference.empty:
            return []
        ref_delta = timeframe_delta(timeframe)
        now_ts = pd.Timestamp(ctx.now)
        today_ist = ctx.now.astimezone(IST).date()
        atr = feats.atr_now
        out: list[TradeSignal] = []
        for t_ist in cfg.time_candle_times_ist:
            ref_start = pd.Timestamp(datetime.combine(today_ist, t_ist, tzinfo=IST).astimezone(UTC))
            ref_close = ref_start + ref_delta
            window_end = ref_close + pd.Timedelta(minutes=cfg.time_candle_valid_minutes)
            if now_ts < ref_close or now_ts > window_end:
                continue
            if ref_start not in reference.index:
                ctx.reject("time_candle_breakout", "-", f"{t_ist:%H:%M} IST reference candle missing")
                continue
            row = reference.loc[ref_start]
            range_high, range_low = float(row["high"]), float(row["low"])
            if range_high <= range_low:
                continue
            after = feats.df[feats.df.index >= ref_close]
            if after.empty:
                continue
            closes = after["close"].to_numpy(dtype=float)
            ups = np.nonzero(closes > range_high)[0]
            downs = np.nonzero(closes < range_low)[0]
            if not ups.size and not downs.size:
                continue
            if ups.size and (not downs.size or ups[0] < downs[0]):
                direction, rel_pos = "BUY", int(ups[0])
            else:
                direction, rel_pos = "SELL", int(downs[0])
            pos = int(feats.df.index.get_loc(after.index[rel_pos]))
            breakout_close = float(closes[rel_pos])
            buffer = cfg.sl_buffer_atr * atr
            if cfg.time_candle_entry == "close":
                entry = breakout_close
            else:
                entry = range_high if direction == "BUY" else range_low
            anchor_level = (range_high + range_low) / 2.0 if cfg.time_candle_sl == "mid" else (
                range_low if direction == "BUY" else range_high)
            stop = anchor_level - buffer if direction == "BUY" else anchor_level + buffer
            body = abs(float(feats.df["close"].iloc[pos]) - float(feats.df["open"].iloc[pos]))
            range_size = range_high - range_low
            core = {
                "range_breakout": (True, 2.0, f"{t_ist:%H:%M} IST Candle Break"),
                "volume": (float(feats.rel_volume[pos]) >= cfg.volume_spike_mult
                           if feats.volume_ok else None, 1.0, "Breakout Volume"),
                "range_quality": (0.5 * atr <= range_size <= 4.0 * atr if atr > 0 else None, 1.0,
                                  "Clean Range"),
                "displacement": (atr > 0 and body >= cfg.displacement_atr * atr, 0.5,
                                 "Displacement Close"),
            }
            meta = {"range_high": range_high, "range_low": range_low,
                    "reference_time": ref_start.isoformat(), "reference_ist": f"{t_ist:%H:%M}"}
            signal = self._build_signal(
                ctx, direction, "time_candle_breakout", entry, stop, pos, core, meta,
                strategy_name=f"IST Time-Candle Breakout ({t_ist:%H:%M} IST)",
                valid_until_cap=window_end.to_pydatetime())
            if signal:
                out.append(signal)
        return out

    # --------------------------------------------------------------- builder --
    def _build_signal(self, ctx: _Context, direction: str, setup: str, entry: float, stop: float,
                      anchor_pos: int, core: dict[str, tuple[bool | None, float, str]],
                      meta: dict[str, Any], strategy_name: str | None = None,
                      valid_until_cap: datetime | None = None) -> TradeSignal | None:
        cfg, feats = self.cfg, ctx.feats
        sign = 1.0 if direction == "BUY" else -1.0
        atr = feats.atr_now
        if atr <= 0:
            ctx.reject(setup, direction, "ATR unavailable")
            return None
        if not cfg.allow_counter_htf and ctx.htf_bias.opposed(direction):
            ctx.reject(setup, direction, f"against HTF {self.htf} {ctx.htf_bias.direction} bias")
            return None
        if (entry - stop) * sign <= 0:
            ctx.reject(setup, direction, "stop on the wrong side of entry")
            return None
        ltf_delta = timeframe_delta(self.ltf)
        anchor_time = feats.df.index[anchor_pos].to_pydatetime()
        anchor_close = anchor_time + ltf_delta
        valid_until = anchor_close + ltf_delta * cfg.setup_expiry_bars
        if valid_until_cap is not None:
            valid_until = min(valid_until, valid_until_cap)
        if ctx.now > valid_until:
            ctx.reject(setup, direction, "setup expired")
            return None
        price = ctx.ask if direction == "BUY" else ctx.bid
        planned_risk = (entry - stop) * sign
        if (price - stop) * sign <= 0:
            ctx.reject(setup, direction, "price is beyond the stop (invalidated)")
            return None
        progress = (price - entry) * sign / planned_risk
        if progress > 0:
            if progress > cfg.max_entry_drift_r:
                ctx.reject(setup, direction, f"price ran {progress:.2f}R from entry")
                return None
            order_type, final_entry = "LIMIT", entry
        else:
            order_type, final_entry = "MARKET", price
        risk_distance = (final_entry - stop) * sign
        if risk_distance < cfg.min_sl_atr * atr:
            ctx.reject(setup, direction, f"stop too tight ({risk_distance / atr:.2f} ATR)")
            return None
        if risk_distance > cfg.max_sl_atr * atr:
            ctx.reject(setup, direction, f"stop too wide ({risk_distance / atr:.2f} ATR)")
            return None
        tp1 = final_entry + sign * risk_distance * self.risk.tp1_rr
        tp2 = final_entry + sign * risk_distance * self.risk.tp2_rr

        factors = dict(core)
        bias_label = ctx.htf_bias.direction.title()
        factors["htf_bias"] = (ctx.htf_bias.aligned(direction), 2.0, f"HTF {self.htf} {bias_label}")
        factors["mtf_bias"] = (ctx.mtf_bias.aligned(direction), 1.0, f"MTF {self.mtf} Aligned")
        zones = active_killzones(anchor_close, cfg.killzones, cfg.killzone_timezone)
        factors["killzone"] = (bool(zones), 1.0,
                               f"{zones[0].replace('_', ' ').title()} Killzone" if zones else "Killzone")
        ema = feats.ema
        if len(ema) > 5:
            slope = ema[-1] - ema[-6]
            last = float(feats.df["close"].iloc[-1])
            ema_ok = (last > ema[-1] and slope > 0) if direction == "BUY" else (last < ema[-1] and slope < 0)
            factors["ema_trend"] = (bool(ema_ok), 0.5, f"EMA{cfg.ema_period} Trend")
        if ctx.pd_zone in ("PREMIUM", "DISCOUNT"):
            wanted = "DISCOUNT" if direction == "BUY" else "PREMIUM"
            factors["premium_discount"] = (ctx.pd_zone == wanted, 0.5, f"HTF {wanted.title()}")
        if ctx.crt is not None:
            factors["crt"] = (ctx.crt.direction == direction, 1.0,
                              f"{ctx.crt.variant} {cfg.crt_timeframe}")
        else:
            factors["crt"] = (False if ctx.crt_available else None, 1.0, f"CRT {cfg.crt_timeframe}")
        dxy_ok = dxy_alignment(ctx.symbol, direction, ctx.dxy_bias)
        if dxy_ok is None and cfg.dxy_required and cfg.dxy_filter:
            ctx.reject(setup, direction, "DXY confirmation required but unavailable")
            return None
        if dxy_ok is False and cfg.dxy_required:
            ctx.reject(setup, direction, "DXY contradicts the trade")
            return None
        factors["dxy"] = (dxy_ok, 1.0, "DXY Confirms")

        evaluable = {k: v for k, v in factors.items() if v[0] is not None}
        total = sum(weight for _, weight, _ in evaluable.values())
        achieved = sum(weight for ok, weight, _ in evaluable.values() if ok)
        score = round(10.0 * achieved / total, 1) if total else 0.0
        if score >= cfg.a_plus_score:
            grade = "A+"
        elif score >= cfg.min_score_to_trade:
            grade = "A"
        elif score >= 5.0:
            grade = "B"
        else:
            grade = "C"
        confluences = [label for ok, _, label in evaluable.values() if ok]
        meta = dict(meta)
        meta.update({"planned_entry": entry, "atr": atr, "progress_r": round(progress, 3),
                     "killzones": zones})
        return TradeSignal(
            symbol=ctx.symbol, direction=direction,  # type: ignore[arg-type]
            strategy=strategy_name or self.SETUP_NAMES.get(setup, setup), setup=setup,
            timeframe=f"{self.htf}/{self.mtf}/{self.ltf}", order_type=order_type,
            entry=float(final_entry), stop_loss=float(stop), tp1=float(tp1), tp2=float(tp2),
            tp1_rr=self.risk.tp1_rr, rr=self.risk.tp2_rr, score=score, grade=grade,
            confluences=confluences,
            factors={k: {"ok": v[0], "weight": v[1], "label": v[2]} for k, v in factors.items()},
            anchor_time=anchor_time, created_at=ctx.now, valid_until=valid_until, meta=meta)

    # ------------------------------------------------------------ manual aid --
    def structure_stop(self, feats: FeatureSet, direction: str, price: float) -> float | None:
        """Nearest confirmed swing beyond price (for manual trades / structure trailing)."""
        candidates = [s.price for s in feats.swings
                      if s.kind == ("low" if direction == "BUY" else "high")
                      and s.confirmed_pos < len(feats.df)
                      and ((s.price < price) if direction == "BUY" else (s.price > price))]
        if not candidates:
            return None
        buffer = self.cfg.sl_buffer_atr * feats.atr_now
        level = max(candidates[-5:]) if direction == "BUY" else min(candidates[-5:])
        return level - buffer if direction == "BUY" else level + buffer
