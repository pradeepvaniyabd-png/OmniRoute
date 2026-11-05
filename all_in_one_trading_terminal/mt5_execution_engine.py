"""
mt5_execution_engine.py — Broker execution layer + automated position protector.

BrokerAdapter implementations
-----------------------------
* ``MT5Broker``  — official ``MetaTrader5`` package: reconnect with back-off, broker
  symbol resolution (suffixes/aliases), server-clock -> UTC conversion, spread filter,
  stop-level validation, ``order_check`` pre-flight, filling-mode fallback and requote
  retries, partial closes and closed-position P&L from deal history.
* ``PaperBroker`` — simulated account on public market data (Binance / Yahoo Finance):
  real bid/ask marking, SL/TP/pending-order triggering, margin, commission, persistent
  state.  Lets the full terminal run (and be forward-tested) without a live account.

Risk guards (pure decision logic, executed by the background worker)
--------------------------------------------------------------------
* ``PositionProtector``  — TP1 partial close, auto-breakeven at 1R, ATR / points /
  structure trailing stop after 1.5R (never loosens a stop, respects stop/freeze levels).
* ``DailyDrawdownGuard`` — realised + floating daily loss vs MAX_DAILY_LOSS_PERCENT.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from config import (
    NEW_YORK,
    PaperSettings,
    RiskSettings,
    Settings,
    canonical_symbol,
    paper_spec_for,
    pip_size_for,
    symbol_value,
)

try:  # pragma: no cover - only importable on Windows with the MT5 package installed
    import MetaTrader5 as mt5  # type: ignore[import-not-found]

    MT5_AVAILABLE = True
    MT5_IMPORT_ERROR: str | None = None
except Exception as _exc:  # noqa: BLE001 - any import failure means "not available"
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False
    MT5_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

log = logging.getLogger("execution")

UTC = timezone.utc
OHLCV = ["open", "high", "low", "close", "volume"]


class BrokerError(RuntimeError):
    """A broker call failed in a way the caller must not interpret as 'no data'."""


class BrokerUnavailableError(BrokerError):
    """The requested broker backend cannot be used on this machine."""


class MarketDataError(RuntimeError):
    """Public market data could not be fetched."""


# --------------------------------------------------------------------------- #
# Data transfer objects
# --------------------------------------------------------------------------- #
def volume_decimals(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def empty_rates() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV, dtype="float64",
                        index=pd.DatetimeIndex([], tz="UTC", name="time"))


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    freeze_level: int = 0
    filling_mode: int = 0
    currency_base: str = ""
    currency_profit: str = "USD"
    trade_allowed: bool = True
    description: str = ""

    def normalize_price(self, price: float) -> float:
        step = self.tick_size or self.point
        if step <= 0:
            return round(float(price), self.digits)
        return round(round(float(price) / step) * step, self.digits)

    def normalize_volume(self, volume: float) -> float:
        step = self.volume_step or 0.01
        lots = math.floor(float(volume) / step + 1e-9) * step
        return round(min(max(lots, 0.0), self.volume_max), volume_decimals(step))

    @property
    def pip_size(self) -> float:
        return pip_size_for(self.name, self.point, self.digits)


@dataclass(frozen=True)
class Tick:
    symbol: str
    time_utc: datetime
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def spread_points(self, point: float) -> float:
        return self.spread / point if point > 0 else 0.0


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    name: str
    currency: str
    balance: float
    equity: float
    profit: float
    margin: float
    free_margin: float
    margin_level: float
    leverage: int
    trade_allowed: bool
    company: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PositionInfo:
    ticket: int
    identifier: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float | None
    tp: float | None
    price_current: float
    profit: float
    swap: float
    time_utc: datetime
    magic: int
    comment: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["time_utc"] = self.time_utc.isoformat()
        return data


@dataclass(frozen=True)
class PendingOrderInfo:
    ticket: int
    symbol: str
    direction: str
    kind: str  # LIMIT | STOP | STOP_LIMIT
    volume: float
    price: float
    sl: float | None
    tp: float | None
    time_utc: datetime
    magic: int
    comment: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["time_utc"] = self.time_utc.isoformat()
        return data


@dataclass(frozen=True)
class OrderResult:
    success: bool
    retcode: int = 0
    message: str = ""
    order_ticket: int | None = None
    position_ticket: int | None = None
    price: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class ClosedPositionSummary:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    price_close: float
    close_time_utc: datetime
    profit: float
    reason: str  # TP | SL | STOP_OUT | MANUAL | EXPERT | CLOSED


# --------------------------------------------------------------------------- #
# Broker interface
# --------------------------------------------------------------------------- #
class BrokerAdapter(ABC):
    mode = "BASE"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_error: str | None = None

    # lifecycle ---------------------------------------------------------------
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    @abstractmethod
    def ensure_connected(self) -> bool: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def ping_ms(self) -> float | None: ...

    # market data -------------------------------------------------------------
    @abstractmethod
    def resolve_symbol(self, canonical: str) -> str | None: ...

    @abstractmethod
    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None: ...

    @abstractmethod
    def get_tick(self, symbol: str) -> Tick | None: ...

    @abstractmethod
    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...

    # account -----------------------------------------------------------------
    @abstractmethod
    def get_account(self) -> AccountInfo | None: ...

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[PositionInfo]: ...

    @abstractmethod
    def get_pending_orders(self, symbol: str | None = None) -> list[PendingOrderInfo]: ...

    # trading -----------------------------------------------------------------
    @abstractmethod
    def place_market_order(self, symbol: str, direction: str, volume: float,
                           sl: float | None = None, tp: float | None = None, comment: str = "",
                           max_spread_points: float | None = None) -> OrderResult: ...

    @abstractmethod
    def place_pending_order(self, symbol: str, direction: str, kind: str, price: float,
                            volume: float, sl: float | None = None, tp: float | None = None,
                            comment: str = "") -> OrderResult: ...

    @abstractmethod
    def modify_position(self, ticket: int, sl: float | None = None,
                        tp: float | None = None) -> OrderResult: ...

    @abstractmethod
    def close_position(self, ticket: int, volume: float | None = None,
                       comment: str = "") -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    def get_closed_position_summary(self, ticket: int) -> ClosedPositionSummary | None: ...

    @abstractmethod
    def calc_profit(self, symbol: str, direction: str, volume: float, price_open: float,
                    price_close: float) -> float | None: ...

    # shared helpers -------------------------------------------------------------
    def process_market(self) -> None:  # noqa: B027 - optional hook, intentionally a no-op
        """Hook for simulated brokers to trigger SL/TP/pending orders (no-op for MT5)."""

    def calc_loss_per_lot(self, symbol: str, direction: str, entry: float,
                          stop: float) -> float | None:
        value = self.calc_profit(symbol, direction, 1.0, entry, stop)
        return abs(value) if value is not None and value != 0 else None

    def close_all_positions(self, magic: int | None = None, comment: str = "close all",
                            symbol: str | None = None) -> list[tuple[int, OrderResult]]:
        results = []
        for position in self.get_positions(symbol):
            if magic is None or position.magic == magic:
                results.append((position.ticket, self.close_position(position.ticket, comment=comment)))
        return results

    def cancel_all_orders(self, magic: int | None = None,
                          symbol: str | None = None) -> list[tuple[int, OrderResult]]:
        results = []
        for order in self.get_pending_orders(symbol):
            if magic is None or order.magic == magic:
                results.append((order.ticket, self.cancel_order(order.ticket)))
        return results

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "connected": self.is_connected(), "ping_ms": self.ping_ms(),
                "last_error": self.last_error}

    @staticmethod
    def validate_stops(direction: str, sl: float | None, tp: float | None, bid: float, ask: float,
                       spec: SymbolSpec) -> str | None:
        """SL/TP must sit on the correct side of the closing price, beyond the stops level."""
        gap = spec.stops_level * spec.point
        if direction == "BUY":
            if sl and sl >= bid - gap:
                return f"SL {sl} must be below bid {bid} - stops level"
            if tp and tp <= bid + gap:
                return f"TP {tp} must be above bid {bid} + stops level"
        else:
            if sl and sl <= ask + gap:
                return f"SL {sl} must be above ask {ask} + stops level"
            if tp and tp >= ask - gap:
                return f"TP {tp} must be below ask {ask} - stops level"
        return None


# --------------------------------------------------------------------------- #
# MetaTrader 5
# --------------------------------------------------------------------------- #
def _mt5_const(name: str, default: int) -> int:
    return int(getattr(mt5, name, default)) if mt5 is not None else default


class MT5Broker(BrokerAdapter):
    """Live execution through the MetaTrader 5 terminal (Windows)."""

    mode = "MT5"
    OFFSET_KEY = "mt5_server_offset_seconds"
    ALIASES: dict[str, tuple[str, ...]] = {
        "XAUUSD": ("GOLD",), "XAGUSD": ("SILVER",), "BTCUSD": ("BITCOIN", "BTCUSDT"),
        "ETHUSD": ("ETHEREUM", "ETHUSDT"),
        "DXY": ("USDX", "DX", "USDIDX", "DOLLARINDEX", "USDINDEX"),
        "US30": ("DJ30", "WS30", "DOW30"), "NAS100": ("USTEC", "NDX100", "US100"),
    }

    def __init__(self, settings: Settings, db: Any = None):
        if not MT5_AVAILABLE:
            raise BrokerUnavailableError(
                f"MetaTrader5 package unavailable ({MT5_IMPORT_ERROR}). It ships Windows wheels "
                "only — run on Windows with the MT5 terminal installed, or set BROKER_MODE=paper.")
        super().__init__(settings)
        self.cfg = settings.mt5
        self.db = db
        self._lock = threading.RLock()
        self._connected = False
        self._next_retry = 0.0
        self._backoff = 1.0
        self._symbol_cache: dict[str, str] = {}
        self._spec_cache: dict[str, tuple[float, SymbolSpec]] = {}
        self._offset: int | None = None
        self._offset_checked = 0.0
        self._offset_detected = False
        self._tick_memory: dict[str, int] = {}
        self._timeframes = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        self.RET_DONE = _mt5_const("TRADE_RETCODE_DONE", 10009)
        self.RET_SUCCESS = {self.RET_DONE, _mt5_const("TRADE_RETCODE_DONE_PARTIAL", 10010),
                            _mt5_const("TRADE_RETCODE_PLACED", 10008)}
        self.RET_RETRY = {_mt5_const("TRADE_RETCODE_REQUOTE", 10004),
                          _mt5_const("TRADE_RETCODE_PRICE_CHANGED", 10020),
                          _mt5_const("TRADE_RETCODE_PRICE_OFF", 10021),
                          _mt5_const("TRADE_RETCODE_TIMEOUT", 10012),
                          _mt5_const("TRADE_RETCODE_TOO_MANY_REQUESTS", 10024),
                          _mt5_const("TRADE_RETCODE_CONNECTION", 10031)}
        self.RET_INVALID_FILL = _mt5_const("TRADE_RETCODE_INVALID_FILL", 10030)
        self.RET_NO_CHANGES = _mt5_const("TRADE_RETCODE_NO_CHANGES", 10025)

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> bool:
        with self._lock:
            kwargs: dict[str, Any] = {"timeout": self.cfg.timeout_ms}
            if self.cfg.portable:
                kwargs["portable"] = True
            if self.cfg.login:
                kwargs.update(login=int(self.cfg.login), password=self.cfg.password,
                              server=self.cfg.server)
            try:
                if self.cfg.terminal_path:
                    ok = mt5.initialize(self.cfg.terminal_path, **kwargs)
                else:
                    ok = mt5.initialize(**kwargs)
            except Exception as exc:  # noqa: BLE001 - C extension may raise anything
                ok = False
                self.last_error = f"initialize raised {type(exc).__name__}: {exc}"
            if not ok:
                self.last_error = self.last_error or f"initialize failed: {mt5.last_error()}"
                log.error("MT5 %s", self.last_error)
                self._connected = False
                return False
            account = mt5.account_info()
            if account is None:
                self.last_error = f"no trading account: {mt5.last_error()}"
                log.error("MT5 %s", self.last_error)
                return False
            if self.cfg.login and int(account.login) != int(self.cfg.login):
                if not mt5.login(int(self.cfg.login), password=self.cfg.password,
                                 server=self.cfg.server, timeout=self.cfg.timeout_ms):
                    self.last_error = f"login failed: {mt5.last_error()}"
                    log.error("MT5 %s", self.last_error)
                    return False
                account = mt5.account_info()
            terminal = mt5.terminal_info()
            if terminal is not None and not terminal.trade_allowed:
                log.warning("MT5 Algo Trading is DISABLED in the terminal — orders will be rejected "
                            "until you press the 'Algo Trading' button.")
            self._connected = True
            self._backoff = 1.0
            self.last_error = None
            for canonical in self.settings.symbols:
                self.resolve_symbol(canonical)
            if self.settings.strategy.dxy_filter:
                self.resolve_symbol(self.settings.strategy.dxy_symbol)
            self._refresh_offset()
            log.info("Connected to MT5 account %s @ %s (%s, leverage 1:%s)", account.login,
                     account.server, account.currency, account.leverage)
            return True

    def shutdown(self) -> None:
        with self._lock:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._connected = False

    def ensure_connected(self) -> bool:
        with self._lock:
            if self._connected:
                try:
                    terminal = mt5.terminal_info()
                    account = mt5.account_info() if terminal is not None else None
                except Exception:  # noqa: BLE001
                    terminal = account = None
                if terminal is not None and account is not None:
                    if not terminal.connected:
                        self.last_error = "MT5 terminal offline (no connection to trade server)"
                        return False
                    return True
                log.warning("Lost IPC link to the MT5 terminal — reconnecting")
                self._connected = False
            now = time.monotonic()
            if now < self._next_retry:
                return False
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
            ok = self.connect()
            if not ok:
                self._next_retry = now + self._backoff
                self._backoff = min(self._backoff * 2.0, 60.0)
            return ok

    def is_connected(self) -> bool:
        with self._lock:
            if not self._connected:
                return False
            terminal = mt5.terminal_info()
            return bool(terminal is not None and terminal.connected)

    def ping_ms(self) -> float | None:
        with self._lock:
            terminal = mt5.terminal_info() if self._connected else None
        return round(terminal.ping_last / 1000.0, 1) if terminal is not None else None

    # ---------------------------------------------------------- server time --
    @staticmethod
    def ny_close_offset(now: datetime | None = None) -> int:
        """Common broker convention: UTC+2 (US winter) / UTC+3 (US summer) = NY close at 00:00."""
        moment = now or datetime.now(UTC)
        ny = moment.astimezone(NEW_YORK).utcoffset() or timedelta(hours=-5)
        return int(ny.total_seconds()) + 7 * 3600

    def server_offset_seconds(self) -> int:
        with self._lock:
            interval = 600.0 if self._offset_detected else 60.0
            if self._offset is None or time.monotonic() - self._offset_checked > interval:
                self._refresh_offset()
            return self._offset if self._offset is not None else self.ny_close_offset()

    def _refresh_offset(self) -> None:
        mode = self.cfg.server_utc_offset
        self._offset_checked = time.monotonic()
        if mode not in ("auto", "ny_close"):
            self._offset = int(round(float(mode) * 3600))
            self._offset_detected = True
            return
        if mode == "ny_close":
            self._offset = self.ny_close_offset()
            self._offset_detected = True
            return
        detected = self._detect_offset()
        if detected is not None:
            if detected != self._offset:
                log.info("MT5 server clock detected at UTC%+.1fh", detected / 3600.0)
            self._offset = detected
            self._offset_detected = True
            if self.db is not None:
                self.db.kv_set(self.OFFSET_KEY, detected)
            return
        if self._offset is None:
            cached = self.db.kv_get(self.OFFSET_KEY) if self.db is not None else None
            self._offset = int(cached) if cached is not None else self.ny_close_offset()
            log.info("MT5 server clock assumed UTC%+.1fh (%s) until live ticks confirm it",
                     self._offset / 3600.0, "cached" if cached is not None else "NY-close rule")

    def _detect_offset(self) -> int | None:
        """Vote using symbols whose last tick advanced since the previous check (live markets)."""
        now = time.time()
        votes: dict[int, int] = {}
        for name in set(self._symbol_cache.values()):
            tick = mt5.symbol_info_tick(name)
            if tick is None or not tick.time:
                continue
            previous = self._tick_memory.get(name)
            self._tick_memory[name] = int(tick.time)
            if previous is None or int(tick.time) <= previous:
                continue
            diff = float(tick.time) - now
            rounded = int(round(diff / 1800.0) * 1800)
            if abs(diff - rounded) <= 90 and -12 * 3600 <= rounded <= 14 * 3600:
                votes[rounded] = votes.get(rounded, 0) + 1
        if not votes:
            return None
        return max(votes.items(), key=lambda item: item[1])[0]

    def _to_utc(self, server_epoch: float) -> datetime:
        return datetime.fromtimestamp(float(server_epoch) - self.server_offset_seconds(), UTC)

    # ------------------------------------------------------------ symbols --
    def resolve_symbol(self, canonical: str) -> str | None:
        key = canonical_symbol(canonical)
        with self._lock:
            if key in self._symbol_cache:
                return self._symbol_cache[key]
            suffix = self.cfg.symbol_suffix
            candidates: list[str] = []
            if self.cfg.symbol_map.get(key):
                candidates.append(self.cfg.symbol_map[key])
            if suffix:
                candidates.append(f"{key}{suffix}")
            candidates.append(key)
            for alias in self.ALIASES.get(key, ()):
                candidates += [alias, f"{alias}{suffix}"] if suffix else [alias]
            for name in dict.fromkeys(c for c in candidates if c):
                if mt5.symbol_info(name) is not None:
                    return self._remember_symbol(key, name)
            for base in (key, *self.ALIASES.get(key, ())):
                found = mt5.symbols_get(f"*{base}*") or ()
                names = sorted((s.name for s in found
                                if canonical_symbol(s.name).startswith(canonical_symbol(base))), key=len)
                if names:
                    return self._remember_symbol(key, names[0])
            log.warning("Symbol %s not found at the broker (set SYMBOL_MAP / SYMBOL_SUFFIX)", key)
            return None

    def _remember_symbol(self, key: str, name: str) -> str:
        if not mt5.symbol_select(name, True):
            log.warning("Could not add %s to Market Watch: %s", name, mt5.last_error())
        self._symbol_cache[key] = name
        if name != key:
            log.info("Resolved %s -> broker symbol %s", key, name)
        return name

    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        with self._lock:
            cached = self._spec_cache.get(symbol)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            info = mt5.symbol_info(symbol)
            if info is None:
                return cached[1] if cached else None
            disabled = _mt5_const("SYMBOL_TRADE_MODE_DISABLED", 0)
            spec = SymbolSpec(
                name=info.name, digits=int(info.digits), point=float(info.point),
                tick_size=float(info.trade_tick_size or info.point),
                tick_value=float(getattr(info, "trade_tick_value_loss", 0.0) or info.trade_tick_value or 0.0),
                contract_size=float(info.trade_contract_size or 1.0),
                volume_min=float(info.volume_min), volume_max=float(info.volume_max),
                volume_step=float(info.volume_step or 0.01),
                stops_level=int(info.trade_stops_level), freeze_level=int(info.trade_freeze_level),
                filling_mode=int(info.filling_mode), currency_base=str(info.currency_base),
                currency_profit=str(info.currency_profit),
                trade_allowed=int(info.trade_mode) != disabled, description=str(info.description))
            self._spec_cache[symbol] = (time.monotonic(), spec)
            return spec

    def get_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            tick = mt5.symbol_info_tick(symbol)
        if tick is None or (not tick.bid and not tick.ask):
            return None
        stamp = tick.time_msc / 1000.0 if getattr(tick, "time_msc", 0) else float(tick.time)
        return Tick(symbol, self._to_utc(stamp), float(tick.bid), float(tick.ask))

    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = self._timeframes.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        with self._lock:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, int(count))
        if rates is None or len(rates) == 0:
            self.last_error = f"no rates for {symbol} {timeframe}: {mt5.last_error()}"
            return empty_rates()
        frame = pd.DataFrame(rates)
        offset = self.server_offset_seconds()
        index = pd.DatetimeIndex(pd.to_datetime(frame["time"].astype("int64") - offset, unit="s",
                                                utc=True), name="time")
        return pd.DataFrame({
            "open": frame["open"].astype(float).to_numpy(),
            "high": frame["high"].astype(float).to_numpy(),
            "low": frame["low"].astype(float).to_numpy(),
            "close": frame["close"].astype(float).to_numpy(),
            "volume": frame["tick_volume"].astype(float).to_numpy(),
        }, index=index)

    # ------------------------------------------------------------ account --
    def get_account(self) -> AccountInfo | None:
        with self._lock:
            account = mt5.account_info()
            terminal = mt5.terminal_info()
        if account is None:
            return None
        allowed = bool(account.trade_allowed and account.trade_expert
                       and (terminal.trade_allowed if terminal is not None else True))
        return AccountInfo(login=int(account.login), server=str(account.server),
                           name=str(account.name), currency=str(account.currency),
                           balance=float(account.balance), equity=float(account.equity),
                           profit=float(account.profit), margin=float(account.margin),
                           free_margin=float(account.margin_free),
                           margin_level=float(account.margin_level or 0.0),
                           leverage=int(account.leverage), trade_allowed=allowed,
                           company=str(account.company))

    def get_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        with self._lock:
            rows = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if rows is None:
            raise BrokerError(f"positions_get failed: {mt5.last_error()}")
        buy = _mt5_const("POSITION_TYPE_BUY", 0)
        return [PositionInfo(
            ticket=int(p.ticket), identifier=int(p.identifier), symbol=str(p.symbol),
            direction="BUY" if int(p.type) == buy else "SELL", volume=float(p.volume),
            price_open=float(p.price_open), sl=float(p.sl) or None, tp=float(p.tp) or None,
            price_current=float(p.price_current), profit=float(p.profit), swap=float(p.swap),
            time_utc=self._to_utc(p.time), magic=int(p.magic), comment=str(p.comment))
            for p in rows]

    def get_pending_orders(self, symbol: str | None = None) -> list[PendingOrderInfo]:
        with self._lock:
            rows = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if rows is None:
            raise BrokerError(f"orders_get failed: {mt5.last_error()}")
        kinds = {
            _mt5_const("ORDER_TYPE_BUY_LIMIT", 2): ("BUY", "LIMIT"),
            _mt5_const("ORDER_TYPE_SELL_LIMIT", 3): ("SELL", "LIMIT"),
            _mt5_const("ORDER_TYPE_BUY_STOP", 4): ("BUY", "STOP"),
            _mt5_const("ORDER_TYPE_SELL_STOP", 5): ("SELL", "STOP"),
            _mt5_const("ORDER_TYPE_BUY_STOP_LIMIT", 6): ("BUY", "STOP_LIMIT"),
            _mt5_const("ORDER_TYPE_SELL_STOP_LIMIT", 7): ("SELL", "STOP_LIMIT"),
        }
        orders: list[PendingOrderInfo] = []
        for o in rows:
            direction, kind = kinds.get(int(o.type), ("BUY", "OTHER"))
            orders.append(PendingOrderInfo(
                ticket=int(o.ticket), symbol=str(o.symbol), direction=direction, kind=kind,
                volume=float(o.volume_current), price=float(o.price_open), sl=float(o.sl) or None,
                tp=float(o.tp) or None, time_utc=self._to_utc(o.time_setup), magic=int(o.magic),
                comment=str(o.comment)))
        return orders

    # ------------------------------------------------------------ trading --
    def _filling_modes(self, spec: SymbolSpec) -> list[int]:
        modes: list[int] = []
        if spec.filling_mode & 1:
            modes.append(_mt5_const("ORDER_FILLING_FOK", 0))
        if spec.filling_mode & 2:
            modes.append(_mt5_const("ORDER_FILLING_IOC", 1))
        modes.append(_mt5_const("ORDER_FILLING_RETURN", 2))
        return list(dict.fromkeys(modes))

    def _position_from_deal(self, deal_ticket: int) -> int | None:
        if not deal_ticket:
            return None
        deals = mt5.history_deals_get(ticket=deal_ticket)
        return int(deals[0].position_id) if deals else None

    def _send(self, request: dict[str, Any], spec: SymbolSpec, retries: int = 3) -> OrderResult:
        """order_check + order_send with filling-mode fallback and requote retries."""
        fillings = self._filling_modes(spec)
        fill_index = 0
        deal = request["action"] == mt5.TRADE_ACTION_DEAL
        buy_type = mt5.ORDER_TYPE_BUY
        last_message = ""
        for attempt in range(retries + len(fillings)):
            req = dict(request)
            req["type_filling"] = fillings[min(fill_index, len(fillings) - 1)]
            if deal:
                tick = mt5.symbol_info_tick(req["symbol"])
                if tick is None:
                    return OrderResult(False, -1, f"no tick for {req['symbol']}")
                req["price"] = float(tick.ask if req["type"] == buy_type else tick.bid)
            check = mt5.order_check(req)
            if check is None:
                return OrderResult(False, -1, f"order_check failed: {mt5.last_error()}")
            if check.retcode not in (0, self.RET_DONE):
                if check.retcode == self.RET_INVALID_FILL and fill_index < len(fillings) - 1:
                    fill_index += 1
                    continue
                return OrderResult(False, int(check.retcode), f"rejected by check: {check.comment}")
            result = mt5.order_send(req)
            if result is None:
                last_message = str(mt5.last_error())
                time.sleep(0.3)
                continue
            if result.retcode in self.RET_SUCCESS:
                position = self._position_from_deal(result.deal) if deal else None
                return OrderResult(True, int(result.retcode), str(result.comment),
                                   int(result.order) or None,
                                   position or (int(result.order) if deal and result.order else None),
                                   float(result.price or req.get("price") or 0.0) or None,
                                   float(result.volume or req["volume"]))
            last_message = f"{result.retcode} {result.comment}"
            if result.retcode == self.RET_INVALID_FILL and fill_index < len(fillings) - 1:
                fill_index += 1
                continue
            if result.retcode in self.RET_RETRY:
                time.sleep(0.25 * (attempt + 1))
                continue
            return OrderResult(False, int(result.retcode), str(result.comment))
        return OrderResult(False, -1, f"order failed after retries: {last_message}")

    def place_market_order(self, symbol: str, direction: str, volume: float,
                           sl: float | None = None, tp: float | None = None, comment: str = "",
                           max_spread_points: float | None = None) -> OrderResult:
        with self._lock:
            if not self.ensure_connected():
                return OrderResult(False, -1, self.last_error or "MT5 not connected")
            spec, tick = self.get_symbol_spec(symbol), self.get_tick(symbol)
            if spec is None or tick is None:
                return OrderResult(False, -1, f"{symbol}: specification or tick unavailable")
            if not spec.trade_allowed:
                return OrderResult(False, -1, f"{symbol}: trading disabled by broker")
            spread_pts = tick.spread_points(spec.point)
            if max_spread_points is not None and spread_pts > max_spread_points:
                return OrderResult(False, -1, f"spread {spread_pts:.0f} > max {max_spread_points:.0f} pts")
            lots = spec.normalize_volume(volume)
            if lots < spec.volume_min:
                return OrderResult(False, -1, f"volume {volume} below minimum {spec.volume_min}")
            sl_n = spec.normalize_price(sl) if sl else 0.0
            tp_n = spec.normalize_price(tp) if tp else 0.0
            problem = self.validate_stops(direction, sl_n or None, tp_n or None, tick.bid, tick.ask, spec)
            if problem:
                return OrderResult(False, -1, problem)
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots,
                "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
                "price": tick.ask if direction == "BUY" else tick.bid, "sl": sl_n, "tp": tp_n,
                "deviation": self.cfg.deviation_points, "magic": self.cfg.magic,
                "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
            }
            result = self._send(request, spec)
            log.info("MT5 market %s %s %.2f -> %s %s", direction, symbol, lots,
                     "OK" if result.success else "FAILED", result.message)
            return result

    def place_pending_order(self, symbol: str, direction: str, kind: str, price: float,
                            volume: float, sl: float | None = None, tp: float | None = None,
                            comment: str = "") -> OrderResult:
        with self._lock:
            if not self.ensure_connected():
                return OrderResult(False, -1, self.last_error or "MT5 not connected")
            spec, tick = self.get_symbol_spec(symbol), self.get_tick(symbol)
            if spec is None or tick is None:
                return OrderResult(False, -1, f"{symbol}: specification or tick unavailable")
            types = {("BUY", "LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT,
                     ("SELL", "LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
                     ("BUY", "STOP"): mt5.ORDER_TYPE_BUY_STOP,
                     ("SELL", "STOP"): mt5.ORDER_TYPE_SELL_STOP}
            order_type = types.get((direction, kind.upper()))
            if order_type is None:
                return OrderResult(False, -1, f"unsupported pending order {direction} {kind}")
            price_n = spec.normalize_price(price)
            gap = spec.stops_level * spec.point
            valid = {("BUY", "LIMIT"): price_n < tick.ask - gap,
                     ("SELL", "LIMIT"): price_n > tick.bid + gap,
                     ("BUY", "STOP"): price_n > tick.ask + gap,
                     ("SELL", "STOP"): price_n < tick.bid - gap}[(direction, kind.upper())]
            if not valid:
                return OrderResult(False, -1, f"{kind} price {price_n} invalid vs bid/ask "
                                              f"{tick.bid}/{tick.ask}")
            lots = spec.normalize_volume(volume)
            if lots < spec.volume_min:
                return OrderResult(False, -1, f"volume {volume} below minimum {spec.volume_min}")
            sl_n = spec.normalize_price(sl) if sl else 0.0
            tp_n = spec.normalize_price(tp) if tp else 0.0
            if direction == "BUY" and ((sl_n and sl_n >= price_n - gap) or (tp_n and tp_n <= price_n + gap)):
                return OrderResult(False, -1, "SL/TP on the wrong side of the order price")
            if direction == "SELL" and ((sl_n and sl_n <= price_n + gap) or (tp_n and tp_n >= price_n - gap)):
                return OrderResult(False, -1, "SL/TP on the wrong side of the order price")
            request = {
                "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": lots,
                "type": order_type, "price": price_n, "sl": sl_n, "tp": tp_n,
                "deviation": self.cfg.deviation_points, "magic": self.cfg.magic,
                "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
            }
            result = self._send(request, spec)
            log.info("MT5 pending %s %s %s %.2f @ %s -> %s %s", direction, kind, symbol, lots,
                     price_n, "OK" if result.success else "FAILED", result.message)
            return result

    def modify_position(self, ticket: int, sl: float | None = None,
                        tp: float | None = None) -> OrderResult:
        with self._lock:
            rows = mt5.positions_get(ticket=int(ticket))
            if not rows:
                return OrderResult(False, -1, f"position {ticket} not found")
            position = rows[0]
            spec, tick = self.get_symbol_spec(position.symbol), self.get_tick(position.symbol)
            if spec is None or tick is None:
                return OrderResult(False, -1, "specification or tick unavailable")
            new_sl = spec.normalize_price(sl) if sl is not None else float(position.sl)
            new_tp = spec.normalize_price(tp) if tp is not None else float(position.tp)
            if abs(new_sl - float(position.sl)) < spec.point / 2 and abs(new_tp - float(position.tp)) < spec.point / 2:
                return OrderResult(True, self.RET_NO_CHANGES, "no change", None, int(ticket))
            direction = "BUY" if int(position.type) == mt5.POSITION_TYPE_BUY else "SELL"
            problem = self.validate_stops(direction, new_sl or None, new_tp or None, tick.bid, tick.ask, spec)
            if problem:
                return OrderResult(False, -1, problem)
            freeze = spec.freeze_level * spec.point
            reference = tick.bid if direction == "BUY" else tick.ask
            if freeze > 0 and position.sl and abs(reference - float(position.sl)) <= freeze:
                return OrderResult(False, -1, "price inside the broker freeze level")
            request = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket),
                       "symbol": position.symbol, "sl": new_sl, "tp": new_tp,
                       "magic": int(position.magic)}
            result = mt5.order_send(request)
            if result is None:
                return OrderResult(False, -1, f"order_send failed: {mt5.last_error()}")
            if result.retcode in (self.RET_DONE, self.RET_NO_CHANGES):
                return OrderResult(True, int(result.retcode), str(result.comment), None, int(ticket))
            return OrderResult(False, int(result.retcode), str(result.comment))

    def close_position(self, ticket: int, volume: float | None = None,
                       comment: str = "") -> OrderResult:
        with self._lock:
            rows = mt5.positions_get(ticket=int(ticket))
            if not rows:
                return OrderResult(False, -1, f"position {ticket} not found")
            position = rows[0]
            spec = self.get_symbol_spec(position.symbol)
            if spec is None:
                return OrderResult(False, -1, "specification unavailable")
            lots = float(position.volume) if volume is None else spec.normalize_volume(
                min(float(volume), float(position.volume)))
            if lots <= 0:
                return OrderResult(False, -1, "nothing to close")
            buy = int(position.type) == mt5.POSITION_TYPE_BUY
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol, "volume": lots,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "position": int(ticket), "price": 0.0, "deviation": self.cfg.deviation_points,
                "magic": int(position.magic), "comment": (comment or "close")[:31],
                "type_time": mt5.ORDER_TIME_GTC,
            }
            result = self._send(request, spec)
            if result.success:
                return OrderResult(True, result.retcode, result.message, result.order_ticket,
                                   int(ticket), result.price, lots)
            return result

    def cancel_order(self, ticket: int) -> OrderResult:
        with self._lock:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        if result is None:
            return OrderResult(False, -1, f"order_send failed: {mt5.last_error()}")
        if result.retcode == self.RET_DONE:
            return OrderResult(True, int(result.retcode), "cancelled", int(ticket))
        return OrderResult(False, int(result.retcode), str(result.comment))

    def get_closed_position_summary(self, ticket: int) -> ClosedPositionSummary | None:
        with self._lock:
            if mt5.positions_get(ticket=int(ticket)):
                return None  # still open (partial closes also create OUT deals)
            deals = mt5.history_deals_get(position=int(ticket))
        if not deals:
            return None
        entry_in = _mt5_const("DEAL_ENTRY_IN", 0)
        outs = [d for d in deals if int(d.entry) in (_mt5_const("DEAL_ENTRY_OUT", 1),
                                                     _mt5_const("DEAL_ENTRY_INOUT", 2),
                                                     _mt5_const("DEAL_ENTRY_OUT_BY", 3))]
        if not outs:
            return None
        opening = next((d for d in deals if int(d.entry) == entry_in), None)
        last = max(outs, key=lambda d: d.time_msc)
        total = sum(float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0.0))
                    for d in deals)
        reasons = {_mt5_const("DEAL_REASON_SL", 4): "SL", _mt5_const("DEAL_REASON_TP", 5): "TP",
                   _mt5_const("DEAL_REASON_SO", 6): "STOP_OUT",
                   _mt5_const("DEAL_REASON_CLIENT", 0): "MANUAL",
                   _mt5_const("DEAL_REASON_MOBILE", 1): "MANUAL",
                   _mt5_const("DEAL_REASON_WEB", 2): "MANUAL",
                   _mt5_const("DEAL_REASON_EXPERT", 3): "EXPERT"}
        if opening is not None:
            direction = "BUY" if int(opening.type) == _mt5_const("DEAL_TYPE_BUY", 0) else "SELL"
        else:  # an OUT sell deal closes a buy position
            direction = "BUY" if int(last.type) == _mt5_const("DEAL_TYPE_SELL", 1) else "SELL"
        return ClosedPositionSummary(
            ticket=int(ticket), symbol=str(last.symbol), direction=direction,
            volume=float(opening.volume) if opening is not None else sum(float(d.volume) for d in outs),
            price_open=float(opening.price) if opening is not None else 0.0,
            price_close=float(last.price), close_time_utc=self._to_utc(last.time),
            profit=round(total, 2), reason=reasons.get(int(last.reason), "CLOSED"))

    def calc_profit(self, symbol: str, direction: str, volume: float, price_open: float,
                    price_close: float) -> float | None:
        with self._lock:
            value = mt5.order_calc_profit(
                mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL, symbol,
                float(volume), float(price_open), float(price_close))
        return float(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Public market data (paper trading)
# --------------------------------------------------------------------------- #
class PublicMarketDataFeed:
    """OHLCV + quotes from Binance spot and the Yahoo Finance chart API, with TTL caching."""

    BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
    YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
    BINANCE_INTERVALS = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h",
                         "H4": "4h", "D1": "1d"}
    YAHOO_INTERVALS = {"M1": ("1m", "5d"), "M5": ("5m", "10d"), "M15": ("15m", "30d"),
                       "M30": ("30m", "60d"), "H1": ("60m", "60d"), "H4": ("60m", "6mo"),
                       "D1": ("1d", "2y")}
    RATE_TTL = {"M1": 15.0, "M5": 20.0, "M15": 60.0, "M30": 90.0, "H1": 120.0, "H4": 300.0,
                "D1": 900.0}
    QUOTE_TTL = {"binance": 2.0, "yahoo": 20.0}

    def __init__(self, timeout: float = 10.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradingSuperTerminal/1.0",
                     "Accept": "application/json"})
        self._lock = threading.RLock()
        self._rates: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._quotes: dict[str, tuple[float, tuple[float, float, datetime]]] = {}
        self._yahoo_last: dict[str, tuple[float, float, datetime]] = {}
        self.last_error: str | None = None
        self.last_ok: float = 0.0
        self.last_latency_ms: float | None = None

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def parse_source(source: str) -> tuple[str, str]:
        provider, _, ticker = source.partition(":")
        provider = provider.strip().lower()
        if provider not in ("binance", "yahoo") or not ticker.strip():
            raise MarketDataError(f"invalid data source '{source}'")
        return provider, ticker.strip()

    def _get_json(self, hosts: tuple[str, ...], path: str, params: dict[str, Any]) -> Any:
        error: Exception | None = None
        for host in hosts:
            started = time.monotonic()
            try:
                response = self._client.get(host + path, params=params)
            except httpx.HTTPError as exc:
                error = exc
                continue
            self.last_latency_ms = round((time.monotonic() - started) * 1000.0, 1)
            if response.status_code == 200:
                self.last_ok = time.monotonic()
                return response.json()
            error = MarketDataError(f"HTTP {response.status_code} from {host}")
            if response.status_code in (418, 429):
                break
        raise MarketDataError(str(error) if error else "no host reachable")

    def get_rates(self, source: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = timeframe.upper()
        key = (source, tf)
        with self._lock:
            cached = self._rates.get(key)
            if cached and time.monotonic() - cached[0] < self.RATE_TTL.get(tf, 60.0):
                return cached[1].tail(count).copy()
        try:
            provider, ticker = self.parse_source(source)
            frame = (self._binance_klines(ticker, tf, count) if provider == "binance"
                     else self._yahoo_chart(ticker, tf))
        except (httpx.HTTPError, MarketDataError, ValueError, KeyError, TypeError, IndexError) as exc:
            self.last_error = f"{source} {tf}: {exc}"
            if cached:
                return cached[1].tail(count).copy()
            raise MarketDataError(self.last_error) from exc
        with self._lock:
            self._rates[key] = (time.monotonic(), frame)
        return frame.tail(count).copy()

    def get_quote(self, source: str) -> tuple[float, float, datetime]:
        """Raw (bid, ask, time) — Yahoo provides a single last price (bid == ask)."""
        provider, ticker = self.parse_source(source)
        with self._lock:
            cached = self._quotes.get(source)
            if cached and time.monotonic() - cached[0] < self.QUOTE_TTL[provider]:
                return cached[1]
        try:
            if provider == "binance":
                data = self._get_json(self.BINANCE_HOSTS, "/api/v3/ticker/bookTicker",
                                      {"symbol": ticker})
                quote_value = (float(data["bidPrice"]), float(data["askPrice"]), datetime.now(UTC))
            else:
                recent = self._yahoo_last.get(ticker)
                if recent is None or (datetime.now(UTC) - recent[2]).total_seconds() > self.QUOTE_TTL["yahoo"]:
                    self._yahoo_chart(ticker, "M1")
                    recent = self._yahoo_last.get(ticker)
                if recent is None:
                    raise MarketDataError(f"no price for {ticker}")
                quote_value = recent
        except (httpx.HTTPError, MarketDataError, ValueError, KeyError, TypeError) as exc:
            self.last_error = f"{source} quote: {exc}"
            if cached:
                return cached[1]
            raise MarketDataError(self.last_error) from exc
        with self._lock:
            self._quotes[source] = (time.monotonic(), quote_value)
        return quote_value

    def _binance_klines(self, pair: str, timeframe: str, count: int) -> pd.DataFrame:
        interval = self.BINANCE_INTERVALS[timeframe]
        data = self._get_json(self.BINANCE_HOSTS, "/api/v3/klines",
                              {"symbol": pair, "interval": interval, "limit": min(1000, max(count, 10))})
        if not isinstance(data, list) or not data:
            raise MarketDataError(f"empty klines for {pair}")
        index = pd.DatetimeIndex(pd.to_datetime([int(row[0]) for row in data], unit="ms", utc=True),
                                 name="time")
        return pd.DataFrame({"open": [float(r[1]) for r in data], "high": [float(r[2]) for r in data],
                             "low": [float(r[3]) for r in data], "close": [float(r[4]) for r in data],
                             "volume": [float(r[5]) for r in data]}, index=index)

    def _yahoo_chart(self, ticker: str, timeframe: str) -> pd.DataFrame:
        interval, span = self.YAHOO_INTERVALS[timeframe]
        data = self._get_json(self.YAHOO_HOSTS, f"/v8/finance/chart/{quote(ticker, safe='=.-^')}",
                              {"interval": interval, "range": span, "includePrePost": "false"})
        chart = data.get("chart") or {}
        if chart.get("error"):
            raise MarketDataError(str(chart["error"]))
        result = (chart.get("result") or [None])[0]
        if not result:
            raise MarketDataError(f"no chart result for {ticker}")
        stamps = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        frame = pd.DataFrame({
            "open": pd.to_numeric(pd.Series(quotes.get("open") or [None] * len(stamps)), errors="coerce").to_numpy(),
            "high": pd.to_numeric(pd.Series(quotes.get("high") or [None] * len(stamps)), errors="coerce").to_numpy(),
            "low": pd.to_numeric(pd.Series(quotes.get("low") or [None] * len(stamps)), errors="coerce").to_numpy(),
            "close": pd.to_numeric(pd.Series(quotes.get("close") or [None] * len(stamps)), errors="coerce").to_numpy(),
            "volume": pd.to_numeric(pd.Series(quotes.get("volume") or [0] * len(stamps)), errors="coerce").to_numpy(),
        }, index=pd.DatetimeIndex(pd.to_datetime(stamps, unit="s", utc=True), name="time"))
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = frame["volume"].fillna(0.0)
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price:
            self._yahoo_last[ticker] = (float(price), float(price), datetime.now(UTC))
        elif not frame.empty:
            last = float(frame["close"].iloc[-1])
            self._yahoo_last[ticker] = (last, last, datetime.now(UTC))
        if timeframe == "H4" and not frame.empty:
            frame = frame.resample("4h", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna(subset=["open"])
        return frame.astype("float64")


# --------------------------------------------------------------------------- #
# Paper broker
# --------------------------------------------------------------------------- #
class PaperBroker(BrokerAdapter):
    """Simulated account with real-time marking on public market data."""

    mode = "PAPER"
    STATE_KEY = "paper_broker_state_v1"
    HISTORY_LIMIT = 500

    def __init__(self, settings: Settings, db: Any = None, feed: PublicMarketDataFeed | None = None):
        super().__init__(settings)
        self.cfg: PaperSettings = settings.paper
        self.db = db
        self.feed = feed or PublicMarketDataFeed(self.cfg.http_timeout)
        self._lock = threading.RLock()
        state = (db.kv_get(self.STATE_KEY) if db is not None else None) or {}
        self._balance = float(state.get("balance", self.cfg.starting_balance))
        self._positions: dict[int, dict[str, Any]] = {int(k): v for k, v in (state.get("positions") or {}).items()}
        self._orders: dict[int, dict[str, Any]] = {int(k): v for k, v in (state.get("orders") or {}).items()}
        self._history: dict[int, dict[str, Any]] = {int(k): v for k, v in (state.get("history") or {}).items()}
        self._next_ticket = int(state.get("next_ticket", 100001))
        self._connected = False
        self._feed_ok_at = 0.0

    # ------------------------------------------------------------- state --
    def _save(self) -> None:
        if self.db is None:
            return
        if len(self._history) > self.HISTORY_LIMIT:
            for ticket in sorted(self._history)[:-self.HISTORY_LIMIT]:
                self._history.pop(ticket, None)
        self.db.kv_set(self.STATE_KEY, {
            "balance": round(self._balance, 2), "next_ticket": self._next_ticket,
            "positions": {str(k): v for k, v in self._positions.items()},
            "orders": {str(k): v for k, v in self._orders.items()},
            "history": {str(k): v for k, v in self._history.items()}})

    def reset_account(self, balance: float | None = None) -> None:
        with self._lock:
            self._balance = float(balance or self.cfg.starting_balance)
            self._positions.clear()
            self._orders.clear()
            self._history.clear()
            self._save()
        log.warning("Paper account reset to %.2f", self._balance)

    def _new_ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _source(self, symbol: str) -> str:
        key = canonical_symbol(symbol)
        source = self.cfg.data_sources.get(key)
        if not source:
            raise MarketDataError(f"no PAPER_DATA_SOURCES entry for {key}")
        return source

    # --------------------------------------------------------- lifecycle --
    def connect(self) -> bool:
        self._connected = True
        ok = self.ensure_connected()
        log.info("Paper broker ready — balance %.2f %s, %s open positions (data feed %s)",
                 self._balance, self.cfg.account_currency, len(self._positions),
                 "online" if ok else "OFFLINE")
        return True

    def shutdown(self) -> None:
        self._connected = False
        self.feed.close()

    def ensure_connected(self) -> bool:
        if time.monotonic() - self._feed_ok_at < 60.0:
            return True
        for symbol in self.settings.symbols:
            if self.get_tick(symbol) is not None:
                return True
        return False

    def is_connected(self) -> bool:
        return self._connected and time.monotonic() - self._feed_ok_at < 120.0

    def ping_ms(self) -> float | None:
        return self.feed.last_latency_ms

    # ------------------------------------------------------- market data --
    def resolve_symbol(self, canonical: str) -> str | None:
        key = canonical_symbol(canonical)
        return key if key in self.cfg.data_sources else None

    def _conversion_to_usd(self, base: str, quote_ccy: str, price: float | None) -> float:
        if quote_ccy == "USD":
            return 1.0
        if base == "USD" and price:
            return 1.0 / price
        for source in (self.cfg.data_sources.get(f"{quote_ccy}USD"), f"yahoo:{quote_ccy}USD=X"):
            if not source:
                continue
            try:
                bid, ask, _ = self.feed.get_quote(source)
                return (bid + ask) / 2.0
            except MarketDataError:
                continue
        for source in (self.cfg.data_sources.get(f"USD{quote_ccy}"), f"yahoo:{quote_ccy}=X"):
            if not source:
                continue
            try:
                bid, ask, _ = self.feed.get_quote(source)
                mid = (bid + ask) / 2.0
                if mid > 0:
                    return 1.0 / mid
            except MarketDataError:
                continue
        raise MarketDataError(f"no USD conversion rate for {quote_ccy}")

    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        key = canonical_symbol(symbol)
        base = paper_spec_for(key)
        digits = int(base["digits"])
        point = 10.0 ** -digits
        try:
            tick = self.get_tick(key)
            conversion = self._conversion_to_usd(base["base"], base["quote"], tick.mid if tick else None)
        except MarketDataError:
            conversion = 1.0 if base["quote"] == "USD" else 0.0
        return SymbolSpec(
            name=key, digits=digits, point=point, tick_size=point,
            tick_value=point * float(base["contract_size"]) * conversion,
            contract_size=float(base["contract_size"]), volume_min=float(base["volume_min"]),
            volume_max=float(base["volume_max"]), volume_step=float(base["volume_step"]),
            currency_base=str(base["base"]), currency_profit=str(base["quote"]),
            trade_allowed=bool(base.get("tradeable", True)), description="Paper / simulated")

    def get_tick(self, symbol: str) -> Tick | None:
        key = canonical_symbol(symbol)
        try:
            bid, ask, stamp = self.feed.get_quote(self._source(key))
        except MarketDataError as exc:
            self.last_error = str(exc)
            return None
        self._feed_ok_at = time.monotonic()
        base = paper_spec_for(key)
        digits = int(base["digits"])
        point = 10.0 ** -digits
        spread_pts = float(self.cfg.spread_points.get(key, base["spread_points"]))
        mid = (bid + ask) / 2.0
        half = max(spread_pts * point, ask - bid) / 2.0
        return Tick(key, stamp, round(mid - half, digits), round(mid + half, digits))

    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        try:
            frame = self.feed.get_rates(self._source(symbol), timeframe, count)
        except MarketDataError as exc:
            self.last_error = str(exc)
            return empty_rates()
        self._feed_ok_at = time.monotonic()
        return frame

    # ------------------------------------------------------------ maths --
    def calc_profit(self, symbol: str, direction: str, volume: float, price_open: float,
                    price_close: float) -> float | None:
        base = paper_spec_for(symbol)
        diff = (float(price_close) - float(price_open)) * (1.0 if direction == "BUY" else -1.0)
        try:
            conversion = self._conversion_to_usd(base["base"], base["quote"], float(price_close))
        except MarketDataError:
            return None
        return diff * float(base["contract_size"]) * float(volume) * conversion

    def _margin(self, symbol: str, volume: float, price: float) -> float:
        base = paper_spec_for(symbol)
        try:
            conversion = self._conversion_to_usd(base["base"], base["quote"], price)
        except MarketDataError:
            conversion = 1.0
        return price * float(base["contract_size"]) * volume * conversion / self.cfg.leverage

    # ----------------------------------------------------------- account --
    def get_account(self) -> AccountInfo | None:
        with self._lock:
            self.process_market()
            floating = sum(float(p.get("profit", 0.0)) for p in self._positions.values())
            margin = sum(float(p.get("margin", 0.0)) for p in self._positions.values())
            equity = self._balance + floating
            return AccountInfo(login=0, server="PAPER", name="Paper Account",
                               currency=self.cfg.account_currency, balance=round(self._balance, 2),
                               equity=round(equity, 2), profit=round(floating, 2),
                               margin=round(margin, 2), free_margin=round(equity - margin, 2),
                               margin_level=round(equity / margin * 100.0, 2) if margin else 0.0,
                               leverage=int(self.cfg.leverage), trade_allowed=True,
                               company="Simulated")

    def get_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        with self._lock:
            self.process_market()
            key = canonical_symbol(symbol) if symbol else None
            return [self._to_position(p) for p in self._positions.values()
                    if key is None or p["symbol"] == key]

    def get_pending_orders(self, symbol: str | None = None) -> list[PendingOrderInfo]:
        with self._lock:
            key = canonical_symbol(symbol) if symbol else None
            return [PendingOrderInfo(ticket=int(o["ticket"]), symbol=o["symbol"],
                                     direction=o["direction"], kind=o["kind"],
                                     volume=float(o["volume"]), price=float(o["price"]),
                                     sl=o.get("sl"), tp=o.get("tp"),
                                     time_utc=datetime.fromisoformat(o["time"]),
                                     magic=int(o["magic"]), comment=o.get("comment", ""))
                    for o in self._orders.values() if key is None or o["symbol"] == key]

    @staticmethod
    def _to_position(p: dict[str, Any]) -> PositionInfo:
        return PositionInfo(ticket=int(p["ticket"]), identifier=int(p["ticket"]), symbol=p["symbol"],
                            direction=p["direction"], volume=float(p["volume"]),
                            price_open=float(p["price_open"]), sl=p.get("sl"), tp=p.get("tp"),
                            price_current=float(p.get("price_current", p["price_open"])),
                            profit=round(float(p.get("profit", 0.0)), 2), swap=0.0,
                            time_utc=datetime.fromisoformat(p["time"]), magic=int(p["magic"]),
                            comment=p.get("comment", ""))

    # ------------------------------------------------------- simulation --
    def process_market(self) -> None:
        """Mark positions to market and trigger SL / TP / pending orders."""
        with self._lock:
            symbols = {p["symbol"] for p in self._positions.values()} | {o["symbol"] for o in self._orders.values()}
            if not symbols:
                return
            ticks = {symbol: self.get_tick(symbol) for symbol in symbols}
            changed = False
            for ticket, order in list(self._orders.items()):
                tick = ticks.get(order["symbol"])
                if tick is None:
                    continue
                fill = self._order_fill_price(order, tick)
                if fill is None:
                    continue
                margin = self._margin(order["symbol"], float(order["volume"]), fill)
                equity = self._balance + sum(float(p.get("profit", 0.0)) for p in self._positions.values())
                used = sum(float(p.get("margin", 0.0)) for p in self._positions.values())
                del self._orders[ticket]
                changed = True
                if margin > equity - used:
                    log.warning("Paper order %s cancelled at trigger: insufficient margin", ticket)
                    continue
                self._open_position(order["symbol"], order["direction"], float(order["volume"]), fill,
                                    order.get("sl"), order.get("tp"), order.get("comment", ""),
                                    int(order["magic"]), ticket=ticket)
                log.info("Paper %s %s order %s filled @ %s", order["direction"], order["kind"], ticket, fill)
            for ticket, position in list(self._positions.items()):
                tick = ticks.get(position["symbol"])
                if tick is None:
                    continue
                buy = position["direction"] == "BUY"
                price = tick.bid if buy else tick.ask
                profit = self.calc_profit(position["symbol"], position["direction"],
                                          float(position["volume"]), float(position["price_open"]), price)
                position["price_current"] = price
                if profit is not None:
                    position["profit"] = profit
                sl, tp = position.get("sl"), position.get("tp")
                spread = tick.spread
                if sl and ((buy and tick.bid <= sl) or (not buy and tick.ask >= sl)):
                    near = abs(price - sl) <= max(spread, 1e-12)
                    self._close(ticket, sl if near else price, "SL")
                    changed = True
                elif tp and ((buy and tick.bid >= tp) or (not buy and tick.ask <= tp)):
                    self._close(ticket, tp, "TP")
                    changed = True
            if changed:
                self._save()

    @staticmethod
    def _order_fill_price(order: dict[str, Any], tick: Tick) -> float | None:
        price = float(order["price"])
        direction, kind = order["direction"], order["kind"]
        if direction == "BUY" and kind == "LIMIT" and tick.ask <= price:
            return min(price, tick.ask)
        if direction == "SELL" and kind == "LIMIT" and tick.bid >= price:
            return max(price, tick.bid)
        if direction == "BUY" and kind == "STOP" and tick.ask >= price:
            return max(price, tick.ask)
        if direction == "SELL" and kind == "STOP" and tick.bid <= price:
            return min(price, tick.bid)
        return None

    def _open_position(self, symbol: str, direction: str, volume: float, price: float,
                       sl: float | None, tp: float | None, comment: str, magic: int,
                       ticket: int | None = None) -> dict[str, Any]:
        ticket = ticket or self._new_ticket()
        commission = self.cfg.commission_per_lot * volume
        self._balance -= commission
        position = {"ticket": ticket, "symbol": symbol, "direction": direction, "volume": volume,
                    "initial_volume": volume, "price_open": price, "sl": sl, "tp": tp,
                    "price_current": price, "profit": 0.0, "commission": commission,
                    "realized": 0.0, "time": datetime.now(UTC).isoformat(), "magic": magic,
                    "comment": comment, "margin": self._margin(symbol, volume, price)}
        self._positions[ticket] = position
        return position

    def _close(self, ticket: int, price: float, reason: str, volume: float | None = None) -> OrderResult:
        position = self._positions.get(ticket)
        if position is None:
            return OrderResult(False, -1, f"position {ticket} not found")
        spec = self.get_symbol_spec(position["symbol"])
        lots = float(position["volume"]) if volume is None else min(float(volume), float(position["volume"]))
        if spec is not None:
            lots = spec.normalize_volume(lots) if volume is not None else lots
        if lots <= 0:
            return OrderResult(False, -1, "nothing to close")
        profit = self.calc_profit(position["symbol"], position["direction"], lots,
                                  float(position["price_open"]), price) or 0.0
        self._balance += profit
        position["realized"] = float(position.get("realized", 0.0)) + profit
        remaining = round(float(position["volume"]) - lots, 8)
        if remaining <= 1e-9:
            del self._positions[ticket]
            self._history[ticket] = {
                "ticket": ticket, "symbol": position["symbol"], "direction": position["direction"],
                "volume": float(position.get("initial_volume", lots)),
                "price_open": float(position["price_open"]), "price_close": float(price),
                "close_time": datetime.now(UTC).isoformat(),
                "profit": round(position["realized"] - float(position.get("commission", 0.0)), 2),
                "reason": reason}
        else:
            position["volume"] = remaining
            position["margin"] = self._margin(position["symbol"], remaining, float(position["price_open"]))
        log.info("Paper close %s %s %.2f @ %s (%s) P&L %.2f", position["symbol"], ticket, lots,
                 price, reason, profit)
        return OrderResult(True, 10009, f"closed {lots} @ {price}", ticket, ticket, price, lots)

    # ------------------------------------------------------------ trading --
    def place_market_order(self, symbol: str, direction: str, volume: float,
                           sl: float | None = None, tp: float | None = None, comment: str = "",
                           max_spread_points: float | None = None) -> OrderResult:
        with self._lock:
            key = canonical_symbol(symbol)
            spec, tick = self.get_symbol_spec(key), self.get_tick(key)
            if spec is None or tick is None:
                return OrderResult(False, -1, f"{key}: no market data ({self.last_error})")
            if not spec.trade_allowed:
                return OrderResult(False, -1, f"{key} is not tradeable in paper mode")
            spread_pts = tick.spread_points(spec.point)
            if max_spread_points is not None and spread_pts > max_spread_points:
                return OrderResult(False, -1, f"spread {spread_pts:.0f} > max {max_spread_points:.0f} pts")
            lots = spec.normalize_volume(volume)
            if lots < spec.volume_min:
                return OrderResult(False, -1, f"volume {volume} below minimum {spec.volume_min}")
            sl_n = spec.normalize_price(sl) if sl else None
            tp_n = spec.normalize_price(tp) if tp else None
            problem = self.validate_stops(direction, sl_n, tp_n, tick.bid, tick.ask, spec)
            if problem:
                return OrderResult(False, -1, problem)
            price = tick.ask if direction == "BUY" else tick.bid
            account = self.get_account()
            margin = self._margin(key, lots, price)
            if account is not None and margin > account.free_margin:
                return OrderResult(False, 10019, f"insufficient margin ({margin:.2f} > {account.free_margin:.2f})")
            position = self._open_position(key, direction, lots, price, sl_n, tp_n, comment[:31],
                                           self.settings.mt5.magic)
            self._save()
            log.info("Paper market %s %s %.2f @ %s (SL %s TP %s)", direction, key, lots, price, sl_n, tp_n)
            return OrderResult(True, 10009, "filled", position["ticket"], position["ticket"], price, lots)

    def place_pending_order(self, symbol: str, direction: str, kind: str, price: float,
                            volume: float, sl: float | None = None, tp: float | None = None,
                            comment: str = "") -> OrderResult:
        with self._lock:
            key = canonical_symbol(symbol)
            spec, tick = self.get_symbol_spec(key), self.get_tick(key)
            if spec is None or tick is None:
                return OrderResult(False, -1, f"{key}: no market data ({self.last_error})")
            kind = kind.upper()
            price_n = spec.normalize_price(price)
            valid = {("BUY", "LIMIT"): price_n < tick.ask, ("SELL", "LIMIT"): price_n > tick.bid,
                     ("BUY", "STOP"): price_n > tick.ask, ("SELL", "STOP"): price_n < tick.bid}.get((direction, kind))
            if not valid:
                return OrderResult(False, -1, f"{kind} price {price_n} invalid vs {tick.bid}/{tick.ask}")
            lots = spec.normalize_volume(volume)
            if lots < spec.volume_min:
                return OrderResult(False, -1, f"volume {volume} below minimum {spec.volume_min}")
            sl_n = spec.normalize_price(sl) if sl else None
            tp_n = spec.normalize_price(tp) if tp else None
            sign = 1 if direction == "BUY" else -1
            if (sl_n and (sl_n - price_n) * sign >= 0) or (tp_n and (tp_n - price_n) * sign <= 0):
                return OrderResult(False, -1, "SL/TP on the wrong side of the order price")
            ticket = self._new_ticket()
            self._orders[ticket] = {"ticket": ticket, "symbol": key, "direction": direction,
                                    "kind": kind, "volume": lots, "price": price_n, "sl": sl_n,
                                    "tp": tp_n, "time": datetime.now(UTC).isoformat(),
                                    "magic": self.settings.mt5.magic, "comment": comment[:31]}
            self._save()
            log.info("Paper pending %s %s %s %.2f @ %s", direction, kind, key, lots, price_n)
            return OrderResult(True, 10008, "placed", ticket, None, price_n, lots)

    def modify_position(self, ticket: int, sl: float | None = None,
                        tp: float | None = None) -> OrderResult:
        with self._lock:
            position = self._positions.get(int(ticket))
            if position is None:
                return OrderResult(False, -1, f"position {ticket} not found")
            spec, tick = self.get_symbol_spec(position["symbol"]), self.get_tick(position["symbol"])
            if spec is None or tick is None:
                return OrderResult(False, -1, "no market data")
            new_sl = spec.normalize_price(sl) if sl is not None else position.get("sl")
            new_tp = spec.normalize_price(tp) if tp is not None else position.get("tp")
            problem = self.validate_stops(position["direction"], new_sl, new_tp, tick.bid, tick.ask, spec)
            if problem:
                return OrderResult(False, -1, problem)
            position["sl"], position["tp"] = new_sl, new_tp
            self._save()
            return OrderResult(True, 10009, "modified", None, int(ticket))

    def close_position(self, ticket: int, volume: float | None = None,
                       comment: str = "") -> OrderResult:
        with self._lock:
            position = self._positions.get(int(ticket))
            if position is None:
                return OrderResult(False, -1, f"position {ticket} not found")
            tick = self.get_tick(position["symbol"])
            if tick is None:
                return OrderResult(False, -1, f"no market data ({self.last_error})")
            price = tick.bid if position["direction"] == "BUY" else tick.ask
            result = self._close(int(ticket), price, "EXPERT", volume)
            self._save()
            return result

    def cancel_order(self, ticket: int) -> OrderResult:
        with self._lock:
            if self._orders.pop(int(ticket), None) is None:
                return OrderResult(False, -1, f"order {ticket} not found")
            self._save()
            return OrderResult(True, 10009, "cancelled", int(ticket))

    def get_closed_position_summary(self, ticket: int) -> ClosedPositionSummary | None:
        with self._lock:
            record = self._history.get(int(ticket))
        if record is None:
            return None
        return ClosedPositionSummary(
            ticket=int(ticket), symbol=record["symbol"], direction=record["direction"],
            volume=float(record["volume"]), price_open=float(record["price_open"]),
            price_close=float(record["price_close"]),
            close_time_utc=datetime.fromisoformat(record["close_time"]),
            profit=float(record["profit"]), reason=record.get("reason", "CLOSED"))


def create_broker(settings: Settings, db: Any = None) -> BrokerAdapter:
    if settings.broker_mode == "mt5":
        return MT5Broker(settings, db)
    return PaperBroker(settings, db)


# --------------------------------------------------------------------------- #
# Automated position protector
# --------------------------------------------------------------------------- #
@dataclass
class ManagedTradeState:
    ticket: int
    symbol: str
    direction: str
    entry: float
    initial_sl: float | None
    tp1: float | None
    tp2: float | None
    trade_id: int | None = None
    be_moved: bool = False
    tp1_hit: bool = False
    trailing_active: bool = False
    news_actions: set[str] = field(default_factory=set)


@dataclass
class ProtectionDecision:
    new_sl: float | None = None
    partial_close_volume: float | None = None
    events: list[str] = field(default_factory=list)
    mark_be: bool = False
    mark_trailing: bool = False
    mark_tp1: bool = False
    progress_r: float = 0.0


class PositionProtector:
    """TP1 partials, auto-breakeven and trailing stops — stops only ever tighten."""

    SL_EVENTS = ("BREAKEVEN", "TRAILING_ON", "TRAIL_UPDATE")

    def __init__(self, risk: RiskSettings):
        self.risk = risk

    def breakeven_buffer(self, spec: SymbolSpec, tick: Tick) -> float:
        points = symbol_value(self.risk.breakeven_buffer_points, spec.name, 0.0)
        return max(points * spec.point, tick.spread * 1.2)

    def trailing_distance(self, spec: SymbolSpec, atr: float | None) -> float | None:
        if self.risk.trailing_mode == "atr":
            return atr * self.risk.trailing_atr_mult if atr and atr > 0 else None
        if self.risk.trailing_mode == "points":
            points = symbol_value(self.risk.trailing_points, spec.name, 0.0)
            return points * spec.point if points > 0 else None
        return None

    def evaluate(self, state: ManagedTradeState, position: PositionInfo, tick: Tick,
                 spec: SymbolSpec, atr: float | None = None,
                 structure_level: float | None = None) -> ProtectionDecision:
        decision = ProtectionDecision()
        if not state.initial_sl or state.initial_sl <= 0:
            return decision
        risk_distance = abs(state.entry - state.initial_sl)
        if risk_distance <= 0:
            return decision
        sign = 1.0 if state.direction == "BUY" else -1.0
        price = tick.bid if state.direction == "BUY" else tick.ask
        progress = (price - state.entry) * sign / risk_distance
        decision.progress_r = progress
        current_sl = position.sl
        best = current_sl
        pending: list[str] = []

        def tighter(candidate: float, reference: float | None, min_step: float = 0.0) -> bool:
            return reference is None or (candidate - reference) * sign > min_step

        # TP1: book partial profits, then protect the runner
        if state.tp1 and not state.tp1_hit and (price - state.tp1) * sign >= 0:
            decision.events.append("TP1_HIT")
            decision.mark_tp1 = True
            fraction = self.risk.tp1_close_percent / 100.0
            if fraction > 0:
                lots = spec.normalize_volume(position.volume * fraction)
                if lots >= spec.volume_min and position.volume - lots >= spec.volume_min - 1e-9:
                    decision.partial_close_volume = lots

        # Auto-breakeven at BREAKEVEN_TRIGGER_R (or at TP1)
        if not state.be_moved and (progress >= self.risk.breakeven_trigger_r or decision.mark_tp1):
            be_level = state.entry + sign * self.breakeven_buffer(spec, tick)
            if current_sl is not None and not tighter(be_level, current_sl):
                decision.mark_be = True  # stop already at/through breakeven
            elif tighter(be_level, best):
                best = be_level
                pending.append("BREAKEVEN")

        # Dynamic trailing stop after TRAILING_START_R
        if progress >= self.risk.trailing_start_r:
            if self.risk.trailing_mode == "structure":
                candidate = structure_level
            else:
                distance = self.trailing_distance(spec, atr)
                candidate = price - sign * distance if distance else None
            if candidate is not None and (candidate - state.entry) * sign > 0:
                step = (atr or 0.0) * self.risk.trailing_min_step_atr
                if tighter(candidate, best, step if best is not None else 0.0):
                    best = candidate
                    pending = [e for e in pending if e != "BREAKEVEN"]
                    pending.append("TRAIL_UPDATE" if state.trailing_active else "TRAILING_ON")

        if pending and best is not None:
            gap = max(spec.stops_level, spec.freeze_level) * spec.point
            limit = price - sign * max(gap, spec.point)
            if (best - limit) * sign > 0:
                best = limit
            if tighter(best, current_sl) and (current_sl is None or abs(best - current_sl) >= spec.point / 2):
                decision.new_sl = spec.normalize_price(best)
                decision.events.extend(pending)
                if "BREAKEVEN" in pending or (best - state.entry) * sign > 0:
                    decision.mark_be = True
                if any(e in ("TRAILING_ON", "TRAIL_UPDATE") for e in pending):
                    decision.mark_trailing = True
        return decision


# --------------------------------------------------------------------------- #
# Daily drawdown circuit breaker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DrawdownStatus:
    trading_day: str
    start_equity: float
    start_balance: float
    reference: float
    equity: float
    drawdown_amount: float
    drawdown_pct: float
    limit_pct: float
    breached: bool
    halted: bool
    halt_reason: str | None

    @property
    def utilization(self) -> float:
        return min(1.0, self.drawdown_pct / self.limit_pct) if self.limit_pct > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["utilization"] = self.utilization
        return data


class DailyDrawdownGuard:
    """Tracks start-of-day equity and the realised + floating loss against the daily limit."""

    def __init__(self, risk: RiskSettings, db: Any):
        self.risk = risk
        self.db = db
        self._tz = ZoneInfo(risk.daily_reset_timezone)
        self._last_write = 0.0

    def trading_day(self, now: datetime) -> str:
        local = now.astimezone(self._tz) - timedelta(hours=self.risk.daily_reset_hour)
        return local.date().isoformat()

    def day_start_utc(self, now: datetime) -> datetime:
        local = now.astimezone(self._tz)
        boundary = datetime.combine(local.date(), dtime(self.risk.daily_reset_hour, 0), tzinfo=self._tz)
        if local < boundary:
            boundary -= timedelta(days=1)
        return boundary.astimezone(UTC)

    def update(self, account: AccountInfo, now: datetime | None = None) -> DrawdownStatus:
        now = now or datetime.now(UTC)
        day = self.trading_day(now)
        row = self.db.get_daily_stats(day)
        if row is None or row.get("start_equity") is None:
            self.db.upsert_daily_stats(day, start_equity=account.equity, start_balance=account.balance,
                                       min_equity=account.equity, max_drawdown_pct=0.0, halted=False)
            row = self.db.get_daily_stats(day) or {}
            log.info("Trading day %s started — reference equity %.2f / balance %.2f", day,
                     account.equity, account.balance)
        start_equity = float(row.get("start_equity") or account.equity)
        start_balance = float(row.get("start_balance") or start_equity)
        reference = {"equity": start_equity, "balance": start_balance,
                     "max": max(start_equity, start_balance)}[self.risk.daily_loss_reference]
        drawdown = max(0.0, reference - account.equity)
        drawdown_pct = drawdown / reference * 100.0 if reference > 0 else 0.0
        worse = (drawdown_pct > float(row.get("max_drawdown_pct") or 0.0) + 0.01
                 or account.equity < float(row.get("min_equity") or math.inf))
        if worse and time.monotonic() - self._last_write > 15.0:
            self.db.upsert_daily_stats(
                day, min_equity=min(account.equity, float(row.get("min_equity") or math.inf)),
                max_drawdown_pct=max(drawdown_pct, float(row.get("max_drawdown_pct") or 0.0)))
            self._last_write = time.monotonic()
        limit = self.risk.max_daily_loss_percent
        return DrawdownStatus(day, start_equity, start_balance, reference, account.equity,
                              round(drawdown, 2), round(drawdown_pct, 3), limit,
                              drawdown_pct >= limit, bool(row.get("halted")), row.get("halt_reason"))

    def halt(self, trading_day: str, reason: str) -> None:
        self.db.upsert_daily_stats(trading_day, halted=True, halted_at=datetime.now(UTC),
                                   halt_reason=reason)
