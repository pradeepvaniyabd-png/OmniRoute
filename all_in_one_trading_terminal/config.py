"""
config.py — Centralised configuration for the All-in-One Trading Super-Terminal.

Every tunable lives here: broker credentials, symbols, timeframes, risk limits,
strategy parameters, news kill-switch windows, Telegram and paper-trading
settings.  Values are read from the environment (``.env``), validated and frozen
into immutable dataclasses so every thread sees one consistent configuration.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

BROKER_MODES = ("mt5", "paper")
EXECUTION_MODES = ("auto", "signal_only")
STRATEGY_CODES = ("sweep_mss_fvg", "order_block", "time_candle_breakout")


class ConfigError(ValueError):
    """Raised when a configuration value is missing or invalid."""


# --------------------------------------------------------------------------- #
# Small helpers shared by every module
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(UTC)


def timeframe_delta(timeframe: str) -> timedelta:
    try:
        return timedelta(minutes=TIMEFRAME_MINUTES[timeframe.upper()])
    except KeyError as exc:
        raise ConfigError(
            f"Unsupported timeframe '{timeframe}'. Use one of: {', '.join(TIMEFRAME_MINUTES)}"
        ) from exc


def canonical_symbol(symbol: str) -> str:
    """'xauusd.r' -> 'XAUUSDR' (alphanumerics only, upper-case)."""
    return "".join(ch for ch in str(symbol).upper() if ch.isalnum())


def symbol_value(mapping: Mapping[str, float], symbol: str, default: float) -> float:
    """Look up a per-symbol value, tolerating broker suffixes (XAUUSDm -> XAUUSD)."""
    key = canonical_symbol(symbol)
    if key in mapping:
        return mapping[key]
    for name, value in mapping.items():
        if key.startswith(name):
            return value
    return default


_PIP_OVERRIDES: dict[str, float] = {
    "XAUUSD": 0.1,
    "XAGUSD": 0.01,
    "BTCUSD": 1.0,
    "ETHUSD": 0.1,
    "US30": 1.0,
    "NAS100": 1.0,
    "SPX500": 0.1,
    "DXY": 0.01,
}


def pip_size_for(symbol: str, point: float | None = None, digits: int | None = None) -> float:
    """Conventional pip size used for human-readable pip figures."""
    key = canonical_symbol(symbol)
    for name, value in _PIP_OVERRIDES.items():
        if key.startswith(name):
            return value
    if "JPY" in key:
        return 0.01
    if point and digits is not None:
        return point * 10 if digits in (3, 5) else point
    return 0.0001


DEFAULT_PAPER_SPECS: dict[str, dict[str, Any]] = {
    "XAUUSD": {"digits": 2, "contract_size": 100.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 50.0, "base": "XAU", "quote": "USD", "spread_points": 20},
    "XAGUSD": {"digits": 3, "contract_size": 5000.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 50.0, "base": "XAG", "quote": "USD", "spread_points": 30},
    "BTCUSD": {"digits": 2, "contract_size": 1.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 20.0, "base": "BTC", "quote": "USD", "spread_points": 1500},
    "ETHUSD": {"digits": 2, "contract_size": 1.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 200.0, "base": "ETH", "quote": "USD", "spread_points": 150},
    "EURUSD": {"digits": 5, "contract_size": 100000.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 100.0, "base": "EUR", "quote": "USD", "spread_points": 12},
    "GBPUSD": {"digits": 5, "contract_size": 100000.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 100.0, "base": "GBP", "quote": "USD", "spread_points": 15},
    "USDJPY": {"digits": 3, "contract_size": 100000.0, "volume_min": 0.01, "volume_step": 0.01,
               "volume_max": 100.0, "base": "USD", "quote": "JPY", "spread_points": 15},
    "DXY": {"digits": 3, "contract_size": 1000.0, "volume_min": 0.01, "volume_step": 0.01,
            "volume_max": 10.0, "base": "DXY", "quote": "USD", "spread_points": 30,
            "tradeable": False},
}

DEFAULT_PAPER_SOURCES: dict[str, str] = {
    "XAUUSD": "yahoo:GC=F",
    "XAGUSD": "yahoo:SI=F",
    "BTCUSD": "binance:BTCUSDT",
    "ETHUSD": "binance:ETHUSDT",
    "EURUSD": "yahoo:EURUSD=X",
    "GBPUSD": "yahoo:GBPUSD=X",
    "USDJPY": "yahoo:JPY=X",
    "DXY": "yahoo:DX-Y.NYB",
}


def paper_spec_for(symbol: str) -> dict[str, Any]:
    """Contract specification used by the paper broker for ``symbol``."""
    key = canonical_symbol(symbol)
    if key in DEFAULT_PAPER_SPECS:
        return dict(DEFAULT_PAPER_SPECS[key])
    if len(key) == 6 and key.isalpha():
        jpy = key.endswith("JPY")
        return {"digits": 3 if jpy else 5, "contract_size": 100000.0, "volume_min": 0.01,
                "volume_step": 0.01, "volume_max": 100.0, "base": key[:3], "quote": key[3:],
                "spread_points": 20}
    return {"digits": 2, "contract_size": 1.0, "volume_min": 0.01, "volume_step": 0.01,
            "volume_max": 100.0, "base": key, "quote": "USD", "spread_points": 50}


# --------------------------------------------------------------------------- #
# Typed setting groups
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionWindow:
    """A daily time window (e.g. a killzone) expressed in a given time zone."""

    name: str
    start: dtime
    end: dtime

    def contains(self, moment: dtime) -> bool:
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # window crosses midnight

    @property
    def label(self) -> str:
        return f"{self.name} {self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class MT5Settings:
    login: int | None
    password: str
    server: str
    terminal_path: str
    timeout_ms: int
    portable: bool
    server_utc_offset: str  # "auto" | "ny_close" | "<hours>"
    magic: int
    deviation_points: int
    symbol_map: dict[str, str]
    symbol_suffix: str


@dataclass(frozen=True)
class PaperSettings:
    starting_balance: float
    leverage: float
    commission_per_lot: float
    data_sources: dict[str, str]
    spread_points: dict[str, float]
    http_timeout: float
    account_currency: str = "USD"


@dataclass(frozen=True)
class RiskSettings:
    risk_percent: float
    max_risk_percent: float
    max_daily_loss_percent: float
    daily_reset_timezone: str
    daily_reset_hour: int
    daily_loss_reference: str  # equity | balance | max
    circuit_breaker_scope: str  # all | bot
    circuit_breaker_alert_channel: bool
    max_open_positions: int
    max_positions_per_symbol: int
    max_trades_per_day: int
    allow_hedging: bool
    loss_cooldown_minutes: int
    allow_min_lot_override: bool
    tp1_rr: float
    tp2_rr: float
    tp1_close_percent: float
    breakeven_trigger_r: float
    breakeven_buffer_points: dict[str, float]
    trailing_start_r: float
    trailing_mode: str  # atr | points | structure
    trailing_atr_mult: float
    trailing_points: dict[str, float]
    trailing_min_step_atr: float
    max_spread_points: dict[str, float]
    default_max_spread_points: float
    manage_manual_positions: bool


@dataclass(frozen=True)
class StrategySettings:
    enabled: tuple[str, ...]
    min_score_to_trade: float
    a_plus_score: float
    require_killzone: bool
    allow_counter_htf: bool
    killzone_timezone: str
    killzones: tuple[SessionWindow, ...]
    session_ranges: tuple[SessionWindow, ...]
    swing_left: int
    swing_right: int
    fvg_min_atr: float
    fvg_entry: str  # top | ce
    ob_entry: str  # proximal | mean
    ob_lookback_bars: int
    displacement_atr: float
    sl_buffer_atr: float
    min_sl_atr: float
    max_sl_atr: float
    setup_lookback_bars: int
    mss_max_bars: int
    setup_expiry_bars: int
    max_entry_drift_r: float
    ema_period: int
    atr_period: int
    volume_spike_mult: float
    crt_timeframe: str
    crt_mode: str  # wick | tbs | both
    dxy_filter: bool
    dxy_symbol: str
    dxy_required: bool
    time_candle_times_ist: tuple[dtime, ...]
    time_candle_timeframe: str
    time_candle_valid_minutes: int
    time_candle_entry: str  # close | retest
    time_candle_sl: str  # range | mid
    ltf_bars: int
    mtf_bars: int
    htf_bars: int


@dataclass(frozen=True)
class NewsSettings:
    filter_enabled: bool
    blackout_before_min: int
    blackout_after_min: int
    blackout_impacts: tuple[str, ...]
    action_on_open_positions: str  # none | breakeven | close
    pre_action_minutes: int
    warning_lead_min: int
    alert_currencies: tuple[str, ...]
    alert_impacts: tuple[str, ...]
    warning_keywords: tuple[str, ...]
    warning_keywords_only: bool
    digest_enabled: bool
    digest_time_ist: dtime
    digest_grace_hours: float
    currency_map: dict[str, str]
    refresh_minutes: int
    json_url: str
    xml_url: str
    html_url: str
    xml_timezone: str
    html_timezone: str
    http_timeout: float


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    bot_token: str
    channel_id: str
    admin_chat_id: str
    brand_name: str
    broadcast_executed: bool
    broadcast_a_plus: bool
    broadcast_updates: bool
    broadcast_trail_steps: bool
    broadcast_manual: bool
    news_to_channel: bool
    admin_news_lock_alerts: bool
    api_base: str
    timeout: float


@dataclass(frozen=True)
class AppSettings:
    data_dir: Path
    log_dir: Path
    cache_dir: Path
    db_path: Path
    lock_file: Path
    log_level: str
    log_retention_days: int
    ui_refresh_seconds: float
    chart_refresh_seconds: float
    chart_bars: int
    chart_timezone: str


@dataclass(frozen=True)
class Settings:
    broker_mode: str
    symbols: tuple[str, ...]
    htf: str
    mtf: str
    ltf: str
    scan_interval_seconds: float
    protector_interval_seconds: float
    news_loop_seconds: float
    execution_mode: str
    auto_start_bot: bool
    mt5: MT5Settings
    paper: PaperSettings
    risk: RiskSettings
    strategy: StrategySettings
    news: NewsSettings
    telegram: TelegramSettings
    app: AppSettings

    def public_summary(self) -> dict[str, Any]:
        """Non-secret overview for the dashboard."""
        return {
            "broker_mode": self.broker_mode,
            "symbols": list(self.symbols),
            "timeframes": {"HTF": self.htf, "MTF": self.mtf, "LTF": self.ltf},
            "risk_percent": self.risk.risk_percent,
            "max_daily_loss_percent": self.risk.max_daily_loss_percent,
            "tp_rr": [self.risk.tp1_rr, self.risk.tp2_rr],
            "strategies": list(self.strategy.enabled),
            "killzone_tz": self.strategy.killzone_timezone,
            "news_window_min": [self.news.blackout_before_min, self.news.blackout_after_min],
            "telegram_configured": bool(self.telegram.bot_token),
        }


# --------------------------------------------------------------------------- #
# Environment parsing
# --------------------------------------------------------------------------- #
class _Env:
    """Typed accessors over an environment mapping with clear error messages."""

    _TRUE = {"1", "true", "yes", "y", "on"}
    _FALSE = {"0", "false", "no", "n", "off"}

    def __init__(self, environ: Mapping[str, str]):
        self._env = environ

    def raw(self, name: str) -> str | None:
        value = self._env.get(name)
        if value is None:
            return None
        value = str(value).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        return value or None

    def get_str(self, name: str, default: str = "") -> str:
        value = self.raw(name)
        return default if value is None else value

    def get_bool(self, name: str, default: bool) -> bool:
        value = self.raw(name)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in self._TRUE:
            return True
        if lowered in self._FALSE:
            return False
        raise ConfigError(f"{name}: expected true/false, got '{value}'")

    def _number(self, name: str, default: float, cast: Callable[[str], Any],
                lo: float | None, hi: float | None) -> Any:
        value = self.raw(name)
        if value is None:
            result = default
        else:
            try:
                result = cast(value)
            except ValueError as exc:
                raise ConfigError(f"{name}: '{value}' is not a valid number") from exc
        if lo is not None and result < lo:
            raise ConfigError(f"{name}: {result} is below the minimum {lo}")
        if hi is not None and result > hi:
            raise ConfigError(f"{name}: {result} is above the maximum {hi}")
        return result

    def get_int(self, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        return int(self._number(name, default, lambda v: int(float(v)), lo, hi))

    def get_float(self, name: str, default: float, lo: float | None = None,
                  hi: float | None = None) -> float:
        return float(self._number(name, default, float, lo, hi))

    def get_choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        value = self.get_str(name, default).lower()
        if value not in choices:
            raise ConfigError(f"{name}: '{value}' must be one of {', '.join(choices)}")
        return value

    def get_list(self, name: str, default: str, upper: bool = True) -> tuple[str, ...]:
        raw = self.get_str(name, default)
        items = [part.strip() for part in raw.split(",") if part.strip()]
        if upper:
            items = [item.upper() for item in items]
        return tuple(dict.fromkeys(items))  # dedupe, keep order

    def get_map(self, name: str, default: str, cast: Callable[[str], Any] = str,
                upper_keys: bool = True) -> dict[str, Any]:
        raw = self.get_str(name, default)
        result: dict[str, Any] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ConfigError(f"{name}: entry '{part}' must look like KEY=VALUE")
            key, value = part.split("=", 1)
            key = canonical_symbol(key) if upper_keys else key.strip()
            try:
                result[key] = cast(value.strip())
            except ValueError as exc:
                raise ConfigError(f"{name}: invalid value in '{part}'") from exc
        return result

    def get_time(self, name: str, default: str) -> dtime:
        return _parse_hhmm(self.get_str(name, default), name)

    def get_times(self, name: str, default: str) -> tuple[dtime, ...]:
        raw = self.get_str(name, default)
        return tuple(_parse_hhmm(part.strip(), name) for part in raw.split(",") if part.strip())

    def get_windows(self, name: str, default: str) -> tuple[SessionWindow, ...]:
        raw = self.get_str(name, default)
        windows: list[SessionWindow] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part or "-" not in part.split("=", 1)[1]:
                raise ConfigError(f"{name}: '{part}' must look like NAME=HH:MM-HH:MM")
            label, span = part.split("=", 1)
            start_s, end_s = span.split("-", 1)
            windows.append(SessionWindow(label.strip().upper(), _parse_hhmm(start_s, name),
                                         _parse_hhmm(end_s, name)))
        return tuple(windows)

    def get_tz(self, name: str, default: str) -> str:
        value = self.get_str(name, default)
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"{name}: unknown time zone '{value}' (on Windows run: pip install tzdata)"
            ) from exc
        return value

    def get_path(self, name: str, default: str) -> Path:
        path = Path(self.get_str(name, default)).expanduser()
        return path if path.is_absolute() else (BASE_DIR / path).resolve()


def _parse_hhmm(value: str, name: str) -> dtime:
    value = value.strip()
    try:
        hours, minutes = value.split(":", 1)
        return dtime(int(hours) % 24, int(minutes))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{name}: '{value}' is not a valid HH:MM time") from exc


def _validate_offset(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in ("auto", "ny_close"):
        return lowered
    try:
        hours = float(lowered)
    except ValueError as exc:
        raise ConfigError("MT5_SERVER_UTC_OFFSET must be 'auto', 'ny_close' or hours like 2") from exc
    if not -12 <= hours <= 14:
        raise ConfigError("MT5_SERVER_UTC_OFFSET must be between -12 and +14 hours")
    return lowered


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build validated :class:`Settings` from ``environ`` (defaults to ``os.environ``)."""
    env = _Env(os.environ if environ is None else environ)

    broker_mode = env.get_choice("BROKER_MODE", "paper", BROKER_MODES)
    symbols = env.get_list("SYMBOLS", "XAUUSD,BTCUSD,EURUSD,GBPUSD")
    if not symbols:
        raise ConfigError("SYMBOLS must list at least one instrument")
    symbols = tuple(canonical_symbol(s) for s in symbols)

    htf = env.get_str("HTF_TIMEFRAME", "H1").upper()
    mtf = env.get_str("MTF_TIMEFRAME", "M15").upper()
    ltf = env.get_str("LTF_TIMEFRAME", "M5").upper()
    for label, tf in (("HTF_TIMEFRAME", htf), ("MTF_TIMEFRAME", mtf), ("LTF_TIMEFRAME", ltf)):
        if tf not in TIMEFRAME_MINUTES:
            raise ConfigError(f"{label}: '{tf}' unsupported ({', '.join(TIMEFRAME_MINUTES)})")
    if not TIMEFRAME_MINUTES[ltf] < TIMEFRAME_MINUTES[mtf] <= TIMEFRAME_MINUTES[htf]:
        raise ConfigError("Timeframes must satisfy LTF < MTF <= HTF (e.g. M5 < M15 <= H1)")

    login_raw = env.raw("MT5_LOGIN")
    try:
        login = int(login_raw) if login_raw else None
    except ValueError as exc:
        raise ConfigError("MT5_LOGIN must be the numeric account number") from exc

    mt5_settings = MT5Settings(
        login=login,
        password=env.get_str("MT5_PASSWORD"),
        server=env.get_str("MT5_SERVER"),
        terminal_path=env.get_str("MT5_TERMINAL_PATH"),
        timeout_ms=env.get_int("MT5_TIMEOUT_MS", 60000, 1000, 600000),
        portable=env.get_bool("MT5_PORTABLE", False),
        server_utc_offset=_validate_offset(env.get_str("MT5_SERVER_UTC_OFFSET", "auto")),
        magic=env.get_int("MAGIC_NUMBER", 770077, 1, 2_147_483_647),
        deviation_points=env.get_int("MAX_DEVIATION_POINTS", 20, 0, 10000),
        symbol_map={k: v.strip() for k, v in env.get_map("SYMBOL_MAP", "").items()},
        symbol_suffix=env.get_str("SYMBOL_SUFFIX"),
    )

    paper_sources = dict(DEFAULT_PAPER_SOURCES)
    paper_sources.update(env.get_map("PAPER_DATA_SOURCES", ""))
    for key, source in paper_sources.items():
        if ":" not in source or source.split(":", 1)[0].lower() not in ("yahoo", "binance"):
            raise ConfigError(f"PAPER_DATA_SOURCES: '{key}={source}' must be yahoo:<ticker> "
                              "or binance:<pair>")
    paper = PaperSettings(
        starting_balance=env.get_float("PAPER_STARTING_BALANCE", 10000.0, 10.0, 1e9),
        leverage=env.get_float("PAPER_LEVERAGE", 100.0, 1.0, 3000.0),
        commission_per_lot=env.get_float("PAPER_COMMISSION_PER_LOT", 0.0, 0.0, 1000.0),
        data_sources=paper_sources,
        spread_points=env.get_map("PAPER_SPREAD_POINTS", "", float),
        http_timeout=env.get_float("PAPER_HTTP_TIMEOUT", 10.0, 1.0, 120.0),
    )

    risk = RiskSettings(
        risk_percent=env.get_float("RISK_PERCENT", 1.0, 0.01, 10.0),
        max_risk_percent=env.get_float("MAX_RISK_PERCENT", 2.0, 0.01, 10.0),
        max_daily_loss_percent=env.get_float("MAX_DAILY_LOSS_PERCENT", 3.0, 0.1, 50.0),
        daily_reset_timezone=env.get_tz("DAILY_RESET_TIMEZONE", "Asia/Kolkata"),
        daily_reset_hour=env.get_int("DAILY_RESET_HOUR", 0, 0, 23),
        daily_loss_reference=env.get_choice("DAILY_LOSS_REFERENCE", "equity",
                                            ("equity", "balance", "max")),
        circuit_breaker_scope=env.get_choice("CIRCUIT_BREAKER_SCOPE", "all", ("all", "bot")),
        circuit_breaker_alert_channel=env.get_bool("CIRCUIT_BREAKER_ALERT_CHANNEL", True),
        max_open_positions=env.get_int("MAX_OPEN_POSITIONS", 3, 1, 100),
        max_positions_per_symbol=env.get_int("MAX_POSITIONS_PER_SYMBOL", 1, 1, 20),
        max_trades_per_day=env.get_int("MAX_TRADES_PER_DAY", 6, 1, 500),
        allow_hedging=env.get_bool("ALLOW_HEDGING", False),
        loss_cooldown_minutes=env.get_int("LOSS_COOLDOWN_MINUTES", 30, 0, 1440),
        allow_min_lot_override=env.get_bool("ALLOW_MIN_LOT_OVERRIDE", False),
        tp1_rr=env.get_float("TP1_RR", 1.5, 0.1, 50.0),
        tp2_rr=env.get_float("TP2_RR", 3.0, 0.2, 100.0),
        tp1_close_percent=env.get_float("TP1_CLOSE_PERCENT", 50.0, 0.0, 100.0),
        breakeven_trigger_r=env.get_float("BREAKEVEN_TRIGGER_R", 1.0, 0.1, 20.0),
        breakeven_buffer_points=env.get_map(
            "BREAKEVEN_BUFFER_POINTS", "XAUUSD=30,BTCUSD=1500,EURUSD=15,GBPUSD=15", float),
        trailing_start_r=env.get_float("TRAILING_START_R", 1.5, 0.1, 50.0),
        trailing_mode=env.get_choice("TRAILING_MODE", "atr", ("atr", "points", "structure")),
        trailing_atr_mult=env.get_float("TRAILING_ATR_MULT", 1.5, 0.1, 20.0),
        trailing_points=env.get_map(
            "TRAILING_POINTS", "XAUUSD=400,BTCUSD=30000,EURUSD=150,GBPUSD=150", float),
        trailing_min_step_atr=env.get_float("TRAILING_MIN_STEP_ATR", 0.1, 0.0, 5.0),
        max_spread_points=env.get_map(
            "MAX_SPREAD_POINTS", "XAUUSD=45,BTCUSD=3500,EURUSD=20,GBPUSD=25", float),
        default_max_spread_points=env.get_float("DEFAULT_MAX_SPREAD_POINTS", 50.0, 0.0, 1e7),
        manage_manual_positions=env.get_bool("MANAGE_MANUAL_POSITIONS", False),
    )
    if risk.risk_percent > risk.max_risk_percent:
        raise ConfigError("RISK_PERCENT cannot exceed MAX_RISK_PERCENT")
    if risk.tp2_rr <= risk.tp1_rr:
        raise ConfigError("TP2_RR must be greater than TP1_RR")

    enabled = tuple(s.lower() for s in env.get_list(
        "ENABLED_STRATEGIES", ",".join(STRATEGY_CODES), upper=False))
    unknown = [code for code in enabled if code not in STRATEGY_CODES]
    if unknown:
        raise ConfigError(f"ENABLED_STRATEGIES: unknown {unknown}; valid: {STRATEGY_CODES}")

    strategy = StrategySettings(
        enabled=enabled,
        min_score_to_trade=env.get_float("MIN_SCORE_TO_TRADE", 6.5, 0.0, 10.0),
        a_plus_score=env.get_float("A_PLUS_SCORE", 8.0, 0.0, 10.0),
        require_killzone=env.get_bool("REQUIRE_KILLZONE", True),
        allow_counter_htf=env.get_bool("ALLOW_COUNTER_HTF", False),
        killzone_timezone=env.get_tz("KILLZONE_TIMEZONE", "America/New_York"),
        killzones=env.get_windows(
            "KILLZONES",
            "LONDON=02:00-05:00,NY_AM=07:00-10:00,SB_LONDON=03:00-04:00,"
            "SB_NY_AM=10:00-11:00,SB_NY_PM=14:00-15:00"),
        session_ranges=env.get_windows(
            "SESSION_RANGES", "ASIA=20:00-00:00,LONDON=02:00-05:00,NEW_YORK=07:00-10:00"),
        swing_left=env.get_int("SWING_LEFT_BARS", 2, 1, 20),
        swing_right=env.get_int("SWING_RIGHT_BARS", 2, 1, 20),
        fvg_min_atr=env.get_float("FVG_MIN_ATR", 0.15, 0.0, 10.0),
        fvg_entry=env.get_choice("FVG_ENTRY", "top", ("top", "ce")),
        ob_entry=env.get_choice("OB_ENTRY", "proximal", ("proximal", "mean")),
        ob_lookback_bars=env.get_int("OB_LOOKBACK_BARS", 10, 1, 100),
        displacement_atr=env.get_float("DISPLACEMENT_ATR", 1.0, 0.0, 10.0),
        sl_buffer_atr=env.get_float("SL_BUFFER_ATR", 0.1, 0.0, 5.0),
        min_sl_atr=env.get_float("MIN_SL_ATR", 0.3, 0.0, 10.0),
        max_sl_atr=env.get_float("MAX_SL_ATR", 4.0, 0.1, 50.0),
        setup_lookback_bars=env.get_int("SETUP_LOOKBACK_BARS", 36, 5, 1000),
        mss_max_bars=env.get_int("MSS_MAX_BARS", 12, 1, 200),
        setup_expiry_bars=env.get_int("SETUP_EXPIRY_BARS", 12, 1, 500),
        max_entry_drift_r=env.get_float("MAX_ENTRY_DRIFT_R", 1.0, 0.0, 10.0),
        ema_period=env.get_int("EMA_PERIOD", 50, 2, 1000),
        atr_period=env.get_int("ATR_PERIOD", 14, 2, 500),
        volume_spike_mult=env.get_float("VOLUME_SPIKE_MULT", 1.5, 0.5, 20.0),
        crt_timeframe=env.get_str("CRT_TIMEFRAME", "H4").upper(),
        crt_mode=env.get_choice("CRT_MODE", "both", ("wick", "tbs", "both")),
        dxy_filter=env.get_bool("DXY_FILTER", True),
        dxy_symbol=canonical_symbol(env.get_str("DXY_SYMBOL", "DXY")),
        dxy_required=env.get_bool("DXY_REQUIRED", False),
        time_candle_times_ist=env.get_times("TIME_CANDLE_TIMES_IST", "20:00,20:45"),
        time_candle_timeframe=env.get_str("TIME_CANDLE_TIMEFRAME", "M15").upper(),
        time_candle_valid_minutes=env.get_int("TIME_CANDLE_VALID_MINUTES", 120, 5, 1440),
        time_candle_entry=env.get_choice("TIME_CANDLE_ENTRY", "close", ("close", "retest")),
        time_candle_sl=env.get_choice("TIME_CANDLE_SL", "range", ("range", "mid")),
        ltf_bars=env.get_int("LTF_BARS", 500, 100, 5000),
        mtf_bars=env.get_int("MTF_BARS", 400, 100, 5000),
        htf_bars=env.get_int("HTF_BARS", 400, 100, 5000),
    )
    for label, tf in (("CRT_TIMEFRAME", strategy.crt_timeframe),
                      ("TIME_CANDLE_TIMEFRAME", strategy.time_candle_timeframe)):
        if tf not in TIMEFRAME_MINUTES:
            raise ConfigError(f"{label}: '{tf}' unsupported ({', '.join(TIMEFRAME_MINUTES)})")
    if strategy.a_plus_score < strategy.min_score_to_trade:
        raise ConfigError("A_PLUS_SCORE must be >= MIN_SCORE_TO_TRADE")
    if strategy.max_sl_atr <= strategy.min_sl_atr:
        raise ConfigError("MAX_SL_ATR must be greater than MIN_SL_ATR")

    news = NewsSettings(
        filter_enabled=env.get_bool("NEWS_FILTER_ENABLED", True),
        blackout_before_min=env.get_int("NEWS_BLACKOUT_BEFORE_MIN", 15, 0, 720),
        blackout_after_min=env.get_int("NEWS_BLACKOUT_AFTER_MIN", 15, 0, 720),
        blackout_impacts=tuple(s.title() for s in env.get_list("NEWS_BLACKOUT_IMPACTS", "High")),
        action_on_open_positions=env.get_choice(
            "NEWS_ACTION_ON_OPEN_POSITIONS", "none", ("none", "breakeven", "close")),
        pre_action_minutes=env.get_int("NEWS_PRE_ACTION_MINUTES", 5, 0, 720),
        warning_lead_min=env.get_int("NEWS_WARNING_LEAD_MIN", 15, 1, 720),
        alert_currencies=env.get_list("NEWS_ALERT_CURRENCIES", "USD,EUR,GBP,JPY"),
        alert_impacts=tuple(s.title() for s in env.get_list("NEWS_ALERT_IMPACTS", "High")),
        warning_keywords=env.get_list(
            "NEWS_WARNING_KEYWORDS",
            "CPI,Non-Farm,NFP,FOMC,Federal Funds,Unemployment Claims,Powell,Interest Rate,"
            "GDP,PCE,Retail Sales,ISM,ECB,BOE,BOJ,Lagarde,Bailey,Ueda",
            upper=False),
        warning_keywords_only=env.get_bool("NEWS_WARNING_KEYWORDS_ONLY", False),
        digest_enabled=env.get_bool("NEWS_DIGEST_ENABLED", True),
        digest_time_ist=env.get_time("NEWS_DIGEST_TIME_IST", "08:00"),
        digest_grace_hours=env.get_float("NEWS_DIGEST_GRACE_HOURS", 3.0, 0.1, 23.0),
        currency_map=env.get_map("NEWS_CURRENCY_MAP", ""),
        refresh_minutes=env.get_int("FF_REFRESH_MINUTES", 30, 5, 360),
        json_url=env.get_str("FF_JSON_URL",
                             "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
        xml_url=env.get_str("FF_XML_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        html_url=env.get_str("FF_HTML_URL", "https://www.forexfactory.com/calendar?week=this"),
        xml_timezone=env.get_tz("FF_XML_TIMEZONE", "UTC"),
        html_timezone=env.get_tz("FF_HTML_TIMEZONE", "America/New_York"),
        http_timeout=env.get_float("FF_HTTP_TIMEOUT", 15.0, 1.0, 120.0),
    )

    telegram = TelegramSettings(
        enabled=env.get_bool("TELEGRAM_ENABLED", True),
        bot_token=env.get_str("TELEGRAM_BOT_TOKEN"),
        channel_id=env.get_str("TELEGRAM_CHANNEL_ID"),
        admin_chat_id=env.get_str("TELEGRAM_ADMIN_CHAT_ID"),
        brand_name=env.get_str("TELEGRAM_BRAND_NAME", "Quant Super-Terminal"),
        broadcast_executed=env.get_bool("TELEGRAM_BROADCAST_EXECUTED", True),
        broadcast_a_plus=env.get_bool("TELEGRAM_BROADCAST_A_PLUS", True),
        broadcast_updates=env.get_bool("TELEGRAM_BROADCAST_UPDATES", True),
        broadcast_trail_steps=env.get_bool("TELEGRAM_BROADCAST_TRAIL_STEPS", False),
        broadcast_manual=env.get_bool("TELEGRAM_BROADCAST_MANUAL", True),
        news_to_channel=env.get_bool("TELEGRAM_NEWS_TO_CHANNEL", True),
        admin_news_lock_alerts=env.get_bool("TELEGRAM_ADMIN_NEWS_LOCK_ALERTS", True),
        api_base=env.get_str("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        timeout=env.get_float("TELEGRAM_TIMEOUT", 20.0, 2.0, 120.0),
    )

    data_dir = env.get_path("DATA_DIR", "data")
    app = AppSettings(
        data_dir=data_dir,
        log_dir=env.get_path("LOG_DIR", "logs"),
        cache_dir=data_dir / "cache",
        db_path=data_dir / "terminal.db",
        lock_file=data_dir / "worker.lock",
        log_level=env.get_choice("LOG_LEVEL", "info",
                                 ("debug", "info", "warning", "error")).upper(),
        log_retention_days=env.get_int("LOG_RETENTION_DAYS", 14, 1, 3650),
        ui_refresh_seconds=env.get_float("UI_REFRESH_SECONDS", 2.0, 0.5, 60.0),
        chart_refresh_seconds=env.get_float("CHART_REFRESH_SECONDS", 5.0, 1.0, 300.0),
        chart_bars=env.get_int("CHART_BARS", 300, 50, 2000),
        chart_timezone=env.get_tz("CHART_TIMEZONE", "Asia/Kolkata"),
    )

    return Settings(
        broker_mode=broker_mode,
        symbols=symbols,
        htf=htf,
        mtf=mtf,
        ltf=ltf,
        scan_interval_seconds=env.get_float("SCAN_INTERVAL_SECONDS", 10.0, 1.0, 600.0),
        protector_interval_seconds=env.get_float("PROTECTOR_INTERVAL_SECONDS", 1.0, 0.25, 60.0),
        news_loop_seconds=env.get_float("NEWS_LOOP_SECONDS", 20.0, 5.0, 600.0),
        execution_mode=env.get_choice("EXECUTION_MODE", "auto", EXECUTION_MODES),
        auto_start_bot=env.get_bool("AUTO_START_BOT", False),
        mt5=mt5_settings,
        paper=paper,
        risk=risk,
        strategy=strategy,
        news=news,
        telegram=telegram,
        app=app,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (reads ``.env`` next to this file once)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(BASE_DIR / ".env", override=False)
    return load_settings()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LOG_MARKER = "_trading_terminal_handler"


def setup_logging(settings: Settings) -> None:
    """Idempotent logging setup: rotating file + console, quiet HTTP libraries."""
    root = logging.getLogger()
    if any(getattr(h, _LOG_MARKER, False) for h in root.handlers):
        return
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        settings.app.log_dir / "terminal.log", maxBytes=5_000_000, backupCount=5,
        encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        setattr(handler, _LOG_MARKER, True)
        root.addHandler(handler)
    root.setLevel(settings.app.log_level)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
