"""
telegram_broadcaster.py — Asynchronous, fault-tolerant Telegram Bot API broadcaster.

* A dedicated thread runs an asyncio event loop; every other thread (trading
  loops, Streamlit UI) enqueues messages thread-safely and never blocks.
* One worker task per chat keeps per-chat ordering and enforces Telegram's
  limits: ~1 msg/s per private chat, ~20 msg/min per channel/group and
  ~30 msg/s globally.  HTTP 429 ``retry_after`` is honoured exactly.
* Network errors and 5xx responses are retried with exponential back-off;
  other 4xx errors are permanent.  HTML Telegram cannot parse is resent as text.
* Rich HTML templates: VIP trade-signal cards, TP1 / breakeven / trailing /
  close updates (threaded as replies to the original signal), Forex Factory
  daily digest, 15-minute pre-news warning and emergency alerts.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from config import IST, TelegramSettings

log = logging.getLogger("telegram")

UTC = timezone.utc
MAX_MESSAGE_LEN = 4096
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
IMPACT_ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪", "Non-Economic": "⚪"}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def fmt_price(value: Any, digits: int) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{max(0, int(digits))}f}"
    except (TypeError, ValueError):
        return esc(value)


def fmt_money(value: Any, currency: str = "USD", signed: bool = True) -> str:
    amount = float(value or 0.0)
    sign = ("+" if amount > 0 else "-" if amount < 0 else "") if signed else ""
    if currency.upper() == "USD":
        return f"{sign}${abs(amount):,.2f}"
    return f"{sign}{abs(amount):,.2f} {esc(currency)}"


def fmt_signed(value: Any, suffix: str = "", decimals: int = 1) -> str:
    number = float(value or 0.0)
    return f"{'+' if number > 0 else ''}{number:,.{decimals}f}{suffix}"


def fmt_time_pair(moment: datetime | None) -> str:
    moment = moment or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{moment.astimezone(IST):%d %b %H:%M} IST | {moment.astimezone(UTC):%H:%M} UTC"


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split on line boundaries so no HTML tag is cut (templates keep tags per line)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def mask_token(token: str) -> str:
    if not token:
        return ""
    return token if len(token) <= 10 else f"{token[:6]}…{token[-4:]}"


# --------------------------------------------------------------------------- #
# Message templates
# --------------------------------------------------------------------------- #
def format_trade_signal(sig: Mapping[str, Any], brand: str = "", mode: str = "",
                        currency: str = "USD") -> str:
    """Institutional signal card for an executed trade or a detected A+ setup."""
    direction = str(sig.get("direction", "")).upper()
    is_buy = direction == "BUY"
    digits = int(sig.get("digits", 2))
    pip = float(sig.get("pip_size") or 0.0)
    entry = float(sig["entry"])
    stop = float(sig["stop_loss"])
    tp1 = float(sig["tp1"])
    tp2 = float(sig["tp2"])
    risk_distance = abs(entry - stop)
    rr = float(sig.get("rr") or (abs(tp2 - entry) / risk_distance if risk_distance else 0.0))
    tp1_rr = float(sig.get("tp1_rr") or (abs(tp1 - entry) / risk_distance if risk_distance else 0.0))
    order_type = str(sig.get("order_type", "MARKET")).upper()
    entry_label = "Market" if order_type == "MARKET" else f"{order_type.title()} order"
    executed = str(sig.get("status", "SIGNAL")).upper() == "EXECUTED"

    def pips(distance: float) -> str:
        return f" ({distance / pip:,.1f} pips)" if pip > 0 else ""

    lines: list[str] = []
    if sig.get("is_test"):
        lines.append("🧪 <b>TEST SIGNAL — NOT A LIVE TRADE</b>")
    lines += [
        f"{'🟢' if is_buy else '🔴'} <b>{esc(direction)} {esc(sig.get('symbol'))}</b>  ·  "
        f"{'⚡ TRADE EXECUTED' if executed else '📡 A+ SETUP ALERT'}",
        f"<i>{esc(sig.get('strategy', 'Algorithmic Setup'))}</i>  ·  {esc(sig.get('timeframe', ''))}",
        DIVIDER,
        f"📍 <b>Entry:</b> <code>{fmt_price(entry, digits)}</code> ({esc(entry_label)})",
        f"🛑 <b>Stop Loss:</b> <code>{fmt_price(stop, digits)}</code>{pips(risk_distance)}",
        f"🎯 <b>TP1:</b> <code>{fmt_price(tp1, digits)}</code>{pips(abs(tp1 - entry))} · 1:{tp1_rr:.1f}",
        f"🎯 <b>TP2:</b> <code>{fmt_price(tp2, digits)}</code>{pips(abs(tp2 - entry))} · 1:{rr:.1f}",
        f"⚖️ <b>Risk-to-Reward:</b> 1:{rr:.1f}",
    ]
    lots = sig.get("lots")
    if lots:
        risk_pct = float(sig.get("risk_percent") or 0.0)
        risk_amount = float(sig.get("risk_amount") or 0.0)
        label = "Lot Size" if executed else "Suggested Lot"
        lines.append(f"📦 <b>{label}:</b> {float(lots):g}  ·  Risk {risk_pct:.2f}% "
                     f"({fmt_money(risk_amount, currency, signed=False)})")
    if sig.get("score") is not None:
        lines.append(f"⭐ <b>Setup Grade:</b> {esc(sig.get('grade', '-'))} "
                     f"({float(sig['score']):.1f}/10)")
    confluences = list(sig.get("confluences") or [])
    if confluences:
        lines.append("🧩 <b>Confluence:</b> " + " • ".join(esc(c) for c in confluences[:9]))
    created = sig.get("created_at")
    lines.append(f"🕒 {fmt_time_pair(created if isinstance(created, datetime) else None)}")
    lines.append(DIVIDER)
    footer = f"🏦 {esc(brand)}" if brand else "🏦 Algo Desk"
    if mode:
        footer += f" · {esc(mode)}"
    lines.append(footer)
    lines.append("<i>⚠️ Not financial advice. Manage your risk.</i>")
    return "\n".join(lines)


def format_trade_update(kind: str, data: Mapping[str, Any], currency: str = "USD") -> str:
    """TP1 / breakeven / trailing / fill / expiry / close notifications."""
    kind = kind.upper()
    symbol = esc(data.get("symbol", ""))
    direction = esc(str(data.get("direction", "")).upper())
    digits = int(data.get("digits", 2))
    pip = float(data.get("pip_size") or 0.0)
    entry = data.get("entry")
    price = data.get("price")
    tag = f"{symbol} {direction}".strip()
    lines: list[str] = []

    def pips_from_entry(level: Any) -> str:
        if pip <= 0 or entry is None or level is None:
            return ""
        sign = 1 if str(data.get("direction", "")).upper() == "BUY" else -1
        return f" | {fmt_signed((float(level) - float(entry)) * sign / pip, ' pips')}"

    if kind == "TP1_HIT":
        lines.append(f"🎯 <b>TP1 HIT — {tag}</b>")
        lines.append(f"Price: <code>{fmt_price(price, digits)}</code>{pips_from_entry(price)}")
        if data.get("closed_volume"):
            lines.append(f"✂️ Partial close: {float(data['closed_volume']):g} lots booked "
                         f"({fmt_money(data.get('pnl'), currency)})")
        lines.append("🛡️ Runner protected — stop moving to breakeven.")
    elif kind == "BREAKEVEN":
        lines.append(f"🛡️ <b>SL MOVED TO BREAKEVEN — {tag}</b>")
        lines.append(f"New SL: <code>{fmt_price(data.get('sl'), digits)}</code> "
                     f"(entry <code>{fmt_price(entry, digits)}</code>)")
        lines.append("✅ Trade is now risk-free.")
    elif kind == "TRAILING_ON":
        lines.append(f"📈 <b>TRAILING STOP ACTIVATED — {tag}</b>")
        lines.append(f"Trailing SL: <code>{fmt_price(data.get('sl'), digits)}</code>"
                     f"{pips_from_entry(data.get('sl'))} locked")
    elif kind == "TRAIL_UPDATE":
        lines.append(f"🔁 <b>Trailing SL updated — {tag}</b>")
        lines.append(f"New SL: <code>{fmt_price(data.get('sl'), digits)}</code>"
                     f"{pips_from_entry(data.get('sl'))} locked")
    elif kind == "PENDING_FILLED":
        lines.append(f"✅ <b>ENTRY TRIGGERED — {tag}</b>")
        lines.append(f"Filled @ <code>{fmt_price(price, digits)}</code> · "
                     f"SL <code>{fmt_price(data.get('sl'), digits)}</code> · "
                     f"TP <code>{fmt_price(data.get('tp'), digits)}</code>")
    elif kind == "PENDING_CANCELLED":
        lines.append(f"⌛ <b>ORDER CANCELLED — {tag}</b>")
        lines.append(esc(data.get("reason") or "Limit order expired before it was filled."))
    elif kind == "CLOSED":
        pnl = float(data.get("pnl") or 0.0)
        reason = str(data.get("reason") or "").upper()
        if reason == "TP":
            title = "🏆 <b>TAKE PROFIT HIT"
        elif reason in ("SL", "STOP_OUT") and pnl < 0:
            title = "❌ <b>STOP LOSS HIT"
        elif reason in ("SL", "STOP_OUT"):
            title = "🛡️ <b>CLOSED AT PROTECTED STOP"
        else:
            label = {"PANIC": "Panic Close", "CIRCUIT_BREAKER": "Daily Loss Breaker",
                     "NEWS": "News Protection", "MANUAL": "Manual", "EXPERT": "Bot",
                     "": "Closed"}.get(reason, reason.replace("_", " ").title())
            title = f"⏹️ <b>POSITION CLOSED ({esc(label)})"
        lines.append(f"{title} — {tag}</b>")
        lines.append(f"Exit: <code>{fmt_price(price, digits)}</code> · "
                     f"Entry: <code>{fmt_price(entry, digits)}</code>")
        result = f"💰 <b>Net P&amp;L: {fmt_money(pnl, currency)}</b>"
        if data.get("pips") is not None:
            result += f" | {fmt_signed(data['pips'], ' pips')}"
        if data.get("r_multiple") is not None:
            result += f" | {fmt_signed(data['r_multiple'], 'R', 2)}"
        lines.append(result)
    else:
        lines.append(f"ℹ️ <b>{esc(kind.replace('_', ' ').title())} — {tag}</b>")
        if data.get("message"):
            lines.append(esc(data["message"]))
    if data.get("strategy"):
        lines.append(f"<i>{esc(data['strategy'])}</i>")
    lines.append(f"🕒 {fmt_time_pair(data.get('time') if isinstance(data.get('time'), datetime) else None)}")
    return "\n".join(lines)


def format_news_digest(events: Iterable[Any], day_label: str, brand: str = "") -> str:
    events = list(events)
    lines = [f"📰 <b>DAILY RED-FOLDER NEWS — {esc(day_label)}</b>",
             "<i>High-impact Forex Factory events · times in IST</i>", DIVIDER]
    if not events:
        lines.append("✅ No high-impact events scheduled today. Normal volatility conditions.")
    for event in events:
        moment = _get(event, "time_utc")
        when = moment.astimezone(IST).strftime("%H:%M") if isinstance(moment, datetime) else "All Day"
        icon = IMPACT_ICON.get(str(_get(event, "impact", "")), "🔴")
        lines.append(f"{icon} <b>{esc(when)}</b> · <b>{esc(_get(event, 'currency'))}</b> · "
                     f"{esc(_get(event, 'title'))}")
        forecast = _get(event, "forecast") or "—"
        previous = _get(event, "previous") or "—"
        lines.append(f"      Forecast: <code>{esc(forecast)}</code> | Previous: <code>{esc(previous)}</code>")
    lines.append(DIVIDER)
    if events:
        lines.append("🧭 Expect spikes & wide spreads ±15 min around each release. "
                     "Auto-bot pauses new entries during the blackout.")
    lines.append(f"🏦 {esc(brand)}" if brand else "🏦 Algo Desk")
    return "\n".join(lines)


def format_news_warning(event: Any, minutes_left: float, affected: Sequence[str] = (),
                        tier1: bool = False, blackout_minutes: int = 15) -> str:
    moment = _get(event, "time_utc")
    ist = moment.astimezone(IST).strftime("%H:%M IST") if isinstance(moment, datetime) else "TBA"
    utc = moment.astimezone(UTC).strftime("%H:%M UTC") if isinstance(moment, datetime) else ""
    minutes = max(0, int(round(minutes_left)))
    lines = [
        "🚨 <b>HIGH IMPACT NEWS WARNING</b> 🚨",
        DIVIDER,
        f"{IMPACT_ICON.get(str(_get(event, 'impact', 'High')), '🔴')} <b>{esc(_get(event, 'currency'))} — "
        f"{esc(_get(event, 'title'))}</b>" + ("  [🔥 TIER-1]" if tier1 else ""),
        f"⏰ Release: <b>{esc(ist)}</b> ({esc(utc)}) — in <b>{minutes} min</b>",
        f"📊 Forecast: <code>{esc(_get(event, 'forecast') or '—')}</code> | "
        f"Previous: <code>{esc(_get(event, 'previous') or '—')}</code>",
    ]
    if affected:
        lines.append("💱 Affected: " + ", ".join(esc(s) for s in affected))
    lines += [
        DIVIDER,
        "⚠️ Spreads widen and price can spike both ways. Tighten stops, reduce size or stay flat.",
        f"🤖 Bot: new entries paused ±{blackout_minutes} min around the release.",
    ]
    return "\n".join(lines)


def format_emergency_alert(title: str, lines: Iterable[str]) -> str:
    body = [f"🚨🚨 <b>{esc(title)}</b> 🚨🚨", DIVIDER]
    body += [esc(line) for line in lines]
    body.append(f"🕒 {fmt_time_pair(None)}")
    return "\n".join(body)


def format_status_alert(title: str, detail: str = "", icon: str = "ℹ️") -> str:
    text = f"{icon} <b>{esc(title)}</b>"
    if detail:
        text += f"\n{esc(detail)}"
    return f"{text}\n🕒 {fmt_time_pair(None)}"


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None
    status_code: int | None = None
    chunks: int = 0


@dataclass
class OutgoingMessage:
    chat_id: str
    text: str
    category: str
    dedupe_key: str | None = None
    ref_id: str | None = None
    reply_to_message_id: int | None = None
    disable_notification: bool = False
    row_id: int | None = None
    on_sent: Callable[[SendResult], None] | None = None
    future: Future | None = None


class TelegramBroadcaster:
    """Thread-safe facade over an asyncio Telegram dispatcher running in its own thread."""

    GLOBAL_INTERVAL = 1.0 / 25.0
    CHANNEL_INTERVAL = 3.1
    PRIVATE_INTERVAL = 1.05
    MAX_ATTEMPTS = 6
    MAX_QUEUE_PER_CHAT = 500
    BACKOFF_SCALE = 1.0  # multiplier on the 2^n second back-off (tests shrink it)
    CREDENTIALS_KEY = "telegram_credentials_v1"

    def __init__(self, settings: TelegramSettings, db: Any = None, *, mode_label: str = "",
                 currency: str = "USD", transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.db = db
        self.mode_label = mode_label
        self.currency = currency
        self._transport = transport
        self._cred_lock = threading.RLock()
        self._token = settings.bot_token
        self._channel = settings.channel_id
        self._admin = settings.admin_chat_id
        self._credentials_source = "env"
        if db is not None:
            stored = db.kv_get(self.CREDENTIALS_KEY)
            if isinstance(stored, dict) and stored.get("bot_token"):
                self._token = str(stored.get("bot_token") or "")
                self._channel = str(stored.get("channel_id") or self._channel)
                self._admin = str(stored.get("admin_chat_id") or self._admin)
                self._credentials_source = "dashboard"
        self._start_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._client: httpx.AsyncClient | None = None
        self._global_lock: asyncio.Lock | None = None
        self._global_next = 0.0
        self._chat_queues: dict[str, asyncio.Queue] = {}
        self._chat_tasks: dict[str, asyncio.Task] = {}
        self._chat_next: dict[str, float] = {}
        self._stats_lock = threading.Lock()
        self._inflight = 0
        self.stats: dict[str, Any] = {"sent": 0, "failed": 0, "dropped": 0, "retries": 0,
                                      "last_error": None, "last_sent_at": None}

    # ------------------------------------------------------------ credentials --
    @property
    def token(self) -> str:
        with self._cred_lock:
            return self._token

    @property
    def channel_id(self) -> str:
        with self._cred_lock:
            return self._channel

    @property
    def admin_chat_id(self) -> str:
        with self._cred_lock:
            return self._admin

    @property
    def configured(self) -> bool:
        return bool(self.settings.enabled and self.token)

    def update_credentials(self, bot_token: str | None = None, channel_id: str | None = None,
                           admin_chat_id: str | None = None, persist: bool = True) -> None:
        with self._cred_lock:
            if bot_token is not None and bot_token.strip():
                self._token = bot_token.strip()
            if channel_id is not None:
                self._channel = channel_id.strip()
            if admin_chat_id is not None:
                self._admin = admin_chat_id.strip()
            self._credentials_source = "dashboard"
            snapshot = {"bot_token": self._token, "channel_id": self._channel,
                        "admin_chat_id": self._admin}
        if persist and self.db is not None:
            self.db.kv_set(self.CREDENTIALS_KEY, snapshot)
        log.info("Telegram credentials updated from dashboard (channel=%s, admin=%s)",
                 snapshot["channel_id"] or "-", snapshot["admin_chat_id"] or "-")

    def clear_saved_credentials(self) -> None:
        if self.db is not None:
            self.db.kv_delete(self.CREDENTIALS_KEY)
        with self._cred_lock:
            self._token = self.settings.bot_token
            self._channel = self.settings.channel_id
            self._admin = self.settings.admin_chat_id
            self._credentials_source = "env"

    def credentials_status(self) -> dict[str, Any]:
        with self._cred_lock:
            return {"configured": self.configured, "enabled": self.settings.enabled,
                    "token_masked": mask_token(self._token), "channel_id": self._channel,
                    "admin_chat_id": self._admin, "source": self._credentials_source}

    def stats_snapshot(self) -> dict[str, Any]:
        with self._stats_lock:
            data = dict(self.stats)
            data["queued"] = self._inflight
        data["running"] = bool(self._loop and self._loop.is_running())
        data.update(self.credentials_status())
        return data

    # --------------------------------------------------------------- lifecycle --
    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run_loop, name="telegram-dispatcher",
                                            daemon=True)
            self._thread.start()
        if not self._ready.wait(10):
            log.error("Telegram dispatcher did not start within 10s")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_start())
            self._ready.set()
            loop.run_forever()
        except Exception:  # pragma: no cover - defensive
            log.exception("Telegram dispatcher loop crashed")
        finally:
            try:
                loop.run_until_complete(self._async_close())
            except Exception:  # pragma: no cover - defensive
                log.exception("Telegram dispatcher cleanup failed")
            loop.close()
            self._loop = None
            self._ready.set()

    async def _async_start(self) -> None:
        self._global_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.timeout, connect=10.0),
            transport=self._transport,
            headers={"User-Agent": "TradingSuperTerminal/1.0"},
        )

    async def _async_close(self) -> None:
        tasks = list(self._chat_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._chat_tasks.clear()
        self._chat_queues.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def pending_count(self) -> int:
        with self._stats_lock:
            return self._inflight

    def stop(self, timeout: float = 5.0) -> None:
        loop = self._loop
        if loop is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.pending_count() > 0:
            time.sleep(0.1)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ queue --
    def enqueue(self, msg: OutgoingMessage) -> bool:
        """Queue a message from any thread. Returns False if disabled, unconfigured or duplicate."""
        if not self.configured or not msg.chat_id:
            return False
        if self._loop is None or not self._loop.is_running():
            self.start()
        loop = self._loop
        if loop is None:
            return False
        if self.db is not None:
            try:
                preview = strip_html(msg.text)[:300]
                if msg.dedupe_key:
                    row = self.db.reserve_broadcast(msg.category, msg.dedupe_key, msg.chat_id,
                                                    msg.ref_id, preview)
                    if row is None:
                        return False
                else:
                    row = self.db.log_broadcast(msg.category, msg.chat_id, msg.ref_id, preview)
                msg.row_id = row
            except Exception:
                log.exception("Could not record Telegram broadcast bookkeeping")
        with self._stats_lock:
            self._inflight += 1
        try:
            loop.call_soon_threadsafe(self._accept, msg)
        except RuntimeError:  # loop closed between checks
            with self._stats_lock:
                self._inflight -= 1
            if self.db is not None and msg.dedupe_key:
                self.db.release_broadcast(msg.dedupe_key)
            return False
        return True

    def _accept(self, msg: OutgoingMessage) -> None:
        queue = self._chat_queues.get(msg.chat_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.MAX_QUEUE_PER_CHAT)
            self._chat_queues[msg.chat_id] = queue
            self._chat_tasks[msg.chat_id] = asyncio.ensure_future(self._chat_worker(msg.chat_id, queue))
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            with self._stats_lock:
                self.stats["dropped"] += 1
            self._finish(msg, SendResult(False, error="per-chat queue full"))

    async def _chat_worker(self, chat_id: str, queue: asyncio.Queue) -> None:
        while True:
            msg: OutgoingMessage = await queue.get()
            result = SendResult(False, error="internal error")
            try:
                result = await self._deliver(msg)
            except asyncio.CancelledError:
                self._finish(msg, SendResult(False, error="dispatcher stopped"))
                raise
            except Exception as exc:
                log.exception("Unexpected Telegram delivery error for %s", chat_id)
                result = SendResult(False, error=type(exc).__name__)
            finally:
                queue.task_done()
            self._finish(msg, result)

    def _finish(self, msg: OutgoingMessage, result: SendResult) -> None:
        with self._stats_lock:
            self._inflight = max(0, self._inflight - 1)
            if result.ok:
                self.stats["sent"] += 1
                self.stats["last_sent_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            else:
                self.stats["failed"] += 1
                self.stats["last_error"] = result.error
        if self.db is not None and msg.row_id:
            try:
                self.db.mark_broadcast(msg.row_id, "SENT" if result.ok else "FAILED",
                                       result.message_id, result.error)
            except Exception:
                log.exception("Could not update broadcast status")
        if result.ok and msg.on_sent is not None:
            try:
                msg.on_sent(result)
            except Exception:
                log.exception("Telegram on_sent callback failed")
        if msg.future is not None and not msg.future.done():
            msg.future.set_result(result)
        if not result.ok:
            log.warning("Telegram %s to %s failed: %s", msg.category, msg.chat_id, result.error)

    # -------------------------------------------------------------- transport --
    def _url(self, method: str) -> str:
        return f"{self.settings.api_base}/bot{self.token}/{method}"

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"ok": False, "description": str(data)[:200]}
        except ValueError:
            return {"ok": False, "description": response.text[:200]}

    def _chat_interval(self, chat_id: str) -> float:
        return self.CHANNEL_INTERVAL if str(chat_id).startswith(("-", "@")) else self.PRIVATE_INTERVAL

    async def _throttle(self, chat_id: str) -> None:
        loop = asyncio.get_running_loop()
        wait = self._chat_next.get(chat_id, 0.0) - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        assert self._global_lock is not None
        async with self._global_lock:
            now = loop.time()
            global_wait = self._global_next - now
            if global_wait > 0:
                await asyncio.sleep(global_wait)
            self._global_next = max(loop.time(), self._global_next) + self.GLOBAL_INTERVAL
        self._chat_next[chat_id] = loop.time() + self._chat_interval(chat_id)

    async def _deliver(self, msg: OutgoingMessage) -> SendResult:
        chunks = split_message(msg.text)
        first: SendResult | None = None
        for index, chunk in enumerate(chunks):
            result = await self._send_with_retry(
                msg.chat_id, chunk, reply_to=msg.reply_to_message_id if index == 0 else None,
                silent=msg.disable_notification)
            if not result.ok:
                if first is not None:
                    return SendResult(False, first.message_id, result.error, result.status_code, index)
                return result
            if first is None:
                first = result
        return SendResult(True, first.message_id if first else None, chunks=len(chunks),
                          status_code=200)

    async def _send_with_retry(self, chat_id: str, text: str, reply_to: int | None = None,
                               silent: bool = False) -> SendResult:
        assert self._client is not None
        parse_mode: str | None = "HTML"
        attempt = 0
        status: int | None = None
        last_error = "unknown error"
        loop = asyncio.get_running_loop()
        while attempt < self.MAX_ATTEMPTS:
            attempt += 1
            await self._throttle(chat_id)
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                       "link_preview_options": {"is_disabled": True},
                                       "disable_notification": bool(silent)}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_to:
                payload["reply_parameters"] = {"message_id": int(reply_to),
                                               "allow_sending_without_reply": True}
            try:
                response = await self._client.post(self._url("sendMessage"), json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"network error ({type(exc).__name__})"
                backoff = min(2.0 ** attempt, 60.0) * self.BACKOFF_SCALE
                with self._stats_lock:
                    self.stats["retries"] += 1
                log.warning("Telegram %s, retrying in %.0fs (attempt %s/%s)", last_error, backoff,
                            attempt, self.MAX_ATTEMPTS)
                await asyncio.sleep(backoff)
                continue
            status = response.status_code
            data = self._json(response)
            if status == 200 and data.get("ok"):
                result = data.get("result") or {}
                return SendResult(True, message_id=result.get("message_id"), status_code=200)
            description = str(data.get("description") or "")
            last_error = f"HTTP {status}: {description}".strip()
            lowered = description.lower()
            if status == 429:
                retry_after = float((data.get("parameters") or {}).get("retry_after", 5))
                self._chat_next[chat_id] = loop.time() + retry_after
                with self._stats_lock:
                    self.stats["retries"] += 1
                log.warning("Telegram flood control on %s — waiting %.0fs", chat_id, retry_after)
                await asyncio.sleep(retry_after + 0.5 * self.BACKOFF_SCALE)
                continue
            if status == 400 and parse_mode and "parse entities" in lowered:
                log.warning("Telegram rejected HTML (%s); resending as plain text", description)
                parse_mode = None
                text = strip_html(text)
                attempt -= 1
                continue
            if status == 400 and reply_to and ("repl" in lowered):
                reply_to = None
                attempt -= 1
                continue
            if status >= 500:
                backoff = min(2.0 ** attempt, 60.0) * self.BACKOFF_SCALE
                with self._stats_lock:
                    self.stats["retries"] += 1
                await asyncio.sleep(backoff)
                continue
            return SendResult(False, error=last_error, status_code=status)  # permanent 4xx
        return SendResult(False, error=f"gave up after {self.MAX_ATTEMPTS} attempts: {last_error}",
                          status_code=status)

    async def _api_call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._client is not None
        try:
            response = await self._client.post(self._url(method), json=payload or {})
        except httpx.HTTPError as exc:
            return {"ok": False, "description": f"network error ({type(exc).__name__})"}
        data = self._json(response)
        if response.status_code != 200:
            data["ok"] = False
        return data

    async def _probe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "bot": None, "channel": None, "channel_admin": None,
                               "admin_chat": None, "error": None}
        me = await self._api_call("getMe")
        if not me.get("ok"):
            out["error"] = me.get("description", "getMe failed")
            return out
        bot = me.get("result") or {}
        out["bot"] = f"@{bot.get('username', '?')}"
        channel = self.channel_id
        if channel:
            chat = await self._api_call("getChat", {"chat_id": channel})
            if chat.get("ok"):
                out["channel"] = (chat.get("result") or {}).get("title") or channel
                member = await self._api_call("getChatMember",
                                              {"chat_id": channel, "user_id": bot.get("id")})
                status = (member.get("result") or {}).get("status") if member.get("ok") else None
                out["channel_admin"] = status in ("administrator", "creator")
            else:
                out["error"] = f"Channel: {chat.get('description', 'not reachable')}"
                return out
        admin = self.admin_chat_id
        if admin:
            chat = await self._api_call("getChat", {"chat_id": admin})
            out["admin_chat"] = bool(chat.get("ok"))
        out["ok"] = True
        return out

    # ---------------------------------------------------------- sync helpers --
    def send_now(self, chat_id: str, text: str, category: str = "TEST",
                 timeout: float = 30.0) -> SendResult:
        """Send and wait for the outcome (used by dashboard buttons)."""
        if not self.configured:
            return SendResult(False, error="Telegram bot token not configured or Telegram disabled")
        if not chat_id:
            return SendResult(False, error="Chat / channel id not configured")
        future: Future = Future()
        if not self.enqueue(OutgoingMessage(chat_id=str(chat_id), text=text, category=category,
                                            future=future)):
            return SendResult(False, error="Message was not queued")
        try:
            return future.result(timeout)
        except FutureTimeout:
            return SendResult(False, error=f"No confirmation after {timeout:.0f}s (still queued)")

    def test_connection(self, timeout: float = 20.0) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "error": "Bot token missing"}
        self.start()
        loop = self._loop
        if loop is None:
            return {"ok": False, "error": "Dispatcher not running"}
        future = asyncio.run_coroutine_threadsafe(self._probe(), loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            return {"ok": False, "error": f"Timed out after {timeout:.0f}s"}

    # -------------------------------------------------------- high-level API --
    def _signal_target(self) -> str:
        return self.channel_id or self.admin_chat_id

    def _news_target(self) -> str:
        return (self.channel_id if self.settings.news_to_channel else "") or self.admin_chat_id

    def broadcast_trade_signal(self, signal: Mapping[str, Any], *,
                               on_sent: Callable[[SendResult], None] | None = None,
                               dedupe_key: str | None = None) -> bool:
        text = format_trade_signal(signal, self.settings.brand_name, self.mode_label, self.currency)
        return self.enqueue(OutgoingMessage(chat_id=self._signal_target(), text=text,
                                            category="TRADE_SIGNAL", dedupe_key=dedupe_key,
                                            ref_id=str(signal.get("signal_id") or ""),
                                            on_sent=on_sent))

    def broadcast_trade_update(self, kind: str, data: Mapping[str, Any], *,
                               reply_to: int | None = None, dedupe_key: str | None = None) -> bool:
        if not self.settings.broadcast_updates:
            return False
        text = format_trade_update(kind, data, self.currency)
        return self.enqueue(OutgoingMessage(chat_id=self._signal_target(), text=text,
                                            category="TRADE_UPDATE", dedupe_key=dedupe_key,
                                            ref_id=str(data.get("ticket") or ""),
                                            reply_to_message_id=reply_to))

    def broadcast_news_digest(self, events: Iterable[Any], day_label: str, *,
                              dedupe_key: str | None = None) -> bool:
        text = format_news_digest(events, day_label, self.settings.brand_name)
        return self.enqueue(OutgoingMessage(chat_id=self._news_target(), text=text,
                                            category="NEWS_DIGEST", dedupe_key=dedupe_key))

    def broadcast_news_warning(self, event: Any, minutes_left: float, affected: Sequence[str] = (),
                               *, tier1: bool = False, blackout_minutes: int = 15,
                               dedupe_key: str | None = None) -> bool:
        text = format_news_warning(event, minutes_left, affected, tier1, blackout_minutes)
        return self.enqueue(OutgoingMessage(chat_id=self._news_target(), text=text,
                                            category="NEWS_WARNING", dedupe_key=dedupe_key,
                                            ref_id=str(_get(event, "event_id") or "")))

    def alert_admin(self, text_html: str, *, also_channel: bool = False,
                    dedupe_key: str | None = None, category: str = "ALERT") -> bool:
        sent = False
        admin = self.admin_chat_id
        if admin:
            sent = self.enqueue(OutgoingMessage(chat_id=admin, text=text_html, category=category,
                                                dedupe_key=f"{dedupe_key}:admin" if dedupe_key else None))
        channel = self.channel_id
        if also_channel and channel and channel != admin:
            sent = self.enqueue(OutgoingMessage(
                chat_id=channel, text=text_html, category=category,
                dedupe_key=f"{dedupe_key}:channel" if dedupe_key else None)) or sent
        return sent

    def emergency(self, title: str, lines: Iterable[str], *, also_channel: bool = True,
                  dedupe_key: str | None = None) -> bool:
        return self.alert_admin(format_emergency_alert(title, lines), also_channel=also_channel,
                                dedupe_key=dedupe_key, category="EMERGENCY")
