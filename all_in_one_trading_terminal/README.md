# All-in-One Forex & Crypto Algorithmic Trading Super-Terminal

Institutional SMC / ICT trading bot with a professional **web trading terminal** for **XAUUSD,
BTCUSD, EURUSD, GBPUSD** (any MT5 symbol): MetaTrader 5 execution, Forex Factory news
kill-switch, a Telegram VIP-channel broadcaster, analytics and trading tools in one Python app.

![Web terminal: trade view (demo data)](docs/web-trade.webp)
<sub>Screenshots use synthetic demo data.</sub>

| Dashboard | Analytics | Economic calendar | Tools |
| --- | --- | --- | --- |
| ![Dashboard](docs/web-dashboard.webp) | ![Analytics](docs/web-analytics.webp) | ![Calendar](docs/web-calendar.webp) | ![Tools](docs/web-tools.webp) |

---

## The web terminal

`run_terminal.bat` starts the engine and opens **http://127.0.0.1:8800**: a FastAPI backend with a
WebSocket live feed (quotes, positions, events, chart candles) and a responsive single-page website
(dark / light theme, desktop and mobile).

| Page | What you get |
| --- | --- |
| **Dashboard** | Balance, equity, today's P&L, daily-loss gauge, win rate, exposure · equity curve · engine & risk panel (start / pause / panic) · market watch with 24h sparklines, HTF/LTF bias, killzone and news-lock state · upcoming news countdowns · open positions with live R · latest signals · activity feed. |
| **Trade Terminal** | Watchlist + account panel · TradingView Lightweight Charts™ candlesticks (M1–D1) with **FVG and order-block boxes, session ranges, liquidity sweeps, BOS / CHoCH / MSS markers, EMA, volume, projected signal trades and your entry / SL / TP lines** · order ticket for **Market, Limit and Stop** orders (click the chart to set the price) with risk-%, ATR / pips / structure stops and R-based targets, live lot-size preview and news-lock override · positions (edit SL/TP, breakeven, close), pending orders (cancel), history and signals tabs. |
| **Signals** | Every detected setup with grade, score, levels, R:R, engine decision and confluence breakdown; filters and CSV export. |
| **Analytics** | Net profit, win rate, profit factor, expectancy, max drawdown, hold time · equity, cumulative and daily P&L charts · monthly-returns heatmap · P&L by hour / weekday · breakdowns by strategy, symbol and killzone · paginated trade journal with CSV export. |
| **Economic Calendar** | Forex Factory week with impact / currency filters, countdowns, actual vs forecast colouring, affected symbols, next red-folder event and the per-symbol kill-switch status. |
| **Trading Tools** | Position size, pip value, risk:reward, margin, profit/loss, **Fibonacci with ICT OTE (62–79%) and −0.27/−0.62 targets**, compounding, drawdown recovery, market sessions & killzones timeline (with the IST time candles), correlation matrix and ADR volatility. |
| **Telegram** | Bot / channel status, credentials, connection test, test signal, news digest and the full broadcast log. |
| **Settings** | Execution mode, risk per trade, strategies, killzone filter, news auto-pause, read-only risk limits, theme / time zone / density, sound and desktop alerts, paper-account reset, panic close. |
| **System Logs** | Live engine log with level filter, search, copy and download. |

Also: command palette (**Ctrl / ⌘ + K**), keyboard shortcuts (**Alt + 1…9**, **B / S** on the ticket),
notification centre with toasts, live clock, Sydney / Tokyo / London / New York session status and a
connection indicator. Times default to **IST** (switch to local, UTC, New York or London in Settings).

### Security
- Binds to `127.0.0.1` by default. To reach it from another machine (VPS), set `WEB_HOST=0.0.0.0`
  **and** `WEB_PASSWORD` — the terminal refuses to expose itself without a password.
- Login uses a signed, HttpOnly, SameSite=Strict session cookie, a constant-time password check
  and a lock-out after 5 failed attempts. Logout revokes the session.
- Every write request must be same-origin JSON (CSRF protection); the WebSocket checks the origin.
- Strict security headers (CSP, frame-ancestors none, no-sniff); errors never expose stack traces.
- For internet access put it behind HTTPS (e.g. Caddy or Nginx) — the cookie becomes `Secure` automatically.

---

## Engine modules

| Module | Responsibility |
| --- | --- |
| `quant_strategy_engine.py` | Fractal swings (no look-ahead), BSL/SSL liquidity sweeps, BOS / CHoCH / MSS on **body close**, Fair Value Gaps, Order Blocks, CRT + Turtle-Body-Soup, DST-aware killzones / Silver Bullet windows, H1/M15 bias + M5 trigger, EMA trend, relative volume, DXY inverse filter, IST time-candle breakout, weighted confluence score (A+/A/B/C) and dynamic lot sizing. |
| `mt5_execution_engine.py` | `MT5Broker` (reconnect with back-off, symbol suffix/alias resolution, broker-clock → UTC, spread filter, stop-level checks, `order_check` pre-flight, filling-mode fallback, requote retries, partial closes, deal-history P&L) and `PaperBroker` (simulated fills on public Binance/Yahoo data). Also the **PositionProtector** (TP1 partial, auto-breakeven at 1R, ATR/points/structure trailing after 1.5R) and the **DailyDrawdownGuard** circuit breaker. |
| `forex_factory_engine.py` | Weekly calendar from the faireconomy JSON feed → XML feed → forexfactory.com HTML → disk cache, rate-limit back-off, IST conversion, countdowns and `is_news_blackout_active(symbol)`. |
| `telegram_broadcaster.py` | Async dispatcher thread with per-chat rate limits, `retry_after` handling, HTML fallback. Signal cards, TP1/BE/trailing/close updates (threaded as replies to the signal), morning red-folder digest, 15-minute pre-news warning, emergency alerts. |
| `background_worker.py` | Scanner, protector, news and supervisor threads; signal gating; manual Market / Limit / Stop orders; SL/TP edits; notification feed; single-instance lock; headless VPS mode. |
| `server.py` + `web/` | FastAPI REST API, WebSocket live feed, login, and the website (vanilla ES modules, no build step). |
| `analytics.py`, `chart_data.py` | Performance analytics and market tools; chart payloads (candles, zones, sessions, markers, levels). |
| `database.py` | SQLite (WAL) journal: trades, signals, equity curve, news, broadcasts, daily stats, logs. |
| `app.py` | Classic Streamlit dashboard (`run_terminal.bat classic`). |

### Default setups traded
1. **Sweep → MSS → FVG (ICT model)** — sell-side sweep, displacement close above the last lower high,
   entry at the FVG (top or CE), stop beyond the sweep wick, TP1 1.5R / TP2 3R.
2. **Order Block retest** — continuation entry at the impulse-origin OB, only with H1 bias.
3. **IST Time-Candle Breakout** — the 20:00 IST (14:30 UTC) and 20:45 IST (15:15 UTC) M15 candles;
   body close beyond the range triggers the trade (times configurable).

Every setup is scored on core pattern quality + context (H1 bias ×2, M15 bias, killzone, CRT,
DXY, EMA, premium/discount, volume). Trades need `MIN_SCORE_TO_TRADE` (6.5 = grade A); A+ (≥ 8)
setups are broadcast even in signal-only mode. Setups against a clear H1 bias are rejected
(`ALLOW_COUNTER_HTF=false`). The Signals page shows every rejection reason.

### Risk controls
- Risk-based lot size from equity, stop distance and the broker's own tick value;
  rejected if the minimum lot would exceed `MAX_RISK_PERCENT`.
- Max positions per symbol / total, max trades per day, hedging off, cool-down after a loss,
  spread filter, killzone filter, setup expiry, limit-order expiry.
- **News kill-switch:** no new entries T-15 → T+15 min around red-folder events for the symbol's
  currencies (optionally move to breakeven or close before the release).
- **Daily drawdown circuit breaker:** realised + floating loss ≥ `MAX_DAILY_LOSS_PERCENT` closes
  positions, cancels orders, halts until the next trading day and fires a Telegram emergency alert.
- **Panic close** (type `CLOSE ALL` to confirm) closes everything immediately.
- Manual orders show a confirmation with lots, SL, TP and money at risk before they are sent.
- The bot always starts **PAUSED** unless `AUTO_START_BOT=true`.

---

## Quick start (Windows)

1. Install **Python 3.11–3.13 (64-bit)** — tick *Add python.exe to PATH*.
2. Double-click **`run_terminal.bat`**. First run creates `.venv`, installs `requirements.txt`,
   copies `.env.example` → `.env` and opens the web terminal at <http://127.0.0.1:8800>.
3. Start in **`BROKER_MODE=paper`** (default) and forward-test before using real money.

```bat
run_terminal.bat                   :: web terminal (default)
run_terminal.bat --start           :: web terminal + enable trading immediately
run_terminal.bat classic           :: classic Streamlit dashboard on :8501
run_terminal.bat headless          :: engine only (Telegram as your UI)
run_terminal.bat headless --start  :: engine only + enable trading immediately
```

macOS / Linux (paper mode only — the MetaTrader5 package is Windows-only):

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env
python server.py
```

### Going live with MetaTrader 5
1. Install the MT5 terminal, log in to a **demo account first**, and press **Algo Trading**.
2. In `.env`: `BROKER_MODE=mt5`, then either fill `MT5_LOGIN / MT5_PASSWORD / MT5_SERVER`
   or leave them blank to attach to the logged-in terminal (`MT5_TERMINAL_PATH` if you run several).
3. Broker symbol names: `SYMBOL_SUFFIX=m` or `SYMBOL_MAP=XAUUSD=XAUUSDm,BTCUSD=BTCUSD.r`
   (the engine also searches aliases such as `GOLD` and wildcard matches).
4. Broker clock: `MT5_SERVER_UTC_OFFSET=auto` detects it from live ticks; set a number
   (e.g. `2`) if you know it. Killzones and the IST candles depend on correct time.
5. DXY filter needs a dollar-index symbol (`DXY`, `USDX`, …); if your broker has none, set
   `DXY_FILTER=false` (the filter is skipped automatically when data is missing).
6. Restart `run_terminal.bat`, check that the topbar shows **Live** and the bot card is not
   *Disconnected*, then press **Start bot**.

### Telegram channel
1. Create a bot with **@BotFather** → copy the token.
2. Create your channel and add the bot as **administrator** with *Post messages*.
3. Channel id: `@your_public_channel` or the `-100…` id of a private channel.
   Admin chat id: message **@userinfobot**.
4. Put them in `.env` or on the **Telegram** page → **Test connection**, **Send test signal**
   (clearly marked as a test) and **Send today's news digest**.

### Headless / VPS mode
A file lock guarantees only one trading engine per `DATA_DIR`; the web terminal refuses to start a
second one while a headless worker or the classic dashboard is running. On a VPS you can run the
web terminal itself (`WEB_HOST=0.0.0.0` + `WEB_PASSWORD`) behind an HTTPS reverse proxy.

---

## Configuration
Every option is documented in [`.env.example`](.env.example): symbols & timeframes, risk limits,
TP/BE/trailing rules, killzones (`KILLZONE_TIMEZONE=America/New_York`, or define windows in IST),
strategy thresholds, news windows, Telegram behaviour, paper-trading data sources and the
`WEB_*` settings of the web terminal.

Paper-mode data: Binance spot for crypto and Yahoo Finance for FX/metals (`GC=F` gold futures
trade a few dollars above spot and may be delayed). It is for logic forward-testing, not for
exact fills.

## Tests
```bash
pip install -r requirements-dev.txt
pytest -q
```
The suite covers the strategy algorithms on hand-built market structures, lot sizing, the news
parsers/kill-switch, Telegram retry/rate-limit behaviour, the paper broker, the position
protector, the drawdown breaker, `MT5Broker` against a fake MetaTrader5 API, a full
signal → fill → TP1 → trailing → close lifecycle, manual Limit/Stop orders, analytics, chart
payloads and the web API (login, lock-out, CSRF guards, trading round-trip, WebSocket feed,
sanitised errors).

## Troubleshooting
| Symptom | Fix |
| --- | --- |
| `MetaTrader5 package unavailable` | Use Windows + 64-bit Python, or `BROKER_MODE=paper`. |
| Orders rejected with retcode 10027 | Enable the **Algo Trading** button in MT5. |
| `Symbol XAUUSD not found` | Set `SYMBOL_SUFFIX` / `SYMBOL_MAP` to your broker's names. |
| Calendar shows a warning / cached data | The feed is rate-limited; the engine backs off and keeps the last good cache. |
| Telegram `403 Forbidden` | The bot is not an admin of the channel. |
| "Another trading worker is already running" | Stop the headless worker or the classic dashboard (one engine per `DATA_DIR`). |
| Port 8800 already in use | Set `WEB_PORT` in `.env` or run `python server.py --port 8900`. |
| Topbar shows *Reconnecting* | The browser lost the WebSocket; it retries automatically and falls back to polling. |
| Time zone errors on Windows | `pip install tzdata` (included in requirements). |

Logs: `logs/terminal.log` · Data: `data/terminal.db` (SQLite) · Calendar cache: `data/cache/`.

Charts by [TradingView Lightweight Charts™](https://www.tradingview.com/) (Apache-2.0, see
`web/vendor/LIGHTWEIGHT-CHARTS-LICENSE.txt`). Fonts: Inter and JetBrains Mono (SIL OFL 1.1, see `web/fonts/`).

> **Risk warning:** trading leveraged products can lose more than your deposit. This software
> places real orders when connected to a live account — test on demo/paper first. Not financial advice.
