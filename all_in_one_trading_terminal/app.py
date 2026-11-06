"""
app.py — All-in-One Forex & Crypto Algorithmic Trading Super-Terminal (dashboard).

Dark institutional Streamlit UI on top of the background trading worker:
  * Header + telemetry — mode, active killzone, next red-folder event, IST / New York / UTC
                         clocks, balance, equity, floating P&L, daily-drawdown meter, bot
                         status and broker link.
  * Market watch       — live bid, spread, 24h change, H1 bias and news lock per symbol.
  * Control deck       — Start / Pause / PANIC, execution mode, risk %, strategy toggles,
                         one-click manual execution pad, Telegram channel control panel.
  * Main chart         — TradingView Lightweight Charts with FVG / OB zones, liquidity sweeps,
                         BOS/MSS markers, session highs/lows, EMA, live position entry / SL /
                         TP lines and detected setups.
  * News dock          — Forex Factory terminal in IST, countdown, auto-pause toggle, locks.
  * Analytics tabs     — positions manager, trade journal + metrics, signals, live system
                         logs and the Telegram broadcast log.

Every panel runs behind an error boundary: a failing panel shows a short notice and logs the
details instead of breaking the page.  Run with ``run_terminal.bat`` or ``streamlit run app.py``.
"""

from __future__ import annotations

import bisect
import functools
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trading Super-Terminal", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

from streamlit_lightweight_charts import renderLightweightCharts  # noqa: E402

from background_worker import TradingWorker, WorkerLockError  # noqa: E402
from config import IST, NEW_YORK, STRATEGY_CODES, ConfigError, get_settings, setup_logging  # noqa: E402
from database import Database, parse_iso  # noqa: E402
from forex_factory_engine import ForexFactoryEngine, format_countdown  # noqa: E402
from quant_strategy_engine import (  # noqa: E402
    FeatureSet,
    MarketAnalysis,
    active_killzones,
    closed_bars,
    compute_ema,
    compute_session_ranges,
)

UTC = timezone.utc
LOG = logging.getLogger("dashboard")
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
OVERLAYS = ["FVG", "Order Blocks", "Liquidity Sweeps", "Structure", "Sessions", "EMA", "Positions",
            "Signals", "Volume"]
DEFAULT_OVERLAYS = ["FVG", "Order Blocks", "Liquidity Sweeps", "Structure", "Sessions", "Positions",
                    "Signals", "Volume"]
STRATEGY_LABELS = {"sweep_mss_fvg": "Sweep + MSS + FVG", "order_block": "OB Retest",
                   "time_candle_breakout": "IST Candle Break"}
STATUS_CLASS = {"RUNNING": "badge-running", "PAUSED": "badge-paused", "STARTING": "badge-paused",
                "NEWS LOCKED": "badge-locked", "HALTED": "badge-locked",
                "DISCONNECTED": "badge-offline"}
SESSION_COLORS = {"ASIA": "#eab308", "LONDON": "#3b82f6", "NEW_YORK": "#f97316", "NY_AM": "#f97316"}
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"]
IMPACT_LABELS = {"High": "🔴 High", "Medium": "🟠 Medium", "Low": "🟡 Low", "Holiday": "⚪ Holiday"}
UP, DOWN, FLAT = "#16c784", "#ea3943", "#8b98ad"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap');
:root {
  --bg:#070b14; --panel:#0e1524; --panel-2:#111a2c; --border:#1c2638; --border-2:#273349;
  --text:#e6edf6; --muted:#8b98ad; --dim:#5d6b82; --green:#16c784; --red:#ea3943;
  --amber:#f5a524; --cyan:#22d3ee; --blue:#3b82f6; --violet:#a78bfa;
  --mono:'JetBrains Mono','Cascadia Mono','Consolas',monospace;
  --sans:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
}
html, body, .stApp, .stMarkdown, button, input, textarea, label { font-family: var(--sans); }
.stApp { background:
    radial-gradient(1100px 520px at 8% -14%, rgba(34,211,238,.08), transparent 62%),
    radial-gradient(900px 480px at 100% -6%, rgba(167,139,250,.07), transparent 58%), var(--bg); }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: .7rem; padding-bottom: 2.2rem; max-width: 100% !important; }
#MainMenu, footer { visibility: hidden; }

/* panels ------------------------------------------------------------------ */
[class*="st-key-panel_"] { background: linear-gradient(180deg, rgba(17,26,44,.92), rgba(10,16,28,.92));
  border: 1px solid var(--border) !important; border-radius: 14px !important; }
.tt-section { display:flex; align-items:center; gap:8px; color:#cbd5e1; font-size:.7rem; font-weight:800;
  letter-spacing:.14em; text-transform:uppercase; margin:.1rem 0 .45rem 0; }
.tt-section::before { content:""; width:3px; height:13px; border-radius:2px; background:var(--cyan); }
.tt-hint { color:var(--dim); font-size:.72rem; margin-top:-.15rem; }

/* header -------------------------------------------------------------------- */
.tt-header { display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap;
  margin-bottom:.6rem; }
.tt-brand { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.tt-title { font-size:1.12rem; font-weight:800; letter-spacing:.06em; color:#f8fafc; }
.tt-title span { background:linear-gradient(90deg,#22d3ee,#a78bfa); -webkit-background-clip:text;
  background-clip:text; color:transparent; }
.tt-meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-family:var(--mono);
  font-size:.78rem; color:var(--muted); }
.tt-meta b { color:#e2e8f0; font-weight:600; }
.pill { display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px;
  font-size:.7rem; font-weight:700; letter-spacing:.05em; border:1px solid var(--border-2);
  background:rgba(15,23,42,.7); color:#cbd5e1; font-family:var(--sans); }
.pill-mode { color:#67e8f9; border-color:rgba(34,211,238,.45); background:rgba(34,211,238,.1); }
.pill-live { color:#fca5a5; border-color:rgba(234,57,67,.5); background:rgba(234,57,67,.12); }
.pill-kz { color:#c4b5fd; border-color:rgba(167,139,250,.45); background:rgba(167,139,250,.1); }
.pill-news { color:#fca5a5; border-color:rgba(234,57,67,.35); }

/* telemetry cards ------------------------------------------------------------ */
.tt-card { position:relative; height:112px; display:flex; flex-direction:column; justify-content:space-between;
  background:linear-gradient(180deg,#101a2c 0%,#0b1220 100%); border:1px solid var(--border);
  border-radius:14px; padding:11px 14px 10px 16px; overflow:hidden; }
.tt-card::before { content:""; position:absolute; left:0; top:12px; bottom:12px; width:3px; border-radius:0 3px 3px 0;
  background:var(--accent, var(--cyan)); }
.tt-label { color:var(--muted); font-size:.66rem; font-weight:700; text-transform:uppercase; letter-spacing:.11em; }
.tt-value { font-family:var(--mono); font-size:1.3rem; font-weight:700; color:var(--text); line-height:1.15; }
.tt-sub { color:var(--muted); font-size:.71rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pos { color:var(--green) !important; } .neg { color:var(--red) !important; } .muted { color:var(--muted); }
.meter { height:6px; background:#1b2537; border-radius:6px; overflow:hidden; }
.meter > div { height:100%; border-radius:6px; }
.badge { display:inline-block; padding:4px 12px; border-radius:999px; font-weight:800; font-size:.78rem;
  letter-spacing:.06em; font-family:var(--sans); }
.badge-running { background:rgba(22,199,132,.14); color:#34d399; border:1px solid rgba(22,199,132,.55);
  animation: glow 2.2s ease-in-out infinite; }
.badge-paused { background:rgba(245,165,36,.13); color:#fbbf24; border:1px solid rgba(245,165,36,.45); }
.badge-locked { background:rgba(234,57,67,.15); color:#f87171; border:1px solid rgba(234,57,67,.55); }
.badge-offline { background:rgba(139,152,173,.12); color:#cbd5e1; border:1px solid rgba(139,152,173,.4); }
@keyframes glow { 0%,100% { box-shadow:0 0 0 0 rgba(22,199,132,.0); } 50% { box-shadow:0 0 14px 0 rgba(22,199,132,.35); } }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:7px; vertical-align:middle; }
.dot-on { background:var(--green); box-shadow:0 0 10px var(--green); } .dot-off { background:var(--red); }

/* market watch ----------------------------------------------------------------- */
.mw { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:8px; margin:0 0 .55rem 0; }
.mw-item { background:linear-gradient(180deg,#0f1828,#0b1220); border:1px solid var(--border); border-radius:12px;
  padding:8px 12px; }
.mw-item.active { border-color:rgba(34,211,238,.55); box-shadow:0 0 0 1px rgba(34,211,238,.18) inset; }
.mw-top { display:flex; justify-content:space-between; align-items:center; font-size:.72rem; font-weight:700;
  letter-spacing:.06em; color:#cbd5e1; }
.mw-px { font-family:var(--mono); font-size:1.05rem; font-weight:700; color:var(--text); margin-top:2px; }
.mw-sub { display:flex; justify-content:space-between; font-size:.68rem; color:var(--muted); margin-top:2px; }
.mw-bias { font-size:.64rem; font-weight:800; padding:1px 6px; border-radius:6px; }
.bias-BULLISH { color:#34d399; background:rgba(22,199,132,.12); }
.bias-BEARISH { color:#f87171; background:rgba(234,57,67,.12); }
.bias-NEUTRAL { color:#94a3b8; background:rgba(148,163,184,.1); }

/* news ------------------------------------------------------------------------- */
.ff-wrap { max-height:420px; overflow-y:auto; border:1px solid var(--border); border-radius:12px; }
.ff-table { width:100%; border-collapse:collapse; font-size:.73rem; }
.ff-table th { color:var(--muted); text-align:left; font-weight:700; padding:7px 6px; position:sticky; top:0;
  background:#0d1523; border-bottom:1px solid var(--border); z-index:1; font-size:.66rem;
  text-transform:uppercase; letter-spacing:.08em; }
.ff-table td { padding:6px 6px; border-bottom:1px solid rgba(28,38,56,.8); vertical-align:top; }
.ff-High td:first-child { box-shadow: inset 3px 0 0 #ea3943; } .ff-High { background:rgba(234,57,67,.06); }
.ff-Medium td:first-child { box-shadow: inset 3px 0 0 #f5a524; } .ff-Medium { background:rgba(245,165,36,.045); }
.ff-Low td:first-child { box-shadow: inset 3px 0 0 #eab308; }
.ff-Holiday td:first-child, .ff-Non-Economic td:first-child { box-shadow: inset 3px 0 0 #64748b; }
.ff-past td { opacity:.4; }
.ff-time { font-family:var(--mono); white-space:nowrap; }
.imp { display:inline-block; width:9px; height:9px; border-radius:2px; }
.imp-High { background:#ea3943; } .imp-Medium { background:#f5a524; } .imp-Low { background:#eab308; }
.imp-Holiday, .imp-Non-Economic { background:#64748b; }
.countdown-card { background:linear-gradient(135deg,rgba(127,29,29,.55),rgba(14,21,36,.95));
  border:1px solid rgba(234,57,67,.45); border-radius:14px; padding:11px 14px; margin-bottom:8px; }
.countdown { font-family:var(--mono); font-size:1.6rem; font-weight:800; color:#fecaca; letter-spacing:.02em; }
.chip { display:inline-block; padding:2px 9px; margin:2px 4px 2px 0; border-radius:8px; font-size:.69rem;
  border:1px solid var(--border-2); }
.chip-lock { color:#fca5a5; border-color:rgba(234,57,67,.55); background:rgba(234,57,67,.08); }
.chip-clear { color:#6ee7b7; border-color:rgba(22,199,132,.4); background:rgba(22,199,132,.05); }
.chip-info { color:#cbd5e1; }

/* manual pad --------------------------------------------------------------------- */
.preview { width:100%; font-size:.75rem; border-collapse:collapse; margin:.15rem 0 .5rem 0; }
.preview td, .preview th { padding:4px 6px; border-bottom:1px solid rgba(28,38,56,.9); }
.preview th { color:var(--muted); text-align:left; font-weight:700; font-size:.66rem; letter-spacing:.08em;
  text-transform:uppercase; }
.preview td { font-family:var(--mono); }

/* logs / signal card --------------------------------------------------------------- */
.log-box { font-family:var(--mono); font-size:.71rem; max-height:430px; overflow-y:auto; background:#05080f;
  border:1px solid var(--border); border-radius:12px; padding:9px 11px; line-height:1.5; }
.lv-WARNING { color:#fbbf24; } .lv-ERROR, .lv-CRITICAL { color:#f87171; } .lv-INFO { color:#cbd5e1; }
.lv-DEBUG { color:#64748b; }
.signal-card { border:1px solid var(--border-2); border-radius:12px; padding:9px 13px;
  background:linear-gradient(180deg,#101a2c,#0c1423); margin-top:6px; }
.footer-note { color:var(--dim); font-size:.72rem; text-align:center; margin-top:1rem; }

/* buttons ------------------------------------------------------------------------- */
.st-key-btn_start button { background:linear-gradient(180deg,#139a65,#0d6e48) !important;
  border:1px solid #16c784 !important; color:#ecfdf5 !important; font-weight:700 !important; }
.st-key-btn_pause button { background:linear-gradient(180deg,#3a2a08,#271c05) !important;
  border:1px solid rgba(245,165,36,.65) !important; color:#fcd34d !important; font-weight:700 !important; }
.st-key-btn_panic button { background:linear-gradient(180deg,#c0262d,#7f1d1d) !important;
  border:1px solid #ef4444 !important; color:#fff !important; font-weight:800 !important; letter-spacing:.05em; }
.st-key-btn_panic button:disabled { opacity:.42; }
.st-key-btn_buy button { background:linear-gradient(180deg,#139a65,#0d6e48) !important;
  border:1px solid #16c784 !important; color:#ecfdf5 !important; font-weight:800 !important; }
.st-key-btn_sell button { background:linear-gradient(180deg,#c0262d,#831c22) !important;
  border:1px solid #ea3943 !important; color:#fef2f2 !important; font-weight:800 !important; }
.st-key-btn_buy button:disabled, .st-key-btn_sell button:disabled { opacity:.45; }
div[data-testid="stMetric"] { background:linear-gradient(180deg,#101a2c,#0b1220); }
</style>
"""


# --------------------------------------------------------------------------- #
# Engine bootstrap (one worker per Streamlit server process)
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner="Booting trading engine…")
def boot_worker() -> TradingWorker:
    settings = get_settings()
    setup_logging(settings)
    db = Database(settings.app.db_path)
    worker = TradingWorker(settings, db)
    try:
        worker.start()
    except Exception:
        db.close()
        raise
    return worker


try:
    WORKER = boot_worker()
except WorkerLockError as exc:
    st.error(f"🔒 {exc}")
    st.info("Only one trading engine may run per data folder. Stop the headless worker (Ctrl+C) "
            "or close the other dashboard window, then refresh this page.")
    st.stop()
except ConfigError as exc:
    st.error(f"⚙️ Configuration error: {exc}")
    st.info("Fix the value in .env and restart run_terminal.bat.")
    st.stop()

SETTINGS = WORKER.settings
CHART_TZ = ZoneInfo(SETTINGS.app.chart_timezone)
UI_REFRESH = float(SETTINGS.app.ui_refresh_seconds)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def guarded(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Error boundary: a failing panel logs and shows a notice instead of breaking the page.

    Streamlit's rerun/stop signals derive from BaseException, so they pass through untouched.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary by design
                LOG.exception("Dashboard panel '%s' failed", name)
                st.warning(f"⚠️ {name} is temporarily unavailable ({type(exc).__name__}). "
                           "It retries automatically; details are in System Logs.")
                return None
        return wrapper
    return decorator


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Any, currency: str = "USD", signed: bool = False) -> str:
    if value is None:
        return "—"
    amount = float(value)
    sign = ("+" if amount > 0 else "-" if amount < 0 else "") if signed else ("-" if amount < 0 else "")
    prefix = "$" if currency == "USD" else ""
    suffix = "" if currency == "USD" else f" {currency}"
    return f"{sign}{prefix}{abs(amount):,.2f}{suffix}"


def price(value: Any, digits: int) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{int(digits)}f}"
    except (TypeError, ValueError):
        return "—"


def ist(value: Any, fmt: str = "%d %b %H:%M:%S") -> str:
    moment = parse_iso(value) if not isinstance(value, datetime) else value
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(IST).strftime(fmt)


def tone(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 0:
        return f"color:{UP};font-weight:600"
    if number < 0:
        return f"color:{DOWN};font-weight:600"
    return f"color:{FLAT}"


def side_tone(value: Any) -> str:
    return {"BUY": f"color:{UP};font-weight:700", "SELL": f"color:{DOWN};font-weight:700"}.get(str(value), "")


def smart_price(value: Any) -> str:
    """Price with instrument-appropriate decimals: gold/BTC 2, JPY/DXY 3, FX majors 5."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    size = abs(number)
    decimals = 2 if size >= 1000 else 3 if size >= 100 else 4 if size >= 10 else 5
    return f"{number:,.{decimals}f}"


def table_height(rows: int, cap: int = 380) -> int:
    """Fit small tables to their rows instead of showing an empty grid."""
    return int(min(cap, 40 + 35 * max(rows, 1)))


def section(title: str) -> None:
    st.markdown(f'<div class="tt-section">{esc(title)}</div>', unsafe_allow_html=True)


def card(column: Any, label: str, value_html: str, sub_html: str = "", accent: str = "#22d3ee",
         extra_html: str = "") -> None:
    column.markdown(f'<div class="tt-card" style="--accent:{accent}"><div class="tt-label">{esc(label)}</div>'
                    f'<div class="tt-value">{value_html}</div>{extra_html}'
                    f'<div class="tt-sub">{sub_html}</div></div>', unsafe_allow_html=True)


def chart_times(index: pd.DatetimeIndex, tz: ZoneInfo) -> list[int]:
    """UTC bar times -> epoch seconds shifted into the display time zone (chart axis shows local)."""
    utc_index = index.tz_convert("UTC")
    epoch = (utc_index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)
    offsets = [int(ts.utcoffset().total_seconds()) for ts in utc_index.tz_convert(tz)]
    return [int(e) + o for e, o in zip(epoch, offsets)]


def chart_time_of(moment: Any, tz: ZoneInfo) -> int:
    ts = pd.Timestamp(moment)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.timestamp()) + int(ts.tz_convert(tz).utcoffset().total_seconds())


def next_red_event(now: datetime):
    return WORKER.news.next_event(now, impacts=("High",),
                                  currencies=set(SETTINGS.news.alert_currencies) | {"ALL"})


# --------------------------------------------------------------------------- #
# Header + telemetry
# --------------------------------------------------------------------------- #
@st.fragment(run_every=UI_REFRESH)
@guarded("Telemetry")
def telemetry_bar() -> None:
    snap = WORKER.get_snapshot()
    now = datetime.now(UTC)
    mode = snap.get("mode", "PAPER")
    zones = active_killzones(now, SETTINGS.strategy.killzones, SETTINGS.strategy.killzone_timezone)
    zone_pill = (f'<span class="pill pill-kz">🕒 {esc(zones[0].replace("_", " "))} KILLZONE</span>' if zones
                 else '<span class="pill">🌙 OFF-SESSION</span>')
    mode_pill = ('<span class="pill pill-mode">● PAPER</span>' if mode == "PAPER"
                 else '<span class="pill pill-live">● LIVE MT5</span>')
    upcoming = next_red_event(now)
    news_pill = ""
    if upcoming is not None and upcoming.time_utc is not None:
        news_pill = (f'<span class="pill pill-news">📰 {esc(upcoming.currency)} '
                     f'{esc(upcoming.title[:26])} · {esc(format_countdown(upcoming.seconds_until(now)))}</span>')
    st.markdown(
        f'<div class="tt-header"><div class="tt-brand"><div class="tt-title">⚡ ALL-IN-ONE '
        f'<span>TRADING SUPER-TERMINAL</span></div>{mode_pill}{zone_pill}</div>'
        f'<div class="tt-meta">{news_pill}<span>IST <b>{now.astimezone(IST):%H:%M:%S}</b></span>'
        f'<span>NY <b>{now.astimezone(NEW_YORK):%H:%M}</b></span><span>UTC <b>{now:%H:%M}</b></span>'
        f'<span>{now.astimezone(IST):%a %d %b %Y}</span></div></div>', unsafe_allow_html=True)

    account = snap.get("account") or {}
    drawdown = snap.get("drawdown") or {}
    currency = account.get("currency", "USD")
    cols = st.columns(6, gap="small")
    balance = account.get("balance")
    equity = account.get("equity")
    card(cols[0], "Balance", esc(money(balance, currency)),
         esc(f"{account.get('name', '—')} · {account.get('server', '—')}"), "#22d3ee")
    delta = (equity - balance) if equity is not None and balance is not None else None
    card(cols[1], "Equity", esc(money(equity, currency)),
         f'<span class="{"pos" if (delta or 0) > 0 else "neg" if (delta or 0) < 0 else ""}">'
         f'{esc(money(delta, currency, signed=True))}</span> vs balance · 1:{esc(account.get("leverage", "—"))}',
         "#3b82f6")
    floating = account.get("profit")
    pnl_css = "pos" if (floating or 0) > 0 else "neg" if (floating or 0) < 0 else ""
    realized = drawdown.get("realized_today")
    card(cols[2], "Floating P&L", f'<span class="{pnl_css}">{esc(money(floating, currency, signed=True))}</span>',
         esc(f"{len(snap.get('positions') or [])} open · {len(snap.get('orders') or [])} pending · "
             f"today {money(realized, currency, signed=True)}"),
         UP if (floating or 0) >= 0 else DOWN)
    dd_pct = float(drawdown.get("drawdown_pct") or 0.0)
    limit = float(drawdown.get("limit_pct") or SETTINGS.risk.max_daily_loss_percent)
    usage = min(1.0, dd_pct / limit) if limit else 0.0
    color = UP if usage < 0.5 else "#f5a524" if usage < 0.8 else DOWN
    card(cols[3], "Daily Drawdown",
         f'<span style="color:{color}">{dd_pct:.2f}%</span><span class="muted" style="font-size:.78rem"> / {limit:.1f}%</span>',
         esc(f"Day ref {money(drawdown.get('reference'), currency)}"), color,
         f'<div class="meter"><div style="width:{max(usage * 100, 1.5):.1f}%;background:{color}"></div></div>')
    status = snap.get("bot_status", "STARTING")
    runtime = snap.get("runtime") or {}
    status_color = {"RUNNING": UP, "PAUSED": "#f5a524", "STARTING": "#f5a524"}.get(status, DOWN)
    card(cols[4], "Bot Status", f'<span class="badge {STATUS_CLASS.get(status, "badge-paused")}">{esc(status)}</span>',
         esc(f"{snap.get('status_reason', '')} · risk {float(runtime.get('risk_percent') or 0):.2f}%"),
         status_color)
    connected = bool(snap.get("broker_connected"))
    ping = snap.get("ping_ms")
    detail = f"ping {ping:.0f} ms" if ping is not None else (snap.get("broker_error") or "no link")
    card(cols[5], "Broker Link",
         f'<span class="dot {"dot-on" if connected else "dot-off"}"></span>{"CONNECTED" if connected else "OFFLINE"}',
         esc(f"{detail} · last scan {ist(snap.get('last_scan_at'), '%H:%M:%S')}"), UP if connected else DOWN)


@st.fragment(run_every=UI_REFRESH)
@guarded("Market watch")
def market_watch() -> None:
    snap = WORKER.get_snapshot()
    symbols_state = snap.get("symbols") or {}
    locks = snap.get("news_locks") or {}
    selected = st.session_state.get("chart_symbol", SETTINGS.symbols[0])
    items = []
    for symbol in SETTINGS.symbols:
        info = symbols_state.get(symbol, {})
        digits = int(info.get("digits") or 2)
        bid = info.get("bid")
        change = info.get("change_24h_pct")
        bias = ((info.get("analysis") or {}).get("htf_bias")) or "NEUTRAL"
        change_html = (f'<span style="color:{UP if change > 0 else DOWN if change < 0 else FLAT}">'
                       f'{"▲" if change > 0 else "▼" if change < 0 else "•"} {abs(change):.2f}%</span>'
                       if change is not None else '<span class="muted">24h —</span>')
        locked = (locks.get(symbol) or {}).get("active")
        items.append(
            f'<div class="mw-item{" active" if symbol == selected else ""}">'
            f'<div class="mw-top"><span>{esc(symbol)}</span><span class="mw-bias bias-{esc(bias)}">H1 {esc(bias[:4])}</span></div>'
            f'<div class="mw-px">{esc(price(bid, digits))}</div>'
            f'<div class="mw-sub">{change_html}<span>spr {esc(info.get("spread_points", "—"))}'
            f'{" · 🔒" if locked else ""}</span></div></div>')
    st.markdown(f'<div class="mw">{"".join(items)}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Control deck
# --------------------------------------------------------------------------- #
def _do_panic() -> None:
    st.session_state["panic_result"] = WORKER.panic_close_all("dashboard")
    st.session_state["arm_panic"] = False


@guarded("Bot controls")
def bot_controls() -> None:
    with st.container(border=True, key="panel_controls"):
        section("Bot master controls")
        c1, c2 = st.columns(2)
        if c1.button("▶  Start Bot", key="btn_start", width="stretch"):
            WORKER.start_bot("dashboard")
            st.toast("Bot started — scanning for A-grade setups", icon="▶️")
        if c2.button("⏸  Pause Bot", key="btn_pause", width="stretch"):
            WORKER.pause_bot("dashboard")
            st.toast("Bot paused — open positions stay protected", icon="⏸️")
        st.toggle("Arm panic button", key="arm_panic")
        st.button("🚨 PANIC CLOSE ALL POSITIONS", key="btn_panic", width="stretch",
                  disabled=not st.session_state.get("arm_panic"), on_click=_do_panic)
        result = st.session_state.pop("panic_result", None)
        if result:
            message = (f"Panic close: {result['closed']} closed, {result['cancelled']} orders cancelled, "
                       f"{result['failed']} failed")
            (st.success if result["ok"] else st.error)(
                message + ("\n\n" + "\n".join(result["errors"]) if result["errors"] else ""))


@guarded("Execution settings")
def execution_settings() -> None:
    runtime = WORKER.runtime
    with st.container(border=True, key="panel_settings"):
        section("Execution settings")
        mode = st.segmented_control(
            "Execution mode", ["auto", "signal_only"], default=runtime.execution_mode, required=True,
            format_func=lambda m: "⚡ Auto-execute" if m == "auto" else "📡 Signals only",
            key="exec_mode", width="stretch")
        if mode and mode != runtime.execution_mode:
            WORKER.set_execution_mode(mode)
        max_risk = float(SETTINGS.risk.max_risk_percent)
        risk = st.slider("Risk per trade (%)", min_value=0.05, max_value=max_risk,
                         value=float(min(max(runtime.risk_percent, 0.05), max_risk)), step=0.05,
                         key="risk_slider")
        if abs(risk - runtime.risk_percent) > 1e-9:
            WORKER.set_risk_percent(risk)
        killzone = st.toggle("Require killzone for entries", value=runtime.require_killzone,
                             key="kz_toggle")
        if killzone != runtime.require_killzone:
            WORKER.set_require_killzone(killzone)
        chosen = st.pills("Active strategies", list(STRATEGY_CODES), selection_mode="multi",
                          default=runtime.enabled_strategies,
                          format_func=lambda code: STRATEGY_LABELS.get(code, code), key="strategies")
        if chosen and set(chosen) != set(runtime.enabled_strategies):
            WORKER.set_enabled_strategies(list(chosen))
        elif not chosen:
            st.caption("⚠️ Keep at least one strategy selected (current set stays active).")


@st.fragment(run_every=5)
@guarded("Manual execution pad")
def manual_pad() -> None:
    with st.container(border=True, key="panel_manual"):
        section("Manual execution pad")
        symbol = st.selectbox("Symbol", list(SETTINGS.symbols), key="man_symbol")
        labels = {"atr": "ATR ×", "structure": "Swing", "pips": "Pips"}
        sl_mode = st.segmented_control("Stop-loss method", list(labels), default="atr", required=True,
                                       format_func=labels.get, key="man_sl_mode", width="stretch")
        sl_mode = sl_mode or "atr"
        c1, c2 = st.columns(2)
        defaults = {"atr": 1.5, "pips": 30.0, "structure": 0.0}
        sl_value = c1.number_input("ATR × / pips", min_value=0.0, value=defaults[sl_mode],
                                   step=0.1 if sl_mode == "atr" else 1.0, key=f"man_sl_{sl_mode}",
                                   disabled=sl_mode == "structure")
        max_risk = float(SETTINGS.risk.max_risk_percent)
        risk = c2.number_input("Risk %", min_value=0.05, max_value=max_risk,
                               value=float(min(max(WORKER.runtime.risk_percent, 0.05), max_risk)),
                               step=0.05, key="man_risk")
        buy = WORKER.preview_manual_trade(symbol, "BUY", risk, sl_mode, sl_value)
        sell = WORKER.preview_manual_trade(symbol, "SELL", risk, sl_mode, sl_value)
        digits = int(buy.get("digits") or sell.get("digits") or 2)

        def cell(preview: dict[str, Any], key: str, kind: str = "price") -> str:
            if not preview.get("ok") and key != "entry":
                return "—"
            value = preview.get(key)
            if kind == "lots":
                return esc(f"{value:g}") if value else "—"
            if kind == "money":
                return esc(money(value))
            return esc(price(value, digits))

        rows = "".join(
            f"<tr><th>{label}</th><td class='pos'>{cell(buy, key, kind)}</td><td class='neg'>{cell(sell, key, kind)}</td></tr>"
            for label, key, kind in (("Entry", "entry", "price"), ("Stop", "sl", "price"),
                                     ("TP1", "tp1", "price"), ("TP2", "tp2", "price"),
                                     ("Lots", "lots", "lots"), ("Risk", "risk_amount", "money")))
        st.markdown(f"<table class='preview'><tr><th></th><th>Buy</th><th>Sell</th></tr>{rows}</table>",
                    unsafe_allow_html=True)
        override = st.checkbox("Override news lock", key="man_override")
        if buy.get("news_lock"):
            st.warning(f"🔒 News lock: {buy.get('news_label')}")
        b1, b2 = st.columns(2)
        buy_label = f"BUY  {price(buy.get('entry'), digits)}" if buy.get("entry") else "BUY"
        sell_label = f"SELL  {price(sell.get('entry'), digits)}" if sell.get("entry") else "SELL"
        if b1.button(buy_label, key="btn_buy", width="stretch", disabled=not buy.get("ok")):
            outcome = WORKER.manual_trade(symbol, "BUY", risk, sl_mode, sl_value, override_news=override)
            st.toast(f"BUY {symbol} {outcome.get('lots')} lots @ {outcome.get('price')}" if outcome.get("ok")
                     else f"BUY failed: {outcome.get('error')}", icon="🟢" if outcome.get("ok") else "⚠️")
        if b2.button(sell_label, key="btn_sell", width="stretch", disabled=not sell.get("ok")):
            outcome = WORKER.manual_trade(symbol, "SELL", risk, sl_mode, sl_value, override_news=override)
            st.toast(f"SELL {symbol} {outcome.get('lots')} lots @ {outcome.get('price')}" if outcome.get("ok")
                     else f"SELL failed: {outcome.get('error')}", icon="🔴" if outcome.get("ok") else "⚠️")
        problems = {preview.get("error") for preview in (buy, sell) if not preview.get("ok") and preview.get("error")}
        for problem in problems:
            st.caption(f"ℹ️ {problem}")


@guarded("Telegram panel")
def telegram_panel() -> None:
    status = WORKER.telegram.credentials_status()
    label = "📣 Telegram channel control" + ("" if status["configured"] else " · not configured")
    with st.expander(label, expanded=False):
        st.caption(f"{'✅ Configured' if status['configured'] else '⚠️ Add a bot token and channel'} · "
                   f"source: {status['source']} · token {status['token_masked'] or '—'}")
        token = st.text_input("Bot Token", type="password", key="tg_token",
                              placeholder="leave blank to keep the current token")
        channel = st.text_input("Channel ID", value=status["channel_id"], key="tg_channel",
                                help="@public_channel or -100xxxxxxxxxx (the bot must be a channel admin)")
        admin = st.text_input("Admin Chat ID", value=status["admin_chat_id"], key="tg_admin",
                              help="Your personal chat id for emergency alerts")
        c1, c2 = st.columns(2)
        if c1.button("💾 Save", key="tg_save", width="stretch"):
            WORKER.update_telegram(token, channel, admin)
            st.toast("Telegram settings saved", icon="💾")
        if c2.button("🔌 Test link", key="tg_test", width="stretch"):
            probe = WORKER.telegram_test_connection()
            if probe.get("ok"):
                st.success(f"Bot {probe.get('bot')} OK · channel: {probe.get('channel') or '—'} · "
                           f"bot is admin: {probe.get('channel_admin')}")
            else:
                st.error(f"Telegram check failed: {probe.get('error')}")
        c3, c4 = st.columns(2)
        if c3.button("🧪 Test signal", key="tg_test_signal", width="stretch"):
            outcome = WORKER.send_test_signal()
            (st.success if outcome.ok else st.error)(
                "Test signal delivered" if outcome.ok else f"Failed: {outcome.error}")
        if c4.button("📰 Send news", key="tg_news", width="stretch"):
            outcome = WORKER.broadcast_news_now()
            (st.success if outcome.ok else st.error)(
                "Today's schedule broadcast" if outcome.ok else f"Failed: {outcome.error}")
        if status["source"] == "dashboard" and st.button("↩️ Use .env credentials", key="tg_clear"):
            WORKER.telegram.clear_saved_credentials()
            st.toast("Reverted to .env Telegram credentials")
        stats = WORKER.telegram.stats_snapshot()
        st.caption(f"Sent {stats['sent']} · failed {stats['failed']} · queued {stats['queued']} · "
                   f"retries {stats['retries']}" + (f" · last error: {stats['last_error']}"
                                                    if stats.get("last_error") else ""))


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
def _zone(t_start: int, t_end: int, top: float, bottom: float, fill: str, line: str) -> dict[str, Any] | None:
    if t_end <= t_start or top <= bottom:
        return None
    return {"type": "Baseline",
            "data": [{"time": t_start, "value": float(top)}, {"time": t_end, "value": float(top)}],
            "options": {"baseValue": {"type": "price", "price": float(bottom)},
                        "topFillColor1": fill, "topFillColor2": fill, "topLineColor": line,
                        "bottomFillColor1": "rgba(0,0,0,0)", "bottomFillColor2": "rgba(0,0,0,0)",
                        "bottomLineColor": "rgba(0,0,0,0)", "lineWidth": 1,
                        "priceLineVisible": False, "lastValueVisible": False,
                        "crosshairMarkerVisible": False}}


def _hline(t_start: int, t_end: int, level: float, color: str, style: int = 0, width: int = 1,
           title: str = "") -> dict[str, Any] | None:
    if t_end <= t_start:
        return None
    return {"type": "Line",
            "data": [{"time": t_start, "value": float(level)}, {"time": t_end, "value": float(level)}],
            "options": {"color": color, "lineWidth": width, "lineStyle": style,
                        "priceLineVisible": False, "lastValueVisible": bool(title), "title": title,
                        "crosshairMarkerVisible": False}}


def build_chart(df: pd.DataFrame, features: FeatureSet | None, analysis: MarketAnalysis | None,
                positions: list[dict[str, Any]], orders: list[dict[str, Any]], overlays: set[str],
                digits: int, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    times = chart_times(df.index, CHART_TZ)
    time_of = dict(zip(df.index, times))
    first, last = times[0], times[-1]
    opens, highs = df["open"].to_numpy(float), df["high"].to_numpy(float)
    lows, closes = df["low"].to_numpy(float), df["close"].to_numpy(float)
    candles = [{"time": t, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)}
               for t, o, h, lo, c in zip(times, opens, highs, lows, closes)]
    min_move = 10 ** -digits if digits > 0 else 1
    markers: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []

    def feat_time(pos: int) -> int | None:
        if features is None or pos >= len(features.df):
            return None
        return time_of.get(features.df.index[pos])

    def snap(moment: Any) -> int:
        """Latest candle time at or before ``moment`` (line points must sit on real bars)."""
        index = bisect.bisect_right(times, chart_time_of(moment, CHART_TZ)) - 1
        return times[max(0, index)]

    if features is not None and "Liquidity Sweeps" in overlays:
        for sweep in features.sweeps[-8:]:
            t = feat_time(sweep.pos)
            if t is None:
                continue
            bearish = sweep.side == "BSL"
            markers.append({"time": t, "position": "aboveBar" if bearish else "belowBar",
                            "color": "#f87171" if bearish else "#34d399",
                            "shape": "arrowDown" if bearish else "arrowUp", "text": sweep.side})
    if features is not None and "Structure" in overlays:
        for event in features.events[-8:]:
            t = feat_time(event.pos)
            if t is None:
                continue
            bullish = event.direction == "BUY"
            markers.append({"time": t, "position": "belowBar" if bullish else "aboveBar",
                            "color": "#22d3ee" if bullish else "#f472b6", "shape": "circle",
                            "text": f"{event.kind}{'↑' if bullish else '↓'}"})
    if analysis is not None and "Signals" in overlays:
        for trade_signal in analysis.signals[:3]:
            t = time_of.get(pd.Timestamp(trade_signal.anchor_time))
            if t is None:
                continue
            buy = trade_signal.direction == "BUY"
            markers.append({"time": t, "position": "belowBar" if buy else "aboveBar",
                            "color": "#16c784" if buy else "#ea3943",
                            "shape": "arrowUp" if buy else "arrowDown",
                            "text": f"{trade_signal.direction} {trade_signal.grade} {trade_signal.score:.1f}"})
    markers.sort(key=lambda m: m["time"])
    series.append({"type": "Candlestick", "data": candles, "markers": markers,
                   "options": {"upColor": "#16c784", "downColor": "#ea3943", "borderVisible": False,
                               "wickUpColor": "#16c784", "wickDownColor": "#ea3943",
                               "priceFormat": {"type": "price", "precision": digits, "minMove": min_move}}})

    volumes = df["volume"].to_numpy(float)
    if "Volume" in overlays and volumes.sum() > 0:
        series.append({"type": "Histogram",
                       "data": [{"time": t, "value": float(v),
                                 "color": "rgba(22,199,132,0.32)" if c >= o else "rgba(234,57,67,0.32)"}
                                for t, v, o, c in zip(times, volumes, opens, closes)],
                       "options": {"priceFormat": {"type": "volume"}, "priceScaleId": "",
                                   "lastValueVisible": False, "priceLineVisible": False},
                       "priceScale": {"scaleMargins": {"top": 0.85, "bottom": 0}}})
    if "EMA" in overlays and len(closes) > 5:
        ema = compute_ema(closes, SETTINGS.strategy.ema_period)
        series.append({"type": "Line", "data": [{"time": t, "value": float(v)} for t, v in zip(times, ema)],
                       "options": {"color": "#a78bfa", "lineWidth": 1, "priceLineVisible": False,
                                   "lastValueVisible": False, "crosshairMarkerVisible": False,
                                   "title": f"EMA{SETTINGS.strategy.ema_period}"}})
    reference_price = float(closes[-1])
    atr = features.atr_now if features is not None else 0.0
    near = (lambda level: abs(level - reference_price) <= 12 * atr) if atr > 0 else (lambda level: True)
    if features is not None and "FVG" in overlays:
        for gap in [g for g in features.fvgs if g.active and near(g.ce)][-6:]:
            t0 = feat_time(gap.start_pos)
            bullish = gap.direction == "BUY"
            zone = _zone(t0, last, gap.top, gap.bottom,
                         "rgba(22,199,132,0.15)" if bullish else "rgba(234,57,67,0.15)",
                         "rgba(22,199,132,0.55)" if bullish else "rgba(234,57,67,0.55)") if t0 else None
            if zone:
                series.append(zone)
    if features is not None and "Order Blocks" in overlays:
        for block in [b for b in features.order_blocks if b.active and near((b.top + b.bottom) / 2)][-4:]:
            t0 = feat_time(block.pos)
            bullish = block.direction == "BUY"
            zone = _zone(t0, last, block.top, block.bottom,
                         "rgba(59,130,246,0.15)" if bullish else "rgba(236,72,153,0.15)",
                         "rgba(59,130,246,0.6)" if bullish else "rgba(236,72,153,0.6)") if t0 else None
            if zone:
                series.append(zone)
    if "Sessions" in overlays:
        now = datetime.now(UTC)
        closed = closed_bars(df, timeframe, now)
        for session in compute_session_ranges(closed, SETTINGS.strategy.session_ranges,
                                              SETTINGS.strategy.killzone_timezone, now):
            if session.start < df.index[0]:
                continue
            t0, t1 = snap(session.start), snap(session.end)
            if not session.complete:
                t1 = last
            color = SESSION_COLORS.get(session.name, "#94a3b8")
            for level in (session.high, session.low):
                line = _hline(t0, t1, level, color, 2, 1)
                if line:
                    series.append(line)
    if "Positions" in overlays:
        for position in positions:
            opened = parse_iso(position.get("time_utc"))
            t0 = snap(opened) if opened else first
            if t0 >= last:
                t0 = times[-2] if len(times) > 1 else first
            buy = position["direction"] == "BUY"
            for level, color, style, width, title in (
                    (position.get("price_open"), "#3b82f6", 0, 2, f"{'BUY' if buy else 'SELL'} {position['volume']:g}"),
                    (position.get("sl"), "#ea3943", 2, 1, "SL"),
                    (position.get("tp1"), "#6ee7b7", 3, 1, "TP1"),
                    (position.get("tp"), "#16c784", 2, 1, "TP")):
                if level:
                    line = _hline(t0, last, level, color, style, width, title)
                    if line:
                        series.append(line)
        for order in orders:
            line = _hline(first, last, order["price"], "#f5a524", 1, 1, f"{order['direction']} {order['kind']}")
            if line:
                series.append(line)
    if analysis is not None and "Signals" in overlays and analysis.best_signal is not None and not positions:
        best = analysis.best_signal
        t0 = time_of.get(pd.Timestamp(best.anchor_time), first)
        for level, color, title in ((best.entry, "rgba(59,130,246,0.7)", f"{best.direction} {best.order_type}"),
                                    (best.stop_loss, "rgba(234,57,67,0.7)", "Setup SL"),
                                    (best.tp2, "rgba(22,199,132,0.7)", "Setup TP2")):
            line = _hline(t0, last, level, color, 1, 1, title)
            if line:
                series.append(line)

    chart = {"height": 560,
             "layout": {"background": {"type": "solid", "color": "#070b14"}, "textColor": "#8b98ad",
                        "fontSize": 11, "fontFamily": "Inter, system-ui, sans-serif"},
             "grid": {"vertLines": {"color": "rgba(28,38,56,0.55)"},
                      "horzLines": {"color": "rgba(28,38,56,0.55)"}},
             "crosshair": {"mode": 0},
             "rightPriceScale": {"borderColor": "#1c2638", "scaleMargins": {"top": 0.06, "bottom": 0.2}},
             "timeScale": {"borderColor": "#1c2638", "timeVisible": True, "secondsVisible": False,
                           "rightOffset": 8, "barSpacing": 7},
             "watermark": {"visible": True, "fontSize": 46, "horzAlign": "center", "vertAlign": "center",
                           "color": "rgba(139,152,173,0.06)", "text": f"{symbol} · {timeframe}"}}
    return [{"chart": chart, "series": series}]


@guarded("Chart controls")
def chart_controls() -> None:
    symbols = list(SETTINGS.symbols)
    c1, c2, c3 = st.columns([2.3, 2.6, 0.7], vertical_alignment="bottom")
    if len(symbols) <= 6:
        c1.segmented_control("Symbol", symbols, default=symbols[0], required=True, key="chart_symbol",
                             label_visibility="collapsed")
    else:
        c1.selectbox("Symbol", symbols, key="chart_symbol", label_visibility="collapsed")
    default_tf = SETTINGS.ltf if SETTINGS.ltf in TIMEFRAMES else "M5"
    c2.segmented_control("Timeframe", TIMEFRAMES, default=default_tf, required=True, key="chart_tf",
                         label_visibility="collapsed")
    c3.toggle("Live", value=True, key="chart_live",
              help="Auto-refresh the chart. Switch off to zoom and scroll freely.")
    st.pills("Overlays", OVERLAYS, selection_mode="multi", default=DEFAULT_OVERLAYS,
             key="chart_overlays", label_visibility="collapsed")


def chart_body() -> None:
    symbol = st.session_state.get("chart_symbol") or SETTINGS.symbols[0]
    timeframe = st.session_state.get("chart_tf") or SETTINGS.ltf
    overlays = set(st.session_state.get("chart_overlays") or [])
    snap = WORKER.get_snapshot()
    data, features = WORKER.get_chart_data(symbol, timeframe, SETTINGS.app.chart_bars)
    if data.empty:
        st.info(f"⏳ Waiting for {symbol} {timeframe} market data… {snap.get('broker_error') or ''}")
        return
    symbol_state = (snap.get("symbols") or {}).get(symbol, {})
    digits = int(symbol_state.get("digits") or (2 if data["close"].iloc[-1] > 50 else 5))
    analysis = WORKER.get_analysis(symbol) if timeframe == SETTINGS.ltf else None
    positions = [p for p in snap.get("positions") or [] if p.get("canonical") == symbol]
    broker_symbol = symbol_state.get("broker_symbol", symbol)
    orders = [o for o in snap.get("orders") or [] if o.get("symbol") == broker_symbol]
    charts = build_chart(data, features, analysis, positions, orders, overlays, digits, symbol, timeframe)
    renderLightweightCharts(charts, key=f"lwc_{symbol}_{timeframe}")
    bid, ask = symbol_state.get("bid"), symbol_state.get("ask")
    st.caption(f"{symbol} {timeframe} · last {price(data['close'].iloc[-1], digits)} · bid/ask "
               f"{price(bid, digits)} / {price(ask, digits)} · spread {symbol_state.get('spread_points', '—')} pts · "
               f"bar {data.index[-1].tz_convert(CHART_TZ):%d %b %H:%M} ({SETTINGS.app.chart_timezone}) · "
               f"sessions: Asia 🟡 London 🔵 New York 🟠")
    render_analysis_strip(symbol)


def render_analysis_strip(symbol: str) -> None:
    analysis = WORKER.get_analysis(symbol)
    if analysis is None:
        st.caption("Strategy engine warming up — analysis appears after the first closed LTF candle.")
        return
    summary = analysis.summary()

    def chip(label: str, value: str) -> str:
        tone_class = "chip-clear" if value in ("BULLISH", "DISCOUNT") else "chip-lock" if value in (
            "BEARISH", "PREMIUM") else "chip-info"
        return f'<span class="chip {tone_class}">{esc(label)}: <b>{esc(value)}</b></span>'

    chips = "".join([
        chip(f"HTF {SETTINGS.htf}", summary["htf_bias"]), chip(f"MTF {SETTINGS.mtf}", summary["mtf_bias"]),
        chip(f"LTF {SETTINGS.ltf}", summary["ltf_trend"]), chip("DXY", summary["dxy_bias"]),
        chip("CRT", summary["crt"]), chip("Zone", summary["premium_discount"]),
        chip("Killzone", ", ".join(summary["killzones"]) or "None"), chip("ATR", f"{summary['atr']:.5g}"),
        chip("Active FVG/OB", f"{summary['active_fvgs']}/{summary['active_obs']}")])
    st.markdown(chips, unsafe_allow_html=True)
    best = analysis.best_signal
    if best is not None:
        side = "pos" if best.direction == "BUY" else "neg"
        st.markdown(
            f'<div class="signal-card"><b class="{side}">{esc(best.direction)} {esc(best.symbol)}</b> · '
            f'{esc(best.strategy)} · <b>{esc(best.grade)} {best.score:.1f}/10</b> · {esc(best.order_type)} '
            f'@ {best.entry:.5g} · SL {best.stop_loss:.5g} · TP1 {best.tp1:.5g} · TP2 {best.tp2:.5g}'
            f'<br><span class="muted">{esc(" • ".join(best.confluences))}</span></div>',
            unsafe_allow_html=True)
    if analysis.rejections or analysis.notes:
        with st.expander("🔎 Why no trade? (engine diagnostics)"):
            for note in analysis.notes:
                st.caption(f"ℹ️ {note}")
            for item in analysis.rejections[-12:]:
                st.caption(f"• {STRATEGY_LABELS.get(item['setup'], item['setup'])} {item['direction']}: "
                           f"{item['reason']}")


@st.fragment(run_every=SETTINGS.app.chart_refresh_seconds)
@guarded("Chart")
def chart_live() -> None:
    chart_body()


@st.fragment
@guarded("Chart")
def chart_static() -> None:
    chart_body()
    st.button("🔄 Refresh chart", key="chart_refresh_btn")


# --------------------------------------------------------------------------- #
# Forex Factory dock
# --------------------------------------------------------------------------- #
@st.fragment(run_every=UI_REFRESH)
@guarded("News countdown")
def news_header() -> None:
    snap = WORKER.get_snapshot()
    now = datetime.now(UTC)
    upcoming = next_red_event(now)
    if upcoming is not None and upcoming.time_utc is not None:
        st.markdown(
            f'<div class="countdown-card"><div class="tt-label">Next 🔴 red-folder event</div>'
            f'<div class="countdown">{esc(format_countdown(upcoming.seconds_until(now) or 0.0))}</div>'
            f'<div class="tt-sub"><b>{esc(upcoming.currency)}</b> · {esc(upcoming.title)} · '
            f'{upcoming.time_utc.astimezone(IST):%a %H:%M} IST</div></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="countdown-card"><div class="tt-label">Next 🔴 red-folder event</div>'
                    '<div class="tt-sub">No upcoming high-impact events loaded for this week.</div></div>',
                    unsafe_allow_html=True)
    locks = snap.get("news_locks") or {}
    chips = "".join(
        f'<span class="chip {"chip-lock" if info.get("active") else "chip-clear"}">'
        f'{"🔒" if info.get("active") else "✓"} {esc(symbol)}'
        f'{" · " + esc(info.get("label")) if info.get("active") else ""}</span>'
        for symbol, info in locks.items())
    if chips:
        st.markdown(chips, unsafe_allow_html=True)


@guarded("News filters")
def news_controls() -> None:
    toggle = st.toggle("Auto-pause bot on high-impact news", value=WORKER.runtime.news_auto_pause,
                       key="news_toggle")
    if toggle != WORKER.runtime.news_auto_pause:
        WORKER.set_news_auto_pause(toggle)
    st.pills("Currency", CURRENCIES, selection_mode="multi", default=["USD", "EUR", "GBP", "JPY"],
             key="ff_currencies")
    st.pills("Impact", list(IMPACT_LABELS), selection_mode="multi", default=["High", "Medium"],
             format_func=IMPACT_LABELS.get, key="ff_impacts")
    st.segmented_control("View", ["Today", "Next 48h", "This week"], default="Today", required=True,
                         key="ff_view", width="stretch")


def _events_for_view(engine: ForexFactoryEngine, now: datetime) -> list[Any]:
    currencies = st.session_state.get("ff_currencies") or None
    impacts = st.session_state.get("ff_impacts") or None
    view = st.session_state.get("ff_view") or "Today"
    if view == "Today":
        return engine.today_events(now, IST, currencies=currencies, impacts=impacts)
    if view == "Next 48h":
        return engine.upcoming_events(now, 48, currencies=currencies, impacts=impacts)
    return engine.get_events(currencies=currencies, impacts=impacts)


@st.fragment(run_every=UI_REFRESH)
@guarded("Economic calendar")
def news_table() -> None:
    engine = WORKER.news
    now = datetime.now(UTC)
    rows = []
    for event in _events_for_view(engine, now)[:250]:
        moment = event.time_utc
        if event.all_day or moment is None:
            when, countdown, past = event.time_label or "All Day", "", False
            day = moment.astimezone(IST).strftime("%a") if moment else ""
        else:
            seconds = (moment - now).total_seconds()
            past = seconds < -60
            when = moment.astimezone(IST).strftime("%H:%M")
            day = moment.astimezone(IST).strftime("%a")
            countdown = "LIVE" if -60 <= seconds <= 60 else format_countdown(seconds) if seconds > 0 else "done"
        rows.append(
            f'<tr class="ff-{esc(event.impact)}{" ff-past" if past else ""}">'
            f'<td class="ff-time">{esc(day)} {esc(when)}</td><td class="ff-time">{esc(countdown)}</td>'
            f'<td><b>{esc(event.currency)}</b></td>'
            f'<td><span class="imp imp-{esc(event.impact)}" title="{esc(event.impact)}"></span></td>'
            f'<td>{esc(event.title)}</td><td>{esc(event.forecast or "—")}</td>'
            f'<td>{esc(event.previous or "—")}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="7" class="muted">No events for this filter.</td></tr>'
    st.markdown('<div class="ff-wrap"><table class="ff-table"><thead><tr><th>IST</th><th>In</th>'
                '<th>Cur</th><th></th><th>Event</th><th>Fcst</th><th>Prev</th></tr></thead>'
                f'<tbody>{body}</tbody></table></div>', unsafe_allow_html=True)
    status = engine.status()
    detail = (f"Source: {status['source']} · {status['events']} events · updated "
              f"{ist(status['fetched_at'], '%d %b %H:%M')} IST")
    if status.get("last_error"):
        detail += f" · ⚠️ {status['last_error'][:140]} (retry in {status['cooldown_seconds'] // 60} min)"
    st.caption(detail)


@guarded("Calendar refresh")
def news_refresh_button() -> None:
    if st.button("🔄 Refresh calendar", key="ff_refresh", width="stretch"):
        ok = WORKER.refresh_news()
        st.toast("Calendar refreshed" if ok else "Refresh skipped (rate-limited or source unavailable)",
                 icon="🔄" if ok else "⏳")


# --------------------------------------------------------------------------- #
# Bottom analytics tabs
# --------------------------------------------------------------------------- #
@st.fragment(run_every=UI_REFRESH)
@guarded("Positions")
def positions_tab() -> None:
    snap = WORKER.get_snapshot()
    positions = snap.get("positions") or []
    orders = snap.get("orders") or []
    if not positions:
        st.info("No open positions — the bot manages new trades here (TP1, breakeven and trailing).")
    else:
        rows = []
        for p in positions:
            sign = 1 if p["direction"] == "BUY" else -1
            initial = p.get("initial_sl")
            risk_distance = abs(p["price_open"] - initial) if initial else None
            r_now = ((p["price_current"] - p["price_open"]) * sign / risk_distance) if risk_distance else None
            rows.append({"Ticket": p["ticket"], "Symbol": p.get("canonical") or p["symbol"],
                         "Side": p["direction"], "Lots": p["volume"], "Entry": p["price_open"],
                         "Price": p["price_current"], "SL": p.get("sl"), "TP": p.get("tp"),
                         "P&L": p["profit"], "R": r_now,
                         "TP1": "✅" if p.get("tp1_hit") else "", "BE": "🛡️" if p.get("be_moved") else "",
                         "Trail": "📈" if p.get("trailing") else "",
                         "Owner": "BOT" if p.get("bot") else "MANUAL",
                         "Opened (IST)": ist(p.get("time_utc"), "%d %b %H:%M")})
        frame = pd.DataFrame(rows)
        price_columns = {column: smart_price for column in ("Entry", "Price", "SL", "TP")}
        styled = (frame.style.map(tone, subset=["P&L", "R"]).map(side_tone, subset=["Side"])
                  .format({"P&L": "{:+,.2f}", "R": "{:+.2f}", "Lots": "{:g}", **price_columns},
                          na_rep="—"))
        st.dataframe(styled, hide_index=True, width="stretch", height=table_height(len(frame)))
        c1, c2, c3 = st.columns([2.2, 1, 1], vertical_alignment="bottom")
        labels = {p["ticket"]: f"#{p['ticket']} {p.get('canonical')} {p['direction']} {p['volume']:g}"
                  for p in positions}
        ticket = c1.selectbox("Position", list(labels), format_func=labels.get, key="pos_select")
        if c2.button("❌ Close", key="pos_close", width="stretch"):
            outcome = WORKER.close_position_manual(ticket)
            st.toast("Close sent" if outcome["ok"] else f"Close failed: {outcome['error']}")
        if c3.button("🛡️ SL → BE", key="pos_be", width="stretch"):
            outcome = WORKER.move_to_breakeven(ticket)
            st.toast("Stop moved to breakeven" if outcome["ok"] else f"BE failed: {outcome['error']}")
    if orders:
        st.markdown("**Pending orders**")
        frame = pd.DataFrame([{"Ticket": o["ticket"], "Symbol": o["symbol"], "Side": o["direction"],
                               "Type": o["kind"], "Lots": o["volume"], "Price": o["price"],
                               "SL": o.get("sl"), "TP": o.get("tp"),
                               "Placed (IST)": ist(o.get("time_utc"), "%d %b %H:%M")} for o in orders])
        prices = {column: smart_price for column in ("Price", "SL", "TP")}
        st.dataframe(frame.style.map(side_tone, subset=["Side"])
                     .format({"Lots": "{:g}", **prices}, na_rep="—"),
                     hide_index=True, width="stretch", height=table_height(len(frame)))


@guarded("Journal filters")
def journal_filters() -> None:
    c1, c2, c3, c4 = st.columns([1, 1, 1, 0.7], vertical_alignment="bottom")
    c1.selectbox("Account", ["Current mode", "All modes"], key="j_scope")
    c2.selectbox("Symbol", ["All", *SETTINGS.symbols], key="j_symbol")
    c3.selectbox("Status", ["All", "OPEN", "PENDING", "CLOSED", "CANCELLED"], key="j_status")
    c4.button("🔄 Refresh", key="j_refresh", width="stretch")


@st.fragment(run_every=10)
@guarded("Trade journal")
def journal_tab() -> None:
    scope = st.session_state.get("j_scope", "Current mode")
    symbol = st.session_state.get("j_symbol", "All")
    status = st.session_state.get("j_status", "All")
    mode = WORKER.mode if scope == "Current mode" else None
    symbol_filter = None if symbol == "All" else symbol
    stats = WORKER.db.get_performance_stats(mode=mode, symbol=symbol_filter)
    pf = stats["profit_factor"]
    metrics = st.columns(8)
    metrics[0].metric("Closed trades", stats["total_trades"], border=True)
    metrics[1].metric("Win rate", f"{stats['win_rate']:.1f}%", border=True)
    metrics[2].metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}", border=True)
    metrics[3].metric("Net P&L", money(stats["net_pnl"], signed=True), border=True)
    metrics[4].metric("Expectancy", money(stats["expectancy"], signed=True), border=True)
    metrics[5].metric("Avg R", f"{stats['avg_r']:+.2f}R", border=True)
    metrics[6].metric("Max DD", money(stats["max_drawdown"]), border=True)
    metrics[7].metric("Loss streak", stats["max_consecutive_losses"], border=True)
    curve = WORKER.db.get_equity_curve(mode=mode, since=datetime.now(UTC) - timedelta(days=30))
    if len(curve) >= 2:
        frame = pd.DataFrame(curve)
        frame["time"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(IST).dt.tz_localize(None)
        st.markdown("**Equity curve · 30 days (IST)**")
        st.line_chart(frame.set_index("time")[["balance", "equity"]], height=220, width="stretch",
                      color=["#22d3ee", "#16c784"])
    breakdown = WORKER.db.get_strategy_breakdown(mode)
    if breakdown:
        st.markdown("**Strategy breakdown**")
        table = pd.DataFrame(breakdown)
        st.dataframe(table.style.map(tone, subset=["net_pnl", "avg_r"])
                     .format({"net_pnl": "{:+,.2f}", "avg_r": "{:+.2f}", "win_rate": "{:.1f}%"}, na_rep="—"),
                     hide_index=True, width="stretch", height=table_height(len(table)))
    trades = WORKER.db.get_trades(limit=500, status=None if status == "All" else status,
                                  symbol=symbol_filter, mode=mode)
    if not trades:
        st.info("The trade journal is empty for this filter.")
        return
    frame = pd.DataFrame(trades)
    frame["confluences"] = frame["confluences"].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
    for column in ("created_at", "opened_at", "closed_at"):
        frame[column] = frame[column].apply(lambda v: ist(v, "%d %b %H:%M") if v else "")
    columns = ["id", "mode", "symbol", "direction", "order_type", "strategy", "status", "volume",
               "entry_price", "fill_price", "sl_initial", "tp1", "tp2", "exit_price", "pnl", "pips",
               "r_multiple", "close_reason", "grade", "score", "opened_at", "closed_at", "confluences"]
    view = frame[[c for c in columns if c in frame.columns]]
    prices = {column: smart_price for column in ("entry_price", "fill_price", "sl_initial", "tp1", "tp2",
                                                  "exit_price")}
    st.dataframe(view.style.map(tone, subset=["pnl", "pips", "r_multiple"]).map(side_tone, subset=["direction"])
                 .format({"volume": "{:g}", "pnl": "{:+,.2f}", "pips": "{:+,.1f}", "r_multiple": "{:+.2f}",
                          "score": "{:.1f}", **prices}, na_rep="—"),
                 hide_index=True, width="stretch", height=table_height(len(view), 400))
    st.download_button("⬇️ Export journal CSV", view.to_csv(index=False).encode("utf-8"),
                       file_name="trade_journal.csv", mime="text/csv", key="j_export")


@st.fragment(run_every=10)
@guarded("Signals")
def signals_tab() -> None:
    signals = WORKER.db.get_recent_signals(250)
    if not signals:
        st.info("No setups detected yet — the engine evaluates every newly closed LTF candle.")
        return
    frame = pd.DataFrame(signals)
    frame["time"] = frame["created_at"].apply(lambda v: ist(v, "%d %b %H:%M"))
    frame["confluences"] = frame["confluences"].apply(lambda v: " • ".join(v) if isinstance(v, list) else v)
    columns = ["time", "symbol", "direction", "strategy", "order_type", "entry", "stop_loss", "tp2",
               "score", "grade", "status", "reason", "confluences"]
    status_colors = {"EXECUTED": f"color:{UP};font-weight:700", "BROADCAST": "color:#22d3ee;font-weight:700",
                     "SKIPPED": "color:#f5a524", "REJECTED": f"color:{FLAT}", "EXPIRED": f"color:{FLAT}"}
    grade_colors = {"A+": "color:#22d3ee;font-weight:800", "A": f"color:{UP};font-weight:700",
                    "B": "color:#f5a524", "C": f"color:{FLAT}"}
    styled = (frame[columns].style.map(side_tone, subset=["direction"])
              .map(lambda v: status_colors.get(str(v), ""), subset=["status"])
              .map(lambda v: grade_colors.get(str(v), ""), subset=["grade"])
              .format({"entry": smart_price, "stop_loss": smart_price, "tp2": smart_price,
                       "score": "{:.1f}"}, na_rep="—"))
    st.dataframe(styled, hide_index=True, width="stretch", height=table_height(len(frame), 400))


@st.fragment(run_every=3)
@guarded("System logs")
def logs_tab() -> None:
    level = st.session_state.get("log_level", "INFO")
    query = (st.session_state.get("log_query") or "").strip().lower()
    rows = WORKER.recent_logs(500, level)
    if query:
        rows = [r for r in rows if query in r["message"].lower() or query in r["source"].lower()]
    lines = "".join(
        f'<div class="lv-{esc(r["level"])}">{esc(ist(r["ts"], "%H:%M:%S"))} · {esc(r["level"][:4])} · '
        f'{esc(r["source"])} · {esc(r["message"])}</div>' for r in rows[:400])
    st.markdown(f'<div class="log-box">{lines or "<span class=muted>No log lines yet.</span>"}</div>',
                unsafe_allow_html=True)


@st.fragment(run_every=10)
@guarded("Telegram log")
def telegram_log_tab() -> None:
    rows = WORKER.db.get_recent_broadcasts(200)
    if not rows:
        st.info("No Telegram broadcasts yet — configure the bot in the Telegram panel.")
        return
    frame = pd.DataFrame(rows)
    frame["time"] = frame["created_at"].apply(lambda v: ist(v, "%d %b %H:%M:%S"))
    status_colors = {"SENT": f"color:{UP};font-weight:700", "FAILED": f"color:{DOWN};font-weight:700",
                     "QUEUED": "color:#f5a524"}
    view = frame[["time", "category", "chat_id", "status", "error", "preview"]]
    st.dataframe(view.style.map(lambda v: status_colors.get(str(v), ""), subset=["status"])
                 .format(na_rep="—"),
                 hide_index=True, width="stretch", height=table_height(len(view), 400))


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.markdown(CSS, unsafe_allow_html=True)
telemetry_bar()

left, center, right = st.columns([1.12, 3.2, 1.45], gap="medium")
with left:
    bot_controls()
    execution_settings()
    manual_pad()
    telegram_panel()
with center:
    market_watch()
    chart_controls()
    if st.session_state.get("chart_live", True):
        chart_live()
    else:
        chart_static()
with right:
    with st.container(border=True, key="panel_news"):
        section("Forex Factory terminal")
        news_header()
        news_controls()
        news_table()
        news_refresh_button()

st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
tabs = st.tabs(["📌 Active Positions", "📒 Trade Journal & Analytics", "📡 Signals", "🧾 System Logs",
                "📣 Telegram Log"])
with tabs[0]:
    positions_tab()
with tabs[1]:
    journal_filters()
    journal_tab()
with tabs[2]:
    signals_tab()
with tabs[3]:
    c1, c2 = st.columns([1, 3])
    c1.selectbox("Minimum level", ["DEBUG", "INFO", "WARNING", "ERROR"], index=1, key="log_level")
    c2.text_input("Search", key="log_query", placeholder="filter by text or source")
    logs_tab()
with tabs[4]:
    telegram_log_tab()

st.markdown(
    f'<div class="footer-note">{esc(SETTINGS.htf)}/{esc(SETTINGS.mtf)}/{esc(SETTINGS.ltf)} · risk '
    f'{WORKER.runtime.risk_percent:.2f}% · daily stop {SETTINGS.risk.max_daily_loss_percent:.1f}% · news ±'
    f'{SETTINGS.news.blackout_before_min}m · killzones in {esc(SETTINGS.strategy.killzone_timezone)}<br>'
    '⚠️ Algorithmic trading carries substantial risk of loss. Forward-test in PAPER mode before '
    'connecting a live MT5 account. Not financial advice.</div>', unsafe_allow_html=True)
