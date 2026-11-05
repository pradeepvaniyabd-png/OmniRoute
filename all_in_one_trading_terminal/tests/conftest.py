"""Shared fixtures: deterministic market structures, settings, temp database, fake feeds."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402
from database import Database  # noqa: E402

UTC = timezone.utc

# Bullish ICT sequence on M5 (o, h, l, c):
#   bearish BOS at bar 7 -> swing low 1995 (bar 10) -> lower high 2001.5 (bar 14)
#   -> SSL sweep at bar 18 (low 1993.8, close back above 1995)
#   -> displacement MSS at bar 20 (close 2003 > 2001.5) with FVG [1997.8, 2000.2]
BULLISH_BARS = [
    (2010.0, 2011.0, 2008.0, 2009.0), (2009.0, 2012.0, 2008.5, 2011.5), (2011.5, 2014.0, 2010.5, 2013.0),
    (2013.0, 2013.5, 2008.0, 2008.5), (2008.5, 2009.0, 2003.0, 2004.0), (2004.0, 2007.5, 2003.5, 2007.0),
    (2007.0, 2008.0, 2005.0, 2005.5), (2005.5, 2006.0, 2001.0, 2001.5), (2001.5, 2002.0, 1998.0, 1998.5),
    (1998.5, 1999.0, 1996.0, 1996.5), (1996.5, 1997.5, 1995.0, 1997.0), (1997.0, 1999.5, 1996.5, 1999.0),
    (1999.0, 2000.5, 1998.0, 2000.0), (2000.0, 2001.0, 1999.0, 2000.5), (2000.5, 2001.5, 1999.5, 2000.0),
    (2000.0, 2000.5, 1998.0, 1998.5), (1998.5, 1999.0, 1996.5, 1997.0), (1997.0, 1997.5, 1995.5, 1996.0),
    (1996.0, 1996.5, 1993.8, 1996.2), (1996.2, 1997.8, 1995.8, 1997.5), (1997.5, 2003.5, 1997.3, 2003.0),
    (2003.0, 2004.5, 2000.2, 2004.0), (2004.0, 2004.2, 2001.5, 2002.0), (2002.0, 2002.5, 2000.8, 2001.0),
]


def make_frame(bars, start: datetime, minutes: int, volume=None) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(bars), freq=f"{minutes}min", tz="UTC", name="time")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index, dtype=float)
    frame["volume"] = volume if volume is not None else 100.0
    return frame


def bullish_frame(start: datetime) -> pd.DataFrame:
    volume = [100.0] * len(BULLISH_BARS)
    volume[20] = 400.0
    return make_frame(BULLISH_BARS, start, 5, volume)


def mirror(frame: pd.DataFrame, pivot: float = 4000.0) -> pd.DataFrame:
    """Reflect prices around pivot/2 -> bearish twin of a bullish structure."""
    out = frame.copy()
    out["open"] = pivot - frame["open"]
    out["close"] = pivot - frame["close"]
    out["high"] = pivot - frame["low"]
    out["low"] = pivot - frame["high"]
    return out


def trend_frame(end: datetime, minutes: int, n: int = 150, base: float = 1900.0,
                step: float = 1.2) -> pd.DataFrame:
    """Zig-zag trend ending at ``end`` (positive step = uptrend)."""
    start = end - timedelta(minutes=minutes * n)
    index = pd.date_range(start, periods=n, freq=f"{minutes}min", tz="UTC", name="time")
    mid = base + np.arange(n) * step + 6.0 * np.sin(np.arange(n) / 2.0)
    opens, closes = mid - 0.5 * np.sign(step), mid + 0.5 * np.sign(step)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + 1.5,
                         "low": np.minimum(opens, closes) - 1.5, "close": closes,
                         "volume": 100.0}, index=index)


def floor_time(moment: datetime, minutes: int) -> datetime:
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % (minutes * 60), UTC)


@pytest.fixture
def base_env(tmp_path) -> dict[str, str]:
    return {
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "BROKER_MODE": "paper",
        "SYMBOLS": "XAUUSD",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHANNEL_ID": "",
    }


@pytest.fixture
def settings(base_env):
    return load_settings(base_env)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


class FakeFeed:
    """Drop-in replacement for PublicMarketDataFeed with controllable prices."""

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None, mid: float = 2000.0):
        self.frames = frames or {}
        self.mid = mid
        self.last_latency_ms = 1.0
        self.last_error = None
        self.fail = False

    def get_rates(self, source: str, timeframe: str, count: int) -> pd.DataFrame:
        from mt5_execution_engine import MarketDataError

        if self.fail:
            raise MarketDataError("feed offline")
        frame = self.frames.get(timeframe)
        if frame is None:
            raise MarketDataError(f"no {timeframe} data")
        return frame.tail(count).copy()

    def get_quote(self, source: str):
        from mt5_execution_engine import MarketDataError

        if self.fail:
            raise MarketDataError("feed offline")
        return self.mid, self.mid, datetime.now(UTC)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_feed() -> FakeFeed:
    return FakeFeed()
