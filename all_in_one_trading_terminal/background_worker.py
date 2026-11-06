"""
background_worker.py — Multi-threaded trading daemon that syncs Bot, News and Telegram.

Threads
-------
scanner     every SCAN_INTERVAL_SECONDS: pull HTF/MTF/LTF candles, run the strategy on each
            newly closed LTF bar, gate signals (bot state, circuit breaker, news kill-switch,
            killzone, exposure limits, spread) and execute / broadcast them.
protector   every PROTECTOR_INTERVAL_SECONDS: broker watchdog + reconnect, account snapshot,
            daily-drawdown circuit breaker, TP1 partials / breakeven / trailing, pending-order
            fills & expiry, closed-trade P&L journaling, equity curve.
news        every NEWS_LOOP_SECONDS: Forex Factory refresh, morning digest, 15-min pre-news
            warnings, per-symbol news locks and optional pre-news position protection.
supervisor  restarts any loop thread that dies.
telegram    asyncio dispatcher thread owned by TelegramBroadcaster.

The dashboard (app.py) embeds one worker per process via ``st.cache_resource``; a file lock
guarantees a single trading worker per data directory.  ``python background_worker.py``
runs the same engine headless (VPS mode) — add ``--start`` to enable trading immediately.
"""

from __future__ import annotations

import argparse
import atexit
import copy
import logging
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from analytics import correlation_matrix, volatility_row
from config import (
    IST,
    STRATEGY_CODES,
    Settings,
    canonical_symbol,
    get_settings,
    pip_size_for,
    setup_logging,
    symbol_value,
)
from database import Database, parse_iso, to_iso
from forex_factory_engine import BlackoutStatus, ForexFactoryEngine
from mt5_execution_engine import (
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerUnavailableError,
    ClosedPositionSummary,
    DailyDrawdownGuard,
    DrawdownStatus,
    ManagedTradeState,
    PendingOrderInfo,
    PositionInfo,
    PositionProtector,
    ProtectionDecision,
    SymbolSpec,
    Tick,
    create_broker,
)
from quant_strategy_engine import (
    FeatureSet,
    MarketAnalysis,
    QuantStrategyEngine,
    TradeSignal,
    calculate_position_size,
    closed_bars,
    compute_atr,
    prepare_ohlcv,
)
from telegram_broadcaster import (
    SendResult,
    TelegramBroadcaster,
    format_news_digest,
    format_status_alert,
    format_trade_signal,
)

log = logging.getLogger("worker")

UTC = timezone.utc
FINAL_SIGNAL_STATUSES = frozenset({"EXECUTED", "BROADCAST", "REJECTED", "EXPIRED"})
RUNTIME_KEY = "runtime_config_v1"
NOISY_LOGGERS = ("streamlit", "tornado", "watchdog", "asyncio", "httpx", "httpcore", "urllib3",
                 "PIL", "matplotlib", "fsevents")


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
class WorkerLockError(RuntimeError):
    """Another trading worker already owns this data directory."""


class SingleInstanceLock:
    """Exclusive, crash-safe OS file lock (released automatically if the process dies)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WorkerLockError(
                f"Another trading worker is already running for this data folder ({self.path}). "
                "Close the other dashboard / headless worker first.") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


class MemoryLogHandler(logging.Handler):
    """Ring buffer for the dashboard + persistent copy of INFO+ records in SQLite."""

    def __init__(self, db: Database | None, capacity: int = 1500, db_level: int = logging.INFO):
        super().__init__(level=logging.DEBUG)
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.db = db
        self.db_level = db_level
        self._tls = threading.local()
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(NOISY_LOGGERS) or getattr(self._tls, "busy", False):
            return
        self._tls.busy = True
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                message += " | " + "".join(
                    traceback.format_exception_only(record.exc_info[0], record.exc_info[1])).strip()
            entry = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
                     "level": record.levelname, "source": record.name, "message": message}
            with self._buffer_lock:
                self.records.append(entry)
            if self.db is not None and record.levelno >= self.db_level:
                self.db.insert_log(entry["ts"], entry["level"], entry["source"], entry["message"])
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        finally:
            self._tls.busy = False

    def tail(self, limit: int = 300, min_level: str | None = None) -> list[dict[str, Any]]:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        threshold = order.get((min_level or "DEBUG").upper(), 10)
        with self._buffer_lock:
            rows = [r for r in self.records if order.get(r["level"], 0) >= threshold]
        return list(reversed(rows[-limit:]))


@dataclass
class RuntimeConfig:
    """Operator toggles changed from the dashboard (persisted, except bot_enabled)."""

    bot_enabled: bool
    execution_mode: str
    risk_percent: float
    news_auto_pause: bool
    require_killzone: bool
    enabled_strategies: list[str]

    @classmethod
    def load(cls, db: Database, settings: Settings) -> "RuntimeConfig":
        stored = db.kv_get(RUNTIME_KEY) or {}
        risk = float(stored.get("risk_percent", settings.risk.risk_percent))
        strategies = [s for s in stored.get("enabled_strategies", list(settings.strategy.enabled))
                      if s in STRATEGY_CODES]
        mode = stored.get("execution_mode", settings.execution_mode)
        return cls(
            bot_enabled=bool(settings.auto_start_bot),
            execution_mode=mode if mode in ("auto", "signal_only") else settings.execution_mode,
            risk_percent=min(max(risk, 0.01), settings.risk.max_risk_percent),
            news_auto_pause=bool(stored.get("news_auto_pause", settings.news.filter_enabled)),
            require_killzone=bool(stored.get("require_killzone", settings.strategy.require_killzone)),
            enabled_strategies=strategies or list(settings.strategy.enabled))

    def save(self, db: Database) -> None:
        data = asdict(self)
        data.pop("bot_enabled", None)
        db.kv_set(RUNTIME_KEY, data)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class TradingWorker:
    """Owns the broker, strategy, news engine and Telegram broadcaster for one process."""

    def __init__(self, settings: Settings, db: Database, *, broker: BrokerAdapter | None = None,
                 telegram: TelegramBroadcaster | None = None, news: ForexFactoryEngine | None = None,
                 acquire_lock: bool = True):
        self.settings = settings
        self.db = db
        self.runtime = RuntimeConfig.load(db, settings)
        self._lock_guard = SingleInstanceLock(settings.app.lock_file) if acquire_lock else None
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._loops: dict[str, Callable[[], None]] = {}
        self._started = False
        self.log_handler = MemoryLogHandler(db)
        self.broker: BrokerAdapter | None = broker
        self.broker_error: str | None = None
        self.strategy = self._build_strategy()
        self.news = news or ForexFactoryEngine(settings.news, settings.app.cache_dir, db)
        mode_label = "PAPER" if settings.broker_mode == "paper" else "LIVE MT5"
        self.telegram = telegram or TelegramBroadcaster(settings.telegram, db, mode_label=mode_label,
                                                        currency=settings.paper.account_currency)
        self.protector = PositionProtector(settings.risk)
        self.dd_guard = DailyDrawdownGuard(settings.risk, db)

        self._analysis: dict[str, MarketAnalysis] = {}
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._last_analyzed: dict[str, pd.Timestamp] = {}
        self._broker_symbols: dict[str, str] = {}
        self._managed: dict[int, ManagedTradeState] = {}
        self._known_positions: set[int] | None = None
        self._closing_retries: dict[int, int] = {}
        self._close_reasons: dict[int, str] = {}
        self._news_lock_state: dict[str, bool] = {}
        self._signal_reasons: dict[str, str] = {}
        self._signal_failures: dict[str, int] = {}
        self._dxy_cache: tuple[float, pd.DataFrame | None] = (0.0, None)
        self._chart_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._feature_cache: dict[tuple[str, str], tuple[Any, FeatureSet | None]] = {}
        self._throttle: dict[str, float] = {}
        self._positions_cache: list[PositionInfo] = []
        self._orders_cache: list[PendingOrderInfo] = []
        self._account_cache: AccountInfo | None = None
        self._last_equity_log = 0.0
        self._last_quote_refresh = 0.0
        self._startup_reconciled = False
        self._events: deque[dict[str, Any]] = deque(maxlen=400)
        self._event_seq = 0
        self._event_lock = threading.Lock()
        self._was_connected: bool | None = None
        self._tool_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._state: dict[str, Any] = {
            "started_at": None, "mode": "PAPER" if settings.broker_mode == "paper" else "MT5",
            "broker_connected": False, "broker_error": None, "ping_ms": None, "account": None,
            "drawdown": None, "positions": [], "orders": [], "bot_status": "STARTING",
            "status_reason": "", "news_locks": {}, "news": {}, "symbols": {},
            "last_scan_at": None, "last_protect_at": None,
        }

    # ------------------------------------------------------------- helpers --
    def _build_strategy(self) -> QuantStrategyEngine:
        strategy = replace(self.settings.strategy, enabled=tuple(self.runtime.enabled_strategies))
        return QuantStrategyEngine(strategy, self.settings.risk, self.settings.ltf,
                                   self.settings.mtf, self.settings.htf)

    def _set_state(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)

    def _get_state(self, key: str, default: Any = None) -> Any:
        with self._state_lock:
            return self._state.get(key, default)

    def _set_symbol_state(self, canonical: str, **values: Any) -> None:
        with self._state_lock:
            self._state["symbols"].setdefault(canonical, {}).update(values)

    def _emit(self, kind: str, title: str, detail: str = "", level: str = "info",
              symbol: str | None = None) -> None:
        """Queue a user-facing notification (web bell, toasts). Never raises."""
        try:
            with self._event_lock:
                self._event_seq += 1
                self._events.append({"id": self._event_seq,
                                     "ts": utc_now().isoformat(timespec="seconds"),
                                     "kind": kind, "level": level, "title": title,
                                     "detail": detail, "symbol": symbol})
        except Exception:  # noqa: BLE001 - notifications must never interrupt trading
            log.debug("event emit failed", exc_info=True)

    @property
    def last_event_id(self) -> int:
        with self._event_lock:
            return self._event_seq

    def events_since(self, last_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._event_lock:
            items = [dict(event) for event in self._events if event["id"] > int(last_id)]
        return items[-max(1, int(limit)):]

    def _log_throttled(self, key: str, level: int, message: str, *args: Any,
                       interval: float = 60.0) -> None:
        now = time.monotonic()
        if now - self._throttle.get(key, 0.0) >= interval:
            self._throttle[key] = now
            log.log(level, message, *args)

    @property
    def mode(self) -> str:
        return self.broker.mode if self.broker is not None else self._state["mode"]

    def _broker_symbol(self, canonical: str) -> str | None:
        name = self._broker_symbols.get(canonical)
        if name or self.broker is None:
            return name
        name = self.broker.resolve_symbol(canonical)
        if name:
            self._broker_symbols[canonical] = name
        return name

    def _canonical_for(self, broker_symbol: str) -> str:
        for canonical, name in self._broker_symbols.items():
            if name == broker_symbol:
                return canonical
        key = canonical_symbol(broker_symbol)
        for canonical in self.settings.symbols:
            if key.startswith(canonical):
                return canonical
        return key

    # ----------------------------------------------------------- lifecycle --
    def start(self) -> None:
        if self._started:
            return
        if self._lock_guard is not None:
            self._lock_guard.acquire()
        root = logging.getLogger()
        if self.log_handler not in root.handlers:
            root.addHandler(self.log_handler)
        self._started = True
        self._stop.clear()
        self._set_state(started_at=utc_now().isoformat(timespec="seconds"))
        log.info("Trading worker starting — %s mode, symbols %s, %s/%s/%s",
                 self.settings.broker_mode.upper(), ",".join(self.settings.symbols),
                 self.settings.htf, self.settings.mtf, self.settings.ltf)
        try:
            self.db.prune_logs(self.settings.app.log_retention_days)
            self.db.prune_equity(180)
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping failed")
        if self.broker is None:
            try:
                self.broker = create_broker(self.settings, self.db)
            except BrokerUnavailableError as exc:
                self.broker_error = str(exc)
                log.error("Broker unavailable: %s", exc)
        if self.broker is not None:
            try:
                self.broker.connect()
            except Exception:  # noqa: BLE001
                log.exception("Initial broker connection failed (will keep retrying)")
        self.telegram.start()
        self._loops = {"scanner": self._scan_loop, "protector": self._protect_loop,
                       "news": self._news_loop}
        for name, target in self._loops.items():
            self._spawn(name, target)
        self._spawn("supervisor", self._supervisor_loop)
        atexit.register(self.stop)
        self.telegram.alert_admin(format_status_alert(
            "Trading terminal online",
            f"Mode: {self.mode} | Symbols: {', '.join(self.settings.symbols)} | "
            f"Bot: {'RUNNING' if self.runtime.bot_enabled else 'PAUSED'}", "🟢"))

    def stop(self, timeout: float = 10.0) -> None:
        if not self._started:
            return
        log.info("Trading worker stopping")
        self._stop.set()
        current = threading.current_thread()
        for thread in list(self._threads.values()):
            if thread is not current and thread.is_alive():
                thread.join(timeout=timeout / max(1, len(self._threads)))
        try:
            self.telegram.stop(timeout=5.0)
        except Exception:  # noqa: BLE001
            log.exception("Telegram shutdown failed")
        if self.broker is not None:
            try:
                self.broker.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Broker shutdown failed")
        logging.getLogger().removeHandler(self.log_handler)
        if self._lock_guard is not None:
            self._lock_guard.release()
        self._started = False

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=self._run_guarded, args=(name, target), daemon=True,
                                  name=f"worker-{name}")
        self._threads[name] = thread
        thread.start()

    def _run_guarded(self, name: str, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception:  # noqa: BLE001
            log.exception("%s thread crashed — supervisor will restart it", name)

    def _loop(self, interval: float, body: Callable[[], None], name: str) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                body()
            except BrokerError as exc:
                self._log_throttled(f"{name}-broker", logging.WARNING, "%s cycle: broker error: %s",
                                    name, exc)
            except Exception:  # noqa: BLE001
                log.exception("%s cycle failed", name)
            self._stop.wait(max(0.05, started + interval - time.monotonic()))

    def _scan_loop(self) -> None:
        self._loop(self.settings.scan_interval_seconds, self._scan_cycle, "scanner")

    def _protect_loop(self) -> None:
        self._loop(self.settings.protector_interval_seconds, self._protect_cycle, "protector")

    def _news_loop(self) -> None:
        self._loop(self.settings.news_loop_seconds, self._news_cycle, "news")

    def _supervisor_loop(self) -> None:
        while not self._stop.wait(5.0):
            for name, target in self._loops.items():
                thread = self._threads.get(name)
                if thread is None or not thread.is_alive():
                    log.error("Thread %s is not alive — restarting", name)
                    self._spawn(name, target)

    # ------------------------------------------------------------- scanner --
    def _scan_cycle(self) -> None:
        broker = self.broker
        if broker is None or not self._get_state("broker_connected"):
            return
        now = utc_now()
        for canonical in self.settings.symbols:
            if self._stop.is_set():
                return
            try:
                self._scan_symbol(broker, canonical, now)
            except BrokerError as exc:
                self._log_throttled(f"scan-{canonical}", logging.WARNING, "%s scan skipped: %s",
                                    canonical, exc)
            except Exception:  # noqa: BLE001
                log.exception("Scan failed for %s", canonical)
        self._set_state(last_scan_at=utc_now().isoformat(timespec="seconds"))

    def _scan_symbol(self, broker: BrokerAdapter, canonical: str, now: datetime) -> None:
        symbol = self._broker_symbol(canonical)
        if not symbol:
            self._log_throttled(f"resolve-{canonical}", logging.WARNING,
                                "%s is not available at the broker / data feed", canonical,
                                interval=600)
            return
        s = self.settings
        ltf = broker.get_rates(symbol, s.ltf, s.strategy.ltf_bars)
        if ltf.empty:
            self._log_throttled(f"rates-{canonical}", logging.WARNING, "No %s candles for %s (%s)",
                                s.ltf, canonical, broker.last_error)
            return
        frames: dict[str, pd.DataFrame] = {s.ltf: ltf}
        frames[s.mtf] = broker.get_rates(symbol, s.mtf, s.strategy.mtf_bars)
        frames[s.htf] = broker.get_rates(symbol, s.htf, s.strategy.htf_bars)
        if s.strategy.crt_timeframe not in frames:
            frames[s.strategy.crt_timeframe] = broker.get_rates(symbol, s.strategy.crt_timeframe, 60)
        if ("time_candle_breakout" in self.runtime.enabled_strategies
                and s.strategy.time_candle_timeframe not in frames):
            frames[s.strategy.time_candle_timeframe] = broker.get_rates(
                symbol, s.strategy.time_candle_timeframe, 200)
        with self._state_lock:
            for timeframe, frame in frames.items():
                self._frames[(canonical, timeframe)] = frame
        tick = broker.get_tick(symbol)
        spec = broker.get_symbol_spec(symbol)
        if tick is not None and spec is not None:
            self._store_quote(canonical, symbol, tick, spec)
        self._set_symbol_state(canonical, change_24h_pct=self._change_24h(ltf))
        closed = closed_bars(prepare_ohlcv(ltf), s.ltf, now)
        if closed.empty or self._last_analyzed.get(canonical) == closed.index[-1]:
            return
        dxy = self._dxy_frame(broker) if s.strategy.dxy_filter else None
        analysis = self.strategy.analyze(
            canonical, ltf, frames[s.mtf], frames[s.htf],
            crt_df=frames.get(s.strategy.crt_timeframe),
            time_candle_df=frames.get(s.strategy.time_candle_timeframe), dxy_df=dxy, now=now,
            bid=tick.bid if tick else None, ask=tick.ask if tick else None)
        with self._state_lock:
            self._analysis[canonical] = analysis
        self._last_analyzed[canonical] = closed.index[-1]
        summary = analysis.summary()
        self._set_symbol_state(canonical, analysis=summary,
                               last_analysis_at=now.isoformat(timespec="seconds"))
        for trade_signal in analysis.signals:
            self._handle_signal(trade_signal, symbol, spec, tick, now)

    def _store_quote(self, canonical: str, symbol: str, tick: Tick, spec: SymbolSpec) -> None:
        self._set_symbol_state(canonical, broker_symbol=symbol, bid=tick.bid, ask=tick.ask,
                               spread_points=round(tick.spread_points(spec.point), 1),
                               digits=spec.digits, tick_time=tick.time_utc.isoformat())

    def _refresh_quotes(self, min_interval: float = 2.0) -> None:
        """Keep market-watch quotes live between scans (cheap: ticks are cached by the broker)."""
        if self.broker is None or time.monotonic() - self._last_quote_refresh < min_interval:
            return
        self._last_quote_refresh = time.monotonic()
        for canonical in self.settings.symbols:
            symbol = self._broker_symbol(canonical)
            if not symbol:
                continue
            tick, spec = self.broker.get_tick(symbol), self.broker.get_symbol_spec(symbol)
            if tick is not None and spec is not None:
                self._store_quote(canonical, symbol, tick, spec)

    @staticmethod
    def _change_24h(frame: pd.DataFrame) -> float | None:
        """Percent change of the last close versus the close 24 hours earlier."""
        if frame is None or frame.empty:
            return None
        closes = frame["close"]
        prior = closes[closes.index <= closes.index[-1] - pd.Timedelta(hours=24)]
        if prior.empty or not float(prior.iloc[-1]):
            return None
        return round((float(closes.iloc[-1]) / float(prior.iloc[-1]) - 1.0) * 100.0, 3)

    def _dxy_frame(self, broker: BrokerAdapter) -> pd.DataFrame | None:
        stamp, frame = self._dxy_cache
        if time.monotonic() - stamp < 300 and stamp:
            return frame
        symbol = self._broker_symbol(self.settings.strategy.dxy_symbol)
        frame = broker.get_rates(symbol, self.settings.htf, self.settings.strategy.htf_bars) if symbol else None
        if frame is not None and frame.empty:
            frame = None
        if frame is None:
            self._log_throttled("dxy", logging.INFO, "DXY data unavailable (%s) — DXY filter skipped",
                                self.settings.strategy.dxy_symbol, interval=1800)
        self._dxy_cache = (time.monotonic(), frame)
        return frame

    # ------------------------------------------------------------- signals --
    def _handle_signal(self, sig: TradeSignal, symbol: str, spec: SymbolSpec | None,
                       tick: Tick | None, now: datetime) -> None:
        signal_id = sig.signal_id
        existing = self.db.get_signal(signal_id)
        if existing is not None and existing["status"] in FINAL_SIGNAL_STATUSES:
            return
        if existing is None:
            self.db.insert_signal(sig.to_record())
            log.info("Setup detected: %s %s %s [%s] entry %.5g SL %.5g score %.1f (%s)", sig.symbol,
                     sig.direction, sig.strategy, sig.order_type, sig.entry, sig.stop_loss,
                     sig.score, sig.grade)
            if str(sig.grade).upper() in ("A+", "A"):
                self._emit("signal", f"{sig.grade} {sig.direction} setup — {sig.symbol}",
                           f"{sig.strategy} · score {sig.score:.1f}", "info", sig.symbol)
        decision = self._gate_signal(sig, symbol, spec, tick, now)
        if decision is not None:
            status, reason = decision
            if self._signal_reasons.get(signal_id) != reason:
                self._signal_reasons[signal_id] = reason
                self.db.update_signal(signal_id, status, reason)
                log.info("Signal %s %s %s: %s", sig.symbol, sig.direction, status, reason)
            return
        assert spec is not None and tick is not None
        if self.runtime.execution_mode == "signal_only":
            self._broadcast_signal_only(sig, symbol, spec, tick)
        else:
            self._execute_signal(sig, symbol, spec, tick, now)

    def _gate_signal(self, sig: TradeSignal, symbol: str, spec: SymbolSpec | None,
                     tick: Tick | None, now: datetime) -> tuple[str, str] | None:
        risk, runtime = self.settings.risk, self.runtime
        if sig.setup not in runtime.enabled_strategies:
            return "REJECTED", "strategy disabled"
        if now > sig.valid_until:
            return "EXPIRED", "setup expired"
        if sig.score < self.settings.strategy.min_score_to_trade:
            return "REJECTED", f"score {sig.score:.1f} below {self.settings.strategy.min_score_to_trade:.1f}"
        if runtime.require_killzone and not (sig.factors.get("killzone") or {}).get("ok"):
            return "REJECTED", "outside killzone"
        if spec is None or tick is None:
            return "SKIPPED", "no market data"
        if not spec.trade_allowed:
            return "REJECTED", "symbol not tradeable"
        if not runtime.bot_enabled:
            return "SKIPPED", "bot paused"
        drawdown = self._get_state("drawdown") or {}
        if drawdown.get("halted"):
            return "SKIPPED", "daily loss circuit breaker active"
        if runtime.news_auto_pause:
            blackout = self.news.blackout_status(sig.symbol, now)
            if blackout.active:
                return "SKIPPED", f"news blackout: {blackout.describe()}"
        if runtime.execution_mode == "auto":
            magic = self.settings.mt5.magic
            with self._state_lock:
                positions = [p for p in self._positions_cache if p.magic == magic]
                orders = [o for o in self._orders_cache if o.magic == magic]
            # The broker cache refreshes once per protector cycle; the journal is written
            # synchronously on execution, so it also covers orders placed earlier in this scan.
            journal = self.db.get_active_trades(self.mode)
            on_symbol = max(
                sum(1 for p in positions if p.symbol == symbol) + sum(1 for o in orders if o.symbol == symbol),
                sum(1 for t in journal if t["broker_symbol"] == symbol))
            if on_symbol >= risk.max_positions_per_symbol:
                return "SKIPPED", "max positions for symbol reached"
            if max(len(positions) + len(orders), len(journal)) >= risk.max_open_positions:
                return "SKIPPED", "max open positions reached"
            opposite = any(p.symbol == symbol and p.direction != sig.direction for p in positions) or any(
                t["broker_symbol"] == symbol and t["direction"] != sig.direction for t in journal)
            if not risk.allow_hedging and opposite:
                return "SKIPPED", "opposite position open (hedging disabled)"
            if self.db.count_trades_since(self.dd_guard.day_start_utc(now), self.mode) >= risk.max_trades_per_day:
                return "SKIPPED", "daily trade limit reached"
            last = self.db.last_closed_trade(sig.symbol, self.mode)
            if last and (last.get("pnl") or 0) < 0 and risk.loss_cooldown_minutes > 0:
                closed_at = parse_iso(last.get("closed_at"))
                if closed_at and now - closed_at < timedelta(minutes=risk.loss_cooldown_minutes):
                    return "SKIPPED", "cool-down after a losing trade"
        max_spread = symbol_value(risk.max_spread_points, sig.symbol, risk.default_max_spread_points)
        spread = tick.spread_points(spec.point)
        if max_spread > 0 and spread > max_spread:
            return "SKIPPED", f"spread {spread:.0f} > {max_spread:.0f} pts"
        return None

    def _size(self, symbol: str, spec: SymbolSpec, direction: str, entry: float, stop: float,
              risk_percent: float, account: AccountInfo):
        assert self.broker is not None
        return calculate_position_size(
            account.equity, risk_percent, entry, stop, tick_size=spec.tick_size,
            tick_value=spec.tick_value, volume_min=spec.volume_min, volume_max=spec.volume_max,
            volume_step=spec.volume_step,
            loss_per_lot=self.broker.calc_loss_per_lot(symbol, direction, entry, stop),
            max_risk_percent=self.settings.risk.max_risk_percent,
            allow_min_lot=self.settings.risk.allow_min_lot_override)

    def _signal_payload(self, sig: TradeSignal, spec: SymbolSpec, *, entry: float, tp1: float,
                        tp2: float, order_type: str, status: str, lots: float | None = None,
                        risk_percent: float | None = None, risk_amount: float | None = None) -> dict[str, Any]:
        return {"signal_id": sig.signal_id, "symbol": sig.symbol, "direction": sig.direction,
                "strategy": sig.strategy, "timeframe": sig.timeframe, "entry": entry,
                "stop_loss": sig.stop_loss, "tp1": tp1, "tp2": tp2, "rr": self.settings.risk.tp2_rr,
                "tp1_rr": self.settings.risk.tp1_rr, "order_type": order_type, "lots": lots,
                "risk_percent": risk_percent, "risk_amount": risk_amount, "score": sig.score,
                "grade": sig.grade, "confluences": sig.confluences, "digits": spec.digits,
                "pip_size": pip_size_for(sig.symbol, spec.point, spec.digits),
                "created_at": utc_now(), "status": status}

    def _execute_signal(self, sig: TradeSignal, symbol: str, spec: SymbolSpec, tick: Tick,
                        now: datetime) -> None:
        broker = self.broker
        assert broker is not None
        account = self._account_cache or broker.get_account()
        if account is None:
            self.db.update_signal(sig.signal_id, "SKIPPED", "account unavailable")
            return
        risk = self.settings.risk
        sign = 1.0 if sig.direction == "BUY" else -1.0
        market = sig.order_type == "MARKET"
        entry = (tick.ask if sig.direction == "BUY" else tick.bid) if market else sig.entry
        distance = (entry - sig.stop_loss) * sign
        if distance <= 0:
            self.db.update_signal(sig.signal_id, "REJECTED", "price moved through the stop")
            return
        tp1 = spec.normalize_price(entry + sign * distance * risk.tp1_rr)
        tp2 = spec.normalize_price(entry + sign * distance * risk.tp2_rr)
        size = self._size(symbol, spec, sig.direction, entry, sig.stop_loss, self.runtime.risk_percent,
                          account)
        if not size.valid:
            self.db.update_signal(sig.signal_id, "REJECTED", f"position size: {size.reason}")
            log.warning("Signal %s %s rejected: %s", sig.symbol, sig.direction, size.reason)
            return
        comment = f"QT {sig.setup[:12]} {sig.grade}"
        max_spread = symbol_value(risk.max_spread_points, sig.symbol, risk.default_max_spread_points)
        if market:
            result = broker.place_market_order(symbol, sig.direction, size.lots, sl=sig.stop_loss,
                                               tp=tp2, comment=comment,
                                               max_spread_points=max_spread or None)
        else:
            result = broker.place_pending_order(symbol, sig.direction, "LIMIT", entry, size.lots,
                                                sl=sig.stop_loss, tp=tp2, comment=comment)
        if not result.success:
            failures = self._signal_failures.get(sig.signal_id, 0) + 1
            self._signal_failures[sig.signal_id] = failures
            status = "REJECTED" if failures >= 2 else "SKIPPED"
            self.db.update_signal(sig.signal_id, status, f"order failed: {result.message}")
            log.error("Order for %s %s failed (%s/2): %s", sig.symbol, sig.direction, failures,
                      result.message)
            self.telegram.alert_admin(format_status_alert(
                f"Order failed — {sig.symbol} {sig.direction}", result.message, "⚠️"),
                dedupe_key=f"orderfail:{sig.signal_id}:{failures}")
            return
        fill_price = float(result.price or entry)
        trade_id = self.db.insert_trade(
            signal_id=sig.signal_id, mode=broker.mode, symbol=sig.symbol, broker_symbol=symbol,
            direction=sig.direction, order_type=sig.order_type, strategy=sig.strategy,
            setup=sig.setup, timeframe=sig.timeframe, status="OPEN" if market else "PENDING",
            order_ticket=result.order_ticket,
            position_ticket=result.position_ticket if market else None,
            magic=self.settings.mt5.magic, volume=size.lots, volume_open=size.lots,
            entry_price=entry, fill_price=fill_price if market else None,
            sl_initial=sig.stop_loss, sl_current=sig.stop_loss, tp1=tp1, tp2=tp2,
            risk_amount=round(size.actual_risk, 2), risk_percent=round(size.actual_risk_percent, 3),
            rr_planned=risk.tp2_rr, score=sig.score, grade=sig.grade,
            confluences=sig.confluences, valid_until=None if market else sig.valid_until,
            opened_at=now if market else None)
        if market and result.position_ticket:
            self._register_managed(result.position_ticket, symbol, sig.direction, fill_price,
                                   sig.stop_loss, tp1, tp2, trade_id)
        self.db.update_signal(sig.signal_id, "EXECUTED",
                              f"{'OPEN' if market else 'PENDING'} #{result.order_ticket}")
        log.info("EXECUTED %s %s %s %.2f lots @ %.5g (SL %.5g TP1 %.5g TP2 %.5g) risk %.2f%%",
                 sig.order_type, sig.direction, sig.symbol, size.lots, fill_price, sig.stop_loss,
                 tp1, tp2, size.actual_risk_percent)
        self._emit("trade", f"{'Executed' if market else 'Order placed'} {sig.direction} {sig.symbol}",
                   f"{'MARKET' if market else 'LIMIT'} {size.lots:g} lots @ {fill_price:.{spec.digits}f} · "
                   f"risk {size.actual_risk_percent:.2f}%", "success" if market else "info", sig.symbol)
        if self.settings.telegram.broadcast_executed:
            payload = self._signal_payload(sig, spec, entry=fill_price if market else entry, tp1=tp1,
                                           tp2=tp2, order_type="MARKET" if market else "LIMIT",
                                           status="EXECUTED",
                                           lots=size.lots, risk_percent=size.actual_risk_percent,
                                           risk_amount=size.actual_risk)
            self.telegram.broadcast_trade_signal(
                payload, dedupe_key=f"signal:{sig.signal_id}",
                on_sent=lambda res, tid=trade_id: self.db.update_trade(tid, telegram_message_id=res.message_id))

    def _broadcast_signal_only(self, sig: TradeSignal, symbol: str, spec: SymbolSpec, tick: Tick) -> None:
        if sig.score < self.settings.strategy.a_plus_score or not self.settings.telegram.broadcast_a_plus:
            self.db.update_signal(sig.signal_id, "REJECTED", "signal-only mode: below A+ threshold")
            return
        account = self._account_cache
        lots = risk_amount = risk_pct = None
        if account is not None:
            size = self._size(symbol, spec, sig.direction, sig.entry, sig.stop_loss,
                              self.runtime.risk_percent, account)
            if size.valid:
                lots, risk_amount, risk_pct = size.lots, size.actual_risk, size.actual_risk_percent
        payload = self._signal_payload(sig, spec, entry=sig.entry, tp1=sig.tp1, tp2=sig.tp2,
                                       order_type=sig.order_type, status="SIGNAL", lots=lots,
                                       risk_percent=risk_pct, risk_amount=risk_amount)
        sent = self.telegram.broadcast_trade_signal(payload, dedupe_key=f"signal:{sig.signal_id}")
        self.db.update_signal(sig.signal_id, "BROADCAST",
                              "A+ setup broadcast" if sent else "A+ setup (Telegram not configured)")
        log.info("A+ setup %s %s %s (signal-only mode)", sig.symbol, sig.direction,
                 "broadcast" if sent else "logged")

    def _register_managed(self, ticket: int, symbol: str, direction: str, entry: float,
                          initial_sl: float | None, tp1: float | None, tp2: float | None,
                          trade_id: int | None) -> None:
        with self._state_lock:
            self._managed[ticket] = ManagedTradeState(ticket, symbol, direction, entry, initial_sl,
                                                      tp1, tp2, trade_id)
            if self._known_positions is not None:
                self._known_positions.add(ticket)

    # ----------------------------------------------------------- protector --
    def _protect_cycle(self) -> None:
        broker = self.broker
        if broker is None:
            self._set_state(broker_connected=False, broker_error=self.broker_error)
            self._update_bot_status()
            return
        connected = broker.ensure_connected()
        self._set_state(broker_connected=connected, ping_ms=broker.ping_ms() if connected else None,
                        broker_error=None if connected else (broker.last_error or "broker offline"))
        if connected != self._was_connected:
            if connected:
                self._emit("broker", "Broker connected", f"{broker.mode} account online", "success")
            else:
                self._emit("broker", "Broker disconnected",
                           broker.last_error or "connection lost — reconnecting", "danger")
            self._was_connected = connected
        if not connected:
            self._update_bot_status()
            return
        now = utc_now()
        broker.process_market()
        account = broker.get_account()
        if account is None:
            self._set_state(broker_error="account information unavailable")
            self._update_bot_status()
            return
        drawdown = self.dd_guard.update(account, now)
        if drawdown.breached and not drawdown.halted:
            self._trigger_circuit_breaker(drawdown, account)
            drawdown = self.dd_guard.update(account, now)
        positions = broker.get_positions()
        orders = broker.get_pending_orders()
        with self._state_lock:
            self._positions_cache, self._orders_cache, self._account_cache = positions, orders, account
        if not self._startup_reconciled:
            self._reconcile_journal(positions, orders, now)
            self._startup_reconciled = True
        self._reconcile_pending(orders, positions, now)
        self._manage_positions(positions)
        self._detect_closed(positions, now)
        if time.monotonic() - self._last_equity_log >= 60.0:
            self.db.log_equity(broker.mode, account.balance, account.equity, account.profit,
                               account.margin, account.free_margin, drawdown.drawdown_pct)
            self._last_equity_log = time.monotonic()
        magic = self.settings.mt5.magic
        rows = []
        for position in positions:
            row = position.to_dict()
            managed = self._managed.get(position.ticket)
            row.update({"canonical": self._canonical_for(position.symbol),
                        "bot": position.magic == magic,
                        "initial_sl": managed.initial_sl if managed else None,
                        "tp1": managed.tp1 if managed else None,
                        "be_moved": bool(managed and managed.be_moved),
                        "tp1_hit": bool(managed and managed.tp1_hit),
                        "trailing": bool(managed and managed.trailing_active)})
            rows.append(row)
        drawdown_state = drawdown.to_dict()
        drawdown_state["realized_today"] = round(
            self.db.realized_pnl_since(self.dd_guard.day_start_utc(now), broker.mode), 2)
        self._set_state(account=account.to_dict(), drawdown=drawdown_state, positions=rows,
                        orders=[{**o.to_dict(), "canonical": self._canonical_for(o.symbol)} for o in orders],
                        last_protect_at=now.isoformat(timespec="seconds"))
        self._update_bot_status()
        self._refresh_quotes()

    def _trigger_circuit_breaker(self, drawdown: DrawdownStatus, account: AccountInfo) -> None:
        broker = self.broker
        assert broker is not None
        reason = (f"Daily loss {drawdown.drawdown_pct:.2f}% reached the "
                  f"{drawdown.limit_pct:.2f}% limit")
        log.critical("CIRCUIT BREAKER: %s — closing exposure and halting until next trading day", reason)
        self._emit("risk", "Circuit breaker triggered", f"{reason} — trading halted for the day", "danger")
        self.dd_guard.halt(drawdown.trading_day, reason)
        magic = None if self.settings.risk.circuit_breaker_scope == "all" else self.settings.mt5.magic
        cancelled = closed = failed = 0
        try:
            cancelled = sum(1 for _, res in broker.cancel_all_orders(magic) if res.success)
            for position in broker.get_positions():
                if magic is not None and position.magic != magic:
                    continue
                self._close_reasons[position.ticket] = "CIRCUIT_BREAKER"
                result = broker.close_position(position.ticket, comment="circuit breaker")
                closed += int(result.success)
                failed += int(not result.success)
                if not result.success:
                    log.error("Breaker could not close #%s: %s", position.ticket, result.message)
        except BrokerError as exc:
            log.error("Circuit breaker broker error: %s", exc)
        self.telegram.emergency(
            "DAILY DRAWDOWN CIRCUIT BREAKER TRIPPED",
            [reason, f"Equity {account.equity:,.2f} vs day reference {drawdown.reference:,.2f}",
             f"Closed {closed} position(s), cancelled {cancelled} order(s), failures {failed}",
             "Automated trading disabled until the next trading day."],
            also_channel=self.settings.risk.circuit_breaker_alert_channel,
            dedupe_key=f"breaker:{drawdown.trading_day}")

    def _reconcile_journal(self, positions: list[PositionInfo], orders: list[PendingOrderInfo],
                           now: datetime) -> None:
        """Align OPEN/PENDING journal rows with the broker after a restart."""
        assert self.broker is not None
        open_tickets = {p.ticket: p for p in positions}
        order_tickets = {o.ticket for o in orders}
        for trade in self.db.get_active_trades(self.mode):
            if trade["status"] == "OPEN":
                ticket = trade.get("position_ticket")
                if ticket in open_tickets:
                    self._managed[ticket] = ManagedTradeState(
                        ticket, trade["broker_symbol"], trade["direction"],
                        float(trade.get("fill_price") or trade["entry_price"]),
                        trade.get("sl_initial"), trade.get("tp1"), trade.get("tp2"), trade["id"],
                        bool(trade.get("be_moved")), bool(trade.get("tp1_hit")),
                        bool(trade.get("trailing_active")))
                    continue
                summary = self.broker.get_closed_position_summary(ticket) if ticket else None
                if summary is not None:
                    self._finalize_closed(ticket, trade, summary, broadcast=False)
                else:
                    self.db.update_trade(trade["id"], status="CLOSED", close_reason="UNKNOWN",
                                         closed_at=now, notes="closed while terminal was offline")
            elif trade["status"] == "PENDING" and trade.get("order_ticket") not in order_tickets:
                continue  # handled by _reconcile_pending (fill / cancel detection)
        log.info("Journal reconciled: %s managed open trade(s)", len(self._managed))

    def _reconcile_pending(self, orders: list[PendingOrderInfo], positions: list[PositionInfo],
                           now: datetime) -> None:
        broker = self.broker
        assert broker is not None
        order_tickets = {o.ticket for o in orders}
        by_ticket: dict[int, PositionInfo] = {}
        for position in positions:
            by_ticket[position.ticket] = position
            by_ticket.setdefault(position.identifier, position)
        for trade in self.db.get_active_trades(self.mode):
            if trade["status"] != "PENDING" or not trade.get("order_ticket"):
                continue
            ticket = int(trade["order_ticket"])
            reply_to = trade.get("telegram_message_id")
            spec = broker.get_symbol_spec(trade["broker_symbol"])
            digits = spec.digits if spec else 5
            if ticket in order_tickets:
                valid_until = parse_iso(trade.get("valid_until"))
                if valid_until and now > valid_until:
                    result = broker.cancel_order(ticket)
                    if result.success:
                        self.db.update_trade(trade["id"], status="CANCELLED", close_reason="EXPIRED",
                                             closed_at=now, notes="limit order expired unfilled")
                        log.info("Pending %s %s #%s expired and was cancelled", trade["symbol"],
                                 trade["direction"], ticket)
                        self._emit("trade", f"Order expired — {trade['symbol']}",
                                   f"{trade['direction']} #{ticket} cancelled unfilled", "warning",
                                   trade["symbol"])
                        self._announce_update(trade, "PENDING_CANCELLED", {
                            "symbol": trade["symbol"], "direction": trade["direction"],
                            "digits": digits, "ticket": ticket,
                            "reason": "Entry not reached before the setup expired — order cancelled."},
                            reply_to=reply_to, dedupe_key=f"pending-cancel:{ticket}")
                continue
            position = by_ticket.get(ticket)
            if position is not None:
                self.db.update_trade(trade["id"], status="OPEN", position_ticket=position.ticket,
                                     fill_price=position.price_open, opened_at=position.time_utc,
                                     volume_open=position.volume)
                self._register_managed(position.ticket, position.symbol, position.direction,
                                       position.price_open, trade.get("sl_initial"), trade.get("tp1"),
                                       trade.get("tp2"), trade["id"])
                log.info("Pending %s %s #%s filled @ %s", trade["symbol"], trade["direction"], ticket,
                         position.price_open)
                self._emit("trade", f"Order filled — {trade['symbol']}",
                           f"{trade['direction']} #{position.ticket} @ {position.price_open}", "success",
                           trade["symbol"])
                self._announce_update(trade, "PENDING_FILLED", {
                    "symbol": trade["symbol"], "direction": trade["direction"], "digits": digits,
                    "price": position.price_open, "sl": position.sl, "tp": position.tp,
                    "ticket": position.ticket, "strategy": trade.get("strategy")},
                    reply_to=reply_to, dedupe_key=f"pending-fill:{ticket}")
                continue
            summary = broker.get_closed_position_summary(ticket)
            if summary is not None:
                self.db.update_trade(trade["id"], status="OPEN", position_ticket=ticket,
                                     fill_price=summary.price_open)
                self._finalize_closed(ticket, self.db.get_trade(trade["id"]), summary)
            else:
                self.db.update_trade(trade["id"], status="CANCELLED", close_reason="CANCELLED",
                                     closed_at=now, notes="order removed outside the bot")
                log.info("Pending order #%s disappeared without a fill — marked cancelled", ticket)

    def _manage_positions(self, positions: list[PositionInfo]) -> None:
        broker = self.broker
        assert broker is not None
        magic = self.settings.mt5.magic
        for position in positions:
            managed = self._managed.get(position.ticket)
            if managed is None:
                if position.magic != magic and not self.settings.risk.manage_manual_positions:
                    continue
                trade = self.db.get_trade_by_position(position.ticket, self.mode)
                if trade is not None:
                    managed = ManagedTradeState(
                        position.ticket, position.symbol, position.direction,
                        float(trade.get("fill_price") or trade["entry_price"]), trade.get("sl_initial"),
                        trade.get("tp1"), trade.get("tp2"), trade["id"], bool(trade.get("be_moved")),
                        bool(trade.get("tp1_hit")), bool(trade.get("trailing_active")))
                else:
                    managed = ManagedTradeState(position.ticket, position.symbol, position.direction,
                                                position.price_open, position.sl, None, position.tp)
                with self._state_lock:
                    self._managed[position.ticket] = managed
            tick = broker.get_tick(position.symbol)
            spec = broker.get_symbol_spec(position.symbol)
            if tick is None or spec is None:
                continue
            with self._state_lock:
                analysis = self._analysis.get(self._canonical_for(position.symbol))
            atr = analysis.atr if analysis is not None and analysis.atr > 0 else None
            structure = None
            if analysis is not None and analysis.features is not None and \
                    self.settings.risk.trailing_mode == "structure":
                structure = self.strategy.structure_stop(
                    analysis.features, position.direction,
                    tick.bid if position.direction == "BUY" else tick.ask)
            decision = self.protector.evaluate(managed, position, tick, spec, atr, structure)
            if decision.events or decision.new_sl is not None or decision.mark_be:
                self._apply_decision(managed, position, decision, tick, spec)

    def _update_payload(self, managed: ManagedTradeState, position: PositionInfo,
                        spec: SymbolSpec, trade: dict[str, Any] | None) -> dict[str, Any]:
        canonical = self._canonical_for(position.symbol)
        return {"symbol": canonical, "direction": position.direction, "entry": managed.entry,
                "digits": spec.digits, "pip_size": pip_size_for(canonical, spec.point, spec.digits),
                "ticket": position.ticket, "strategy": (trade or {}).get("strategy"),
                "time": utc_now()}

    def _apply_decision(self, managed: ManagedTradeState, position: PositionInfo,
                        decision: ProtectionDecision, tick: Tick, spec: SymbolSpec) -> None:
        broker = self.broker
        assert broker is not None
        trade = self.db.get_trade(managed.trade_id) if managed.trade_id else None
        reply_to = (trade or {}).get("telegram_message_id")
        base = self._update_payload(managed, position, spec, trade)
        price = tick.bid if position.direction == "BUY" else tick.ask
        if decision.mark_tp1 and not managed.tp1_hit:
            closed_volume = partial_pnl = None
            if decision.partial_close_volume:
                result = broker.close_position(position.ticket, decision.partial_close_volume, "TP1 partial")
                if result.success:
                    closed_volume = result.volume or decision.partial_close_volume
                    partial_pnl = broker.calc_profit(position.symbol, position.direction, closed_volume,
                                                     managed.entry, result.price or price)
                else:
                    log.warning("TP1 partial close failed for #%s: %s", position.ticket, result.message)
            managed.tp1_hit = True
            if trade is not None:
                self.db.update_trade(trade["id"], tp1_hit=True,
                                     volume_open=round(position.volume - (closed_volume or 0.0), 4),
                                     partial_pnl=round(float(trade.get("partial_pnl") or 0.0)
                                                       + float(partial_pnl or 0.0), 2))
            log.info("TP1 hit on %s #%s (%s)", position.symbol, position.ticket,
                     f"closed {closed_volume} lots" if closed_volume else "no partial")
            self._emit("trade", f"TP1 hit — {position.symbol}",
                       f"#{position.ticket} " + (f"closed {closed_volume:g} lots" if closed_volume
                                                 else "no partial close"), "success",
                       self._canonical_for(position.symbol))
            self.telegram.broadcast_trade_update(
                "TP1_HIT", {**base, "price": price, "closed_volume": closed_volume, "pnl": partial_pnl},
                reply_to=reply_to, dedupe_key=f"tp1:{position.ticket}")
        if decision.new_sl is not None:
            result = broker.modify_position(position.ticket, sl=decision.new_sl)
            if not result.success:
                self._log_throttled(f"modify-{position.ticket}", logging.WARNING,
                                    "SL update for #%s failed: %s", position.ticket, result.message)
                return
            for event in decision.events:
                if event == "BREAKEVEN" and not managed.be_moved:
                    log.info("SL moved to breakeven on %s #%s -> %s", position.symbol,
                             position.ticket, decision.new_sl)
                    self._emit("trade", f"Breakeven — {position.symbol}",
                               f"#{position.ticket} SL → {decision.new_sl}", "info",
                               self._canonical_for(position.symbol))
                    self.telegram.broadcast_trade_update(
                        "BREAKEVEN", {**base, "sl": decision.new_sl}, reply_to=reply_to,
                        dedupe_key=f"be:{position.ticket}")
                elif event == "TRAILING_ON" and not managed.trailing_active:
                    log.info("Trailing stop activated on %s #%s -> %s", position.symbol,
                             position.ticket, decision.new_sl)
                    self._emit("trade", f"Trailing stop on — {position.symbol}",
                               f"#{position.ticket} SL → {decision.new_sl}", "info",
                               self._canonical_for(position.symbol))
                    self.telegram.broadcast_trade_update(
                        "TRAILING_ON", {**base, "sl": decision.new_sl}, reply_to=reply_to,
                        dedupe_key=f"trail:{position.ticket}")
                elif event == "TRAIL_UPDATE":
                    log.info("Trailing SL %s #%s -> %s", position.symbol, position.ticket,
                             decision.new_sl)
                    if self.settings.telegram.broadcast_trail_steps:
                        self.telegram.broadcast_trade_update(
                            "TRAIL_UPDATE", {**base, "sl": decision.new_sl}, reply_to=reply_to)
            managed.be_moved = managed.be_moved or decision.mark_be
            managed.trailing_active = managed.trailing_active or decision.mark_trailing
            if trade is not None:
                self.db.update_trade(trade["id"], sl_current=decision.new_sl,
                                     be_moved=managed.be_moved, trailing_active=managed.trailing_active)
        elif decision.mark_be and not managed.be_moved:
            managed.be_moved = True
            if trade is not None:
                self.db.update_trade(trade["id"], be_moved=True)

    def _detect_closed(self, positions: list[PositionInfo], now: datetime) -> None:
        broker = self.broker
        assert broker is not None
        current = {p.ticket for p in positions}
        with self._state_lock:
            if self._known_positions is None:
                self._known_positions = set(current)
                return
            gone = (self._known_positions | set(self._closing_retries)) - current
        for ticket in gone:
            summary = broker.get_closed_position_summary(ticket)
            trade = self.db.get_trade_by_position(ticket, self.mode)
            if summary is None:
                attempts = self._closing_retries.get(ticket, 0) + 1
                self._closing_retries[ticket] = attempts
                if attempts >= 60:
                    self._closing_retries.pop(ticket, None)
                    self._managed.pop(ticket, None)
                    if trade is not None and trade["status"] == "OPEN":
                        self.db.update_trade(trade["id"], status="CLOSED", close_reason="UNKNOWN",
                                             closed_at=now, notes="closing deal not found in history")
                    log.warning("Position #%s closed but its history is unavailable", ticket)
                continue
            self._closing_retries.pop(ticket, None)
            self._finalize_closed(ticket, trade, summary)
        with self._state_lock:
            self._known_positions = set(current)

    def _finalize_closed(self, ticket: int, trade: dict[str, Any] | None,
                         summary: ClosedPositionSummary, broadcast: bool = True) -> None:
        reason = self._close_reasons.pop(ticket, None) or summary.reason
        with self._state_lock:
            self._managed.pop(ticket, None)
        if trade is None:
            log.info("Untracked position #%s %s closed (%s) P&L %.2f", ticket, summary.symbol,
                     reason, summary.profit)
            return
        if trade["status"] == "CLOSED":
            return
        entry = float(trade.get("fill_price") or trade["entry_price"])
        sign = 1.0 if trade["direction"] == "BUY" else -1.0
        pip = pip_size_for(trade["symbol"])
        pips = (summary.price_close - entry) * sign / pip if pip else None
        risk_amount = float(trade.get("risk_amount") or 0.0)
        r_multiple = summary.profit / risk_amount if risk_amount > 0 else None
        self.db.update_trade(trade["id"], status="CLOSED", exit_price=summary.price_close,
                             pnl=summary.profit, pips=round(pips, 1) if pips is not None else None,
                             r_multiple=round(r_multiple, 2) if r_multiple is not None else None,
                             close_reason=reason, closed_at=summary.close_time_utc, volume_open=0.0)
        log.info("CLOSED %s %s #%s @ %s (%s) P&L %.2f | %s pips | %sR", trade["symbol"],
                 trade["direction"], ticket, summary.price_close, reason, summary.profit,
                 f"{pips:.1f}" if pips is not None else "-",
                 f"{r_multiple:.2f}" if r_multiple is not None else "-")
        self._emit("trade", f"Closed {trade['symbol']} {summary.profit:+.2f}",
                   f"{trade['direction']} #{ticket} · {reason}"
                   + (f" · {r_multiple:+.2f}R" if r_multiple is not None else ""),
                   "success" if summary.profit >= 0 else "danger", trade["symbol"])
        manual = trade.get("setup") == "manual"
        if broadcast and (not manual or self.settings.telegram.broadcast_manual):
            spec = self.broker.get_symbol_spec(trade["broker_symbol"]) if self.broker else None
            self.telegram.broadcast_trade_update("CLOSED", {
                "symbol": trade["symbol"], "direction": trade["direction"], "entry": entry,
                "price": summary.price_close, "pnl": summary.profit, "pips": pips,
                "r_multiple": r_multiple, "reason": reason, "digits": spec.digits if spec else 5,
                "ticket": ticket, "strategy": trade.get("strategy"), "time": summary.close_time_utc},
                reply_to=trade.get("telegram_message_id"), dedupe_key=f"close:{ticket}")

    def _update_bot_status(self) -> None:
        with self._state_lock:
            drawdown = self._state.get("drawdown") or {}
            locked = [s for s, info in (self._state.get("news_locks") or {}).items() if info.get("active")]
            if not self._state.get("broker_connected"):
                status, reason = "DISCONNECTED", self._state.get("broker_error") or "broker offline"
            elif drawdown.get("halted"):
                status, reason = "HALTED", drawdown.get("halt_reason") or "daily loss limit"
            elif not self.runtime.bot_enabled:
                status, reason = "PAUSED", "press Start to enable trading"
            elif self.runtime.news_auto_pause and locked:
                status, reason = "NEWS LOCKED", ", ".join(locked)
            else:
                status = "RUNNING"
                reason = "auto-execution" if self.runtime.execution_mode == "auto" else "signal-only mode"
            self._state["bot_status"], self._state["status_reason"] = status, reason

    # ---------------------------------------------------------------- news --
    def _news_cycle(self) -> None:
        now = utc_now()
        self.news.refresh_if_due(now)
        self._maybe_send_digest(now)
        self._maybe_send_warnings(now)
        self._update_news_locks(now)
        self._set_state(news=self.news.status())

    def _maybe_send_digest(self, now: datetime) -> None:
        cfg = self.settings.news
        if not cfg.digest_enabled or not self.telegram.configured or not self.news.events():
            return
        now_ist = now.astimezone(IST)
        digest_at = datetime.combine(now_ist.date(), cfg.digest_time_ist, tzinfo=IST)
        if not digest_at <= now_ist <= digest_at + timedelta(hours=cfg.digest_grace_hours):
            return
        key = f"news_digest:{now_ist.date().isoformat()}"
        if self.db.broadcast_exists(key):
            return
        events = self.news.today_events(now, IST, currencies=cfg.alert_currencies,
                                        impacts=cfg.alert_impacts)
        if self.telegram.broadcast_news_digest(events, now_ist.strftime("%a, %d %b %Y"), dedupe_key=key):
            log.info("Morning news digest queued (%s high-impact events)", len(events))

    def _maybe_send_warnings(self, now: datetime) -> None:
        if not self.telegram.configured:
            return
        for event, seconds in self.news.warning_candidates(now):
            key = f"news_warning:{event.event_id}"
            if self.db.broadcast_exists(key):
                continue
            affected = self.news.affected_symbols(event, self.settings.symbols)
            if self.telegram.broadcast_news_warning(event, seconds / 60.0, affected,
                                                    tier1=self.news.is_tier1(event),
                                                    blackout_minutes=self.settings.news.blackout_before_min,
                                                    dedupe_key=key):
                log.warning("Pre-news warning queued: %s %s in %.0f min", event.currency, event.title,
                            seconds / 60.0)
                self._emit("news", f"{event.currency} news in {seconds / 60.0:.0f} min", event.title,
                           "warning")

    def _update_news_locks(self, now: datetime) -> None:
        locks: dict[str, Any] = {}
        for canonical in self.settings.symbols:
            status = self.news.blackout_status(canonical, now)
            event = status.event
            locks[canonical] = {"active": status.active, "label": status.describe(),
                                "phase": status.phase, "seconds": status.seconds_to_event,
                                "ends_at": to_iso(status.ends_at),
                                "event": event.title if event else None,
                                "currency": event.currency if event else None}
            previous = self._news_lock_state.get(canonical, False)
            if status.active and not previous:
                log.warning("NEWS LOCK ON %s — %s", canonical, status.describe())
                self._emit("news", f"News lock ON — {canonical}", status.describe(), "warning", canonical)
                if self.runtime.news_auto_pause and self.settings.telegram.admin_news_lock_alerts and event:
                    self.telegram.alert_admin(
                        format_status_alert(f"News lock ON — {canonical}", status.describe(), "🔒"),
                        dedupe_key=f"newslock:{canonical}:{event.event_id}")
            elif not status.active and previous:
                log.info("NEWS LOCK OFF %s — trading window reopened", canonical)
                self._emit("news", f"News lock OFF — {canonical}", "trading window reopened", "info",
                           canonical)
            self._news_lock_state[canonical] = status.active
            if status.active and status.phase == "PRE" and self.runtime.news_auto_pause:
                self._apply_news_position_action(canonical, status)
        self._set_state(news_locks=locks)
        self._update_bot_status()

    def _apply_news_position_action(self, canonical: str, status: BlackoutStatus) -> None:
        action = self.settings.news.action_on_open_positions
        broker = self.broker
        if action == "none" or broker is None or status.event is None:
            return
        if status.seconds_to_event is None or status.seconds_to_event > self.settings.news.pre_action_minutes * 60:
            return
        symbol = self._broker_symbols.get(canonical)
        if not symbol:
            return
        magic = self.settings.mt5.magic
        with self._state_lock:
            positions = [p for p in self._positions_cache if p.symbol == symbol]
        for position in positions:
            if position.magic != magic and not self.settings.risk.manage_manual_positions:
                continue
            managed = self._managed.get(position.ticket)
            key = status.event.event_id
            if managed is not None and key in managed.news_actions:
                continue
            if action == "close":
                self._close_reasons[position.ticket] = "NEWS"
                result = broker.close_position(position.ticket, comment="news protection")
                log.warning("News protection close #%s %s: %s", position.ticket, position.symbol,
                            "OK" if result.success else result.message)
            elif action == "breakeven" and position.profit > 0:
                self.move_to_breakeven(position.ticket, source="news protection")
            if managed is not None:
                managed.news_actions.add(key)

    # ------------------------------------------------------ dashboard API --
    def start_bot(self, source: str = "dashboard") -> str:
        self.runtime.bot_enabled = True
        self._update_bot_status()
        log.info("Bot STARTED (%s) — %s, risk %.2f%%", source, self.runtime.execution_mode,
                 self.runtime.risk_percent)
        self._emit("bot", "Bot started",
                   f"{self.runtime.execution_mode} · risk {self.runtime.risk_percent:.2f}%", "success")
        self.telegram.alert_admin(format_status_alert(
            "Bot started", f"Execution: {self.runtime.execution_mode} | Risk {self.runtime.risk_percent:.2f}%",
            "▶️"))
        return self._get_state("bot_status")

    def pause_bot(self, source: str = "dashboard") -> str:
        self.runtime.bot_enabled = False
        self._update_bot_status()
        log.info("Bot PAUSED (%s) — open positions stay protected", source)
        self._emit("bot", "Bot paused", "no new entries — open positions stay protected", "warning")
        self.telegram.alert_admin(format_status_alert(
            "Bot paused", "No new entries. Open positions remain managed.", "⏸️"))
        return self._get_state("bot_status")

    def panic_close_all(self, source: str = "dashboard") -> dict[str, Any]:
        self.runtime.bot_enabled = False
        broker = self.broker
        if broker is None:
            return {"ok": False, "closed": 0, "cancelled": 0, "failed": 0,
                    "errors": [self.broker_error or "broker unavailable"]}
        closed = cancelled = failed = 0
        errors: list[str] = []
        try:
            for _, result in broker.cancel_all_orders():
                cancelled += int(result.success)
            for position in broker.get_positions():
                self._close_reasons[position.ticket] = "PANIC"
                result = broker.close_position(position.ticket, comment="PANIC CLOSE")
                if result.success:
                    closed += 1
                else:
                    failed += 1
                    errors.append(f"{position.symbol} #{position.ticket}: {result.message}")
        except BrokerError as exc:
            errors.append(str(exc))
        log.critical("PANIC CLOSE by %s: closed %s, cancelled %s, failed %s", source, closed,
                     cancelled, failed)
        self._emit("risk", "Panic close executed",
                   f"closed {closed} · cancelled {cancelled} · failed {failed}", "danger")
        self.telegram.emergency("PANIC CLOSE EXECUTED",
                                [f"Closed {closed} position(s), cancelled {cancelled} order(s)",
                                 f"Failures: {failed}", "Bot paused."], also_channel=False)
        self._update_bot_status()
        return {"ok": failed == 0 and not errors, "closed": closed, "cancelled": cancelled,
                "failed": failed, "errors": errors}

    def set_risk_percent(self, value: float) -> float:
        self.runtime.risk_percent = round(min(max(float(value), 0.01),
                                              self.settings.risk.max_risk_percent), 2)
        self.runtime.save(self.db)
        log.info("Risk per trade set to %.2f%%", self.runtime.risk_percent)
        return self.runtime.risk_percent

    def set_execution_mode(self, mode: str) -> None:
        if mode not in ("auto", "signal_only"):
            raise ValueError("mode must be 'auto' or 'signal_only'")
        self.runtime.execution_mode = mode
        self.runtime.save(self.db)
        self._update_bot_status()
        log.info("Execution mode set to %s", mode)

    def set_news_auto_pause(self, enabled: bool) -> None:
        self.runtime.news_auto_pause = bool(enabled)
        self.runtime.save(self.db)
        self._update_bot_status()
        log.info("News auto-pause %s", "ENABLED" if enabled else "DISABLED")

    def set_require_killzone(self, enabled: bool) -> None:
        self.runtime.require_killzone = bool(enabled)
        self.runtime.save(self.db)
        log.info("Killzone requirement %s", "ON" if enabled else "OFF")

    def set_enabled_strategies(self, strategies: list[str]) -> None:
        chosen = [s for s in strategies if s in STRATEGY_CODES]
        if not chosen:
            raise ValueError("enable at least one strategy")
        self.runtime.enabled_strategies = chosen
        self.runtime.save(self.db)
        self.strategy = self._build_strategy()
        self._last_analyzed.clear()
        log.info("Enabled strategies: %s", ", ".join(chosen))

    def _atr_for(self, canonical: str, symbol: str) -> float | None:
        with self._state_lock:
            analysis = self._analysis.get(canonical)
        if analysis is not None and analysis.atr > 0:
            return analysis.atr
        if self.broker is None:
            return None
        frame = closed_bars(prepare_ohlcv(self.broker.get_rates(symbol, self.settings.ltf, 100)),
                            self.settings.ltf, utc_now())
        values = compute_atr(frame, self.settings.strategy.atr_period)
        return float(values[-1]) if len(values) else None

    def preview_manual_trade(self, canonical: str, direction: str, risk_percent: float,
                             sl_mode: str, sl_value: float, tp1_rr: float | None = None,
                             tp2_rr: float | None = None, order_kind: str = "MARKET",
                             entry_price: float | None = None) -> dict[str, Any]:
        broker = self.broker
        if broker is None:
            return {"ok": False, "error": self.broker_error or "broker unavailable"}
        symbol = self._broker_symbol(canonical)
        if not symbol:
            return {"ok": False, "error": f"{canonical} not available"}
        tick, spec = broker.get_tick(symbol), broker.get_symbol_spec(symbol)
        account = self._account_cache or broker.get_account()
        if tick is None or spec is None or account is None:
            return {"ok": False, "error": "market data / account unavailable"}
        direction = str(direction).upper()
        if direction not in ("BUY", "SELL"):
            return {"ok": False, "error": "direction must be BUY or SELL"}
        order_kind = str(order_kind or "MARKET").upper()
        if order_kind not in ("MARKET", "LIMIT", "STOP"):
            return {"ok": False, "error": "order type must be MARKET, LIMIT or STOP"}
        sign = 1.0 if direction == "BUY" else -1.0
        market = tick.ask if direction == "BUY" else tick.bid
        if order_kind == "MARKET":
            entry = market
        else:
            if not entry_price or float(entry_price) <= 0:
                return {"ok": False, "error": f"enter a {order_kind.lower()} price", "digits": spec.digits,
                        "market_price": market}
            entry = spec.normalize_price(float(entry_price))
            valid = {("BUY", "LIMIT"): entry < tick.ask, ("SELL", "LIMIT"): entry > tick.bid,
                     ("BUY", "STOP"): entry > tick.ask, ("SELL", "STOP"): entry < tick.bid}[(direction, order_kind)]
            if not valid:
                side = "below" if (direction, order_kind) in (("BUY", "LIMIT"), ("SELL", "STOP")) else "above"
                return {"ok": False, "error": f"{direction} {order_kind} price must be {side} the market "
                                              f"price {market:.{spec.digits}f}",
                        "digits": spec.digits, "market_price": market, "entry": entry}
        pip = pip_size_for(canonical, spec.point, spec.digits)
        atr = self._atr_for(canonical, symbol)
        if sl_mode == "atr":
            if not atr:
                return {"ok": False, "error": "ATR unavailable yet"}
            distance = atr * float(sl_value)
        elif sl_mode == "pips":
            distance = float(sl_value) * pip
        elif sl_mode == "structure":
            with self._state_lock:
                analysis = self._analysis.get(canonical)
            level = (self.strategy.structure_stop(analysis.features, direction, entry)
                     if analysis is not None and analysis.features is not None else None)
            if level is None:
                return {"ok": False, "error": "no confirmed swing level beyond price yet"}
            distance = (entry - level) * sign
        else:
            return {"ok": False, "error": f"unknown SL mode {sl_mode}"}
        if distance <= 0:
            return {"ok": False, "error": "stop distance must be positive"}
        tp1_rr = float(tp1_rr or self.settings.risk.tp1_rr)
        tp2_rr = float(tp2_rr or self.settings.risk.tp2_rr)
        stop = spec.normalize_price(entry - sign * distance)
        tp1 = spec.normalize_price(entry + sign * distance * tp1_rr)
        tp2 = spec.normalize_price(entry + sign * distance * tp2_rr)
        size = self._size(symbol, spec, direction, entry, stop, float(risk_percent), account)
        blackout = self.news.blackout_status(canonical)
        return {"ok": size.valid, "error": None if size.valid else size.reason, "symbol": canonical,
                "broker_symbol": symbol, "direction": direction, "entry": entry, "sl": stop,
                "tp1": tp1, "tp2": tp2, "tp1_rr": tp1_rr, "tp2_rr": tp2_rr, "lots": size.lots,
                "risk_amount": round(size.actual_risk, 2), "risk_percent": round(size.actual_risk_percent, 3),
                "spread_points": round(tick.spread_points(spec.point), 1), "digits": spec.digits,
                "sl_pips": round(distance / pip, 1) if pip else None, "atr": atr,
                "news_lock": blackout.active, "news_label": blackout.describe(),
                "order_kind": order_kind, "market_price": market}

    def manual_trade(self, canonical: str, direction: str, risk_percent: float, sl_mode: str,
                     sl_value: float, tp1_rr: float | None = None, tp2_rr: float | None = None,
                     override_news: bool = False, order_kind: str = "MARKET",
                     entry_price: float | None = None) -> dict[str, Any]:
        preview = self.preview_manual_trade(canonical, direction, risk_percent, sl_mode, sl_value,
                                            tp1_rr, tp2_rr, order_kind, entry_price)
        if not preview.get("ok"):
            return preview
        if (self._get_state("drawdown") or {}).get("halted"):
            return {"ok": False, "error": "Daily loss circuit breaker active — trading blocked until "
                                          "the next trading day"}
        if preview["news_lock"] and not override_news:
            return {"ok": False, "error": f"News blackout: {preview['news_label']} (tick override)"}
        if preview["order_kind"] != "MARKET":
            return self._place_manual_pending(canonical, preview)
        broker = self.broker
        assert broker is not None
        symbol = preview["broker_symbol"]
        result = broker.place_market_order(symbol, preview["direction"], preview["lots"],
                                           sl=preview["sl"], tp=preview["tp2"], comment="QT manual")
        if not result.success:
            log.error("Manual %s %s failed: %s", preview["direction"], canonical, result.message)
            return {"ok": False, "error": result.message}
        fill = float(result.price or preview["entry"])
        now = utc_now()
        trade_id = self.db.insert_trade(
            mode=broker.mode, symbol=canonical, broker_symbol=symbol, direction=preview["direction"],
            order_type="MARKET", strategy="Manual Execution", setup="manual",
            timeframe=self.settings.ltf, status="OPEN", order_ticket=result.order_ticket,
            position_ticket=result.position_ticket, magic=self.settings.mt5.magic,
            volume=preview["lots"], volume_open=preview["lots"], entry_price=preview["entry"],
            fill_price=fill, sl_initial=preview["sl"], sl_current=preview["sl"], tp1=preview["tp1"],
            tp2=preview["tp2"], risk_amount=preview["risk_amount"],
            risk_percent=preview["risk_percent"], rr_planned=preview["tp2_rr"],
            confluences=["Discretionary"], opened_at=now)
        if result.position_ticket:
            self._register_managed(result.position_ticket, symbol, preview["direction"], fill,
                                   preview["sl"], preview["tp1"], preview["tp2"], trade_id)
        log.info("MANUAL %s %s %.2f lots @ %s (SL %s TP %s)", preview["direction"], canonical,
                 preview["lots"], fill, preview["sl"], preview["tp2"])
        self._emit("trade", f"Manual {preview['direction']} {canonical}",
                   f"{preview['lots']:g} lots @ {fill:.{preview['digits']}f} · SL {preview['sl']} · TP {preview['tp2']}",
                   "success", canonical)
        self._broadcast_manual(canonical, preview, trade_id, fill, "MARKET", now)
        return {"ok": True, "ticket": result.position_ticket, "price": fill, "lots": preview["lots"]}

    def _broadcast_manual(self, canonical: str, preview: dict[str, Any], trade_id: int, entry: float,
                          order_type: str, now: datetime) -> None:
        if not self.settings.telegram.broadcast_manual or self.broker is None:
            return
        spec = self.broker.get_symbol_spec(preview["broker_symbol"])
        payload = {"symbol": canonical, "direction": preview["direction"],
                   "strategy": "Manual Execution (Discretionary)", "timeframe": self.settings.ltf,
                   "entry": entry, "stop_loss": preview["sl"], "tp1": preview["tp1"],
                   "tp2": preview["tp2"], "rr": preview["tp2_rr"], "tp1_rr": preview["tp1_rr"],
                   "order_type": order_type, "lots": preview["lots"],
                   "risk_percent": preview["risk_percent"], "risk_amount": preview["risk_amount"],
                   "digits": spec.digits if spec else preview["digits"],
                   "pip_size": pip_size_for(canonical), "created_at": now, "status": "EXECUTED"}
        self.telegram.broadcast_trade_signal(
            payload, on_sent=lambda res, tid=trade_id: self.db.update_trade(tid, telegram_message_id=res.message_id))

    def _announce_update(self, trade: dict[str, Any], kind: str, data: dict[str, Any], **kwargs: Any) -> None:
        """Trade-update broadcast that honours TELEGRAM_BROADCAST_MANUAL for discretionary trades."""
        if trade.get("setup") == "manual" and not self.settings.telegram.broadcast_manual:
            return
        self.telegram.broadcast_trade_update(kind, data, **kwargs)

    def _place_manual_pending(self, canonical: str, preview: dict[str, Any]) -> dict[str, Any]:
        broker = self.broker
        assert broker is not None
        symbol, kind = preview["broker_symbol"], preview["order_kind"]
        result = broker.place_pending_order(symbol, preview["direction"], kind, preview["entry"],
                                            preview["lots"], sl=preview["sl"], tp=preview["tp2"],
                                            comment="QT manual")
        if not result.success:
            log.error("Manual %s %s %s failed: %s", preview["direction"], kind, canonical, result.message)
            return {"ok": False, "error": result.message}
        trade_id = self.db.insert_trade(
            mode=broker.mode, symbol=canonical, broker_symbol=symbol, direction=preview["direction"],
            order_type=kind, strategy="Manual Execution", setup="manual", timeframe=self.settings.ltf,
            status="PENDING", order_ticket=result.order_ticket, magic=self.settings.mt5.magic,
            volume=preview["lots"], volume_open=preview["lots"], entry_price=preview["entry"],
            sl_initial=preview["sl"], sl_current=preview["sl"], tp1=preview["tp1"], tp2=preview["tp2"],
            risk_amount=preview["risk_amount"], risk_percent=preview["risk_percent"],
            rr_planned=preview["tp2_rr"], confluences=["Discretionary"])
        log.info("MANUAL %s %s %s %.2f lots @ %s (SL %s TP %s)", preview["direction"], kind, canonical,
                 preview["lots"], preview["entry"], preview["sl"], preview["tp2"])
        self._emit("trade", f"{preview['direction']} {kind} placed — {canonical}",
                   f"{preview['lots']:g} lots @ {preview['entry']:.{preview['digits']}f}", "info", canonical)
        self._broadcast_manual(canonical, preview, trade_id, preview["entry"], kind, utc_now())
        return {"ok": True, "pending": True, "order_ticket": result.order_ticket, "price": preview["entry"],
                "lots": preview["lots"]}

    def close_position_manual(self, ticket: int) -> dict[str, Any]:
        if self.broker is None:
            return {"ok": False, "error": "broker unavailable"}
        self._close_reasons[int(ticket)] = "MANUAL"
        result = self.broker.close_position(int(ticket), comment="dashboard close")
        log.info("Dashboard close #%s: %s", ticket, "OK" if result.success else result.message)
        if result.success:
            self._emit("trade", f"Close sent — #{ticket}", "position closed from the dashboard", "info")
        return {"ok": result.success, "error": None if result.success else result.message}

    def modify_position_manual(self, ticket: int, sl: float | None = None,
                               tp: float | None = None) -> dict[str, Any]:
        broker = self.broker
        if broker is None:
            return {"ok": False, "error": "broker unavailable"}
        if sl is None and tp is None:
            return {"ok": False, "error": "nothing to change"}
        for value in (sl, tp):
            if value is not None and float(value) <= 0:
                return {"ok": False, "error": "prices must be positive"}
        result = broker.modify_position(int(ticket), sl=sl, tp=tp)
        if not result.success:
            return {"ok": False, "error": result.message}
        trade = self.db.get_trade_by_position(int(ticket), self.mode)
        if trade is not None:
            changes: dict[str, Any] = {}
            if sl is not None:
                changes["sl_current"] = float(sl)
            if tp is not None:
                changes["tp2"] = float(tp)
            self.db.update_trade(trade["id"], **changes)
        managed = self._managed.get(int(ticket))
        if managed is not None and tp is not None:
            managed.tp2 = float(tp)
        log.info("Dashboard modify #%s: SL %s TP %s", ticket, sl, tp)
        self._emit("trade", f"Stops updated — #{ticket}", f"SL {sl if sl is not None else '—'} · "
                                                          f"TP {tp if tp is not None else '—'}", "info")
        return {"ok": True}

    def cancel_order_manual(self, ticket: int) -> dict[str, Any]:
        broker = self.broker
        if broker is None:
            return {"ok": False, "error": "broker unavailable"}
        result = broker.cancel_order(int(ticket))
        if not result.success:
            return {"ok": False, "error": result.message}
        trade = self.db.get_trade_by_order(int(ticket), self.mode)
        if trade is not None and trade["status"] == "PENDING":
            self.db.update_trade(trade["id"], status="CANCELLED", close_reason="MANUAL", closed_at=utc_now(),
                                 notes="cancelled from the dashboard")
        log.info("Dashboard cancel order #%s", ticket)
        self._emit("trade", f"Order cancelled — #{ticket}", "pending order removed", "warning")
        return {"ok": True}

    def move_to_breakeven(self, ticket: int, source: str = "dashboard") -> dict[str, Any]:
        broker = self.broker
        if broker is None:
            return {"ok": False, "error": "broker unavailable"}
        with self._state_lock:
            position = next((p for p in self._positions_cache if p.ticket == int(ticket)), None)
        if position is None:
            return {"ok": False, "error": f"position {ticket} not found"}
        tick, spec = broker.get_tick(position.symbol), broker.get_symbol_spec(position.symbol)
        if tick is None or spec is None:
            return {"ok": False, "error": "market data unavailable"}
        sign = 1.0 if position.direction == "BUY" else -1.0
        level = spec.normalize_price(position.price_open + sign * self.protector.breakeven_buffer(spec, tick))
        price = tick.bid if position.direction == "BUY" else tick.ask
        if (price - level) * sign <= 0:
            return {"ok": False, "error": "price has not cleared breakeven + buffer yet"}
        if position.sl is not None and (level - position.sl) * sign <= 0:
            return {"ok": False, "error": "stop is already at or beyond breakeven"}
        result = broker.modify_position(position.ticket, sl=level)
        if not result.success:
            return {"ok": False, "error": result.message}
        managed = self._managed.get(position.ticket)
        trade = self.db.get_trade_by_position(position.ticket, self.mode)
        if managed is not None:
            managed.be_moved = True
        if trade is not None:
            self.db.update_trade(trade["id"], sl_current=level, be_moved=True)
            payload = {"symbol": trade["symbol"], "direction": position.direction,
                       "entry": position.price_open, "sl": level, "digits": spec.digits,
                       "pip_size": pip_size_for(trade["symbol"], spec.point, spec.digits),
                       "ticket": position.ticket, "strategy": trade.get("strategy"), "time": utc_now()}
            self.telegram.broadcast_trade_update("BREAKEVEN", payload,
                                                 reply_to=trade.get("telegram_message_id"),
                                                 dedupe_key=f"be:{position.ticket}")
        log.info("Breakeven (%s) on #%s -> %s", source, position.ticket, level)
        self._emit("trade", f"Breakeven — {position.symbol}", f"#{position.ticket} SL → {level}", "info",
                   self._canonical_for(position.symbol))
        return {"ok": True, "sl": level}

    def send_test_signal(self) -> SendResult:
        canonical = self.settings.symbols[0]
        broker = self.broker
        symbol = self._broker_symbol(canonical) if broker else None
        tick = broker.get_tick(symbol) if broker and symbol else None
        spec = broker.get_symbol_spec(symbol) if broker and symbol else None
        atr = self._atr_for(canonical, symbol) if symbol else None
        digits = spec.digits if spec else 2
        entry = tick.ask if tick else 2350.00
        unit = (atr or entry * 0.001) * 1.5
        payload = {"symbol": canonical, "direction": "BUY", "strategy": "SMC Liquidity Sweep + MSS + FVG",
                   "timeframe": f"{self.settings.htf}/{self.settings.mtf}/{self.settings.ltf}",
                   "entry": entry, "stop_loss": entry - unit,
                   "tp1": entry + unit * self.settings.risk.tp1_rr,
                   "tp2": entry + unit * self.settings.risk.tp2_rr, "rr": self.settings.risk.tp2_rr,
                   "tp1_rr": self.settings.risk.tp1_rr, "order_type": "MARKET", "lots": 0.10,
                   "risk_percent": self.runtime.risk_percent, "risk_amount": None, "score": 8.6,
                   "grade": "A+", "confluences": ["SSL Sweep", "MSS", "FVG", "HTF Bullish", "London Killzone"],
                   "digits": digits, "pip_size": pip_size_for(canonical, spec.point if spec else None, digits),
                   "created_at": utc_now(), "status": "SIGNAL", "is_test": True}
        text = format_trade_signal(payload, self.settings.telegram.brand_name, self.mode)
        target = self.telegram.channel_id or self.telegram.admin_chat_id
        return self.telegram.send_now(target, text, "TEST")

    def broadcast_news_now(self) -> SendResult:
        now = utc_now()
        cfg = self.settings.news
        events = self.news.today_events(now, IST, currencies=cfg.alert_currencies,
                                        impacts=cfg.alert_impacts)
        text = format_news_digest(events, now.astimezone(IST).strftime("%a, %d %b %Y"),
                                  self.settings.telegram.brand_name)
        channel = self.telegram.channel_id if self.settings.telegram.news_to_channel else ""
        target = channel or self.telegram.admin_chat_id
        return self.telegram.send_now(target, text, "NEWS_DIGEST")

    def telegram_test_connection(self) -> dict[str, Any]:
        return self.telegram.test_connection()

    def update_telegram(self, token: str | None, channel: str | None, admin: str | None) -> None:
        self.telegram.update_credentials(token or None, channel, admin)

    def refresh_news(self) -> bool:
        return self.news.refresh(force=True)

    # -------------------------------------------------------------- queries --
    def get_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = copy.deepcopy(self._state)
        snapshot["runtime"] = asdict(self.runtime)
        snapshot["telegram"] = self.telegram.stats_snapshot()
        snapshot["settings"] = self.settings.public_summary()
        snapshot["now"] = utc_now().isoformat(timespec="seconds")
        snapshot["last_event_id"] = self.last_event_id
        return snapshot

    def get_analysis(self, canonical: str) -> MarketAnalysis | None:
        with self._state_lock:
            return self._analysis.get(canonical)

    def get_chart_data(self, canonical: str, timeframe: str,
                       bars: int) -> tuple[pd.DataFrame, FeatureSet | None]:
        """Latest candles (incl. the forming one) + SMC features on the closed candles."""
        key = (canonical, timeframe)
        with self._state_lock:
            frame = self._frames.get(key)
        if frame is None or frame.empty:
            cached = self._chart_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < 15.0:
                frame = cached[1]
            elif self.broker is not None and self._get_state("broker_connected"):
                symbol = self._broker_symbol(canonical)
                frame = self.broker.get_rates(symbol, timeframe, max(bars, 120)) if symbol else None
                if frame is not None:
                    self._chart_cache[key] = (time.monotonic(), frame)
        if frame is None or frame.empty:
            return prepare_ohlcv(None), None
        data = prepare_ohlcv(frame).tail(bars)
        closed = closed_bars(data, timeframe, utc_now())
        if len(closed) < 20:
            return data, None
        marker = closed.index[-1]
        cached_features = self._feature_cache.get(key)
        if cached_features is not None and cached_features[0] == marker:
            return data, cached_features[1]
        features = self.strategy.detect_features(closed)
        self._feature_cache[key] = (marker, features)
        return data, features

    def recent_logs(self, limit: int = 300, min_level: str | None = None) -> list[dict[str, Any]]:
        return self.log_handler.tail(limit, min_level)

    # --------------------------------------------------------- web tools --
    def _cached_tool(self, key: tuple[Any, ...], ttl: float, build: Callable[[], Any]) -> Any:
        cached = self._tool_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]
        value = build()
        self._tool_cache[key] = (time.monotonic(), value)
        return value

    def _frame_for(self, canonical: str, timeframe: str, count: int) -> pd.DataFrame | None:
        """Scanner frame when it is long enough, else a (cached) broker fetch; never raises."""
        with self._state_lock:
            frame = self._frames.get((canonical, timeframe))
        if frame is not None and len(frame) >= count:
            return frame
        cached = self._chart_cache.get((canonical, timeframe))
        if cached is not None and time.monotonic() - cached[0] < 60.0 and len(cached[1]) >= count:
            return cached[1]
        broker = self.broker
        if broker is None or not self._get_state("broker_connected"):
            return frame
        try:
            symbol = self._broker_symbol(canonical)
            fetched = broker.get_rates(symbol, timeframe, count) if symbol else None
        except BrokerError as exc:
            self._log_throttled(f"frame-{canonical}-{timeframe}", logging.WARNING,
                                "%s %s candles unavailable: %s", canonical, timeframe, exc)
            return frame
        if fetched is None or fetched.empty:
            return frame
        self._chart_cache[(canonical, timeframe)] = (time.monotonic(), fetched)
        return fetched

    def symbol_specs(self) -> dict[str, Any]:
        """Contract specs + live quotes for the calculators (pip value, margin, lot size)."""
        broker = self.broker
        with self._state_lock:
            account = self._account_cache
            quotes = copy.deepcopy(self._state.get("symbols", {}))
        currency = account.currency if account else self.settings.paper.account_currency
        leverage = int(account.leverage) if account and account.leverage else None
        rows: list[dict[str, Any]] = []
        for canonical in self.settings.symbols:
            row: dict[str, Any] = {"symbol": canonical, "available": False}
            try:
                symbol = self._broker_symbol(canonical) if broker is not None else None
                spec = broker.get_symbol_spec(symbol) if broker is not None and symbol else None
            except BrokerError:
                symbol, spec = None, None
            if spec is not None:
                quote = quotes.get(canonical, {})
                bid, ask = quote.get("bid"), quote.get("ask")
                mid = (bid + ask) / 2.0 if bid and ask else None
                pip = pip_size_for(canonical, spec.point, spec.digits)
                per_unit = spec.tick_value / spec.tick_size if spec.tick_size > 0 else None
                notional = mid * per_unit if mid and per_unit else None
                row.update({"available": True, "broker_symbol": symbol, "digits": spec.digits,
                            "point": spec.point, "tick_size": spec.tick_size,
                            "tick_value": spec.tick_value, "contract_size": spec.contract_size,
                            "volume_min": spec.volume_min, "volume_max": spec.volume_max,
                            "volume_step": spec.volume_step, "pip": pip,
                            "pip_value": per_unit * pip if per_unit else None,
                            "value_per_unit": per_unit, "bid": bid, "ask": ask,
                            "notional_per_lot": notional,
                            "margin_per_lot": notional / leverage if notional and leverage else None,
                            "currency_base": spec.currency_base,
                            "currency_profit": spec.currency_profit,
                            "description": spec.description})
            rows.append(row)
        return {"account_currency": currency, "leverage": leverage,
                "balance": account.balance if account else None,
                "equity": account.equity if account else None, "symbols": rows}

    def sparklines(self, points: int = 96, timeframe: str | None = None) -> dict[str, list[float]]:
        """Recent closes per symbol for watchlist mini-charts (scanner cache only — no I/O)."""
        timeframe = timeframe or self.settings.mtf
        with self._state_lock:
            frames = {c: self._frames.get((c, timeframe)) for c in self.settings.symbols}
        result: dict[str, list[float]] = {}
        for canonical, frame in frames.items():
            if frame is None or frame.empty or "close" not in frame:
                result[canonical] = []
                continue
            closes = frame["close"].astype(float).tail(max(2, int(points)))
            result[canonical] = [round(float(value), 6) for value in closes]
        return result

    def correlation(self, timeframe: str | None = None, bars: int = 200) -> dict[str, Any]:
        timeframe = (timeframe or self.settings.htf).upper()
        bars = max(20, min(int(bars), 1000))

        def build() -> dict[str, Any]:
            closes = {}
            for canonical in self.settings.symbols:
                frame = self._frame_for(canonical, timeframe, bars + 5)
                if frame is not None and not frame.empty and "close" in frame:
                    closes[canonical] = prepare_ohlcv(frame)["close"]
            result = correlation_matrix(closes, bars)
            result["timeframe"] = timeframe
            return result

        return self._cached_tool(("correlation", timeframe, bars), 120.0, build)

    def volatility(self) -> list[dict[str, Any]]:
        """ADR (14 days of H1 bars), today's range used and the strategy-timeframe ATR."""

        def build() -> list[dict[str, Any]]:
            rows = []
            broker = self.broker
            for canonical in self.settings.symbols:
                spec = None
                try:
                    symbol = self._broker_symbol(canonical) if broker is not None else None
                    spec = broker.get_symbol_spec(symbol) if broker is not None and symbol else None
                except BrokerError:
                    spec = None
                pip = pip_size_for(canonical, spec.point if spec else None, spec.digits if spec else None)
                frame = self._frame_for(canonical, "H1", 24 * 16)
                analysis = self.get_analysis(canonical)
                features = getattr(analysis, "features", None)
                atr = float(features.atr_now) if features is not None and features.atr_now else None
                h1 = prepare_ohlcv(frame) if frame is not None else None
                row = volatility_row(canonical, h1, pip, atr)
                row["digits"] = spec.digits if spec else None
                rows.append(row)
            return rows

        return self._cached_tool(("volatility",), 120.0, build)

    def reset_paper_account(self, balance: float | None = None,
                            clear_history: bool = False) -> dict[str, Any]:
        """Paper mode only: flatten the simulated account and restart from a clean balance."""
        broker = self.broker
        if broker is None or broker.mode != "PAPER" or not hasattr(broker, "reset_account"):
            return {"ok": False, "error": "account reset is only available in paper mode"}
        if balance is not None and float(balance) <= 0:
            return {"ok": False, "error": "balance must be positive"}
        now = utc_now()
        broker.reset_account(balance)
        with self._state_lock:
            self._managed.clear()
            self._closing_retries.clear()
            self._known_positions = set()
            self._positions_cache, self._orders_cache = [], []
        purged = {"trades": 0, "equity_points": 0}
        if clear_history:
            purged = self.db.purge_mode_history(broker.mode)
        else:
            for trade in self.db.get_active_trades(broker.mode):
                self.db.update_trade(trade["id"], status="CANCELLED", close_reason="RESET",
                                     closed_at=now, volume_open=0.0, notes="paper account reset")
        account = broker.get_account()
        if account is not None:
            self.dd_guard.rebase(account, now)
            with self._state_lock:
                self._account_cache = account
            self._set_state(account=account.to_dict(), positions=[], orders=[])
        new_balance = account.balance if account else float(balance or 0.0)
        log.warning("Paper account reset to %.2f (history %s)", new_balance,
                    "cleared" if clear_history else "kept")
        self._emit("bot", "Paper account reset", f"new balance {new_balance:,.2f}", "warning")
        return {"ok": True, "balance": new_balance, **purged}


# --------------------------------------------------------------------------- #
# Headless entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless trading worker (no dashboard)")
    parser.add_argument("--start", action="store_true", help="enable automated trading immediately")
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings)
    db = Database(settings.app.db_path)
    worker = TradingWorker(settings, db)
    try:
        worker.start()
    except WorkerLockError as exc:
        log.error("%s", exc)
        db.close()
        return 2
    if args.start:
        worker.start_bot("headless --start")
    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("Signal %s received — shutting down", signum)
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):
                pass
    log.info("Headless worker running (data: %s). Press Ctrl+C to stop.", Path(settings.app.data_dir))
    try:
        while not stop_event.wait(1.0):
            pass
    finally:
        worker.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
