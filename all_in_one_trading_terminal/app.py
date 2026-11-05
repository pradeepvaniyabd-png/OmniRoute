"""
app.py — All-in-One Forex & Crypto Algorithmic Trading Super-Terminal (dashboard).

Dark institutional Streamlit UI on top of the background trading worker:
  * Top telemetry bar   — balance, equity, floating P&L, daily-drawdown meter, bot status,
                           broker connection + ping, live IST/UTC clock.
  * Left control deck   — Start / Pause / PANIC, execution mode, risk %, strategy toggles,
                           one-click manual execution pad, Telegram channel control panel.
  * Main chart          — TradingView Lightweight Charts with FVG / OB zones, liquidity
                           sweeps, BOS/MSS markers, session highs/lows, EMA, live position
                           entry / SL / TP lines and detected setups.
  * Right dock          — Forex Factory terminal (IST), countdown to the next red event,
                           auto-pause toggle and per-symbol news locks.
  * Bottom tabs         — active positions manager, trade journal + analytics, signals,
                           live system logs and the Telegram broadcast log.

Run with ``run_terminal.bat`` or ``streamlit run app.py``.
"""

from __future__ import annotations

import bisect
import html
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trading Super-Terminal", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

from streamlit_lightweight_charts import renderLightweightCharts  # noqa: E402

from background_worker import TradingWorker, WorkerLockError  # noqa: E402
from config import IST, STRATEGY_CODES, ConfigError, get_settings, setup_logging  # noqa: E402
from database import Database, parse_iso  # noqa: E402
from forex_factory_engine import ForexFactoryEngine, format_countdown  # noqa: E402
from quant_strategy_engine import (  # noqa: E402
    FeatureSet,
    MarketAnalysis,
    closed_bars,
    compute_ema,
    compute_session_ranges,
)

UTC = timezone.utc
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
OVERLAYS = ["FVG", "Order Blocks", "Liquidity Sweeps", "Structure", "Sessions", "EMA", "Positions",
            "Signals", "Volume"]
DEFAULT_OVERLAYS = ["FVG", "Order Blocks", "Liquidity Sweeps", "Structure", "Sessions", "Positions",
                    "Signals", "Volume"]
STRATEGY_LABELS = {"sweep_mss_fvg": "Sweep + MSS + FVG", "order_block": "Order Block Retest",
                   "time_candle_breakout": "IST Time-Candle Breakout"}
STATUS_CLASS = {"RUNNING": "badge-running", "PAUSED": "badge-paused", "STARTING": "badge-paused",
                "NEWS LOCKED": "badge-locked", "HALTED": "badge-locked",
                "DISCONNECTED": "badge-offline"}
SESSION_COLORS = {"ASIA": "#eab308", "LONDON": "#3b82f6", "NEW_YORK": "#f97316", "NY_AM": "#f97316"}
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"]

CSS = """
<style>
:root { --bg:#0b0f17; --panel:#111827; --border:#1f2937; --text:#e5e7eb; --muted:#94a3b8;
        --green:#10b981; --red:#ef4444; --amber:#f59e0b; --cyan:#22d3ee; }
.stApp { background: radial-gradient(1400px 700px at 8% -12%, #13203a 0%, #0b0f17 55%) fixed; }
.block-container { padding-top: 0.9rem; padding-bottom: 2rem; max-width: 100% !important; }
#MainMenu, footer { visibility: hidden; }
h4 { margin-bottom: .2rem; }
.tt-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:.55rem; }
.tt-title { font-size:1.18rem; font-weight:800; letter-spacing:.06em; color:#f8fafc; }
.tt-title span { color: var(--cyan); }
.tt-clock { font-family:'JetBrains Mono','Consolas',monospace; color:var(--muted); font-size:.86rem; }
.tt-card { background:linear-gradient(180deg,#111827 0%,#0d1424 100%); border:1px solid var(--border);
           border-radius:12px; padding:10px 14px; min-height:92px; }
.tt-label { color:var(--muted); font-size:.68rem; text-transform:uppercase; letter-spacing:.09em; }
.tt-value { font-family:'JetBrains Mono','Consolas',monospace; font-size:1.22rem; font-weight:700;
            color:var(--text); margin-top:2px; }
.tt-sub { color:var(--muted); font-size:.72rem; margin-top:3px; white-space:nowrap; overflow:hidden;
          text-overflow:ellipsis; }
.tt-section { color:#cbd5e1; font-size:.74rem; font-weight:800; letter-spacing:.12em;
              margin:.35rem 0 .3rem 0; border-left:3px solid var(--cyan); padding-left:8px; }
.pos { color:#34d399 !important; } .neg { color:#f87171 !important; } .muted { color:var(--muted); }
.badge { display:inline-block; padding:3px 11px; border-radius:999px; font-weight:800; font-size:.8rem;
         letter-spacing:.05em; font-family:inherit; }
.badge-running { background:rgba(16,185,129,.15); color:#34d399; border:1px solid rgba(16,185,129,.5); }
.badge-paused { background:rgba(245,158,11,.13); color:#fbbf24; border:1px solid rgba(245,158,11,.45); }
.badge-locked { background:rgba(239,68,68,.15); color:#f87171; border:1px solid rgba(239,68,68,.5); }
.badge-offline { background:rgba(148,163,184,.12); color:#cbd5e1; border:1px solid rgba(148,163,184,.4); }
.badge-mode { background:rgba(34,211,238,.12); color:#67e8f9; border:1px solid rgba(34,211,238,.4);
              font-size:.7rem; margin-left:8px; }
.meter { height:8px; background:#1f2937; border-radius:6px; overflow:hidden; margin-top:7px; }
.meter > div { height:100%; border-radius:6px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.dot-on { background:#10b981; box-shadow:0 0 8px #10b981; } .dot-off { background:#ef4444; }
.ff-wrap { max-height:430px; overflow-y:auto; border:1px solid var(--border); border-radius:10px; }
.ff-table { width:100%; border-collapse:collapse; font-size:.74rem; }
.ff-table th { color:var(--muted); text-align:left; font-weight:700; padding:6px 5px; position:sticky;
               top:0; background:#0f172a; border-bottom:1px solid var(--border); z-index:1; }
.ff-table td { padding:5px 5px; border-bottom:1px solid rgba(31,41,55,.6); vertical-align:top; }
.ff-High td:first-child { border-left:3px solid #ef4444; } .ff-High { background:rgba(239,68,68,.07); }
.ff-Medium td:first-child { border-left:3px solid #f59e0b; } .ff-Medium { background:rgba(245,158,11,.05); }
.ff-Low td:first-child { border-left:3px solid #eab308; } .ff-Low { background:rgba(234,179,8,.035); }
.ff-Holiday td:first-child, .ff-Non-Economic td:first-child { border-left:3px solid #64748b; }
.ff-past td { opacity:.42; }
.imp { display:inline-block; width:10px; height:10px; border-radius:2px; }
.imp-High { background:#ef4444; } .imp-Medium { background:#f59e0b; } .imp-Low { background:#eab308; }
.imp-Holiday, .imp-Non-Economic { background:#64748b; }
.countdown-card { background:linear-gradient(135deg,rgba(127,29,29,.45),rgba(17,24,39,.9));
                  border:1px solid rgba(239,68,68,.45); border-radius:12px; padding:10px 12px; margin-bottom:8px; }
.countdown { font-family:'JetBrains Mono','Consolas',monospace; font-size:1.55rem; font-weight:800; color:#fca5a5; }
.chip { display:inline-block; padding:2px 8px; margin:2px 3px 2px 0; border-radius:8px; font-size:.7rem;
        border:1px solid var(--border); }
.chip-lock { color:#fca5a5; border-color:rgba(239,68,68,.55); background:rgba(239,68,68,.08); }
.chip-clear { color:#6ee7b7; border-color:rgba(16,185,129,.45); }
.chip-info { color:#cbd5e1; }
.preview { width:100%; font-size:.76rem; border-collapse:collapse; margin:.25rem 0 .45rem 0; }
.preview td, .preview th { padding:3px 5px; border-bottom:1px solid rgba(31,41,55,.7); }
.preview th { color:var(--muted); text-align:left; font-weight:700; }
.log-box { font-family:'JetBrains Mono','Consolas',monospace; font-size:.72rem; max-height:430px;
           overflow-y:auto; background:#070a10; border:1px solid var(--border); border-radius:10px;
           padding:8px 10px; line-height:1.45; }
.lv-WARNING { color:#fbbf24; } .lv-ERROR, .lv-CRITICAL { color:#f87171; } .lv-INFO { color:#cbd5e1; }
.lv-DEBUG { color:#64748b; }
.signal-card { border:1px solid var(--border); border-radius:10px; padding:8px 12px; background:#0f172a; }
.st-key-btn_panic button { background:#991b1b !important; border:1px solid #ef4444 !important;
                           color:#fff !important; font-weight:800 !important; }
.st-key-btn_buy button { background:#065f46 !important; border:1px solid #10b981 !important; color:#ecfdf5 !important; font-weight:800 !important; }
.st-key-btn_sell button { background:#7f1d1d !important; border:1px solid #ef4444 !important; color:#fef2f2 !important; font-weight:800 !important; }
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
# Formatting helpers
# --------------------------------------------------------------------------- #
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


def section(title: str) -> None:
    st.markdown(f'<div class="tt-section">{esc(title)}</div>', unsafe_allow_html=True)


def card(column: Any, label: str, value_html: str, sub_html: str = "", extra_html: str = "") -> None:
    column.markdown(f'<div class="tt-card"><div class="tt-label">{esc(label)}</div>'
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


# --------------------------------------------------------------------------- #
# Telemetry bar
# --------------------------------------------------------------------------- #
@st.fragment(run_every=UI_REFRESH)
def telemetry_bar() -> None:
    snap = WORKER.get_snapshot()
    now = datetime.now(UTC)
    mode = snap.get("mode", "PAPER")
    st.markdown(
        f'<div class="tt-header"><div class="tt-title">⚡ ALL-IN-ONE <span>TRADING SUPER-TERMINAL</span>'
        f'<span class="badge badge-mode">{esc(mode)}</span></div>'
        f'<div class="tt-clock">{now.astimezone(IST):%a %d %b %Y · %H:%M:%S} IST &nbsp;|&nbsp; '
        f'{now:%H:%M:%S} UTC</div></div>', unsafe_allow_html=True)
    account = snap.get("account") or {}
    drawdown = snap.get("drawdown") or {}
    currency = account.get("currency", "USD")
    cols = st.columns(6, gap="small")
    card(cols[0], "Balance", esc(money(account.get("balance"), currency)),
         esc(f"{account.get('name', '—')} · {account.get('server', '—')}"))
    card(cols[1], "Equity", esc(money(account.get("equity"), currency)),
         esc(f"Free margin {money(account.get('free_margin'), currency)} · 1:{account.get('leverage', '—')}"))
    floating = account.get("profit")
    css = "pos" if (floating or 0) > 0 else "neg" if (floating or 0) < 0 else ""
    open_count = len(snap.get("positions") or [])
    card(cols[2], "Floating P&L", f'<span class="{css}">{esc(money(floating, currency, signed=True))}</span>',
         esc(f"{open_count} open position(s) · {len(snap.get('orders') or [])} pending"))
    dd_pct = float(drawdown.get("drawdown_pct") or 0.0)
    limit = float(drawdown.get("limit_pct") or SETTINGS.risk.max_daily_loss_percent)
    usage = min(1.0, dd_pct / limit) if limit else 0.0
    color = "#10b981" if usage < 0.5 else "#f59e0b" if usage < 0.8 else "#ef4444"
    meter = f'<div class="meter"><div style="width:{usage * 100:.1f}%;background:{color}"></div></div>'
    card(cols[3], "Daily Drawdown", f'<span style="color:{color}">{dd_pct:.2f}%</span>'
         f'<span class="muted" style="font-size:.8rem"> / {limit:.1f}%</span>',
         esc(f"Day ref {money(drawdown.get('reference'), currency)} · {drawdown.get('trading_day', '—')}"),
         meter)
    status = snap.get("bot_status", "STARTING")
    runtime = snap.get("runtime") or {}
    card(cols[4], "Bot Status",
         f'<span class="badge {STATUS_CLASS.get(status, "badge-paused")}">{esc(status)}</span>',
         esc(f"{snap.get('status_reason', '')} · risk {runtime.get('risk_percent', 0):.2f}%"))
    connected = bool(snap.get("broker_connected"))
    ping = snap.get("ping_ms")
    detail = f"ping {ping:.0f} ms" if ping is not None else (snap.get("broker_error") or "no link")
    card(cols[5], "Broker Link",
         f'<span class="dot {"dot-on" if connected else "dot-off"}"></span>'
         f'{"CONNECTED" if connected else "OFFLINE"}',
         esc(f"{mode} · {detail} · scan {ist(snap.get('last_scan_at'), '%H:%M:%S')}"))


# --------------------------------------------------------------------------- #
# Control deck
# --------------------------------------------------------------------------- #
def _do_panic() -> None:
    st.session_state["panic_result"] = WORKER.panic_close_all("dashboard")
    st.session_state["arm_panic"] = False


def control_deck() -> None:
    runtime = WORKER.runtime
    section("🎛️ BOT MASTER CONTROLS")
    c1, c2 = st.columns(2)
    if c1.button("▶️ Start Bot", key="btn_start", type="primary", width="stretch"):
        WORKER.start_bot("dashboard")
        st.toast("Bot started — scanning for A-grade setups", icon="▶️")
    if c2.button("⏸️ Pause Bot", key="btn_pause", width="stretch"):
        WORKER.pause_bot("dashboard")
        st.toast("Bot paused — open positions stay protected", icon="⏸️")
    st.checkbox("Arm panic button", key="arm_panic")
    st.button("🚨 PANIC CLOSE ALL POSITIONS", key="btn_panic", width="stretch",
              disabled=not st.session_state.get("arm_panic"), on_click=_do_panic)
    result = st.session_state.pop("panic_result", None)
    if result:
        message = (f"Panic close: {result['closed']} closed, {result['cancelled']} orders cancelled, "
                   f"{result['failed']} failed")
        (st.success if result["ok"] else st.error)(message + ("\n\n" + "\n".join(result["errors"])
                                                             if result["errors"] else ""))

    section("⚙️ EXECUTION SETTINGS")
    modes = ["auto", "signal_only"]
    mode = st.radio("Execution mode", modes, index=modes.index(runtime.execution_mode), horizontal=True,
                    format_func=lambda m: "⚡ Auto-execute" if m == "auto" else "📡 Signal only",
                    key="exec_mode")
    if mode != runtime.execution_mode:
        WORKER.set_execution_mode(mode)
    max_risk = float(SETTINGS.risk.max_risk_percent)
    risk = st.slider("Risk per trade (%)", min_value=0.05, max_value=max_risk,
                     value=float(min(max(runtime.risk_percent, 0.05), max_risk)), step=0.05,
                     key="risk_slider")
    if abs(risk - runtime.risk_percent) > 1e-9:
        WORKER.set_risk_percent(risk)
    killzone = st.toggle("Require killzone for entries", value=runtime.require_killzone, key="kz_toggle")
    if killzone != runtime.require_killzone:
        WORKER.set_require_killzone(killzone)
    chosen = st.multiselect("Active strategies", list(STRATEGY_CODES), default=runtime.enabled_strategies,
                            format_func=lambda code: STRATEGY_LABELS.get(code, code), key="strategies")
    if chosen and set(chosen) != set(runtime.enabled_strategies):
        WORKER.set_enabled_strategies(chosen)
    elif not chosen:
        st.caption("⚠️ Select at least one strategy (current selection kept).")
    manual_pad()
    telegram_panel()


@st.fragment(run_every=5)
def manual_pad() -> None:
    section("🎯 MANUAL EXECUTION PAD")
    symbol = st.selectbox("Symbol", list(SETTINGS.symbols), key="man_symbol")
    labels = {"atr": "ATR multiple", "structure": "Last swing", "pips": "Fixed pips"}
    c1, c2 = st.columns(2)
    sl_mode = c1.selectbox("Stop-loss", list(labels), format_func=labels.get, key="man_sl_mode")
    defaults = {"atr": 1.5, "pips": 30.0, "structure": 0.0}
    sl_value = c2.number_input("ATR × / pips", min_value=0.0, value=defaults[sl_mode],
                               step=0.1 if sl_mode == "atr" else 1.0, key=f"man_sl_{sl_mode}",
                               disabled=sl_mode == "structure")
    max_risk = float(SETTINGS.risk.max_risk_percent)
    risk = st.number_input("Risk %", min_value=0.05, max_value=max_risk,
                           value=float(min(max(WORKER.runtime.risk_percent, 0.05), max_risk)), step=0.05,
                           key="man_risk")
    override = st.checkbox("Override news lock", key="man_override")
    buy = WORKER.preview_manual_trade(symbol, "BUY", risk, sl_mode, sl_value)
    sell = WORKER.preview_manual_trade(symbol, "SELL", risk, sl_mode, sl_value)
    digits = int(buy.get("digits") or sell.get("digits") or 2)

    def cell(preview: dict[str, Any], key: str, kind: str = "price") -> str:
        if not preview.get("ok") and key not in ("entry",):
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
                                 ("Lots", "lots", "lots"), ("Risk $", "risk_amount", "money")))
    st.markdown(f"<table class='preview'><tr><th></th><th>BUY</th><th>SELL</th></tr>{rows}</table>",
                unsafe_allow_html=True)
    if buy.get("news_lock"):
        st.warning(f"🔒 News lock: {buy.get('news_label')}")
    b1, b2 = st.columns(2)
    if b1.button("🟢 MARKET BUY", key="btn_buy", width="stretch", disabled=not buy.get("ok")):
        outcome = WORKER.manual_trade(symbol, "BUY", risk, sl_mode, sl_value, override_news=override)
        st.toast(f"BUY {symbol} {outcome.get('lots')} lots @ {outcome.get('price')}" if outcome.get("ok")
                 else f"BUY failed: {outcome.get('error')}", icon="🟢" if outcome.get("ok") else "⚠️")
    if b2.button("🔴 MARKET SELL", key="btn_sell", width="stretch", disabled=not sell.get("ok")):
        outcome = WORKER.manual_trade(symbol, "SELL", risk, sl_mode, sl_value, override_news=override)
        st.toast(f"SELL {symbol} {outcome.get('lots')} lots @ {outcome.get('price')}" if outcome.get("ok")
                 else f"SELL failed: {outcome.get('error')}", icon="🔴" if outcome.get("ok") else "⚠️")
    for side, preview in (("BUY", buy), ("SELL", sell)):
        if not preview.get("ok") and preview.get("error"):
            st.caption(f"{side}: {preview['error']}")


def telegram_panel() -> None:
    status = WORKER.telegram.credentials_status()
    with st.expander("📣 TELEGRAM CHANNEL CONTROL", expanded=not status["configured"]):
        st.caption(f"{'✅ Connected config' if status['configured'] else '⚠️ Not configured'} · "
                   f"source: {status['source']} · token {status['token_masked'] or '—'}")
        token = st.text_input("Bot Token", type="password", key="tg_token",
                              placeholder="leave blank to keep the current token")
        channel = st.text_input("Channel ID", value=status["channel_id"], key="tg_channel",
                                help="@public_channel or -100xxxxxxxxxx (bot must be channel admin)")
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
        if c3.button("🧪 Test Signal", key="tg_test_signal", width="stretch"):
            outcome = WORKER.send_test_signal()
            (st.success if outcome.ok else st.error)(
                "Test signal delivered" if outcome.ok else f"Failed: {outcome.error}")
        if c4.button("📰 Send News", key="tg_news", width="stretch"):
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
                            "color": "#10b981" if buy else "#ef4444",
                            "shape": "arrowUp" if buy else "arrowDown",
                            "text": f"{trade_signal.direction} {trade_signal.grade} {trade_signal.score:.1f}"})
    markers.sort(key=lambda m: m["time"])
    series.append({"type": "Candlestick", "data": candles, "markers": markers,
                   "options": {"upColor": "#10b981", "downColor": "#ef4444", "borderVisible": False,
                               "wickUpColor": "#10b981", "wickDownColor": "#ef4444",
                               "priceFormat": {"type": "price", "precision": digits, "minMove": min_move}}})

    volumes = df["volume"].to_numpy(float)
    if "Volume" in overlays and volumes.sum() > 0:
        series.append({"type": "Histogram",
                       "data": [{"time": t, "value": float(v),
                                 "color": "rgba(16,185,129,0.35)" if c >= o else "rgba(239,68,68,0.35)"}
                                for t, v, o, c in zip(times, volumes, opens, closes)],
                       "options": {"priceFormat": {"type": "volume"}, "priceScaleId": "",
                                   "lastValueVisible": False, "priceLineVisible": False},
                       "priceScale": {"scaleMargins": {"top": 0.84, "bottom": 0}}})
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
        gaps = [g for g in features.fvgs if g.active and near(g.ce)][-6:]
        for gap in gaps:
            t0 = feat_time(gap.start_pos)
            bullish = gap.direction == "BUY"
            zone = _zone(t0 or first, last, gap.top, gap.bottom,
                         "rgba(16,185,129,0.16)" if bullish else "rgba(239,68,68,0.16)",
                         "rgba(16,185,129,0.55)" if bullish else "rgba(239,68,68,0.55)") if t0 else None
            if zone:
                series.append(zone)
    if features is not None and "Order Blocks" in overlays:
        blocks = [b for b in features.order_blocks if b.active and near((b.top + b.bottom) / 2)][-4:]
        for block in blocks:
            t0 = feat_time(block.pos)
            bullish = block.direction == "BUY"
            zone = _zone(t0 or first, last, block.top, block.bottom,
                         "rgba(59,130,246,0.15)" if bullish else "rgba(236,72,153,0.15)",
                         "rgba(59,130,246,0.6)" if bullish else "rgba(236,72,153,0.6)") if t0 else None
            if zone:
                series.append(zone)
    def snap(moment: Any) -> int:
        """Latest candle time at or before ``moment`` (line points must sit on real bars)."""
        index = bisect.bisect_right(times, chart_time_of(moment, CHART_TZ)) - 1
        return times[max(0, index)]

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
                    (position.get("sl"), "#ef4444", 2, 1, "SL"),
                    (position.get("tp1"), "#6ee7b7", 3, 1, "TP1"),
                    (position.get("tp"), "#10b981", 2, 1, "TP")):
                if level:
                    line = _hline(t0, last, level, color, style, width, title)
                    if line:
                        series.append(line)
        for order in orders:
            line = _hline(first, last, order["price"], "#f59e0b", 1, 1,
                          f"{order['direction']} {order['kind']}")
            if line:
                series.append(line)
    if analysis is not None and "Signals" in overlays and analysis.best_signal is not None and not positions:
        best = analysis.best_signal
        t0 = time_of.get(pd.Timestamp(best.anchor_time), first)
        for level, color, title in ((best.entry, "rgba(59,130,246,0.7)", f"{best.direction} {best.order_type}"),
                                    (best.stop_loss, "rgba(239,68,68,0.7)", "Setup SL"),
                                    (best.tp2, "rgba(16,185,129,0.7)", "Setup TP2")):
            line = _hline(t0, last, level, color, 1, 1, title)
            if line:
                series.append(line)

    chart = {"height": 560,
             "layout": {"background": {"type": "solid", "color": "#0b0f17"}, "textColor": "#94a3b8",
                        "fontSize": 11},
             "grid": {"vertLines": {"color": "rgba(31,41,55,0.45)"},
                      "horzLines": {"color": "rgba(31,41,55,0.45)"}},
             "crosshair": {"mode": 0},
             "rightPriceScale": {"borderColor": "#1f2937", "scaleMargins": {"top": 0.06, "bottom": 0.2}},
             "timeScale": {"borderColor": "#1f2937", "timeVisible": True, "secondsVisible": False,
                           "rightOffset": 8, "barSpacing": 7},
             "watermark": {"visible": True, "fontSize": 46, "horzAlign": "center", "vertAlign": "center",
                           "color": "rgba(148,163,184,0.07)", "text": f"{symbol} · {timeframe}"}}
    return [{"chart": chart, "series": series}]


def chart_controls() -> None:
    c1, c2, c3, c4 = st.columns([1.1, 0.9, 3.4, 0.8], vertical_alignment="bottom")
    c1.selectbox("Symbol", list(SETTINGS.symbols), key="chart_symbol")
    c2.selectbox("Timeframe", TIMEFRAMES, index=TIMEFRAMES.index(SETTINGS.ltf), key="chart_tf")
    c3.multiselect("Overlays", OVERLAYS, default=DEFAULT_OVERLAYS, key="chart_overlays")
    c4.toggle("Live", value=True, key="chart_live",
              help="Auto-refresh the chart. Switch off to zoom/scroll without resets.")


def chart_body() -> None:
    symbol = st.session_state.get("chart_symbol", SETTINGS.symbols[0])
    timeframe = st.session_state.get("chart_tf", SETTINGS.ltf)
    overlays = set(st.session_state.get("chart_overlays", DEFAULT_OVERLAYS))
    snap = WORKER.get_snapshot()
    data, features = WORKER.get_chart_data(symbol, timeframe, SETTINGS.app.chart_bars)
    if data.empty:
        st.info(f"⏳ Waiting for {symbol} {timeframe} market data… "
                f"{snap.get('broker_error') or ''}")
        return
    symbol_state = (snap.get("symbols") or {}).get(symbol, {})
    digits = int(symbol_state.get("digits") or (2 if data["close"].iloc[-1] > 50 else 5))
    analysis = WORKER.get_analysis(symbol) if timeframe == SETTINGS.ltf else None
    positions = [p for p in snap.get("positions") or [] if p.get("canonical") == symbol]
    broker_symbol = symbol_state.get("broker_symbol", symbol)
    orders = [o for o in snap.get("orders") or [] if o.get("symbol") == broker_symbol]
    charts = build_chart(data, features, analysis, positions, orders, overlays, digits, symbol, timeframe)
    renderLightweightCharts(charts, key=f"lwc_{symbol}_{timeframe}")
    last_bar = data.index[-1]
    bid, ask = symbol_state.get("bid"), symbol_state.get("ask")
    st.caption(f"{symbol} {timeframe} · last {price(data['close'].iloc[-1], digits)} · bid/ask "
               f"{price(bid, digits)} / {price(ask, digits)} · spread {symbol_state.get('spread_points', '—')} pts · "
               f"bar {last_bar.tz_convert(CHART_TZ):%d %b %H:%M} ({SETTINGS.app.chart_timezone}) · "
               f"{len(data)} bars")
    render_analysis_strip(symbol)


def render_analysis_strip(symbol: str) -> None:
    analysis = WORKER.get_analysis(symbol)
    if analysis is None:
        st.caption("Strategy engine warming up — analysis appears after the first closed LTF candle.")
        return
    summary = analysis.summary()

    def chip(label: str, value: str) -> str:
        tone = "chip-clear" if value in ("BULLISH", "DISCOUNT") else "chip-lock" if value in (
            "BEARISH", "PREMIUM") else "chip-info"
        return f'<span class="chip {tone}">{esc(label)}: <b>{esc(value)}</b></span>'

    chips = "".join([
        chip(f"HTF {SETTINGS.htf}", summary["htf_bias"]), chip(f"MTF {SETTINGS.mtf}", summary["mtf_bias"]),
        chip(f"LTF {SETTINGS.ltf}", summary["ltf_trend"]), chip("DXY", summary["dxy_bias"]),
        chip("CRT", summary["crt"]), chip("Zone", summary["premium_discount"]),
        chip("Killzone", ", ".join(summary["killzones"]) or "None"),
        chip("ATR", f"{summary['atr']:.5g}"), chip("Active FVG/OB", f"{summary['active_fvgs']}/{summary['active_obs']}")])
    st.markdown(chips, unsafe_allow_html=True)
    best = analysis.best_signal
    if best is not None:
        tone = "pos" if best.direction == "BUY" else "neg"
        st.markdown(
            f'<div class="signal-card"><b class="{tone}">{esc(best.direction)} {esc(best.symbol)}</b> · '
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
def chart_live() -> None:
    chart_body()


@st.fragment
def chart_static() -> None:
    chart_body()
    st.button("🔄 Refresh chart", key="chart_refresh_btn")


# --------------------------------------------------------------------------- #
# Forex Factory dock
# --------------------------------------------------------------------------- #
def news_controls() -> None:
    section("📰 FOREX FACTORY TERMINAL")
    toggle = st.toggle("Auto-Pause Bot on High-Impact News", value=WORKER.runtime.news_auto_pause,
                       key="news_toggle")
    if toggle != WORKER.runtime.news_auto_pause:
        WORKER.set_news_auto_pause(toggle)
    c1, c2 = st.columns(2)
    c1.multiselect("Currency", CURRENCIES, default=["USD", "EUR", "GBP", "JPY"], key="ff_currencies")
    c2.multiselect("Impact", ["High", "Medium", "Low", "Holiday"], default=["High", "Medium"],
                   key="ff_impacts")
    st.radio("View", ["Today", "Next 48h", "This week"], horizontal=True, key="ff_view")


def _events_for_view(engine: ForexFactoryEngine, now: datetime) -> list[Any]:
    currencies = st.session_state.get("ff_currencies") or None
    impacts = st.session_state.get("ff_impacts") or None
    view = st.session_state.get("ff_view", "Today")
    if view == "Today":
        return engine.today_events(now, IST, currencies=currencies, impacts=impacts)
    if view == "Next 48h":
        return engine.upcoming_events(now, 48, currencies=currencies, impacts=impacts)
    return engine.get_events(currencies=currencies, impacts=impacts)


@st.fragment(run_every=UI_REFRESH)
def news_dock() -> None:
    engine = WORKER.news
    snap = WORKER.get_snapshot()
    now = datetime.now(UTC)
    upcoming = engine.next_event(now, impacts=("High",),
                                 currencies=set(SETTINGS.news.alert_currencies) | {"ALL"})
    if upcoming is not None and upcoming.time_utc is not None:
        seconds = upcoming.seconds_until(now) or 0.0
        st.markdown(
            f'<div class="countdown-card"><div class="tt-label">Next 🔴 red-folder event</div>'
            f'<div class="countdown">{esc(format_countdown(seconds))}</div>'
            f'<div class="tt-sub"><b>{esc(upcoming.currency)}</b> · {esc(upcoming.title)} · '
            f'{upcoming.time_utc.astimezone(IST):%a %H:%M} IST</div></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="countdown-card"><div class="tt-label">Next 🔴 red-folder event</div>'
                    '<div class="tt-sub">No upcoming high-impact events loaded for this week.</div></div>',
                    unsafe_allow_html=True)
    locks = snap.get("news_locks") or {}
    chips = "".join(
        f'<span class="chip {"chip-lock" if info.get("active") else "chip-clear"}">'
        f'{"🔒" if info.get("active") else "✅"} {esc(symbol)}'
        f'{" · " + esc(info.get("label")) if info.get("active") else ""}</span>'
        for symbol, info in locks.items())
    if chips:
        st.markdown(chips, unsafe_allow_html=True)
    events = _events_for_view(engine, now)
    rows = []
    for event in events[:250]:
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
            f'<td>{esc(day)} {esc(when)}</td><td>{esc(countdown)}</td><td><b>{esc(event.currency)}</b></td>'
            f'<td><span class="imp imp-{esc(event.impact)}" title="{esc(event.impact)}"></span></td>'
            f'<td>{esc(event.title)}</td><td>{esc(event.forecast or "—")}</td>'
            f'<td>{esc(event.previous or "—")}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="7" class="muted">No events for this filter.</td></tr>'
    st.markdown('<div class="ff-wrap"><table class="ff-table"><thead><tr><th>IST</th><th>In</th>'
                '<th>Cur</th><th></th><th>Event</th><th>Fcst</th><th>Prev</th></tr></thead>'
                f'<tbody>{body}</tbody></table></div>', unsafe_allow_html=True)
    status = engine.status()
    detail = f"Source: {status['source']} · {status['events']} events · updated {ist(status['fetched_at'], '%d %b %H:%M')} IST"
    if status.get("last_error"):
        detail += f" · ⚠️ {status['last_error'][:140]} (retry in {status['cooldown_seconds'] // 60} min)"
    st.caption(detail)


def news_refresh_button() -> None:
    if st.button("🔄 Refresh calendar", key="ff_refresh", width="stretch"):
        ok = WORKER.refresh_news()
        st.toast("Calendar refreshed" if ok else "Refresh skipped (rate-limited / source unavailable)",
                 icon="🔄" if ok else "⏳")


# --------------------------------------------------------------------------- #
# Bottom analytics tabs
# --------------------------------------------------------------------------- #
@st.fragment(run_every=UI_REFRESH)
def positions_tab() -> None:
    snap = WORKER.get_snapshot()
    positions = snap.get("positions") or []
    orders = snap.get("orders") or []
    if not positions:
        st.info("No open positions.")
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
                         "P&L": p["profit"], "R": round(r_now, 2) if r_now is not None else None,
                         "TP1": "✅" if p.get("tp1_hit") else "", "BE": "🛡️" if p.get("be_moved") else "",
                         "Trail": "📈" if p.get("trailing") else "",
                         "Owner": "BOT" if p.get("bot") else "MANUAL",
                         "Opened (IST)": ist(p.get("time_utc"), "%d %b %H:%M")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"P&L": st.column_config.NumberColumn(format="%.2f"),
                                    "R": st.column_config.NumberColumn(format="%.2f")})
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
        st.dataframe(pd.DataFrame([{"Ticket": o["ticket"], "Symbol": o["symbol"], "Side": o["direction"],
                                    "Type": o["kind"], "Lots": o["volume"], "Price": o["price"],
                                    "SL": o.get("sl"), "TP": o.get("tp"),
                                    "Placed (IST)": ist(o.get("time_utc"), "%d %b %H:%M")} for o in orders]),
                     hide_index=True, width="stretch")


def journal_filters() -> None:
    c1, c2, c3, c4 = st.columns([1, 1, 1, 0.7], vertical_alignment="bottom")
    c1.selectbox("Account", ["Current mode", "All modes"], key="j_scope")
    c2.selectbox("Symbol", ["All", *SETTINGS.symbols], key="j_symbol")
    c3.selectbox("Status", ["All", "OPEN", "PENDING", "CLOSED", "CANCELLED"], key="j_status")
    c4.button("🔄 Refresh", key="j_refresh", width="stretch")


@st.fragment(run_every=10)
def journal_tab() -> None:
    scope = st.session_state.get("j_scope", "Current mode")
    symbol = st.session_state.get("j_symbol", "All")
    status = st.session_state.get("j_status", "All")
    mode = WORKER.mode if scope == "Current mode" else None
    symbol_filter = None if symbol == "All" else symbol
    stats = WORKER.db.get_performance_stats(mode=mode, symbol=symbol_filter)
    pf = stats["profit_factor"]
    metrics = st.columns(8)
    metrics[0].metric("Closed trades", stats["total_trades"])
    metrics[1].metric("Win rate", f"{stats['win_rate']:.1f}%")
    metrics[2].metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}")
    metrics[3].metric("Net P&L", money(stats["net_pnl"], signed=True))
    metrics[4].metric("Expectancy", money(stats["expectancy"], signed=True))
    metrics[5].metric("Avg R", f"{stats['avg_r']:+.2f}R")
    metrics[6].metric("Max DD", money(stats["max_drawdown"]))
    metrics[7].metric("Loss streak", stats["max_consecutive_losses"])
    curve = WORKER.db.get_equity_curve(mode=mode, since=datetime.now(UTC) - timedelta(days=30))
    if curve:
        frame = pd.DataFrame(curve)
        frame["time"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(IST).dt.tz_localize(None)
        st.markdown("**Equity curve (30 days, IST)**")
        st.line_chart(frame.set_index("time")[["balance", "equity"]], height=220, width="stretch")
    breakdown = WORKER.db.get_strategy_breakdown(mode)
    if breakdown:
        st.markdown("**Strategy breakdown**")
        st.dataframe(pd.DataFrame(breakdown), hide_index=True, width="stretch")
    trades = WORKER.db.get_trades(limit=500, status=None if status == "All" else status,
                                  symbol=symbol_filter, mode=mode)
    if not trades:
        st.info("Trade journal is empty for this filter.")
        return
    frame = pd.DataFrame(trades)
    frame["confluences"] = frame["confluences"].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
    for column in ("created_at", "opened_at", "closed_at"):
        frame[column] = frame[column].apply(lambda v: ist(v, "%d %b %H:%M") if v else "")
    columns = ["id", "mode", "symbol", "direction", "order_type", "strategy", "status", "volume",
               "entry_price", "fill_price", "sl_initial", "tp1", "tp2", "exit_price", "pnl", "pips",
               "r_multiple", "close_reason", "grade", "score", "opened_at", "closed_at", "confluences"]
    st.dataframe(frame[[c for c in columns if c in frame.columns]], hide_index=True, width="stretch",
                 height=360)
    st.download_button("⬇️ Export journal CSV", frame.to_csv(index=False).encode("utf-8"),
                       file_name="trade_journal.csv", mime="text/csv", key="j_export")


@st.fragment(run_every=10)
def signals_tab() -> None:
    signals = WORKER.db.get_recent_signals(250)
    if not signals:
        st.info("No setups detected yet — the engine evaluates each newly closed LTF candle.")
        return
    frame = pd.DataFrame(signals)
    frame["time"] = frame["created_at"].apply(lambda v: ist(v, "%d %b %H:%M"))
    frame["confluences"] = frame["confluences"].apply(lambda v: " • ".join(v) if isinstance(v, list) else v)
    columns = ["time", "symbol", "direction", "strategy", "order_type", "entry", "stop_loss", "tp2",
               "score", "grade", "status", "reason", "confluences"]
    st.dataframe(frame[columns], hide_index=True, width="stretch", height=380)


@st.fragment(run_every=3)
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
def telegram_log_tab() -> None:
    rows = WORKER.db.get_recent_broadcasts(200)
    if not rows:
        st.info("No Telegram broadcasts yet.")
        return
    frame = pd.DataFrame(rows)
    frame["time"] = frame["created_at"].apply(lambda v: ist(v, "%d %b %H:%M:%S"))
    st.dataframe(frame[["time", "category", "chat_id", "status", "error", "preview"]], hide_index=True,
                 width="stretch", height=360)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.markdown(CSS, unsafe_allow_html=True)
telemetry_bar()

left, center, right = st.columns([1.15, 3.15, 1.5], gap="medium")
with left:
    control_deck()
with center:
    chart_controls()
    if st.session_state.get("chart_live", True):
        chart_live()
    else:
        chart_static()
with right:
    news_controls()
    news_dock()
    news_refresh_button()

st.markdown("<br>", unsafe_allow_html=True)
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
    c2.text_input("Search", key="log_query", placeholder="filter by text / source")
    logs_tab()
with tabs[4]:
    telegram_log_tab()

st.caption("⚠️ Algorithmic trading carries substantial risk of loss. Forward-test in PAPER mode before "
           "connecting a live MT5 account. Not financial advice.")
