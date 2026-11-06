"""
server.py — Web terminal backend (FastAPI): REST API, WebSocket live feed and the website.

    python server.py                  # http://127.0.0.1:8800  (WEB_HOST / WEB_PORT in .env)
    python server.py --start          # also enable automated trading at boot
    python server.py --port 9000 --no-browser

One TradingWorker runs per process and is file-locked per DATA_DIR, so do not run the Streamlit
dashboard (app.py) or the headless worker against the same data directory at the same time.

Security model
--------------
* Binds to 127.0.0.1 by default. Binding to a network interface requires WEB_PASSWORD
  (enforced in config.py) unless WEB_ALLOW_INSECURE_REMOTE=true (reverse proxy does auth).
* With WEB_PASSWORD set, every API call and the WebSocket need a signed, HttpOnly,
  SameSite=Strict session cookie from POST /api/login (constant-time compare, per-IP lockout).
* State-changing requests must be JSON and same-origin (Origin check) which blocks CSRF; the
  WebSocket handshake is origin-checked as well (cross-site WebSocket hijacking).
* Error responses never contain stack traces — details go to the log (Logs page).

WebSocket protocol (/ws)
------------------------
server → client  {"type": "hello"} · {"type": "snapshot", "data"} · {"type": "events", "items"}
                 {"type": "chart", "data"} (full payload) · {"type": "chart_tick", "data"}
                 {"type": "pong"} · {"type": "error", "error"}
client → server  {"op": "chart", "symbol", "tf", "bars"} · {"op": "unchart"} · {"op": "ping"}
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from analytics import build_report
from background_worker import TradingWorker, WorkerLockError
from chart_data import build_chart_payload
from config import (
    BASE_DIR,
    EXECUTION_MODES,
    IST,
    STRATEGY_CODES,
    TIMEFRAME_MINUTES,
    ConfigError,
    Settings,
    canonical_symbol,
    get_settings,
    setup_logging,
)
from database import Database

log = logging.getLogger("web")

APP_NAME = "Quant Super-Terminal"
APP_VERSION = "2.0.0"
UTC = timezone.utc
WEB_DIR = BASE_DIR / "web"
SESSION_COOKIE = "qst_session"
PAGES = ("dashboard", "trade", "signals", "analytics", "calendar", "tools", "telegram", "settings", "logs")
CHART_TIMEFRAMES = tuple(tf for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1") if tf in TIMEFRAME_MINUTES)
STRATEGIES = {
    "sweep_mss_fvg": ("Liquidity Sweep → MSS → FVG",
                      "Buy-side / sell-side liquidity sweep, market-structure shift on a body close, "
                      "entry at the displacement fair-value gap."),
    "order_block": ("Order Block Retest",
                    "Fresh order block behind a displacement leg — limit entry on the first retest."),
    "time_candle_breakout": ("IST Time-Candle Breakout",
                             "Range of the 8:00 PM / 8:45 PM IST candles — trade the confirmed breakout."),
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'; "
       "frame-ancestors 'none'; form-action 'self'")


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def to_jsonable(value: Any) -> Any:
    """Recursively convert to strict-JSON types (NaN/inf → null, datetimes → ISO strings)."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    item = getattr(value, "item", None)  # numpy scalars
    if callable(item):
        with suppress(Exception):
            return to_jsonable(item())
    tolist = getattr(value, "tolist", None)  # numpy arrays
    if callable(tolist):
        with suppress(Exception):
            return to_jsonable(tolist())
    return str(value)


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")


def reply(data: Any, status: int = 200) -> SafeJSONResponse:
    return SafeJSONResponse(data, status_code=status)


def result_reply(result: Any) -> SafeJSONResponse:
    """Worker actions return {"ok": bool, "error": str|None, ...}; failures map to HTTP 400."""
    data = to_jsonable(result)
    if not isinstance(data, dict):
        data = {"ok": bool(data)}
    data.setdefault("ok", True)
    return reply(data, 200 if data.get("ok") else 400)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
class SessionSigner:
    """Stateless HMAC-SHA256 session tokens: ``<expiry>.<nonce>.<signature>``."""

    def __init__(self, secret: bytes, password: str, max_age_seconds: float):
        # Binding the key to the password invalidates every session when the password changes.
        self._key = hashlib.sha256(secret + b"\x00" + password.encode("utf-8")).digest()
        self.max_age = float(max_age_seconds)
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{int(time.time() + self.max_age)}.{secrets.token_urlsafe(16)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> bool:
        if not token or len(token) > 256:
            return False
        parts = token.split(".")
        if len(parts) != 3:
            return False
        expires, nonce, signature = parts
        try:
            expected = self._sign(f"{expires}.{nonce}")
            expiry = int(expires)
        except (ValueError, UnicodeEncodeError):
            return False
        if not hmac.compare_digest(signature, expected) or expiry <= time.time():
            return False
        with self._lock:
            return nonce not in self._revoked

    def revoke(self, token: str | None) -> None:
        if not token or not self.verify(token):
            return
        expires, nonce, _ = token.split(".")
        now = time.time()
        with self._lock:
            self._revoked = {n: exp for n, exp in self._revoked.items() if exp > now}
            self._revoked[nonce] = float(expires)


class LoginThrottle:
    """Per-client lockout after repeated wrong passwords."""

    def __init__(self, max_failures: int = 5, window_seconds: float = 300.0, lockout_seconds: float = 300.0):
        self.max_failures = max_failures
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> float:
        with self._lock:
            return max(0.0, self._locked_until.get(client, 0.0) - time.monotonic())

    def failure(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            history = self._failures.setdefault(client, deque())
            history.append(now)
            while history and now - history[0] > self.window:
                history.popleft()
            if len(history) >= self.max_failures:
                self._locked_until[client] = now + self.lockout
                history.clear()

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
            self._locked_until.pop(client, None)


def load_session_secret(settings: Settings) -> bytes:
    """WEB_SESSION_SECRET, else a random key persisted in DATA_DIR (0600) so restarts keep sessions."""
    if settings.web.session_secret:
        return settings.web.session_secret.encode("utf-8")
    path = Path(settings.app.data_dir) / "web_session.key"
    with suppress(OSError):
        existing = path.read_text(encoding="ascii").strip()
        if len(existing) >= 32:
            return existing.encode("ascii")
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secret)
    except OSError as exc:
        log.warning("Could not persist the web session key (%s) — sessions end on restart", exc)
    return secret.encode("ascii")


# --------------------------------------------------------------------------- #
# Application context
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    settings: Settings
    worker: TradingWorker
    db: Database
    signer: SessionSigner
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    owns_worker: bool = False
    start_worker: bool = False
    start_bot: bool = False


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


def client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def origin_allowed(headers: Mapping[str, str]) -> bool:
    """Same-origin check: the Origin header (when a browser sends one) must match the Host."""
    origin = headers.get("origin")
    if not origin:
        return True
    if origin == "null":
        return False
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return bool(netloc) and netloc == host


def session_valid(ctx: Context, token: str | None) -> bool:
    return not ctx.settings.web.auth_enabled or ctx.signer.verify(token)


def guard_write(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not origin_allowed(request.headers):
        raise HTTPException(403, "cross-origin request blocked")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "requests must send a JSON body (Content-Type: application/json)")


def require_session(request: Request) -> None:
    if not session_valid(ctx_of(request), request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "login required")


def require_symbol(ctx: Context, raw: str) -> str:
    canonical = canonical_symbol(raw)
    if canonical not in ctx.settings.symbols:
        raise HTTPException(404, f"{raw} is not on the watchlist ({', '.join(ctx.settings.symbols)})")
    return canonical


def require_timeframe(raw: str | None, default: str) -> str:
    timeframe = (raw or default).upper()
    if timeframe not in TIMEFRAME_MINUTES:
        raise HTTPException(422, f"unsupported timeframe {timeframe} ({', '.join(CHART_TIMEFRAMES)})")
    return timeframe


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class Body(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class LoginBody(Body):
    password: str = Field(min_length=1, max_length=256)


class EmptyBody(Body):
    pass


class TradeBody(Body):
    symbol: str = Field(min_length=2, max_length=24)
    direction: Literal["BUY", "SELL"]
    order_kind: Literal["MARKET", "LIMIT", "STOP"] = "MARKET"
    entry_price: float | None = Field(None, gt=0)
    risk_percent: float = Field(gt=0, le=10)
    sl_mode: Literal["atr", "pips", "structure"] = "atr"
    sl_value: float = Field(1.5, ge=0, le=1_000_000)
    tp1_rr: float | None = Field(None, gt=0, le=50)
    tp2_rr: float | None = Field(None, gt=0, le=100)
    override_news: bool = False

    @field_validator("direction", "order_kind", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("sl_mode", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value


class ModifyBody(Body):
    sl: float | None = Field(None, gt=0)
    tp: float | None = Field(None, gt=0)


class RiskBody(Body):
    risk_percent: float = Field(gt=0, le=10)


class ModeBody(Body):
    mode: Literal[EXECUTION_MODES]  # type: ignore[valid-type]


class ToggleBody(Body):
    enabled: bool


class StrategiesBody(Body):
    strategies: list[str] = Field(min_length=1, max_length=len(STRATEGY_CODES))

    @field_validator("strategies")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(STRATEGY_CODES))
        if unknown:
            raise ValueError(f"unknown strategies: {', '.join(unknown)}")
        return list(dict.fromkeys(value))


class TelegramBody(Body):
    token: str | None = Field(None, max_length=200)
    channel: str | None = Field(None, max_length=128)
    admin: str | None = Field(None, max_length=128)


class ConfirmBody(Body):
    confirm: Literal[True]


class ResetBody(Body):
    confirm: Literal[True]
    balance: float | None = Field(None, gt=0, le=1_000_000_000)
    clear_history: bool = False


# --------------------------------------------------------------------------- #
# Data builders
# --------------------------------------------------------------------------- #
def build_chart(ctx: Context, canonical: str, timeframe: str, bars: int,
                snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    worker, settings = ctx.worker, ctx.settings
    snapshot = snapshot if snapshot is not None else worker.get_snapshot()
    quote = (snapshot.get("symbols") or {}).get(canonical) or {}
    frame, features = worker.get_chart_data(canonical, timeframe, bars)
    analysis = worker.get_analysis(canonical) if timeframe == settings.ltf else None
    positions = [p for p in snapshot.get("positions") or [] if p.get("canonical") == canonical]
    orders = [o for o in snapshot.get("orders") or [] if o.get("canonical") == canonical]
    payload = build_chart_payload(
        frame, features, analysis, symbol=canonical, timeframe=timeframe,
        digits=int(quote.get("digits") or 5), ema_period=settings.strategy.ema_period,
        session_windows=settings.strategy.session_ranges, session_tz=settings.strategy.killzone_timezone,
        positions=positions, orders=orders)
    payload.update({"bid": quote.get("bid"), "ask": quote.get("ask"), "bars": bars})
    return payload


def chart_signature(payload: Mapping[str, Any]) -> str:
    candles = payload.get("candles") or []
    core = {key: value for key, value in payload.items()
            if key not in ("candles", "volume", "ema", "last", "bid", "ask")}
    closed = candles[-2]["time"] if len(candles) > 1 else None
    return f"{len(candles)}|{closed}|{dumps(core)}"


def chart_tick(payload: Mapping[str, Any]) -> dict[str, Any]:
    def last(key: str) -> Any:
        items = payload.get(key) or []
        return items[-1] if items else None

    return {"symbol": payload.get("symbol"), "timeframe": payload.get("timeframe"), "candle": last("candles"),
            "volume": last("volume"), "ema": last("ema"), "last": payload.get("last"),
            "bid": payload.get("bid"), "ask": payload.get("ask")}


def downsample(points: list[dict[str, Any]], limit: int = 1500) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled


def build_analytics(ctx: Context) -> dict[str, Any]:
    worker, settings = ctx.worker, ctx.settings
    mode = worker.mode
    trades = ctx.db.get_trades(limit=20000, mode=mode)
    equity = downsample(ctx.db.get_equity_curve(mode=mode, limit=50000))
    snapshot_account = worker.get_snapshot().get("account") or {}
    closed_pnl = sum(float(t.get("pnl") or 0.0) for t in trades if t.get("status") == "CLOSED")
    if equity and equity[0].get("balance"):
        starting = float(equity[0]["balance"])
    elif snapshot_account.get("balance") is not None:
        starting = float(snapshot_account["balance"]) - closed_pnl
    else:
        starting = settings.paper.starting_balance if settings.broker_mode == "paper" else None
    report = build_report(trades, equity, starting, settings.strategy.killzones,
                          settings.strategy.killzone_timezone, IST)
    report["starting_balance"] = starting
    report["currency"] = snapshot_account.get("currency") or settings.paper.account_currency
    report["account"] = snapshot_account
    report["mode"] = mode
    return report


def build_calendar(ctx: Context) -> dict[str, Any]:
    news, symbols = ctx.worker.news, ctx.settings.symbols
    now = datetime.now(UTC)
    events = []
    for event in sorted(news.events(), key=lambda e: (e.time_utc or datetime.max.replace(tzinfo=UTC),
                                                     -e.impact_rank)):
        row = event.to_dict()
        ist = event.time_ist
        row.update({"time_ist": ist.isoformat() if ist else None, "tier1": news.is_tier1(event),
                    "affected": news.affected_symbols(event, symbols),
                    "seconds_until": event.seconds_until(now)})
        events.append(row)
    snapshot = ctx.worker.get_snapshot()
    return {"events": events, "status": news.status(), "locks": snapshot.get("news_locks") or {},
            "window_min": [ctx.settings.news.blackout_before_min, ctx.settings.news.blackout_after_min],
            "now": now}


def build_bootstrap(ctx: Context) -> dict[str, Any]:
    s = ctx.settings
    return {
        "name": APP_NAME, "version": APP_VERSION, "mode": ctx.worker.mode, "broker_mode": s.broker_mode,
        "symbols": list(s.symbols), "timeframes": {"ltf": s.ltf, "mtf": s.mtf, "htf": s.htf},
        "chart_timeframes": list(CHART_TIMEFRAMES),
        "strategies": [{"code": code, "label": STRATEGIES.get(code, (code, ""))[0],
                        "description": STRATEGIES.get(code, (code, ""))[1]} for code in STRATEGY_CODES],
        "execution_modes": list(EXECUTION_MODES),
        "risk": {"risk_percent": s.risk.risk_percent, "max_risk_percent": s.risk.max_risk_percent,
                 "max_daily_loss_percent": s.risk.max_daily_loss_percent, "tp1_rr": s.risk.tp1_rr,
                 "tp2_rr": s.risk.tp2_rr, "max_open_positions": s.risk.max_open_positions,
                 "max_positions_per_symbol": s.risk.max_positions_per_symbol,
                 "max_trades_per_day": s.risk.max_trades_per_day,
                 "breakeven_trigger_r": s.risk.breakeven_trigger_r, "trailing_start_r": s.risk.trailing_start_r,
                 "trailing_mode": s.risk.trailing_mode, "daily_reset_timezone": s.risk.daily_reset_timezone,
                 "daily_reset_hour": s.risk.daily_reset_hour},
        "killzones": [{"name": w.name, "start": w.start.strftime("%H:%M"), "end": w.end.strftime("%H:%M")}
                      for w in s.strategy.killzones],
        "sessions": [{"name": w.name, "start": w.start.strftime("%H:%M"), "end": w.end.strftime("%H:%M")}
                     for w in s.strategy.session_ranges],
        "killzone_tz": s.strategy.killzone_timezone,
        "time_candles_ist": [t.strftime("%H:%M") for t in s.strategy.time_candle_times_ist],
        "chart": {"bars": s.app.chart_bars, "timezone": s.app.chart_timezone,
                  "refresh_seconds": s.app.chart_refresh_seconds, "ema_period": s.strategy.ema_period},
        "news_window_min": [s.news.blackout_before_min, s.news.blackout_after_min],
        "auth_enabled": s.web.auth_enabled,
        "paper": ({"starting_balance": s.paper.starting_balance, "leverage": s.paper.leverage,
                   "currency": s.paper.account_currency} if s.broker_mode == "paper" else None),
    }


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None, *, worker: TradingWorker | None = None,
               db: Database | None = None, owns_worker: bool | None = None,
               start_worker: bool = True, start_bot: bool = False) -> FastAPI:
    """Build the web app. Tests inject a prepared worker with ``start_worker=False``."""
    settings = settings or get_settings()
    if worker is None:
        db = db or Database(settings.app.db_path)
        worker = TradingWorker(settings, db)
        owns_worker = True if owns_worker is None else owns_worker
    db = db or worker.db
    ctx = Context(settings=settings, worker=worker, db=db,
                  signer=SessionSigner(load_session_secret(settings), settings.web.password,
                                       settings.web.session_hours * 3600.0),
                  owns_worker=bool(owns_worker), start_worker=start_worker, start_bot=start_bot)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if ctx.start_worker and not getattr(ctx.worker, "_started", False):
            await asyncio.to_thread(ctx.worker.start)
        if ctx.start_bot:
            await asyncio.to_thread(ctx.worker.start_bot, "web --start")
        log.info("%s %s web terminal ready on http://%s:%s (login %s)", APP_NAME, APP_VERSION,
                 settings.web.host, settings.web.port, "ON" if settings.web.auth_enabled else "OFF")
        try:
            yield
        finally:
            if ctx.owns_worker:
                await asyncio.to_thread(ctx.worker.stop)
                with suppress(Exception):
                    ctx.db.close()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, default_response_class=SafeJSONResponse)
    app.state.ctx = ctx

    # ---------------------------------------------------------- middleware --
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        headers.setdefault("Content-Security-Policy", CSP)
        path = request.url.path
        if path.startswith("/api/"):
            headers["Cache-Control"] = "no-store"
        elif path.startswith(("/fonts/", "/vendor/")):
            headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
        else:
            headers.setdefault("Cache-Control", "no-cache")
        return response

    # ------------------------------------------------------------- errors --
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        response = reply({"ok": False, "error": detail}, exc.status_code)
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        problems = []
        for error in exc.errors()[:4]:
            location = ".".join(str(part) for part in error.get("loc", ())[1:]) or "body"
            problems.append(f"{location}: {error.get('msg', 'invalid value')}")
        return reply({"ok": False, "error": "invalid request — " + "; ".join(problems)}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return reply({"ok": False, "error": "internal error — details were written to the Logs page"}, 500)

    # --------------------------------------------------------- public API --
    public = APIRouter(prefix="/api", dependencies=[Depends(guard_write)])

    @public.get("/health")
    def health() -> SafeJSONResponse:
        return reply({"ok": True, "name": APP_NAME, "version": APP_VERSION})

    @public.get("/session")
    def session_state(request: Request) -> SafeJSONResponse:
        return reply({"auth_enabled": settings.web.auth_enabled,
                      "authenticated": session_valid(ctx, request.cookies.get(SESSION_COOKIE))})

    @public.post("/login")
    def login(body: LoginBody, request: Request) -> SafeJSONResponse:
        client = client_id(request)
        if not settings.web.auth_enabled:
            return reply({"ok": True, "auth_enabled": False})
        wait = ctx.throttle.retry_after(client)
        if wait > 0:
            raise HTTPException(429, f"too many failed attempts — try again in {math.ceil(wait / 60)} min",
                                headers={"Retry-After": str(math.ceil(wait))})
        if not hmac.compare_digest(body.password.encode("utf-8"), settings.web.password.encode("utf-8")):
            ctx.throttle.failure(client)
            log.warning("Failed web login from %s", client)
            raise HTTPException(401, "incorrect password")
        ctx.throttle.success(client)
        response = reply({"ok": True})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(SESSION_COOKIE, ctx.signer.issue(), max_age=int(ctx.signer.max_age),
                            httponly=True, samesite="strict", secure=secure, path="/")
        log.info("Web login from %s", client)
        return response

    @public.post("/logout")
    def logout(request: Request) -> SafeJSONResponse:
        ctx.signer.revoke(request.cookies.get(SESSION_COOKIE))
        response = reply({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ------------------------------------------------------ protected API --
    api = APIRouter(prefix="/api", dependencies=[Depends(guard_write), Depends(require_session)])

    @api.get("/bootstrap")
    def bootstrap() -> SafeJSONResponse:
        return reply(build_bootstrap(ctx))

    @api.get("/snapshot")
    def snapshot() -> SafeJSONResponse:
        return reply(ctx.worker.get_snapshot())

    @api.get("/events")
    def events(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=400)) -> SafeJSONResponse:
        return reply({"items": ctx.worker.events_since(after, limit), "last_id": ctx.worker.last_event_id})

    @api.get("/chart")
    def chart(symbol: str = Query(min_length=2, max_length=24), tf: str | None = Query(None, max_length=4),
              bars: int = Query(300, ge=50, le=2000)) -> SafeJSONResponse:
        canonical = require_symbol(ctx, symbol)
        return reply(build_chart(ctx, canonical, require_timeframe(tf, settings.ltf), bars))

    @api.get("/sparklines")
    def sparklines(points: int = Query(96, ge=8, le=500)) -> SafeJSONResponse:
        return reply({"timeframe": settings.mtf, "series": ctx.worker.sparklines(points)})

    @api.get("/trades")
    def trades(status: str | None = Query(None, max_length=16), symbol: str | None = Query(None, max_length=24),
               limit: int = Query(500, ge=1, le=5000)) -> SafeJSONResponse:
        canonical = require_symbol(ctx, symbol) if symbol else None
        rows = ctx.db.get_trades(limit=limit, status=status.upper() if status else None, symbol=canonical,
                                 mode=ctx.worker.mode)
        return reply({"items": rows, "mode": ctx.worker.mode})

    @api.get("/signals")
    def signals(limit: int = Query(200, ge=1, le=2000)) -> SafeJSONResponse:
        return reply({"items": ctx.db.get_recent_signals(limit)})

    @api.get("/analytics")
    def analytics() -> SafeJSONResponse:
        return reply(build_analytics(ctx))

    @api.get("/calendar")
    def calendar() -> SafeJSONResponse:
        return reply(build_calendar(ctx))

    @api.get("/telegram")
    def telegram(limit: int = Query(150, ge=1, le=1000)) -> SafeJSONResponse:
        return reply({"stats": ctx.worker.telegram.stats_snapshot(),
                      "broadcasts": ctx.db.get_recent_broadcasts(limit)})

    @api.get("/logs")
    def logs(limit: int = Query(400, ge=1, le=1500),
             level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None) -> SafeJSONResponse:
        return reply({"items": ctx.worker.recent_logs(limit, level)})

    @api.get("/tools/specs")
    def tool_specs() -> SafeJSONResponse:
        return reply(ctx.worker.symbol_specs())

    @api.get("/tools/correlation")
    def tool_correlation(tf: str | None = Query(None, max_length=4),
                         bars: int = Query(200, ge=20, le=1000)) -> SafeJSONResponse:
        return reply(ctx.worker.correlation(require_timeframe(tf, settings.htf), bars))

    @api.get("/tools/volatility")
    def tool_volatility() -> SafeJSONResponse:
        return reply({"items": ctx.worker.volatility()})

    # bot control ---------------------------------------------------------
    @api.post("/bot/start")
    def bot_start(_body: EmptyBody) -> SafeJSONResponse:
        return reply({"ok": True, "status": ctx.worker.start_bot("web")})

    @api.post("/bot/pause")
    def bot_pause(_body: EmptyBody) -> SafeJSONResponse:
        return reply({"ok": True, "status": ctx.worker.pause_bot("web")})

    @api.post("/bot/panic")
    def bot_panic(_body: ConfirmBody) -> SafeJSONResponse:
        return result_reply(ctx.worker.panic_close_all("web"))

    # settings ------------------------------------------------------------
    @api.post("/settings/risk")
    def set_risk(body: RiskBody) -> SafeJSONResponse:
        return reply({"ok": True, "risk_percent": ctx.worker.set_risk_percent(body.risk_percent)})

    @api.post("/settings/execution")
    def set_execution(body: ModeBody) -> SafeJSONResponse:
        ctx.worker.set_execution_mode(body.mode)
        return reply({"ok": True, "execution_mode": ctx.worker.runtime.execution_mode})

    @api.post("/settings/news-pause")
    def set_news_pause(body: ToggleBody) -> SafeJSONResponse:
        ctx.worker.set_news_auto_pause(body.enabled)
        return reply({"ok": True, "enabled": ctx.worker.runtime.news_auto_pause})

    @api.post("/settings/killzone")
    def set_killzone(body: ToggleBody) -> SafeJSONResponse:
        ctx.worker.set_require_killzone(body.enabled)
        return reply({"ok": True, "enabled": ctx.worker.runtime.require_killzone})

    @api.post("/settings/strategies")
    def set_strategies(body: StrategiesBody) -> SafeJSONResponse:
        ctx.worker.set_enabled_strategies(body.strategies)
        return reply({"ok": True, "strategies": list(ctx.worker.runtime.enabled_strategies)})

    @api.post("/settings/telegram")
    def set_telegram(body: TelegramBody) -> SafeJSONResponse:
        ctx.worker.update_telegram(body.token, body.channel, body.admin)
        return reply({"ok": True, "stats": ctx.worker.telegram.stats_snapshot()})

    # manual trading ------------------------------------------------------
    def trade_args(body: TradeBody) -> dict[str, Any]:
        return {"canonical": require_symbol(ctx, body.symbol), "direction": body.direction,
                "risk_percent": body.risk_percent, "sl_mode": body.sl_mode, "sl_value": body.sl_value,
                "tp1_rr": body.tp1_rr, "tp2_rr": body.tp2_rr, "order_kind": body.order_kind,
                "entry_price": body.entry_price}

    @api.post("/trade/preview")
    def trade_preview(body: TradeBody) -> SafeJSONResponse:
        return reply(ctx.worker.preview_manual_trade(**trade_args(body)))

    @api.post("/trade/place")
    def trade_place(body: TradeBody) -> SafeJSONResponse:
        return result_reply(ctx.worker.manual_trade(**trade_args(body), override_news=body.override_news))

    @api.post("/positions/{ticket}/close")
    def position_close(_body: EmptyBody, ticket: int = PathParam(gt=0)) -> SafeJSONResponse:
        return result_reply(ctx.worker.close_position_manual(ticket))

    @api.post("/positions/{ticket}/breakeven")
    def position_breakeven(_body: EmptyBody, ticket: int = PathParam(gt=0)) -> SafeJSONResponse:
        return result_reply(ctx.worker.move_to_breakeven(ticket, "web"))

    @api.post("/positions/{ticket}/modify")
    def position_modify(body: ModifyBody, ticket: int = PathParam(gt=0)) -> SafeJSONResponse:
        return result_reply(ctx.worker.modify_position_manual(ticket, body.sl, body.tp))

    @api.post("/orders/{ticket}/cancel")
    def order_cancel(_body: EmptyBody, ticket: int = PathParam(gt=0)) -> SafeJSONResponse:
        return result_reply(ctx.worker.cancel_order_manual(ticket))

    # telegram / news / paper ----------------------------------------------
    @api.post("/telegram/test-connection")
    def telegram_test(_body: EmptyBody) -> SafeJSONResponse:
        return reply(ctx.worker.telegram_test_connection())

    @api.post("/telegram/test-signal")
    def telegram_test_signal(_body: EmptyBody) -> SafeJSONResponse:
        return result_reply(ctx.worker.send_test_signal())

    @api.post("/telegram/news-digest")
    def telegram_news_digest(_body: EmptyBody) -> SafeJSONResponse:
        return result_reply(ctx.worker.broadcast_news_now())

    @api.post("/news/refresh")
    def news_refresh(_body: EmptyBody) -> SafeJSONResponse:
        refreshed = ctx.worker.refresh_news()
        return reply({"ok": True, "refreshed": refreshed, "status": ctx.worker.news.status()})

    @api.post("/paper/reset")
    def paper_reset(body: ResetBody) -> SafeJSONResponse:
        return result_reply(ctx.worker.reset_paper_account(body.balance, body.clear_history))

    app.include_router(public)
    app.include_router(api)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest: str) -> SafeJSONResponse:
        return reply({"ok": False, "error": f"unknown endpoint /api/{rest}"}, 404)

    # ----------------------------------------------------------- websocket --
    @app.websocket("/ws")
    async def live_feed(websocket: WebSocket) -> None:
        if not origin_allowed(websocket.headers):
            await websocket.close(code=1008)
            return
        if not session_valid(ctx, websocket.cookies.get(SESSION_COOKIE)):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        send_lock = asyncio.Lock()
        wake = asyncio.Event()
        sub: dict[str, Any] = {"chart": None, "signature": None, "due": 0.0}

        async def send(message: dict[str, Any]) -> None:
            text = dumps(message)
            async with send_lock:
                await websocket.send_text(text)

        async def reader() -> None:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 4096:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == "chart":
                    try:
                        canonical = require_symbol(ctx, str(message.get("symbol") or ""))
                        timeframe = require_timeframe(message.get("tf"), settings.ltf)
                        bars = max(50, min(int(message.get("bars") or settings.app.chart_bars), 2000))
                    except (HTTPException, TypeError, ValueError) as exc:
                        await send({"type": "error", "error": getattr(exc, "detail", "invalid chart request")})
                        continue
                    sub.update(chart=(canonical, timeframe, bars), signature=None, due=0.0)
                    wake.set()
                elif op == "unchart":
                    sub.update(chart=None, signature=None)
                elif op == "ping":
                    await send({"type": "pong", "ts": time.time()})

        reader_task = asyncio.create_task(reader())
        try:
            after = int(websocket.query_params.get("after") or 0)
        except ValueError:
            after = 0
        last_event = after if after > 0 else max(0, ctx.worker.last_event_id - 25)
        interval = max(0.25, min(settings.app.ui_refresh_seconds, 1.0))
        try:
            await send({"type": "hello", "name": APP_NAME, "version": APP_VERSION, "interval": interval})
            while not reader_task.done():
                snapshot_data = await asyncio.to_thread(ctx.worker.get_snapshot)
                await send({"type": "snapshot", "data": snapshot_data})
                items = ctx.worker.events_since(last_event, 60)
                if items:
                    last_event = items[-1]["id"]
                    await send({"type": "events", "items": items})
                chart_sub = sub["chart"]
                if chart_sub is not None and time.monotonic() >= sub["due"]:
                    sub["due"] = time.monotonic() + settings.app.chart_refresh_seconds
                    payload = await asyncio.to_thread(build_chart, ctx, *chart_sub, snapshot_data)
                    if sub["chart"] == chart_sub:
                        signature = chart_signature(payload)
                        if signature != sub["signature"]:
                            sub["signature"] = signature
                            await send({"type": "chart", "data": payload})
                        else:
                            await send({"type": "chart_tick", "data": chart_tick(payload)})
                wake.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=interval)
        except Exception as exc:  # noqa: BLE001 - disconnects surface as assorted exception types
            if not reader_task.done():
                log.debug("live feed closed: %s", exc)
        finally:
            reader_task.cancel()
            with suppress(BaseException):
                await reader_task

    # --------------------------------------------------------------- pages --
    def html_page(name: str) -> HTMLResponse:
        text = (WEB_DIR / name).read_text(encoding="utf-8").replace("__APP_VERSION__", APP_VERSION)
        return HTMLResponse(text, headers={"Cache-Control": "no-cache"})

    def safe_next(raw: str | None) -> str:
        if raw and raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
            return raw
        return "/"

    def app_shell(request: Request) -> Response:
        if not session_valid(ctx, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
        return html_page("index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request, next: str | None = None) -> Response:  # noqa: A002 - query name
        if not settings.web.auth_enabled or session_valid(ctx, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(safe_next(next), status_code=303)
        return html_page("login.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> Response:
        return RedirectResponse("/favicon.svg", status_code=301)

    for mount in ("assets", "vendor", "fonts"):
        directory = WEB_DIR / mount
        if directory.is_dir():
            app.mount(f"/{mount}", StaticFiles(directory=directory), name=mount)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> Response:
        return app_shell(request)

    @app.get("/{page}", include_in_schema=False)
    def page(page: str, request: Request) -> Response:
        if page not in PAGES:
            if "." in page:
                raise HTTPException(404, "not found")
            return app_shell(request)  # the SPA renders its own 404 view
        return app_shell(request)

    return app


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} web terminal")
    parser.add_argument("--start", action="store_true", help="enable automated trading immediately")
    parser.add_argument("--host", help="bind address (default WEB_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default WEB_PORT or 8800)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        if args.host or args.port:
            web = replace(settings.web, host=args.host or settings.web.host, port=args.port or settings.web.port)
            if not web.is_loopback and not web.auth_enabled and not web.allow_insecure_remote:
                raise ConfigError(f"--host {web.host} exposes the terminal — set WEB_PASSWORD in .env first")
            settings = replace(settings, web=web)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed — run: pip install -r requirements.txt", file=sys.stderr)
        return 2
    db = Database(settings.app.db_path)
    worker = TradingWorker(settings, db)
    try:
        worker.start()  # fail fast (single-instance lock) before binding the port
    except WorkerLockError as exc:
        print(f"{exc}", file=sys.stderr)
        db.close()
        return 2
    app = create_app(settings, worker=worker, db=db, owns_worker=True, start_worker=False, start_bot=args.start)
    host = settings.web.host
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{settings.web.port}/"
    if settings.web.open_browser and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  {APP_NAME} {APP_VERSION} → {url}  (Ctrl+C to stop)\n", flush=True)
    try:
        uvicorn.run(app, host=host, port=settings.web.port, log_config=None, access_log=False,
                    log_level=settings.app.log_level.lower(), ws="websockets", ws_max_size=65536,
                    timeout_graceful_shutdown=10)
    finally:
        if getattr(worker, "_started", False):
            worker.stop()
        with suppress(Exception):
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
