from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from forex_factory_engine import (
    EconomicEvent,
    ForexFactoryEngine,
    format_countdown,
    make_event_id,
    parse_ff_html,
    parse_ff_json,
    parse_ff_xml,
    symbol_currencies,
)

UTC = timezone.utc

FF_JSON = json.dumps([
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
     "impact": "High", "forecast": "180K", "previous": "254K"},
    {"title": "German Factory Orders m/m", "country": "EUR", "date": "2026-11-05T02:00:00-05:00",
     "impact": "Medium", "forecast": "0.5%", "previous": "-1.2%"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-11-03T00:00:00-05:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "ISM Services <PMI>", "country": "USD", "date": "2026-11-05T10:00:00-05:00",
     "impact": "High", "forecast": "52.1", "previous": "51.4"},
])

FF_XML = b"""<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
<event><title>ISM Services PMI</title><country>USD</country><date><![CDATA[11-05-2026]]></date>
<time><![CDATA[3:00pm]]></time><impact><![CDATA[High]]></impact><forecast><![CDATA[52.1]]></forecast>
<previous><![CDATA[51.4]]></previous><url><![CDATA[https://www.forexfactory.com/calendar/x]]></url></event>
<event><title>Bank Holiday</title><country>JPY</country><date><![CDATA[11-03-2026]]></date>
<time><![CDATA[All Day]]></time><impact><![CDATA[Holiday]]></impact><forecast /><previous /></event>
<event><title>BOJ Policy Rate</title><country>JPY</country><date><![CDATA[11-04-2026]]></date>
<time><![CDATA[Tentative]]></time><impact><![CDATA[High]]></impact><forecast /><previous /></event>
</weeklyevents>"""

FF_HTML = """<table class="calendar__table">
<tr class="calendar__row calendar__row--day-breaker"><td colspan="8"><span>Thu Nov 5</span></td></tr>
<tr class="calendar__row" data-event-id="1">
  <td class="calendar__cell calendar__date"><span>Thu<span>Nov 5</span></span></td>
  <td class="calendar__cell calendar__time">10:00am</td>
  <td class="calendar__cell calendar__currency">USD</td>
  <td class="calendar__cell calendar__impact"><span class="icon icon--ff-impact-red" title="High Impact Expected"></span></td>
  <td class="calendar__cell calendar__event"><span class="calendar__event-title">ISM Services PMI</span></td>
  <td class="calendar__cell calendar__actual"></td>
  <td class="calendar__cell calendar__forecast">52.1</td>
  <td class="calendar__cell calendar__previous">51.4</td>
</tr>
<tr class="calendar__row" data-event-id="2">
  <td class="calendar__cell calendar__date"></td>
  <td class="calendar__cell calendar__time"></td>
  <td class="calendar__cell calendar__currency">USD</td>
  <td class="calendar__cell calendar__impact"><span class="icon icon--ff-impact-yel" title="Low Impact Expected"></span></td>
  <td class="calendar__cell calendar__event"><span class="calendar__event-title">ISM Services Prices</span></td>
  <td class="calendar__cell calendar__actual"></td>
  <td class="calendar__cell calendar__forecast">68.0</td>
  <td class="calendar__cell calendar__previous">69.4</td>
</tr>
</table>"""


def event(title: str, currency: str, when: datetime, impact: str = "High") -> EconomicEvent:
    return EconomicEvent(make_event_id(title, currency, when.isoformat()), title, currency, impact, when)


@pytest.fixture
def engine(settings, tmp_path):
    return ForexFactoryEngine(settings.news, tmp_path / "cache")


def test_parse_json_converts_eastern_offsets_to_utc_and_ist():
    events = {e.title: e for e in parse_ff_json(FF_JSON)}
    nfp = events["Non-Farm Employment Change"]
    assert nfp.time_utc == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    assert (nfp.time_ist.hour, nfp.time_ist.minute) == (19, 0)
    assert nfp.forecast == "180K" and nfp.impact == "High"
    assert events["Bank Holiday"].all_day is True
    assert events["German Factory Orders m/m"].impact == "Medium"


def test_parse_xml_bytes_with_windows_1252_declaration():
    events = {e.title: e for e in parse_ff_xml(FF_XML, ZoneInfo("UTC"))}
    assert events["ISM Services PMI"].time_utc == datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    assert events["Bank Holiday"].all_day and events["Bank Holiday"].time_label == "All Day"
    assert events["BOJ Policy Rate"].all_day and events["BOJ Policy Rate"].time_label == "Tentative"


def test_parse_html_inherits_blank_time_and_detects_impact():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    events = parse_ff_html(FF_HTML, ZoneInfo("America/New_York"), now)
    assert [(e.title, e.impact) for e in events] == [("ISM Services PMI", "High"),
                                                     ("ISM Services Prices", "Low")]
    assert all(e.time_utc == datetime(2026, 11, 5, 15, 0, tzinfo=UTC) for e in events)


def test_symbol_currency_mapping():
    assert symbol_currencies("XAUUSD") == {"USD", "ALL"}
    assert symbol_currencies("EURUSDm") == {"EUR", "USD", "ALL"}
    assert symbol_currencies("BTCUSD") == {"USD", "ALL"}
    assert symbol_currencies("US30") == {"USD", "ALL"}
    assert symbol_currencies("XAUUSD", {"XAUUSD": "USD|CNY"}) == {"USD", "CNY", "ALL"}


def test_blackout_window_edges_and_currency_filter(engine):
    release = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    engine.load_events([event("Non-Farm Employment Change", "USD", release),
                        event("ECB Press Conference", "EUR", release + timedelta(hours=5)),
                        event("Retail Sales", "USD", release + timedelta(hours=8), "Medium")])
    assert not engine.is_news_blackout_active("XAUUSD", release - timedelta(minutes=16))
    pre = engine.blackout_status("XAUUSD", release - timedelta(minutes=15))
    assert pre.active and pre.phase == "PRE" and pre.event.title.startswith("Non-Farm")
    post = engine.blackout_status("XAUUSD", release + timedelta(minutes=15))
    assert post.active and post.phase == "POST"
    assert not engine.is_news_blackout_active("XAUUSD", release + timedelta(minutes=16))
    ecb = release + timedelta(hours=5)
    assert engine.is_news_blackout_active("EURUSD", ecb) and not engine.is_news_blackout_active("XAUUSD", ecb)
    assert not engine.is_news_blackout_active("XAUUSD", release + timedelta(hours=8))  # medium impact


def test_warning_candidates_and_keyword_filter(settings, tmp_path):
    release = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    items = [event("Non-Farm Employment Change", "USD", release),
             event("Obscure Index", "USD", release + timedelta(minutes=1))]
    engine = ForexFactoryEngine(settings.news, tmp_path / "a")
    engine.load_events(items)
    near = engine.warning_candidates(release - timedelta(minutes=10))
    assert {e.title for e, _ in near} == {"Non-Farm Employment Change", "Obscure Index"}
    assert engine.warning_candidates(release - timedelta(minutes=20)) == []
    assert engine.is_tier1(items[0]) and not engine.is_tier1(items[1])
    strict = ForexFactoryEngine(dataclasses.replace(settings.news, warning_keywords_only=True), tmp_path / "b")
    strict.load_events(items)
    assert [e.title for e, _ in strict.warning_candidates(release - timedelta(minutes=10))] == [
        "Non-Farm Employment Change"]
    assert engine.affected_symbols(items[0], ["XAUUSD", "EURGBP", "BTCUSD"]) == ["XAUUSD", "BTCUSD"]


def _client_factory(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_falls_back_from_rate_limited_json_to_xml_and_caches(settings, tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith(".json"):
            return httpx.Response(429, headers={"Retry-After": "120"})
        if request.url.path.endswith(".xml"):
            return httpx.Response(200, content=FF_XML)
        return httpx.Response(403)

    engine = ForexFactoryEngine(settings.news, tmp_path / "cache", client_factory=_client_factory(handler))
    assert engine.refresh(force=True) is True
    assert engine.status()["source"] == "xml" and len(engine.events()) == 3
    assert calls[:2] == ["/ff_calendar_thisweek.json", "/ff_calendar_thisweek.xml"]
    reloaded = ForexFactoryEngine(settings.news, tmp_path / "cache")
    assert reloaded.status()["source"] == "cache:xml" and len(reloaded.events()) == 3


def test_total_failure_backs_off(settings, tmp_path):
    engine = ForexFactoryEngine(settings.news, tmp_path / "cache",
                                client_factory=_client_factory(lambda request: httpx.Response(500)))
    assert engine.refresh(force=True) is False
    status = engine.status()
    assert status["failures"] == 1 and status["cooldown_seconds"] > 0 and status["last_error"]
    assert engine.refresh_due() is False  # cooling down


def test_json_rate_limit_html_page_detected(settings, tmp_path):
    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, text="<html>Request Denied: rate limited</html>")
        return httpx.Response(503)

    engine = ForexFactoryEngine(settings.news, tmp_path / "c", client_factory=_client_factory(handler))
    assert engine.refresh(force=True) is False
    assert "rate-limit" in (engine.status()["last_error"] or "")


def test_countdown_format():
    assert format_countdown(3725) == "1h 02m 05s"
    assert format_countdown(59) == "00m 59s"
    assert format_countdown(-120) == "02m 00s ago"
    assert format_countdown(90061) == "1d 01h 01m"
