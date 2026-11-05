"""
database.py — Thread-safe SQLite persistence for the trading terminal.

Tables
------
trades               Trade journal (pending -> open -> closed lifecycle, P&L, R-multiple)
signals              Every detected setup with its execution / skip decision
equity_log           Periodic balance / equity snapshots (equity curve)
news_events          Forex Factory events seen by the calendar engine
telegram_broadcasts  Every Telegram send (trade signals, updates, news digests/warnings)
daily_stats          Start-of-day equity + circuit-breaker state per trading day
system_logs          Persistent system log (INFO and above)
kv_store             Small JSON key/value store (runtime toggles, paper account, caches)

One connection is shared by all threads, serialised by an RLock, in WAL mode.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

log = logging.getLogger("database")

UTC = timezone.utc

MIGRATIONS: list[str] = [
    # ---------------------------------------------------------------- v1 ----
    """
    CREATE TABLE IF NOT EXISTS trades (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id            TEXT,
        mode                 TEXT NOT NULL,
        symbol               TEXT NOT NULL,
        broker_symbol        TEXT NOT NULL,
        direction            TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
        order_type           TEXT NOT NULL,
        strategy             TEXT NOT NULL,
        setup                TEXT,
        timeframe            TEXT,
        status               TEXT NOT NULL,
        order_ticket         INTEGER,
        position_ticket      INTEGER,
        magic                INTEGER,
        volume               REAL NOT NULL,
        volume_open          REAL,
        entry_price          REAL NOT NULL,
        fill_price           REAL,
        sl_initial           REAL NOT NULL,
        sl_current           REAL,
        tp1                  REAL,
        tp2                  REAL,
        risk_amount          REAL,
        risk_percent         REAL,
        rr_planned           REAL,
        score                REAL,
        grade                TEXT,
        confluences          TEXT,
        be_moved             INTEGER NOT NULL DEFAULT 0,
        tp1_hit              INTEGER NOT NULL DEFAULT 0,
        trailing_active      INTEGER NOT NULL DEFAULT 0,
        partial_pnl          REAL NOT NULL DEFAULT 0,
        exit_price           REAL,
        pnl                  REAL,
        pips                 REAL,
        r_multiple           REAL,
        close_reason         TEXT,
        valid_until          TEXT,
        opened_at            TEXT,
        closed_at            TEXT,
        telegram_message_id  INTEGER,
        notes                TEXT,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_ticket);
    CREATE INDEX IF NOT EXISTS idx_trades_order    ON trades(order_ticket);
    CREATE INDEX IF NOT EXISTS idx_trades_closed   ON trades(closed_at);

    CREATE TABLE IF NOT EXISTS signals (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id    TEXT NOT NULL UNIQUE,
        symbol       TEXT NOT NULL,
        direction    TEXT NOT NULL,
        strategy     TEXT NOT NULL,
        setup        TEXT,
        timeframe    TEXT,
        order_type   TEXT,
        entry        REAL,
        stop_loss    REAL,
        tp1          REAL,
        tp2          REAL,
        rr           REAL,
        score        REAL,
        grade        TEXT,
        confluences  TEXT,
        status       TEXT NOT NULL,
        reason       TEXT,
        anchor_time  TEXT,
        valid_until  TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

    CREATE TABLE IF NOT EXISTS equity_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        mode          TEXT,
        balance       REAL,
        equity        REAL,
        floating_pnl  REAL,
        margin        REAL,
        free_margin   REAL,
        drawdown_pct  REAL
    );
    CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_log(ts);

    CREATE TABLE IF NOT EXISTS news_events (
        event_id        TEXT PRIMARY KEY,
        title           TEXT NOT NULL,
        currency        TEXT NOT NULL,
        impact          TEXT NOT NULL,
        event_time_utc  TEXT,
        all_day         INTEGER NOT NULL DEFAULT 0,
        forecast        TEXT,
        previous        TEXT,
        actual          TEXT,
        source          TEXT,
        fetched_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_news_time ON news_events(event_time_utc);

    CREATE TABLE IF NOT EXISTS telegram_broadcasts (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        category             TEXT NOT NULL,
        dedupe_key           TEXT UNIQUE,
        chat_id              TEXT,
        ref_id               TEXT,
        status               TEXT NOT NULL,
        telegram_message_id  INTEGER,
        error                TEXT,
        preview              TEXT,
        created_at           TEXT NOT NULL,
        sent_at              TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_broadcasts_created ON telegram_broadcasts(created_at);

    CREATE TABLE IF NOT EXISTS daily_stats (
        trading_day       TEXT PRIMARY KEY,
        start_balance     REAL,
        start_equity      REAL,
        min_equity        REAL,
        max_drawdown_pct  REAL NOT NULL DEFAULT 0,
        halted            INTEGER NOT NULL DEFAULT 0,
        halted_at         TEXT,
        halt_reason       TEXT,
        updated_at        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS system_logs (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        level    TEXT NOT NULL,
        source   TEXT,
        message  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_logs_ts ON system_logs(ts);

    CREATE TABLE IF NOT EXISTS kv_store (
        key         TEXT PRIMARY KEY,
        value       TEXT,
        updated_at  TEXT NOT NULL
    );
    """,
]

TRADE_COLUMNS = frozenset({
    "signal_id", "mode", "symbol", "broker_symbol", "direction", "order_type", "strategy",
    "setup", "timeframe", "status", "order_ticket", "position_ticket", "magic", "volume",
    "volume_open", "entry_price", "fill_price", "sl_initial", "sl_current", "tp1", "tp2",
    "risk_amount", "risk_percent", "rr_planned", "score", "grade", "confluences", "be_moved",
    "tp1_hit", "trailing_active", "partial_pnl", "exit_price", "pnl", "pips", "r_multiple",
    "close_reason", "valid_until", "opened_at", "closed_at", "telegram_message_id", "notes",
    "created_at",
})
SIGNAL_COLUMNS = frozenset({
    "signal_id", "symbol", "direction", "strategy", "setup", "timeframe", "order_type", "entry",
    "stop_loss", "tp1", "tp2", "rr", "score", "grade", "confluences", "status", "reason",
    "anchor_time", "valid_until", "created_at",
})
DAILY_COLUMNS = frozenset({
    "start_balance", "start_equity", "min_equity", "max_drawdown_pct", "halted", "halted_at",
    "halt_reason",
})
LOG_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
ACTIVE_TRADE_STATUSES = ("OPEN", "PENDING")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Pure analytics (unit-testable without a database)
# --------------------------------------------------------------------------- #
def compute_performance(pnls: Sequence[float], r_multiples: Sequence[float | None] = (),
                        eps: float = 1e-9) -> dict[str, Any]:
    """Win-rate, profit factor, expectancy, streaks and drawdown from closed-trade P&L."""
    pnls = [float(p or 0.0) for p in pnls]
    total = len(pnls)
    wins = [p for p in pnls if p > eps]
    losses = [p for p in pnls if p < -eps]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > eps:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > eps else 0.0
    rs = [float(r) for r in r_multiples if r is not None and not math.isnan(float(r))]

    max_wins = max_losses = cur_wins = cur_losses = 0
    cumulative = peak = max_drawdown = 0.0
    for pnl in pnls:
        if pnl > eps:
            cur_wins, cur_losses = cur_wins + 1, 0
        elif pnl < -eps:
            cur_wins, cur_losses = 0, cur_losses + 1
        else:
            cur_wins = cur_losses = 0
        max_wins = max(max_wins, cur_wins)
        max_losses = max(max_losses, cur_losses)
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    net = sum(pnls)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": total - len(wins) - len(losses),
        "win_rate": round(len(wins) / total * 100.0, 2) if total else 0.0,
        "profit_factor": profit_factor if math.isinf(profit_factor) else round(profit_factor, 3),
        "net_pnl": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "expectancy": round(net / total, 2) if total else 0.0,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "max_drawdown": round(max_drawdown, 2),
    }


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class Database:
    """SQLite gateway shared by the worker threads and the Streamlit UI."""

    SCHEMA_VERSION = len(MIGRATIONS)

    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    # ------------------------------------------------------------ plumbing --
    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            for version in range(current, len(MIGRATIONS)):
                script = f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
                try:
                    self._conn.executescript(script)
                except sqlite3.Error:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                log.info("Database migrated to schema v%s", version + 1)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @staticmethod
    def _clean(fields: Mapping[str, Any], allowed: frozenset[str], table: str) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                log.error("Ignoring unknown %s column '%s'", table, key)
                continue
            if isinstance(value, datetime):
                value = to_iso(value)
            elif isinstance(value, bool):
                value = int(value)
            elif isinstance(value, (list, tuple, dict)):
                value = json.dumps(value, default=str)
            elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                value = None
            data[key] = value
        return data

    @staticmethod
    def _decode_json_field(row: dict[str, Any] | None, field: str) -> dict[str, Any] | None:
        if row and isinstance(row.get(field), str):
            try:
                row[field] = json.loads(row[field])
            except (TypeError, ValueError):
                pass
        return row

    # ------------------------------------------------------------ kv store --
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._fetchone("SELECT value FROM kv_store WHERE key = ?", (key,))
        if not row or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), now_iso()),
        )

    def kv_delete(self, key: str) -> None:
        self._execute("DELETE FROM kv_store WHERE key = ?", (key,))

    # -------------------------------------------------------------- trades --
    def insert_trade(self, **fields: Any) -> int:
        data = self._clean(fields, TRADE_COLUMNS, "trades")
        stamp = now_iso()
        data.setdefault("created_at", stamp)
        data["updated_at"] = stamp
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        cursor = self._execute(f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
                               tuple(data.values()))
        return int(cursor.lastrowid)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        data = self._clean(fields, TRADE_COLUMNS, "trades")
        if not data:
            return
        data["updated_at"] = now_iso()
        assignments = ", ".join(f"{column} = ?" for column in data)
        self._execute(f"UPDATE trades SET {assignments} WHERE id = ?", (*data.values(), trade_id))

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        return self._decode_json_field(
            self._fetchone("SELECT * FROM trades WHERE id = ?", (trade_id,)), "confluences")

    def get_trade_by_position(self, ticket: int, mode: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM trades WHERE position_ticket = ?"
        params: list[Any] = [ticket]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        return self._decode_json_field(self._fetchone(sql + " ORDER BY id DESC LIMIT 1", params),
                                       "confluences")

    def get_trade_by_order(self, ticket: int, mode: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM trades WHERE order_ticket = ?"
        params: list[Any] = [ticket]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        return self._decode_json_field(self._fetchone(sql + " ORDER BY id DESC LIMIT 1", params),
                                       "confluences")

    def get_active_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trades WHERE status IN ('OPEN', 'PENDING')"
        params: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        rows = self._fetchall(sql + " ORDER BY id ASC", params)
        return [self._decode_json_field(row, "confluences") for row in rows]

    def get_trades(self, limit: int = 500, status: str | None = None, symbol: str | None = None,
                   mode: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if since:
            clauses.append("created_at >= ?")
            params.append(to_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT ?",
                              (*params, int(limit)))
        return [self._decode_json_field(row, "confluences") for row in rows]

    def count_trades_since(self, since: datetime, mode: str | None = None) -> int:
        sql = ("SELECT COUNT(*) AS n FROM trades WHERE created_at >= ? "
               "AND status IN ('OPEN', 'CLOSED', 'PENDING')")
        params: list[Any] = [to_iso(since)]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        row = self._fetchone(sql, params)
        return int(row["n"]) if row else 0

    def realized_pnl_since(self, since: datetime, mode: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status = 'CLOSED' AND closed_at >= ?"
        params: list[Any] = [to_iso(since)]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        row = self._fetchone(sql, params)
        return float(row["total"]) if row else 0.0

    def last_closed_trade(self, symbol: str, mode: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM trades WHERE status = 'CLOSED' AND symbol = ?"
        params: list[Any] = [symbol]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        return self._fetchone(sql + " ORDER BY closed_at DESC, id DESC LIMIT 1", params)

    # ------------------------------------------------------------- signals --
    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))

    def insert_signal(self, record: Mapping[str, Any]) -> bool:
        data = self._clean(record, SIGNAL_COLUMNS, "signals")
        stamp = now_iso()
        data.setdefault("created_at", stamp)
        data["updated_at"] = stamp
        data.setdefault("status", "DETECTED")
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        cursor = self._execute(f"INSERT OR IGNORE INTO signals ({columns}) VALUES ({placeholders})",
                               tuple(data.values()))
        return cursor.rowcount == 1

    def update_signal(self, signal_id: str, status: str, reason: str | None = None) -> None:
        self._execute("UPDATE signals SET status = ?, reason = ?, updated_at = ? WHERE signal_id = ?",
                      (status, reason, now_iso(), signal_id))

    def get_recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (int(limit),))
        return [self._decode_json_field(row, "confluences") for row in rows]

    # -------------------------------------------------------------- equity --
    def log_equity(self, mode: str, balance: float, equity: float, floating_pnl: float,
                   margin: float, free_margin: float, drawdown_pct: float,
                   ts: datetime | None = None) -> None:
        self._execute(
            "INSERT INTO equity_log (ts, mode, balance, equity, floating_pnl, margin, free_margin, "
            "drawdown_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (to_iso(ts) or now_iso(), mode, balance, equity, floating_pnl, margin, free_margin,
             drawdown_pct),
        )

    def get_equity_curve(self, mode: str | None = None, since: datetime | None = None,
                         limit: int = 5000) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if since:
            clauses.append("ts >= ?")
            params.append(to_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(f"SELECT * FROM equity_log {where} ORDER BY id DESC LIMIT ?",
                              (*params, int(limit)))
        rows.reverse()
        return rows

    def prune_equity(self, days: int = 120) -> int:
        cutoff = to_iso(datetime.now(UTC) - timedelta(days=days))
        return self._execute("DELETE FROM equity_log WHERE ts < ?", (cutoff,)).rowcount

    # ---------------------------------------------------------------- news --
    def upsert_news_events(self, events: Iterable[Mapping[str, Any]]) -> int:
        stamp = now_iso()
        rows = [
            (e["event_id"], e["title"], e["currency"], e["impact"], to_iso(e.get("time_utc")),
             int(bool(e.get("all_day"))), e.get("forecast", ""), e.get("previous", ""),
             e.get("actual", ""), e.get("source", ""), stamp)
            for e in events
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO news_events (event_id, title, currency, impact, event_time_utc, all_day, "
                "forecast, previous, actual, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET title = excluded.title, impact = excluded.impact, "
                "event_time_utc = excluded.event_time_utc, forecast = excluded.forecast, "
                "previous = excluded.previous, actual = excluded.actual, source = excluded.source, "
                "fetched_at = excluded.fetched_at",
                rows,
            )
        return len(rows)

    def get_news_events(self, start: datetime | None = None, end: datetime | None = None,
                        limit: int = 2000) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("event_time_utc >= ?")
            params.append(to_iso(start))
        if end:
            clauses.append("event_time_utc <= ?")
            params.append(to_iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._fetchall(
            f"SELECT * FROM news_events {where} ORDER BY event_time_utc ASC LIMIT ?",
            (*params, int(limit)))

    # ---------------------------------------------------------- broadcasts --
    def reserve_broadcast(self, category: str, dedupe_key: str, chat_id: str,
                          ref_id: str | None = None, preview: str | None = None) -> int | None:
        """Insert a QUEUED row; returns its id, or ``None`` if ``dedupe_key`` already exists."""
        cursor = self._execute(
            "INSERT OR IGNORE INTO telegram_broadcasts (category, dedupe_key, chat_id, ref_id, status, "
            "preview, created_at) VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)",
            (category, dedupe_key, str(chat_id), ref_id, (preview or "")[:500], now_iso()),
        )
        return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def log_broadcast(self, category: str, chat_id: str, ref_id: str | None = None,
                      preview: str | None = None) -> int:
        cursor = self._execute(
            "INSERT INTO telegram_broadcasts (category, dedupe_key, chat_id, ref_id, status, preview, "
            "created_at) VALUES (?, NULL, ?, ?, 'QUEUED', ?, ?)",
            (category, str(chat_id), ref_id, (preview or "")[:500], now_iso()),
        )
        return int(cursor.lastrowid)

    def mark_broadcast(self, row_id: int, status: str, message_id: int | None = None,
                       error: str | None = None) -> None:
        self._execute(
            "UPDATE telegram_broadcasts SET status = ?, telegram_message_id = ?, error = ?, sent_at = ? "
            "WHERE id = ?",
            (status, message_id, (error or None) and str(error)[:500], now_iso(), row_id),
        )

    def broadcast_exists(self, dedupe_key: str) -> bool:
        return self._fetchone("SELECT 1 AS x FROM telegram_broadcasts WHERE dedupe_key = ?",
                              (dedupe_key,)) is not None

    def release_broadcast(self, dedupe_key: str) -> None:
        self._execute("DELETE FROM telegram_broadcasts WHERE dedupe_key = ? AND status = 'QUEUED'",
                      (dedupe_key,))

    def get_recent_broadcasts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._fetchall("SELECT * FROM telegram_broadcasts ORDER BY id DESC LIMIT ?",
                              (int(limit),))

    # --------------------------------------------------------- daily stats --
    def get_daily_stats(self, trading_day: str) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM daily_stats WHERE trading_day = ?", (trading_day,))

    def upsert_daily_stats(self, trading_day: str, **fields: Any) -> None:
        data = self._clean(fields, DAILY_COLUMNS, "daily_stats")
        stamp = now_iso()
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM daily_stats WHERE trading_day = ?",
                                        (trading_day,)).fetchone()
            if exists:
                if not data:
                    return
                assignments = ", ".join(f"{column} = ?" for column in data)
                self._conn.execute(
                    f"UPDATE daily_stats SET {assignments}, updated_at = ? WHERE trading_day = ?",
                    (*data.values(), stamp, trading_day))
            else:
                data["trading_day"] = trading_day
                data["updated_at"] = stamp
                columns = ", ".join(data)
                placeholders = ", ".join("?" for _ in data)
                self._conn.execute(f"INSERT INTO daily_stats ({columns}) VALUES ({placeholders})",
                                   tuple(data.values()))

    # ---------------------------------------------------------------- logs --
    def insert_log(self, ts: str, level: str, source: str, message: str) -> None:
        self._execute("INSERT INTO system_logs (ts, level, source, message) VALUES (?, ?, ?, ?)",
                      (ts, level, source, message[:4000]))

    def get_logs(self, limit: int = 300, min_level: str | None = None) -> list[dict[str, Any]]:
        if min_level:
            levels = [name for name, value in LOG_LEVEL_ORDER.items()
                      if value >= LOG_LEVEL_ORDER.get(min_level.upper(), 0)]
            placeholders = ", ".join("?" for _ in levels)
            return self._fetchall(
                f"SELECT * FROM system_logs WHERE level IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*levels, int(limit)))
        return self._fetchall("SELECT * FROM system_logs ORDER BY id DESC LIMIT ?", (int(limit),))

    def prune_logs(self, days: int) -> int:
        cutoff = to_iso(datetime.now(UTC) - timedelta(days=days))
        return self._execute("DELETE FROM system_logs WHERE ts < ?", (cutoff,)).rowcount

    # ----------------------------------------------------------- analytics --
    def get_performance_stats(self, mode: str | None = None, since: datetime | None = None,
                              symbol: str | None = None) -> dict[str, Any]:
        clauses = ["status = 'CLOSED'"]
        params: list[Any] = []
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if since:
            clauses.append("closed_at >= ?")
            params.append(to_iso(since))
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        rows = self._fetchall(
            f"SELECT pnl, r_multiple FROM trades WHERE {' AND '.join(clauses)} "
            "ORDER BY closed_at ASC, id ASC", params)
        return compute_performance([r["pnl"] for r in rows], [r["r_multiple"] for r in rows])

    def get_strategy_breakdown(self, mode: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT strategy, COUNT(*) AS trades, "
               "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
               "ROUND(COALESCE(SUM(pnl), 0), 2) AS net_pnl, "
               "ROUND(COALESCE(AVG(r_multiple), 0), 3) AS avg_r "
               "FROM trades WHERE status = 'CLOSED'")
        params: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        rows = self._fetchall(sql + " GROUP BY strategy ORDER BY net_pnl DESC", params)
        for row in rows:
            row["win_rate"] = round(row["wins"] / row["trades"] * 100.0, 2) if row["trades"] else 0.0
        return rows
