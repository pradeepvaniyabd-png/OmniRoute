// Dashboard: account KPIs, equity curve, bot & risk, market watch, news, positions, signals, activity.

import { api } from "../api.js";
import { panicClose, router, toggleBot } from "../actions.js";
import { MiniChart, chartsAvailable, toChartTime } from "../chart.js";
import { fmtCountdown, fmtDateTime, fmtLots, fmtMoney, fmtNum, fmtPct, fmtPrice, fmtRelative, fmtTime, toDate, tone } from "../format.js";
import { store } from "../store.js";
import { biasBadge, impactBars, kv, orderColumns, positionColumns } from "./common.js";
import {
  DataTable, badge, card, dirBadge, emptyState, errorState, gradeBadge, h, icon, kpi, progress, replace, segmented,
  setText, sparkline, statusBadge, symbolCell,
} from "../ui.js";

const EVENT_ICONS = { trade: "zap", signal: "signal", risk: "shield", news: "calendar", bot: "activity", broker: "wifi" };

export async function mount(root, { scope }) {
  const k = {
    balance: kpi({ label: "Balance", iconName: "wallet", toneName: "brand" }),
    equity: kpi({ label: "Equity", iconName: "activity", toneName: "info" }),
    today: kpi({ label: "Today's P&L", iconName: "trendUp", toneName: "up" }),
    dd: kpi({ label: "Daily loss used", iconName: "shield", toneName: "warn" }),
    win: kpi({ label: "Win rate", iconName: "target", toneName: "violet" }),
    exposure: kpi({ label: "Open exposure", iconName: "layers", toneName: "brand" }),
  };
  const ddBar = progress(0, "up", { lg: true });

  // equity -----------------------------------------------------------
  const equityBox = h("div", { class: "chart-area" });
  let equityChart = null;
  let equityPoints = [];
  let equityRange = "ALL";
  const rangeSeg = segmented([{ value: "1D", label: "1D" }, { value: "1W", label: "1W" }, { value: "1M", label: "1M" }, { value: "ALL", label: "All" }],
    "ALL", (v) => { equityRange = v; drawEquity(); }, { sm: true, label: "Equity range" });
  const equityCard = card({ title: "Equity curve", iconName: "analytics", sub: "Account equity vs balance", actions: rangeSeg, body: equityBox });

  // bot panel ---------------------------------------------------------
  const botStatus = h("div", { class: "row gap-12" });
  const botRows = h("div");
  const botButtons = h("div", { class: "row-wrap mt-12" });
  const botCard = card({ title: "Engine & risk", iconName: "zap", sub: "Automated execution status", body: h("div", null, botStatus, botRows, botButtons) });

  // market watch ------------------------------------------------------
  let sparks = {};
  const watch = new DataTable({
    rowKey: (r) => r.symbol,
    onRowClick: (r) => router.navigate(`/trade?symbol=${r.symbol}`),
    empty: "Waiting for quotes…",
    columns: [
      { key: "symbol", label: "Symbol", render: (r) => symbolCell(r.symbol), sig: (r) => r.symbol },
      { key: "bid", label: "Bid", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.bid, r.digits)) },
      { key: "ask", label: "Ask", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.ask, r.digits)) },
      { key: "spread", label: "Spread", align: "right", render: (r) => h("span", { class: "num dim" }, r.spread === null ? "—" : `${fmtNum(r.spread, 1)}`) },
      { key: "chg", label: "24h", align: "right", render: (r) => h("span", { class: `num ${tone(r.chg)}` }, fmtPct(r.chg)) },
      { key: "trend", label: "Trend", sort: false, render: (r) => sparkline(sparks[r.symbol] || [], { width: 90, height: 26 }), sig: (r) => `${(sparks[r.symbol] || []).length}|${(sparks[r.symbol] || []).at(-1)}` },
      { key: "bias", label: "HTF / LTF", sort: false, render: (r) => h("div", { class: "row gap-4" }, biasBadge(r.htf), biasBadge(r.ltf)) },
      { key: "zone", label: "Zone", sort: false, render: (r) => r.kz ? badge(r.kz, "violet", { sm: true }) : h("span", { class: "dim small" }, "Off-session") },
      { key: "lock", label: "News", sort: false, align: "center", render: (r) => r.lock ? h("span", { class: "warn", title: r.lockLabel }, icon("lock", 15)) : h("span", { class: "dim" }, icon("unlock", 14)) },
    ],
  });
  const watchCard = card({ title: "Market watch", iconName: "globe", sub: "Live quotes, 24h change and SMC bias", body: watch.el, flush: true });

  // news --------------------------------------------------------------
  const newsList = h("div", { class: "feed" });
  const newsCard = card({ title: "Upcoming news", iconName: "calendar", sub: "High & medium impact (Forex Factory)",
    actions: h("a", { class: "btn btn-sm btn-ghost", href: "/calendar", "data-link": "" }, "Calendar", icon("chevronRight", 14)), body: newsList, flush: true });
  let newsEvents = [];

  // positions ---------------------------------------------------------
  const positions = new DataTable({
    rowKey: (r) => r.ticket,
    empty: "No open positions — the bot will open trades when an A-grade setup triggers.",
    onRowClick: (r) => router.navigate(`/trade?symbol=${r.canonical || r.symbol}`),
    columns: positionColumns(),
  });
  const orders = new DataTable({ rowKey: (r) => r.ticket, empty: "No pending orders", columns: orderColumns() });
  const ordersWrap = h("div", { hidden: true }, h("div", { class: "card-head" }, h("h3", { class: "card-title" }, icon("clock", 16), "Pending orders")), orders.el);
  const positionsCard = card({ title: "Open positions", iconName: "layers", sub: "Live P&L · protected by breakeven & trailing logic",
    actions: [h("a", { class: "btn btn-sm btn-ghost", href: "/trade", "data-link": "" }, "Trade terminal", icon("chevronRight", 14))], body: [positions.el, ordersWrap], flush: true });

  // signals & activity ------------------------------------------------
  const signals = new DataTable({
    rowKey: (r) => r.signal_id || r.id,
    empty: "No setups yet — signals appear as soon as the engine detects one.",
    onRowClick: () => router.navigate("/signals"),
    columns: [
      { key: "created_at", label: "Time", render: (r) => h("span", { class: "dim small nowrap" }, fmtDateTime(r.created_at)) },
      { key: "symbol", label: "Symbol", render: (r) => h("span", { class: "strong" }, r.symbol) },
      { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
      { key: "strategy", label: "Setup", render: (r) => h("span", { class: "small ellipsis", style: { maxWidth: "190px", display: "inline-block" }, title: r.strategy }, r.strategy) },
      { key: "grade", label: "Grade", render: (r) => gradeBadge(r.grade) },
      { key: "score", label: "Score", align: "right", render: (r) => h("span", { class: "num" }, fmtNum(r.score, 1)) },
      { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
    ],
  });
  const signalsCard = card({ title: "Latest signals", iconName: "signal", sub: "SMC / ICT setups",
    actions: h("a", { class: "btn btn-sm btn-ghost", href: "/signals", "data-link": "" }, "All signals", icon("chevronRight", 14)), body: signals.el, flush: true });
  const activity = h("div", { class: "feed" });
  const activityCard = card({ title: "Activity", iconName: "bell", sub: "Trades, risk and news events", body: activity, flush: true });

  root.append(h("div", { class: "stack" },
    h("div", { class: "kpis k6" }, Object.values(k)),
    h("div", { class: "grid" }, h("div", { class: "span-8" }, equityCard), h("div", { class: "span-4" }, botCard)),
    h("div", { class: "grid" }, h("div", { class: "span-7" }, watchCard), h("div", { class: "span-5" }, newsCard)),
    positionsCard,
    h("div", { class: "grid" }, h("div", { class: "span-7" }, signalsCard), h("div", { class: "span-5" }, activityCard))));

  // --------------------------------------------------------- renderers --
  function renderKpis(s) {
    const a = s.account || {};
    const d = s.drawdown || {};
    const cur = a.currency || "USD";
    k.balance.set({ value: fmtMoney(a.balance, { currency: cur }), sub: [s.mode === "MT5" ? `#${a.login || "—"} · ${a.server || ""}` : "Paper account", a.leverage ? badge(`1:${a.leverage}`, "", { sm: true }) : null] });
    k.equity.set({ value: fmtMoney(a.equity, { currency: cur }), sub: [h("span", { class: tone(a.profit) }, `Floating ${fmtMoney(a.profit, { currency: cur, sign: true })}`), h("span", { class: "dim" }, `· Free ${fmtMoney(a.free_margin, { currency: cur, compact: true })}`)] });
    const start = d.start_equity;
    const todayPnl = a.equity !== undefined && start ? a.equity - start : null;
    const todayPct = todayPnl !== null && start ? (todayPnl / start) * 100 : null;
    k.today.set({ value: fmtMoney(todayPnl, { currency: cur, sign: true }), valueTone: tone(todayPnl), tone: todayPnl < 0 ? "down" : "up", iconName: todayPnl < 0 ? "trendDown" : "trendUp",
      sub: [h("span", { class: tone(todayPct) }, fmtPct(todayPct)), h("span", { class: "dim" }, `· Realized ${fmtMoney(d.realized_today, { currency: cur, sign: true })}`)] });
    const used = d.limit_pct ? Math.min(100, ((d.drawdown_pct || 0) / d.limit_pct) * 100) : 0;
    ddBar.set(used, used >= 80 ? "down" : used >= 50 ? "warn" : "up");
    k.dd.set({ value: `${fmtNum(d.drawdown_pct ?? 0, 2)}%`, tone: d.halted ? "down" : used >= 50 ? "warn" : "up",
      sub: h("div", { class: "gauge", style: { width: "100%" } }, ddBar, h("div", { class: "gauge-meta" }, h("span", null, d.halted ? "HALTED" : `${fmtNum(used, 0)}% of limit`), h("span", null, `Limit ${fmtNum(d.limit_pct, 1)}%`))) });
    const pos = s.positions || [];
    const lots = pos.reduce((sum, p) => sum + (p.volume || 0), 0);
    k.exposure.set({ value: `${pos.length} position${pos.length === 1 ? "" : "s"}`, sub: [`${fmtLots(lots)} lots`, h("span", { class: "dim" }, `· ${(s.orders || []).length} pending`), a.margin_level ? h("span", { class: "dim" }, `· ML ${fmtNum(a.margin_level, 0)}%`) : null] });
  }

  function renderBot(s) {
    const r = s.runtime || {};
    const status = s.bot_status || "STARTING";
    const toneMap = { RUNNING: "up", PAUSED: "warn", HALTED: "down", DISCONNECTED: "down", "NEWS LOCKED": "warn" };
    replace(botStatus, h("span", { class: `kpi-icon`, style: { width: "42px", height: "42px", background: `var(--${toneMap[status] || "brand"}-soft)`, color: `var(--${toneMap[status] || "brand"})` } }, icon(status === "RUNNING" ? "zap" : status === "PAUSED" ? "pause" : "alert", 20)),
      h("div", null, h("div", { class: "strong", style: { fontSize: "16px" } }, status), h("div", { class: "small muted" }, s.status_reason || "")));
    const strategies = (store.bootstrap?.strategies || []).filter((x) => (r.enabled_strategies || []).includes(x.code));
    replace(botRows, h("div", { class: "mt-12" },
      kv("Execution", r.execution_mode === "signal_only" ? badge("Signal only", "info", { sm: true }) : badge("Auto-trade", "up", { sm: true })),
      kv("Risk per trade", h("span", { class: "num" }, `${fmtNum(r.risk_percent, 2)}%`)),
      kv("Strategies", h("div", { class: "row-wrap", style: { justifyContent: "flex-end" } }, strategies.length ? strategies.map((x) => badge(x.label, "brand", { sm: true })) : h("span", { class: "dim" }, "None"))),
      kv("Killzone filter", r.require_killzone ? badge("Required", "violet", { sm: true }) : badge("Off", "", { sm: true })),
      kv("News auto-pause", r.news_auto_pause ? badge("On", "up", { sm: true }) : badge("Off", "warn", { sm: true })),
      kv("Last scan", h("span", { class: "small" }, s.last_scan_at ? fmtRelative(s.last_scan_at) : "—")),
      kv("Broker", s.broker_connected ? h("span", { class: "row gap-4 small" }, h("span", { class: "dot dot-up" }), `${s.mode} · ${s.ping_ms ? `${Math.round(s.ping_ms)} ms` : "online"}`) : h("span", { class: "down small" }, "Disconnected"))));
    const enabled = Boolean(r.bot_enabled);
    replace(botButtons,
      h("button", { class: `btn btn-sm ${enabled ? "btn-warn" : "btn-success"}`, onClick: toggleBot }, icon(enabled ? "pause" : "play", 14), enabled ? "Pause bot" : "Start bot"),
      h("button", { class: "btn btn-sm btn-danger", onClick: panicClose }, icon("power", 14), "Panic close"),
      h("a", { class: "btn btn-sm btn-ghost", href: "/settings", "data-link": "" }, icon("sliders", 14), "Settings"));
  }

  function renderWatch(s) {
    const rows = (store.bootstrap?.symbols || []).map((symbol) => {
      const q = s.symbols?.[symbol] || {};
      const an = q.analysis || {};
      const lock = s.news_locks?.[symbol];
      return { symbol, bid: q.bid, ask: q.ask, digits: q.digits ?? 5, spread: q.spread_points ?? null, chg: q.change_24h_pct ?? null,
        htf: an.htf_bias, ltf: an.ltf_trend, kz: (an.killzones || [])[0] || null, lock: Boolean(lock?.active), lockLabel: lock?.label || "" };
    });
    watch.update(rows);
  }

  function renderPositions(s) {
    positions.update(s.positions || []);
    const ord = s.orders || [];
    ordersWrap.hidden = ord.length === 0;
    orders.update(ord);
  }

  function renderNews() {
    const now = Date.now();
    const upcoming = newsEvents.filter((e) => ["High", "Medium"].includes(e.impact) && e.time_utc && new Date(e.time_utc).getTime() > now - 30 * 60000).slice(0, 7);
    if (!upcoming.length) {
      replace(newsList, emptyState({ iconName: "calendar", title: "No major news ahead", text: "High-impact events for your symbols' currencies will show here." }));
      return;
    }
    replace(newsList, upcoming.map((e) => {
      const t = toDate(e.time_utc);
      const secs = (t.getTime() - now) / 1000;
      const cd = h("span", { class: `countdown ${secs <= 0 ? "live" : secs < 3600 ? "soon" : ""}` }, secs <= 0 ? "LIVE" : fmtCountdown(secs));
      cd.dataset.t = String(t.getTime());
      return h("div", { class: "feed-item" },
        h("div", { style: { width: "54px" } }, h("div", { class: "num small" }, fmtTime(t)), h("div", { class: "tiny dim" }, e.currency)),
        h("div", { class: "feed-body" }, h("div", { class: "row gap-4" }, impactBars(e.impact), h("span", { class: "feed-title ellipsis" }, e.title)),
          h("div", { class: "feed-detail" }, `Fcst ${e.forecast || "—"} · Prev ${e.previous || "—"}${e.affected?.length ? ` · ${e.affected.join(", ")}` : ""}`)),
        cd);
    }));
  }

  function tickCountdowns() {
    const now = Date.now();
    newsList.querySelectorAll(".countdown[data-t]").forEach((el) => {
      const secs = (Number(el.dataset.t) - now) / 1000;
      setText(el, secs <= 0 ? "LIVE" : fmtCountdown(secs));
      el.className = `countdown ${secs <= 0 ? "live" : secs < 3600 ? "soon" : ""}`;
    });
  }

  function renderActivity() {
    const items = [...store.events].reverse().slice(0, 9);
    if (!items.length) {
      replace(activity, emptyState({ iconName: "bell", title: "No activity yet", text: "Executions, stop moves, news locks and risk alerts stream here in real time." }));
      return;
    }
    replace(activity, items.map((e) => {
      const level = ["success", "danger", "warning"].includes(e.level) ? e.level : "info";
      return h("div", { class: "feed-item" }, h("span", { class: `feed-icon ${level}` }, icon(EVENT_ICONS[e.kind] || "info", 15)),
        h("div", { class: "feed-body" }, h("div", { class: "feed-title" }, e.title), e.detail ? h("div", { class: "feed-detail" }, e.detail) : null),
        h("span", { class: "feed-time" }, fmtRelative(e.ts)));
    }));
  }

  function drawEquity() {
    if (!chartsAvailable()) { replace(equityBox, errorState("Chart library failed to load.")); return; }
    const now = Date.now();
    const spans = { "1D": 86400e3, "1W": 7 * 86400e3, "1M": 30 * 86400e3 };
    const cutoff = spans[equityRange] ? now - spans[equityRange] : 0;
    const pts = equityPoints.filter((p) => p.ts && new Date(p.ts).getTime() >= cutoff && p.equity !== null);
    if (pts.length < 2) {
      equityChart?.destroy();
      equityChart = null;
      replace(equityBox, emptyState({ iconName: "analytics", title: "Equity curve is building", text: "The engine logs equity once a minute — the curve appears after a few minutes of running." }));
      return;
    }
    if (!equityChart) {
      equityBox.replaceChildren();
      equityChart = new MiniChart(equityBox, { valueFormatter: (v) => fmtMoney(v, { digits: 0 }) });
      scope.add(() => equityChart?.destroy());
    }
    const dedupe = (rows) => {
      const out = [];
      for (const r of rows) {
        if (r.time === null) continue;
        if (out.length && r.time <= out[out.length - 1].time) out[out.length - 1] = { ...r, time: out[out.length - 1].time };
        else out.push(r);
      }
      return out;
    };
    equityChart.clear();
    const up = pts[pts.length - 1].equity >= pts[0].equity;
    equityChart.add("area", dedupe(pts.map((p) => ({ time: toChartTime(p.ts), value: p.equity }))), { color: up ? "up" : "down" });
    equityChart.add("line", dedupe(pts.filter((p) => p.balance !== null).map((p) => ({ time: toChartTime(p.ts), value: p.balance }))), { color: "brand", dashed: true, width: 1 });
    equityChart.fit();
  }

  // ------------------------------------------------------------ loaders --
  async function loadAnalytics() {
    try {
      const report = await api.get("/api/analytics", { signal: scope.signal });
      equityPoints = report.equity || [];
      const st = report.stats || {};
      k.win.set({ value: st.total_trades ? `${fmtNum(st.win_rate, 1)}%` : "—",
        sub: st.total_trades ? [`${st.wins}W · ${st.losses}L`, h("span", { class: "dim" }, `· PF ${st.profit_factor === null ? "∞" : fmtNum(st.profit_factor, 2)}`), h("span", { class: tone(st.net_pnl) }, `· ${fmtMoney(st.net_pnl, { sign: true, compact: true })}`)] : "No closed trades yet" });
      drawEquity();
    } catch (err) {
      if (err.name !== "AbortError") k.win.set({ value: "—", sub: "Analytics unavailable" });
    }
  }
  async function loadSignals() {
    const res = await api.get("/api/signals?limit=8", { signal: scope.signal });
    signals.update(res.items || []);
  }
  async function loadNews() {
    const res = await api.get("/api/calendar", { signal: scope.signal });
    newsEvents = res.events || [];
    renderNews();
  }
  async function loadSparks() {
    const res = await api.get("/api/sparklines?points=96", { signal: scope.signal });
    sparks = res.series || {};
    if (store.snapshot) renderWatch(store.snapshot);
  }

  const update = (s) => {
    if (!s) return;
    renderKpis(s);
    renderBot(s);
    renderWatch(s);
    renderPositions(s);
  };
  update(store.snapshot);
  renderActivity();
  scope.on("snapshot", update);
  scope.on("events", renderActivity);
  scope.every(60000, loadAnalytics);
  scope.every(15000, loadSignals);
  scope.every(60000, loadNews);
  scope.every(60000, loadSparks);
  scope.every(1000, tickCountdowns, { immediate: false });
  scope.every(30000, renderActivity, { immediate: false });
}
