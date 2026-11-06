from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from config import load_settings
from forex_factory_engine import EconomicEvent
from telegram_broadcaster import (
    OutgoingMessage,
    TelegramBroadcaster,
    format_news_digest,
    format_news_warning,
    format_trade_signal,
    format_trade_update,
    split_message,
    strip_html,
)

UTC = timezone.utc

SIGNAL = {"symbol": "XAUUSD", "direction": "BUY", "strategy": "SMC <Sweep> & FVG", "timeframe": "H1/M15/M5",
          "entry": 2345.6, "stop_loss": 2341.2, "tp1": 2352.2, "tp2": 2358.8, "rr": 3.0, "tp1_rr": 1.5,
          "order_type": "MARKET", "lots": 0.12, "risk_percent": 1.0, "risk_amount": 52.8,
          "score": 8.6, "grade": "A+", "confluences": ["SSL Sweep", "MSS", "FVG"], "digits": 2,
          "pip_size": 0.1, "created_at": datetime(2026, 11, 5, 9, 2, tzinfo=UTC), "status": "EXECUTED"}


class Recorder:
    """httpx MockTransport handler that scripts Telegram API responses."""

    def __init__(self, script=None):
        self.requests: list[dict] = []
        self.script = list(script or [])
        self.next_id = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"method": request.url.path.rsplit("/", 1)[-1], **body})
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})


@pytest.fixture
def make_broadcaster(base_env, db):
    created: list[TelegramBroadcaster] = []

    def factory(recorder: Recorder, **env) -> TelegramBroadcaster:
        settings = load_settings({**base_env, "TELEGRAM_BOT_TOKEN": "123:ABC",
                                  "TELEGRAM_CHANNEL_ID": "@vip", "TELEGRAM_ADMIN_CHAT_ID": "999", **env})
        broadcaster = TelegramBroadcaster(settings.telegram, db, mode_label="PAPER",
                                          transport=httpx.MockTransport(recorder))
        broadcaster.CHANNEL_INTERVAL = broadcaster.PRIVATE_INTERVAL = 0.0
        broadcaster.BACKOFF_SCALE = 0.001
        broadcaster.start()
        created.append(broadcaster)
        return broadcaster

    yield factory
    for broadcaster in created:
        broadcaster.stop(timeout=2)


def wait_idle(broadcaster: TelegramBroadcaster, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while broadcaster.pending_count() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_signal_card_contents_and_html_escaping():
    text = format_trade_signal(SIGNAL, "Desk", "PAPER")
    assert "🟢 <b>BUY XAUUSD</b>" in text and "TRADE EXECUTED" in text
    assert "SMC &lt;Sweep&gt; &amp; FVG" in text and "<Sweep>" not in text
    assert "<code>2,345.60</code>" in text and "(44.0 pips)" in text
    assert "1:3.0" in text and "0.12" in text and "A+" in text and "PAPER" in text


def test_update_and_news_templates():
    closed = format_trade_update("CLOSED", {"symbol": "XAUUSD", "direction": "BUY", "entry": 2000,
                                            "price": 2010, "pnl": 100, "pips": 100, "r_multiple": 1.5,
                                            "reason": "TP", "digits": 2})
    assert "TAKE PROFIT HIT" in closed and "+$100.00" in closed and "+1.50R" in closed
    stopped = format_trade_update("CLOSED", {"symbol": "XAUUSD", "direction": "BUY", "pnl": 0.5,
                                             "reason": "SL", "digits": 2})
    assert "PROTECTED STOP" in stopped
    nfp = EconomicEvent("id1", "Non-Farm <Payrolls>", "USD", "High",
                        datetime(2026, 11, 6, 13, 30, tzinfo=UTC), forecast="180K", previous="254K")
    warning = format_news_warning(nfp, 14.6, ["XAUUSD"], tier1=True)
    assert "HIGH IMPACT NEWS WARNING" in warning and "19:00 IST" in warning and "in <b>15 min</b>" in warning
    assert "&lt;Payrolls&gt;" in warning and "TIER-1" in warning
    digest = format_news_digest([nfp], "Fri, 06 Nov 2026")
    assert "19:00" in digest and "180K" in digest
    assert "No high-impact events" in format_news_digest([], "Sat")


def test_split_message_preserves_lines():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300))
    chunks = split_message(text, 4096)
    assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == text
    assert strip_html("<b>a</b> &amp; b") == "a & b"


def test_send_now_success_records_broadcast(make_broadcaster, db):
    recorder = Recorder()
    broadcaster = make_broadcaster(recorder)
    result = broadcaster.send_now("@vip", "<b>hello</b>", "TEST")
    assert result.ok and result.message_id == 101
    sent = recorder.requests[0]
    assert sent["parse_mode"] == "HTML" and sent["link_preview_options"] == {"is_disabled": True}
    assert db.get_recent_broadcasts(1)[0]["status"] == "SENT"


def test_flood_control_retry_after_is_honoured(make_broadcaster):
    recorder = Recorder([httpx.Response(429, json={"ok": False, "error_code": 429,
                                                   "description": "Too Many Requests: retry after 0",
                                                   "parameters": {"retry_after": 0}})])
    broadcaster = make_broadcaster(recorder)
    result = broadcaster.send_now("@vip", "x", "TEST")
    assert result.ok and len(recorder.requests) == 2
    assert broadcaster.stats_snapshot()["retries"] >= 1


def test_bad_html_falls_back_to_plain_text(make_broadcaster):
    recorder = Recorder([httpx.Response(400, json={"ok": False, "description":
                                                   "Bad Request: can't parse entities: unclosed tag"})])
    broadcaster = make_broadcaster(recorder)
    result = broadcaster.send_now("@vip", "<b>broken", "TEST")
    assert result.ok
    assert "parse_mode" not in recorder.requests[1] and recorder.requests[1]["text"] == "broken"


def test_permanent_error_and_network_retry(make_broadcaster):
    forbidden = Recorder([httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was kicked"})])
    result = make_broadcaster(forbidden).send_now("@vip", "x", "TEST")
    assert not result.ok and "403" in result.error and len(forbidden.requests) == 1
    flaky = Recorder([httpx.ConnectError("boom")])
    assert make_broadcaster(flaky).send_now("@vip", "x", "TEST").ok


def test_dedupe_and_reply_threading(make_broadcaster):
    recorder = Recorder()
    broadcaster = make_broadcaster(recorder)
    message = OutgoingMessage(chat_id="@vip", text="warning", category="NEWS_WARNING", dedupe_key="news:1")
    assert broadcaster.enqueue(message) is True
    assert broadcaster.enqueue(OutgoingMessage(chat_id="@vip", text="warning", category="NEWS_WARNING",
                                               dedupe_key="news:1")) is False
    assert broadcaster.broadcast_trade_update("BREAKEVEN", {"symbol": "XAUUSD", "direction": "BUY",
                                                            "entry": 2000, "sl": 2000.3, "digits": 2},
                                              reply_to=77)
    wait_idle(broadcaster)
    replies = [r for r in recorder.requests if "reply_parameters" in r]
    assert replies and replies[0]["reply_parameters"]["message_id"] == 77
    assert sum(1 for r in recorder.requests if r["text"] == "warning") == 1


def test_test_connection_probe(make_broadcaster):
    recorder = Recorder([
        httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "desk_bot"}}),
        httpx.Response(200, json={"ok": True, "result": {"title": "VIP Signals"}}),
        httpx.Response(200, json={"ok": True, "result": {"status": "administrator"}}),
        httpx.Response(200, json={"ok": True, "result": {"id": 999}}),
    ])
    probe = make_broadcaster(recorder).test_connection()
    assert probe["ok"] and probe["bot"] == "@desk_bot" and probe["channel"] == "VIP Signals"
    assert probe["channel_admin"] is True and probe["admin_chat"] is True


def test_unconfigured_broadcaster_is_inert(base_env, db):
    settings = load_settings(base_env)
    broadcaster = TelegramBroadcaster(settings.telegram, db)
    assert not broadcaster.configured
    assert broadcaster.broadcast_trade_signal(SIGNAL) is False
    assert not broadcaster.send_now("@vip", "x").ok
