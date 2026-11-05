"""
forex_factory_engine.py — Live Forex Factory economic calendar + smart news kill-switch.

Data sources, tried in order on every refresh:
  1. faireconomy JSON feed  (ff_calendar_thisweek.json — explicit UTC offsets)
  2. faireconomy XML feed   (ff_calendar_thisweek.xml — times in FF_XML_TIMEZONE)
  3. forexfactory.com HTML  (calendar table — times in FF_HTML_TIMEZONE; often behind
     Cloudflare, so it is a best-effort last resort)
  4. last good disk cache   (data/cache/ff_calendar_cache.json survives restarts)

The feed is rate-limited by its host, so refreshes are spaced by FF_REFRESH_MINUTES,
failures back off exponentially (HTTP 429 honours Retry-After) and the cache is reused
across restarts.  All timestamps are stored in UTC and rendered in IST.

Kill-switch: ``is_news_blackout_active(symbol)`` is True while a high-impact event that
moves one of the symbol's currencies is within [T-before, T+after].
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx

from config import IST, NEW_YORK, NewsSettings, canonical_symbol

log = logging.getLogger("forex_factory")

UTC = timezone.utc
KNOWN_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY", "ALL"})
IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0, "Non-Economic": 0}
INDEX_CURRENCY = {"US30": "USD", "US100": "USD", "NAS100": "USD", "USTEC": "USD", "US500": "USD",
                  "SPX500": "USD", "DXY": "USD", "USDX": "USD", "GER40": "EUR", "DE40": "EUR",
                  "UK100": "GBP", "JP225": "JPY", "AUS200": "AUD"}
MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradingSuperTerminal/1.0"
MAX_PAYLOAD_BYTES = 5_000_000
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[ap]m$", re.IGNORECASE)
_DATE_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})",
                      re.IGNORECASE)


class CalendarFetchError(RuntimeError):
    """A source could not deliver a usable calendar."""


class CalendarRateLimited(CalendarFetchError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EconomicEvent:
    event_id: str
    title: str
    currency: str
    impact: str
    time_utc: datetime | None
    all_day: bool = False
    time_label: str = ""  # "All Day" / "Tentative" / "" for timed events
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    source: str = ""
    url: str = ""

    @property
    def time_ist(self) -> datetime | None:
        return self.time_utc.astimezone(IST) if self.time_utc else None

    @property
    def impact_rank(self) -> int:
        return IMPACT_RANK.get(self.impact, 0)

    @property
    def is_timed(self) -> bool:
        return self.time_utc is not None and not self.all_day

    def seconds_until(self, now: datetime) -> float | None:
        if self.time_utc is None:
            return None
        return (self.time_utc - now).total_seconds()

    def matches_keywords(self, keywords: Iterable[str]) -> bool:
        title = self.title.lower()
        return any(keyword.lower() in title for keyword in keywords if keyword)

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "title": self.title, "currency": self.currency,
                "impact": self.impact,
                "time_utc": self.time_utc.isoformat() if self.time_utc else None,
                "all_day": self.all_day, "time_label": self.time_label, "forecast": self.forecast,
                "previous": self.previous, "actual": self.actual, "source": self.source,
                "url": self.url}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EconomicEvent":
        raw_time = data.get("time_utc")
        moment = datetime.fromisoformat(raw_time) if raw_time else None
        if moment is not None and moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return cls(event_id=str(data["event_id"]), title=str(data["title"]),
                   currency=str(data["currency"]), impact=str(data["impact"]), time_utc=moment,
                   all_day=bool(data.get("all_day")), time_label=str(data.get("time_label") or ""),
                   forecast=str(data.get("forecast") or ""), previous=str(data.get("previous") or ""),
                   actual=str(data.get("actual") or ""), source=str(data.get("source") or ""),
                   url=str(data.get("url") or ""))


@dataclass(frozen=True)
class BlackoutStatus:
    active: bool
    symbol: str
    event: EconomicEvent | None = None
    phase: str | None = None  # PRE | POST
    seconds_to_event: float | None = None
    ends_at: datetime | None = None

    def describe(self) -> str:
        if not self.active or self.event is None:
            return "Clear"
        when = format_countdown(self.seconds_to_event)
        rel = f"in {when}" if self.phase == "PRE" else when
        return f"{self.event.currency} {self.event.title} ({rel})"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_impact(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value.startswith("high") or value in {"red", "3"}:
        return "High"
    if value.startswith("med") or value in {"orange", "ora", "2"}:
        return "Medium"
    if value.startswith("low") or value in {"yellow", "yel", "1"}:
        return "Low"
    if value.startswith("holiday"):
        return "Holiday"
    return "Non-Economic"


def make_event_id(title: str, currency: str, when_key: str) -> str:
    return hashlib.sha1(f"{currency}|{title}|{when_key}".encode("utf-8")).hexdigest()[:16]


def symbol_currencies(symbol: str, overrides: Mapping[str, str] | None = None) -> frozenset[str]:
    """Currencies whose news moves ``symbol`` (always includes FF's 'ALL')."""
    key = canonical_symbol(symbol)
    if overrides:
        for name, value in overrides.items():
            if key.startswith(canonical_symbol(name)):
                parts = [p.strip().upper() for p in re.split(r"[|/ ;]+", str(value)) if p.strip()]
                return frozenset(parts) | {"ALL"}
    for name, currency in INDEX_CURRENCY.items():
        if key.startswith(name):
            return frozenset({currency, "ALL"})
    result = {code for code in (key[:3], key[3:6]) if code in KNOWN_CURRENCIES}
    if not result:
        result.add("USD")
    result.add("ALL")
    return frozenset(result)


def format_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    past = seconds < 0
    total = int(abs(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        text = f"{days}d {hours:02d}h {minutes:02d}m"
    elif hours:
        text = f"{hours}h {minutes:02d}m {secs:02d}s"
    else:
        text = f"{minutes:02d}m {secs:02d}s"
    return f"{text} ago" if past else text


def _combine_local(day: date, time_text: str, tz: ZoneInfo) -> tuple[datetime, bool, str]:
    """Local date + FF time text -> (UTC datetime, all_day, label)."""
    text = (time_text or "").strip()
    compact = text.replace(" ", "")
    if compact and _TIME_RE.match(compact):
        parsed = datetime.strptime(compact.lower(), "%I:%M%p").time()
        return datetime.combine(day, parsed, tzinfo=tz).astimezone(UTC), False, ""
    label = "Tentative" if text.lower().startswith("tentative") else (text or "All Day")
    return datetime.combine(day, dtime(0, 0), tzinfo=tz).astimezone(UTC), True, label


def _infer_date(month_token: str, day: int, now: datetime) -> date:
    month = MONTHS[month_token[:3].lower()]
    candidates: list[date] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise ValueError(f"invalid calendar date {month_token} {day}")
    return min(candidates, key=lambda d: abs((d - now.date()).days))


def _sorted(events: Iterable[EconomicEvent]) -> list[EconomicEvent]:
    far = datetime.max.replace(tzinfo=UTC)
    unique = {event.event_id: event for event in events}
    return sorted(unique.values(), key=lambda e: (e.time_utc or far, -e.impact_rank, e.currency))


# --------------------------------------------------------------------------- #
# Parsers (pure functions)
# --------------------------------------------------------------------------- #
def parse_ff_json(payload: str | bytes | list | dict) -> list[EconomicEvent]:
    data = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    if not isinstance(data, list):
        raise CalendarFetchError("unexpected JSON structure")
    events: list[EconomicEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        currency = str(item.get("country") or item.get("currency") or "").strip().upper()
        if not title or not currency:
            continue
        impact = normalize_impact(item.get("impact"))
        raw_date = str(item.get("date") or "").strip()
        moment: datetime | None = None
        if raw_date:
            try:
                moment = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                log.debug("Skipping FF JSON event with bad date %r", raw_date)
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=NEW_YORK)
            moment = moment.astimezone(UTC)
        all_day = impact == "Holiday" or moment is None
        events.append(EconomicEvent(
            event_id=make_event_id(title, currency, moment.isoformat() if moment else raw_date),
            title=title, currency=currency, impact=impact, time_utc=moment, all_day=all_day,
            time_label="All Day" if all_day else "",
            forecast=str(item.get("forecast") or "").strip(),
            previous=str(item.get("previous") or "").strip(),
            actual=str(item.get("actual") or "").strip(), source="json",
            url=str(item.get("url") or "")))
    return _sorted(events)


def parse_ff_xml(payload: str | bytes, source_tz: ZoneInfo) -> list[EconomicEvent]:
    """Parse the weekly XML feed. Pass raw bytes so the declared encoding (windows-1252) is honoured."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise CalendarFetchError("XML payload too large")
    if isinstance(payload, str):  # str input cannot carry an encoding declaration
        payload = re.sub(r"^\s*<\?xml[^>]*\?>", "", payload, count=1)
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise CalendarFetchError(f"invalid XML: {exc}") from exc
    events: list[EconomicEvent] = []
    for node in root.iter("event"):
        title = (node.findtext("title") or "").strip()
        currency = (node.findtext("country") or "").strip().upper()
        date_text = (node.findtext("date") or "").strip()
        if not title or not currency or not date_text:
            continue
        try:
            day = datetime.strptime(date_text, "%m-%d-%Y").date()
        except ValueError:
            continue
        impact = normalize_impact(node.findtext("impact"))
        moment, all_day, label = _combine_local(day, node.findtext("time") or "", source_tz)
        if impact == "Holiday":
            all_day, label = True, label or "All Day"
        events.append(EconomicEvent(
            event_id=make_event_id(title, currency, moment.isoformat()), title=title,
            currency=currency, impact=impact, time_utc=moment, all_day=all_day, time_label=label,
            forecast=(node.findtext("forecast") or "").strip(),
            previous=(node.findtext("previous") or "").strip(),
            actual=(node.findtext("actual") or "").strip(), source="xml",
            url=(node.findtext("url") or "").strip()))
    return _sorted(events)


def parse_ff_html(text: str, source_tz: ZoneInfo, now: datetime) -> list[EconomicEvent]:
    from bs4 import BeautifulSoup  # local import keeps module import light

    soup = BeautifulSoup(text, "html.parser")
    rows = soup.select("tr.calendar__row")
    if not rows:
        raise CalendarFetchError("no calendar rows found (blocked page or layout change)")
    events: list[EconomicEvent] = []
    current_day: date | None = None
    current_time = ""
    for row in rows:
        date_cell = row.select_one("td.calendar__date")
        if date_cell is not None:
            match = _DATE_RE.search(date_cell.get_text(" ", strip=True))
            if match:
                current_day = _infer_date(match.group(1), int(match.group(2)), now)
                current_time = ""
        currency_cell = row.select_one("td.calendar__currency")
        title_el = row.select_one(".calendar__event-title") or row.select_one("td.calendar__event")
        currency = currency_cell.get_text(strip=True).upper() if currency_cell else ""
        title = title_el.get_text(" ", strip=True) if title_el else ""
        time_cell = row.select_one("td.calendar__time")
        if time_cell is not None and time_cell.get_text(strip=True):
            current_time = time_cell.get_text(" ", strip=True)
        if not currency or not title or current_day is None:
            continue
        impact = "Non-Economic"
        impact_el = row.select_one("td.calendar__impact span")
        if impact_el is not None:
            classes = " ".join(impact_el.get("class", []))
            hint = f"{classes} {impact_el.get('title', '')}".lower()
            if "red" in hint or "high" in hint:
                impact = "High"
            elif "ora" in hint or "medium" in hint:
                impact = "Medium"
            elif "yel" in hint or "low" in hint:
                impact = "Low"
            elif "holiday" in hint:
                impact = "Holiday"

        def cell(name: str) -> str:
            element = row.select_one(f"td.calendar__{name}")
            return element.get_text(" ", strip=True) if element else ""

        moment, all_day, label = _combine_local(current_day, current_time, source_tz)
        events.append(EconomicEvent(
            event_id=make_event_id(title, currency, moment.isoformat()), title=title,
            currency=currency, impact=impact, time_utc=moment,
            all_day=all_day or impact == "Holiday", time_label=label, forecast=cell("forecast"),
            previous=cell("previous"), actual=cell("actual"), source="html"))
    if not events:
        raise CalendarFetchError("calendar table contained no events")
    return _sorted(events)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ForexFactoryEngine:
    """Thread-safe calendar store with rate-limit-aware refresh and kill-switch queries."""

    CACHE_FILE = "ff_calendar_cache.json"
    MIN_REFETCH_SECONDS = 60.0

    def __init__(self, settings: NewsSettings, cache_dir: Path, db: Any = None,
                 client_factory: Callable[[], httpx.Client] | None = None):
        self.settings = settings
        self.db = db
        self.cache_path = Path(cache_dir) / self.CACHE_FILE
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._client_factory = client_factory or self._default_client
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._events: list[EconomicEvent] = []
        self._source = "none"
        self._fetched_at: datetime | None = None
        self._last_attempt = 0.0
        self._cooldown_until = 0.0
        self._failures = 0
        self._last_error: str | None = None
        self._load_cache()

    # ------------------------------------------------------------- caching --
    def _default_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.settings.http_timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/json, application/xml, text/xml, text/html;q=0.9, */*;q=0.8"})

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable calendar cache: %s", exc)
            return
        try:
            events = [EconomicEvent.from_dict(item) for item in raw.get("events", [])
                      if isinstance(item, dict)]
        except (KeyError, ValueError) as exc:
            log.warning("Ignoring malformed calendar cache: %s", exc)
            return
        fetched = raw.get("fetched_at")
        with self._lock:
            self._events = _sorted(events)
            self._source = f"cache:{raw.get('source', '?')}"
            self._fetched_at = datetime.fromisoformat(fetched) if fetched else None
        log.info("Loaded %s calendar events from cache (%s)", len(events), self._source)

    def _save_cache(self, events: Sequence[EconomicEvent], source: str, fetched_at: datetime) -> None:
        payload = {"fetched_at": fetched_at.isoformat(), "source": source,
                   "events": [event.to_dict() for event in events]}
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.warning("Could not write calendar cache: %s", exc)

    @staticmethod
    def _week_start(moment: datetime) -> datetime:
        """Forex Factory weeks run Sunday 00:00 -> Saturday (US Eastern)."""
        local = moment.astimezone(NEW_YORK)
        start = (local - timedelta(days=(local.weekday() + 1) % 7)).date()
        return datetime.combine(start, dtime(0, 0), tzinfo=NEW_YORK).astimezone(UTC)

    # ------------------------------------------------------------- refresh --
    def refresh_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        with self._lock:
            fetched = self._fetched_at
            has_events = bool(self._events)
        if time.monotonic() < self._cooldown_until:
            return False
        if fetched is None or not has_events:
            return True
        if fetched < self._week_start(now):
            return True
        return now - fetched >= timedelta(minutes=self.settings.refresh_minutes)

    def refresh_if_due(self, now: datetime | None = None) -> bool:
        if not self.refresh_due(now):
            return False
        return self.refresh(now)

    def refresh(self, now: datetime | None = None, force: bool = False) -> bool:
        """Fetch from JSON -> XML -> HTML. Returns True when fresh data was applied."""
        now = now or datetime.now(UTC)
        if not self._fetch_lock.acquire(blocking=False):
            return False
        try:
            mono = time.monotonic()
            if mono - self._last_attempt < self.MIN_REFETCH_SECONDS and self._last_attempt:
                return False
            if not force and mono < self._cooldown_until:
                return False
            self._last_attempt = mono
            errors: list[str] = []
            retry_after: float | None = None
            sources: list[tuple[str, Callable[[datetime], list[EconomicEvent]]]] = [
                ("json", self._fetch_json), ("xml", self._fetch_xml), ("html", self._fetch_html)]
            for name, fetcher in sources:
                try:
                    events = fetcher(now)
                    if not events:
                        raise CalendarFetchError("empty calendar")
                except CalendarRateLimited as exc:
                    errors.append(f"{name}: {exc}")
                    retry_after = max(retry_after or 0.0, exc.retry_after or 1800.0)
                    continue
                except (CalendarFetchError, httpx.HTTPError, ValueError, OSError) as exc:
                    errors.append(f"{name}: {exc}")
                    continue
                self._apply(events, name, now)
                return True
            self._register_failure(errors, retry_after)
            return False
        finally:
            self._fetch_lock.release()

    def _apply(self, events: list[EconomicEvent], source: str, now: datetime) -> None:
        events = _sorted(events)
        with self._lock:
            self._events = events
            self._source = source
            self._fetched_at = now
            self._failures = 0
            self._last_error = None
            self._cooldown_until = 0.0
        self._save_cache(events, source, now)
        if self.db is not None:
            try:
                self.db.upsert_news_events(event.to_dict() for event in events)
            except Exception:
                log.exception("Could not persist news events")
        high = sum(1 for e in events if e.impact == "High")
        log.info("Forex Factory calendar refreshed from %s: %s events (%s high impact)",
                 source, len(events), high)

    def _register_failure(self, errors: list[str], retry_after: float | None) -> None:
        with self._lock:
            self._failures += 1
            backoff = min(120.0 * (2 ** (self._failures - 1)), 3600.0)
            if retry_after:
                backoff = max(backoff, min(retry_after, 7200.0))
            self._cooldown_until = time.monotonic() + backoff
            self._last_error = "; ".join(errors) or "unknown error"
        log.warning("Calendar refresh failed (%s); next attempt in %.0f min — %s",
                    self._failures, backoff / 60.0, self._last_error)

    def _http_get(self, url: str) -> httpx.Response:
        with self._client_factory() as client:
            response = client.get(url)
        if response.status_code == 429:
            header = response.headers.get("Retry-After", "")
            raise CalendarRateLimited("HTTP 429 rate limited",
                                      float(header) if header.isdigit() else None)
        if response.status_code != 200:
            raise CalendarFetchError(f"HTTP {response.status_code}")
        if len(response.content) > MAX_PAYLOAD_BYTES:
            raise CalendarFetchError("payload too large")
        return response

    def _fetch_json(self, now: datetime) -> list[EconomicEvent]:
        text = self._http_get(self.settings.json_url).text
        stripped = text.lstrip()
        if stripped.startswith("<"):
            lowered = stripped[:600].lower()
            if "denied" in lowered or "rate" in lowered or "too many" in lowered:
                raise CalendarRateLimited("feed returned a rate-limit page")
            raise CalendarFetchError("feed returned HTML instead of JSON")
        return parse_ff_json(text)

    def _fetch_xml(self, now: datetime) -> list[EconomicEvent]:
        content = self._http_get(self.settings.xml_url).content
        if b"<weeklyevents" not in content[:4000].lower():
            raise CalendarFetchError("unexpected XML document")
        return parse_ff_xml(content, ZoneInfo(self.settings.xml_timezone))

    def _fetch_html(self, now: datetime) -> list[EconomicEvent]:
        return parse_ff_html(self._http_get(self.settings.html_url).text,
                             ZoneInfo(self.settings.html_timezone), now)

    # ------------------------------------------------------------- queries --
    def events(self) -> list[EconomicEvent]:
        with self._lock:
            return list(self._events)

    def load_events(self, events: Iterable[EconomicEvent], source: str = "manual",
                    now: datetime | None = None) -> None:
        """Inject events (tests, offline replay)."""
        self._apply(list(events), source, now or datetime.now(UTC))

    def get_events(self, currencies: Iterable[str] | None = None,
                   impacts: Iterable[str] | None = None, start: datetime | None = None,
                   end: datetime | None = None, include_all_day: bool = True) -> list[EconomicEvent]:
        cur = {c.upper() for c in currencies} if currencies else None
        imp = {i.title() for i in impacts} if impacts else None
        out: list[EconomicEvent] = []
        for event in self.events():
            if cur is not None and event.currency not in cur and event.currency != "ALL":
                continue
            if imp is not None and event.impact not in imp:
                continue
            if not include_all_day and event.all_day:
                continue
            if start is not None and (event.time_utc is None or event.time_utc < start):
                continue
            if end is not None and (event.time_utc is None or event.time_utc > end):
                continue
            out.append(event)
        return out

    def today_events(self, now: datetime | None = None, tz: ZoneInfo = IST,
                     currencies: Iterable[str] | None = None,
                     impacts: Iterable[str] | None = None) -> list[EconomicEvent]:
        now = now or datetime.now(UTC)
        today = now.astimezone(tz).date()
        return [event for event in self.get_events(currencies, impacts)
                if event.time_utc is not None and event.time_utc.astimezone(tz).date() == today]

    def upcoming_events(self, now: datetime | None = None, hours: float = 48.0,
                        currencies: Iterable[str] | None = None,
                        impacts: Iterable[str] | None = None,
                        include_recent_minutes: float = 30.0) -> list[EconomicEvent]:
        now = now or datetime.now(UTC)
        return self.get_events(currencies, impacts,
                               start=now - timedelta(minutes=include_recent_minutes),
                               end=now + timedelta(hours=hours))

    def next_event(self, now: datetime | None = None, impacts: Iterable[str] = ("High",),
                   currencies: Iterable[str] | None = None) -> EconomicEvent | None:
        now = now or datetime.now(UTC)
        for event in self.get_events(currencies, impacts, start=now, include_all_day=False):
            return event
        return None

    def blackout_status(self, symbol: str, now: datetime | None = None) -> BlackoutStatus:
        now = now or datetime.now(UTC)
        currencies = symbol_currencies(symbol, self.settings.currency_map)
        before = timedelta(minutes=self.settings.blackout_before_min)
        after = timedelta(minutes=self.settings.blackout_after_min)
        impacts = set(self.settings.blackout_impacts)
        overlapping: list[EconomicEvent] = []
        for event in self.events():
            if not event.is_timed or event.impact not in impacts or event.currency not in currencies:
                continue
            assert event.time_utc is not None
            if event.time_utc - before <= now <= event.time_utc + after:
                overlapping.append(event)
        if not overlapping:
            return BlackoutStatus(False, symbol)
        best = min(overlapping, key=lambda e: abs(e.seconds_until(now) or 0.0))
        seconds = best.seconds_until(now)
        ends_at = max(e.time_utc for e in overlapping if e.time_utc) + after
        return BlackoutStatus(True, symbol, best, "PRE" if (seconds or 0) > 0 else "POST",
                              seconds, ends_at)

    def is_news_blackout_active(self, symbol: str, now: datetime | None = None) -> bool:
        return self.blackout_status(symbol, now).active

    def warning_candidates(self, now: datetime | None = None) -> list[tuple[EconomicEvent, float]]:
        """High-impact events due within NEWS_WARNING_LEAD_MIN that deserve a pre-news alert."""
        now = now or datetime.now(UTC)
        lead = self.settings.warning_lead_min * 60.0
        currencies = set(self.settings.alert_currencies) | {"ALL"}
        impacts = set(self.settings.alert_impacts)
        out: list[tuple[EconomicEvent, float]] = []
        for event in self.events():
            if not event.is_timed or event.impact not in impacts or event.currency not in currencies:
                continue
            if self.settings.warning_keywords_only and not self.is_tier1(event):
                continue
            seconds = event.seconds_until(now)
            if seconds is not None and 0 < seconds <= lead:
                out.append((event, seconds))
        return out

    def is_tier1(self, event: EconomicEvent) -> bool:
        return event.matches_keywords(self.settings.warning_keywords)

    def affected_symbols(self, event: EconomicEvent, symbols: Iterable[str]) -> list[str]:
        return [symbol for symbol in symbols
                if event.currency == "ALL"
                or event.currency in symbol_currencies(symbol, self.settings.currency_map)]

    def status(self) -> dict[str, Any]:
        with self._lock:
            cooldown = max(0.0, self._cooldown_until - time.monotonic())
            fetched = self._fetched_at
            return {"source": self._source, "fetched_at": fetched.isoformat() if fetched else None,
                    "events": len(self._events), "failures": self._failures,
                    "last_error": self._last_error, "cooldown_seconds": round(cooldown),
                    "next_refresh_at": (fetched + timedelta(minutes=self.settings.refresh_minutes)
                                        ).isoformat() if fetched else None}
