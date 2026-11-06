// Analytics: performance KPIs, equity / cumulative / daily charts, monthly heatmap, breakdowns and journal.

import { api } from "../api.js";
import { MiniChart, chartsAvailable, toChartTime } from "../chart.js";
import { dayKey, fmtDateTime, fmtDuration, fmtLots, fmtMoney, fmtNum, fmtPct, fmtPrice, getDisplayTz, toDate, tone, wallClock } from "../format.js";
import { digitsOf, store } from "../store.js";
import {
  DataTable, card, dirBadge, downloadFile, emptyState, errorState, h, icon, kpi, replace, searchInput, segmented, setText, statusBadge, toCSV,
} from "../ui.js";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export async function mount(root, { scope }) {
  let report = null;
  let trades = [];
  let period = "ALL";
  const charts = {};
  scope.add(() => Object.values(charts).forEach((c) => c?.destroy()));

  const k = {
    net: kpi({ label: "Net profit", iconName: "wallet", toneName: "up" }),
    win: kpi({ label: "Win rate", iconName: "target", toneName: "brand" }),
    pf: kpi({ label: "Profit factor", iconName: "scale", toneName: "violet" }),
    exp: kpi({ label: "Expectancy", iconName: "sigma", toneName: "info" }),
    dd: kpi({ label: "Max drawdown", iconName: "trendDown", toneName: "down" }),
    hold: kpi({ label: "Avg hold time", iconName: "clock", toneName: "warn" }),
  };
  const periodSeg = segmented([{ value: "7D", label: "7D" }, { value: "30D", label: "30D" }, { value: "90D", label: "90D" }, { value: "ALL", label: "All time" }], period,
    (v) => { period = v; render(); }, { label: "Period" });
  const refreshBtn = h("button", { class: "btn btn-sm", onClick: () => load(true) }, icon("refresh", 14), "Refresh");
  const exportBtn = h("button", { class: "btn btn-sm", onClick: () => exportCsv() }, icon("download", 14), "Export journal");

  const equityBox = h("div", { class: "chart-area" });
  const cumBox = h("div", { class: "chart-area sm" });
  const dailyBox = h("div", { class: "chart-area sm" });
  const heatmap = h("div", { class: "table-wrap" });
  const hourBars = h("div");
  const dayBars = h("div");
  const statList = h("div", { class: "stat-list" });
  const lsSplit = h("div", { class: "ls-split" });
  const byStrategy = breakdownTable("Strategy");
  const bySymbol = breakdownTable("Symbol");
  const bySession = breakdownTable("Session");

  let journalPage = 0;
  const PAGE = 25;
  let journalQuery = "";
  const pagerInfo = h("span");
  const prevBtn = h("button", { class: "btn btn-xs", onClick: () => { journalPage = Math.max(0, journalPage - 1); renderJournal(); } }, icon("chevronLeft", 13), "Prev");
  const nextBtn = h("button", { class: "btn btn-xs", onClick: () => { journalPage += 1; renderJournal(); } }, "Next", icon("chevronRight", 13));
  const journal = new DataTable({
    rowKey: (r) => r.id, empty: "No closed trades in this period.", sortBy: "closed_at",
    columns: [
      { key: "closed_at", label: "Closed", render: (r) => h("span", { class: "small nowrap" }, fmtDateTime(r.closed_at)) },
      { key: "symbol", label: "Symbol", render: (r) => h("span", { class: "strong" }, r.symbol) },
      { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
      { key: "strategy", label: "Strategy", render: (r) => h("span", { class: "small" }, r.strategy) },
      { key: "volume", label: "Lots", align: "right", render: (r) => h("span", { class: "num" }, fmtLots(r.volume)) },
      { key: "fill_price", label: "Entry", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.fill_price ?? r.entry_price, digitsOf(r.symbol))) },
      { key: "exit_price", label: "Exit", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.exit_price, digitsOf(r.symbol))) },
      { key: "pips", label: "Pips", align: "right", render: (r) => h("span", { class: `num ${tone(r.pips)}` }, fmtNum(r.pips, 1, { sign: true })) },
      { key: "r_multiple", label: "R", align: "right", render: (r) => h("span", { class: `num ${tone(r.r_multiple)}` }, r.r_multiple === null ? "—" : `${fmtNum(r.r_multiple, 2, { sign: true })}R`) },
      { key: "pnl", label: "P&L", align: "right", render: (r) => h("span", { class: `num strong ${tone(r.pnl)}` }, fmtMoney(r.pnl, { sign: true })) },
      { key: "close_reason", label: "Exit reason", render: (r) => statusBadge(r.close_reason) },
      { key: "hold", label: "Held", align: "right", sortValue: holdMinutes, render: (r) => h("span", { class: "small dim" }, fmtDuration(holdMinutes(r))) },
    ],
  });
  const journalSearch = searchInput({ placeholder: "Filter by symbol, strategy, reason…", onInput: (v) => { journalQuery = v.toLowerCase(); journalPage = 0; renderJournal(); } });

  const body = h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("div", null, h("h2", null, "Performance analytics"), h("p", null, "Every closed trade from the journal — computed live from the terminal database.")),
      h("div", { class: "toolbar" }, periodSeg, refreshBtn, exportBtn)),
    h("div", { class: "kpis k6" }, Object.values(k)),
    h("div", { class: "grid" },
      h("div", { class: "span-8" }, card({ title: "Equity curve", iconName: "analytics", sub: "Equity (area) and balance (dashed)", body: equityBox })),
      h("div", { class: "span-4" }, card({ title: "Trade statistics", iconName: "sigma", body: [statList, h("div", { class: "mt-16" }, lsSplit)] }))),
    h("div", { class: "grid" },
      h("div", { class: "span-6" }, card({ title: "Cumulative P&L", iconName: "trendUp", sub: "Running total after each closed trade", body: cumBox })),
      h("div", { class: "span-6" }, card({ title: "Daily P&L", iconName: "calendar", sub: "Net result per trading day", body: dailyBox }))),
    card({ title: "Monthly returns", iconName: "grid", sub: "Return % vs the balance at the start of each month (all time)", body: heatmap }),
    h("div", { class: "grid" },
      h("div", { class: "span-6" }, card({ title: "P&L by hour", iconName: "clock", sub: "Entry hour in your display time zone", body: hourBars })),
      h("div", { class: "span-6" }, card({ title: "P&L by weekday", iconName: "calendar", sub: "Which days pay", body: dayBars }))),
    h("div", { class: "grid" },
      h("div", { class: "span-4" }, card({ title: "By strategy", iconName: "zap", body: byStrategy.el, flush: true })),
      h("div", { class: "span-4" }, card({ title: "By symbol", iconName: "globe", body: bySymbol.el, flush: true })),
      h("div", { class: "span-4" }, card({ title: "By killzone", iconName: "target", body: bySession.el, flush: true }))),
    card({ title: "Trade journal", iconName: "book", sub: "Closed trades", actions: journalSearch, flush: true,
      body: [journal.el, h("div", { class: "pager" }, pagerInfo, h("div", { class: "row" }, prevBtn, nextBtn))] }));
  root.append(body);

  // ----------------------------------------------------------- compute --
  function periodTrades() {
    const spans = { "7D": 7, "30D": 30, "90D": 90 };
    if (!spans[period]) return trades;
    const cutoff = Date.now() - spans[period] * 86400e3;
    return trades.filter((t) => (toDate(t.closed_at)?.getTime() || 0) >= cutoff);
  }

  function periodEquity() {
    const points = report?.equity || [];
    const spans = { "7D": 7, "30D": 30, "90D": 90 };
    if (!spans[period]) return points;
    const cutoff = Date.now() - spans[period] * 86400e3;
    return points.filter((p) => (toDate(p.ts)?.getTime() || 0) >= cutoff);
  }

  function render() {
    if (!report) return;
    const list = periodTrades();
    const s = stats(list);
    const dd = drawdown(periodEquity());
    const cur = report.currency || "USD";
    k.net.set({ value: fmtMoney(s.net, { currency: cur, sign: true }), valueTone: tone(s.net), tone: s.net < 0 ? "down" : "up",
      sub: [`${s.total} trades`, h("span", { class: "dim" }, `· Gross +${fmtMoney(s.grossProfit, { currency: cur, compact: true })} / -${fmtMoney(s.grossLoss, { currency: cur, compact: true })}`)] });
    k.win.set({ value: s.total ? `${fmtNum(s.winRate, 1)}%` : "—", sub: `${s.wins} wins · ${s.losses} losses · ${s.breakeven} BE` });
    k.pf.set({ value: s.total ? (s.pf === Infinity ? "∞" : fmtNum(s.pf, 2)) : "—", sub: s.pf >= 1.5 ? "Healthy edge (≥ 1.5)" : s.total ? "Target ≥ 1.5" : "No trades yet" });
    k.exp.set({ value: s.total ? fmtMoney(s.expectancy, { currency: cur, sign: true }) : "—", sub: `Avg R ${s.avgR === null ? "—" : `${fmtNum(s.avgR, 2, { sign: true })}R`} per trade` });
    k.dd.set({ value: `${fmtNum(dd.maxPct, 2)}%`, sub: `${fmtMoney(dd.maxAmount, { currency: cur })} peak-to-trough · now ${fmtNum(dd.currentPct, 2)}%` });
    k.hold.set({ value: s.avgHold === null ? "—" : fmtDuration(s.avgHold), sub: s.medianHold === null ? "—" : `Median ${fmtDuration(s.medianHold)}` });

    replace(statList,
      stat("Average win", fmtMoney(s.avgWin, { currency: cur }), "up"), stat("Average loss", fmtMoney(-s.avgLoss, { currency: cur }), "down"),
      stat("Best trade", fmtMoney(s.best, { currency: cur, sign: true }), tone(s.best)), stat("Worst trade", fmtMoney(s.worst, { currency: cur, sign: true }), tone(s.worst)),
      stat("Win streak", `${s.maxWins} trades`), stat("Loss streak", `${s.maxLosses} trades`),
      stat("Reward : risk (avg)", s.avgLoss ? `${fmtNum(s.avgWin / s.avgLoss, 2)} : 1` : "—"), stat("Break-even win rate", s.avgLoss && s.avgWin ? `${fmtNum((s.avgLoss / (s.avgWin + s.avgLoss)) * 100, 1)}%` : "—"));
    const longs = stats(list.filter((t) => t.direction === "BUY"));
    const shorts = stats(list.filter((t) => t.direction === "SELL"));
    replace(lsSplit, lsBox("Long", longs, cur, "up"), lsBox("Short", shorts, cur, "down"));

    drawEquity();
    drawCumulative(list);
    drawDaily(list);
    drawHeatmap();
    drawBars(hourBars, Array.from({ length: 24 }, (_, i) => ({ label: String(i).padStart(2, "0"), ...bucket(list.filter((t) => hourOf(t) === i)) })), cur, 3);
    drawBars(dayBars, WEEKDAYS.map((d, i) => ({ label: d, ...bucket(list.filter((t) => weekdayOf(t) === i)) })), cur, 1);
    byStrategy.update(groupBy(list, (t) => t.strategy || "—"));
    bySymbol.update(groupBy(list, (t) => t.symbol || "—"));
    bySession.update(groupBy(list, (t) => sessionOf(t)));
    renderJournal();
  }

  function renderJournal() {
    const rows = periodTrades().filter((t) => !journalQuery || `${t.symbol} ${t.strategy} ${t.close_reason} ${t.direction}`.toLowerCase().includes(journalQuery));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    journalPage = Math.min(journalPage, pages - 1);
    const sorted = [...rows].sort((a, b) => (toDate(b.closed_at)?.getTime() || 0) - (toDate(a.closed_at)?.getTime() || 0));
    journal.update(sorted.slice(journalPage * PAGE, journalPage * PAGE + PAGE));
    setText(pagerInfo, rows.length ? `Showing ${journalPage * PAGE + 1}–${Math.min(rows.length, (journalPage + 1) * PAGE)} of ${rows.length}` : "No trades");
    prevBtn.disabled = journalPage === 0;
    nextBtn.disabled = journalPage >= pages - 1;
  }

  // ------------------------------------------------------------- charts --
  function chartSlot(key, box, factory) {
    if (!chartsAvailable()) { replace(box, errorState("Chart library failed to load.")); return null; }
    if (!charts[key]) { box.replaceChildren(); charts[key] = factory(box); }
    return charts[key];
  }
  function dropChart(key, box, empty) {
    charts[key]?.destroy();
    charts[key] = null;
    replace(box, empty);
  }
  function monotonic(points) {
    const out = [];
    for (const p of points) {
      if (p.time === null || p.value === null || p.value === undefined) continue;
      if (out.length && p.time <= out[out.length - 1].time) out[out.length - 1] = { time: out[out.length - 1].time, value: p.value };
      else out.push(p);
    }
    return out;
  }

  function drawEquity() {
    const pts = periodEquity().filter((p) => p.equity !== null);
    if (pts.length < 2) return dropChart("equity", equityBox, emptyState({ iconName: "analytics", title: "Not enough equity history", text: "The engine records equity every minute while it runs." }));
    const chart = chartSlot("equity", equityBox, (box) => new MiniChart(box, { valueFormatter: (v) => fmtMoney(v, { digits: 0 }) }));
    if (!chart) return;
    chart.clear();
    chart.add("area", monotonic(pts.map((p) => ({ time: toChartTime(p.ts), value: p.equity }))), { color: pts.at(-1).equity >= pts[0].equity ? "up" : "down" });
    chart.add("line", monotonic(pts.filter((p) => p.balance !== null).map((p) => ({ time: toChartTime(p.ts), value: p.balance }))), { color: "brand", dashed: true, width: 1 });
    chart.fit();
  }

  function drawCumulative(list) {
    const ordered = [...list].sort((a, b) => (toDate(a.closed_at)?.getTime() || 0) - (toDate(b.closed_at)?.getTime() || 0));
    if (ordered.length < 2) return dropChart("cum", cumBox, emptyState({ iconName: "trendUp", title: "Needs two or more closed trades" }));
    let running = 0;
    const pts = monotonic(ordered.map((t) => { running += Number(t.pnl) || 0; return { time: toChartTime(t.closed_at), value: Number(running.toFixed(2)) }; }));
    const chart = chartSlot("cum", cumBox, (box) => new MiniChart(box, { valueFormatter: (v) => fmtMoney(v, { digits: 0 }) }));
    if (!chart) return;
    chart.clear();
    chart.add("baseline", pts, { base: 0 });
    chart.fit();
  }

  function drawDaily(list) {
    const days = new Map();
    for (const t of list) {
      const key = dayKey(t.closed_at);
      if (key) days.set(key, (days.get(key) || 0) + (Number(t.pnl) || 0));
    }
    if (!days.size) return dropChart("daily", dailyBox, emptyState({ iconName: "calendar", title: "No closed trades in this period" }));
    const data = [...days].sort((a, b) => a[0].localeCompare(b[0])).map(([time, value]) => ({ time, value: Number(value.toFixed(2)) }));
    const chart = chartSlot("daily", dailyBox, (box) => new MiniChart(box, { valueFormatter: (v) => fmtMoney(v, { digits: 0 }), timeVisible: false }));
    if (!chart) return;
    chart.clear();
    chart.add("histogram", data);
    chart.fit();
  }

  function drawHeatmap() {
    const monthly = report.monthly || [];
    if (!monthly.length) { replace(heatmap, emptyState({ iconName: "grid", title: "No monthly history yet" })); return; }
    const byYear = new Map();
    for (const m of monthly) {
      const [y, mo] = m.month.split("-").map(Number);
      if (!byYear.has(y)) byYear.set(y, new Map());
      byYear.get(y).set(mo, m);
    }
    const maxAbs = Math.max(1, ...monthly.map((m) => Math.abs(m.return_pct ?? 0)));
    const grid = h("div", { class: "heatmap" }, h("div", { class: "hm-head" }, "Year"), MONTHS.map((m) => h("div", { class: "hm-head" }, m)), h("div", { class: "hm-head" }, "Year %"));
    for (const [year, months] of [...byYear].sort((a, b) => b[0] - a[0])) {
      grid.append(h("div", { class: "hm-year" }, String(year)));
      let compounded = 1;
      let hasPct = false;
      for (let i = 1; i <= 12; i += 1) {
        const m = months.get(i);
        if (!m) { grid.append(h("div", { class: "hm-cell" }, "")); continue; }
        const pct = m.return_pct;
        if (pct !== null && pct !== undefined) { compounded *= 1 + pct / 100; hasPct = true; }
        const strength = pct === null || pct === undefined ? 0.15 : Math.min(1, Math.abs(pct) / maxAbs);
        const color = (pct ?? m.pnl) >= 0 ? "var(--up)" : "var(--down)";
        const cell = h("div", { class: "hm-cell has", title: `${MONTHS[i - 1]} ${year}: ${fmtMoney(m.pnl, { sign: true })} · ${m.trades} trades` }, pct === null || pct === undefined ? fmtMoney(m.pnl, { compact: true, sign: true }) : fmtPct(pct, 1));
        cell.style.background = `color-mix(in srgb, ${color} ${Math.round(12 + strength * 50)}%, var(--surface-2))`;
        grid.append(cell);
      }
      const yearPct = hasPct ? (compounded - 1) * 100 : null;
      grid.append(h("div", { class: `hm-cell has hm-total ${tone(yearPct)}` }, yearPct === null ? "—" : fmtPct(yearPct, 1)));
    }
    replace(heatmap, grid);
  }

  function drawBars(container, rows, cur, labelEvery) {
    if (!rows.some((r) => r.trades)) { replace(container, emptyState({ iconName: "growth", title: "No trades in this period" })); return; }
    const maxAbs = Math.max(1, ...rows.map((r) => Math.abs(r.pnl)));
    replace(container, h("div", { class: "bars" }, rows.map((r, i) => {
      const height = `${(Math.abs(r.pnl) / maxAbs) * 100}%`;
      const bar = h("div", { class: `bar ${r.pnl < 0 ? "neg" : ""} ${r.trades ? "" : "zero"}` });
      bar.style.height = height;
      return h("div", { class: "bar-col" },
        h("div", { class: "bar-tip" }, `${r.label}: ${fmtMoney(r.pnl, { currency: cur, sign: true })} · ${r.trades} trades · ${fmtNum(r.winRate, 0)}% win`),
        h("div", { class: "bar-zone" }, r.pnl >= 0 ? bar : null), h("div", { class: "bar-zone neg" }, r.pnl < 0 ? bar : null),
        h("div", { class: "bar-label" }, i % labelEvery === 0 ? r.label : ""));
    })));
  }

  // -------------------------------------------------------------- data --
  function exportCsv() {
    const rows = periodTrades();
    downloadFile(`trade-journal-${dayKey(new Date())}.csv`, toCSV(rows, [
      { label: "id", key: "id" }, { label: "opened_at", key: "opened_at" }, { label: "closed_at", key: "closed_at" }, { label: "symbol", key: "symbol" },
      { label: "direction", key: "direction" }, { label: "strategy", key: "strategy" }, { label: "order_type", key: "order_type" }, { label: "volume", key: "volume" },
      { label: "entry", value: (r) => r.fill_price ?? r.entry_price }, { label: "exit", key: "exit_price" }, { label: "sl_initial", key: "sl_initial" },
      { label: "tp1", key: "tp1" }, { label: "tp2", key: "tp2" }, { label: "pips", key: "pips" }, { label: "r_multiple", key: "r_multiple" },
      { label: "pnl", key: "pnl" }, { label: "close_reason", key: "close_reason" }, { label: "grade", key: "grade" },
    ]), "text/csv");
  }

  async function load(manual = false) {
    try {
      const [rep, closed] = await Promise.all([
        api.get("/api/analytics", { signal: scope.signal, timeout: 30000 }),
        api.get("/api/trades?status=CLOSED&limit=5000", { signal: scope.signal, timeout: 30000 }),
      ]);
      report = rep;
      trades = (closed.items || []).filter((t) => t.status === "CLOSED" && t.closed_at);
      render();
      if (manual) refreshBtn.blur();
    } catch (err) {
      if (err.name === "AbortError") return;
      if (!report) root.prepend(errorState(`Analytics could not be loaded: ${err.message}`, () => load(true)));
    }
  }
  scope.every(60000, () => load());
  scope.on("prefs", () => render());
}

// ---------------------------------------------------------------- helpers --
function holdMinutes(t) {
  const a = toDate(t.opened_at);
  const b = toDate(t.closed_at);
  return a && b && b >= a ? (b - a) / 60000 : null;
}

function hourOf(t) {
  const d = toDate(t.opened_at) || toDate(t.closed_at);
  return d ? wallClock(d, getDisplayTz()).h : -1;
}

function weekdayOf(t) {
  const d = toDate(t.opened_at) || toDate(t.closed_at);
  if (!d) return -1;
  const w = wallClock(d, getDisplayTz()).weekday; // 0 = Sunday
  return (w + 6) % 7;
}

function sessionOf(t) {
  const d = toDate(t.opened_at) || toDate(t.closed_at);
  const zones = store.bootstrap?.killzones || [];
  const tz = store.bootstrap?.killzone_tz || "America/New_York";
  if (!d || !zones.length) return "—";
  const w = wallClock(d, tz);
  const minutes = w.h * 60 + w.m;
  const toMin = (hhmm) => { const [hh, mm] = String(hhmm).split(":").map(Number); return hh * 60 + mm; };
  for (const z of zones) {
    const a = toMin(z.start);
    const b = toMin(z.end);
    const inside = a <= b ? minutes >= a && minutes < b : minutes >= a || minutes < b;
    if (inside) return z.name;
  }
  return "Off-session";
}

function stats(list) {
  const pnls = list.map((t) => Number(t.pnl) || 0);
  const wins = pnls.filter((p) => p > 1e-9);
  const losses = pnls.filter((p) => p < -1e-9);
  const grossProfit = wins.reduce((a, b) => a + b, 0);
  const grossLoss = -losses.reduce((a, b) => a + b, 0);
  const net = grossProfit - grossLoss;
  let maxWins = 0; let maxLosses = 0; let w = 0; let l = 0;
  const ordered = [...list].sort((a, b) => (toDate(a.closed_at)?.getTime() || 0) - (toDate(b.closed_at)?.getTime() || 0));
  for (const t of ordered) {
    const p = Number(t.pnl) || 0;
    if (p > 1e-9) { w += 1; l = 0; } else if (p < -1e-9) { l += 1; w = 0; } else { w = 0; l = 0; }
    maxWins = Math.max(maxWins, w);
    maxLosses = Math.max(maxLosses, l);
  }
  const rs = list.map((t) => t.r_multiple).filter((r) => r !== null && r !== undefined && Number.isFinite(Number(r))).map(Number);
  const holds = list.map(holdMinutes).filter((m) => m !== null).sort((a, b) => a - b);
  const mid = Math.floor(holds.length / 2);
  return {
    total: list.length, wins: wins.length, losses: losses.length, breakeven: list.length - wins.length - losses.length,
    winRate: list.length ? (wins.length / list.length) * 100 : 0, grossProfit, grossLoss, net,
    pf: grossLoss > 1e-9 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0,
    expectancy: list.length ? net / list.length : 0, avgWin: wins.length ? grossProfit / wins.length : 0, avgLoss: losses.length ? grossLoss / losses.length : 0,
    best: pnls.length ? Math.max(...pnls) : 0, worst: pnls.length ? Math.min(...pnls) : 0, maxWins, maxLosses,
    avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
    avgHold: holds.length ? holds.reduce((a, b) => a + b, 0) / holds.length : null,
    medianHold: holds.length ? (holds.length % 2 ? holds[mid] : (holds[mid - 1] + holds[mid]) / 2) : null,
  };
}

function drawdown(points) {
  let peak = null; let maxAmount = 0; let maxPct = 0; let currentPct = 0;
  for (const p of points) {
    if (p.equity === null || p.equity === undefined) continue;
    peak = peak === null ? p.equity : Math.max(peak, p.equity);
    const amount = peak - p.equity;
    const pct = peak ? (amount / peak) * 100 : 0;
    maxAmount = Math.max(maxAmount, amount);
    maxPct = Math.max(maxPct, pct);
    currentPct = pct;
  }
  return { maxAmount, maxPct, currentPct };
}

function bucket(list) {
  const s = stats(list);
  return { trades: s.total, pnl: s.net, winRate: s.winRate };
}

function groupBy(list, keyFn) {
  const groups = new Map();
  for (const t of list) {
    const key = keyFn(t);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  return [...groups].map(([key, items]) => {
    const s = stats(items);
    return { key, trades: s.total, winRate: s.winRate, pnl: s.net, pf: s.pf, avgR: s.avgR };
  }).sort((a, b) => b.pnl - a.pnl);
}

function breakdownTable(label) {
  return new DataTable({
    rowKey: (r) => r.key, empty: "No trades", sortBy: "pnl",
    columns: [
      { key: "key", label, render: (r) => h("span", { class: "strong small" }, r.key) },
      { key: "trades", label: "Trades", align: "right", render: (r) => h("span", { class: "num" }, String(r.trades)) },
      { key: "winRate", label: "Win %", align: "right", render: (r) => h("span", { class: "num" }, `${fmtNum(r.winRate, 0)}%`) },
      { key: "pf", label: "PF", align: "right", render: (r) => h("span", { class: "num" }, r.pf === Infinity ? "∞" : fmtNum(r.pf, 2)) },
      { key: "pnl", label: "P&L", align: "right", render: (r) => h("span", { class: `num strong ${tone(r.pnl)}` }, fmtMoney(r.pnl, { sign: true, compact: true })) },
    ],
  });
}

function stat(label, value, cls = "") {
  return h("div", { class: "stat" }, h("span", { class: "stat-label" }, label), h("span", { class: `stat-value ${cls}` }, value));
}

function lsBox(label, s, cur, toneName) {
  return h("div", { class: "ls-box" }, h("div", { class: "row-between" }, h("span", { class: `strong ${toneName}` }, label), h("span", { class: "small dim" }, `${s.total} trades`)),
    h("div", { class: `num strong ${tone(s.net)}` }, fmtMoney(s.net, { currency: cur, sign: true })),
    h("div", { class: "small muted" }, `${fmtNum(s.winRate, 1)}% win · PF ${s.pf === Infinity ? "∞" : fmtNum(s.pf, 2)}`));
}
