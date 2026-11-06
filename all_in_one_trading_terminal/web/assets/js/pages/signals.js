// Signals: every SMC / ICT setup the engine detected, with grade, score, levels, status and confluences.

import { api } from "../api.js";
import { router } from "../actions.js";
import { dayKey, fmtDateTime, fmtNum, fmtPrice } from "../format.js";
import { digitsOf, store } from "../store.js";
import { DataTable, card, dirBadge, downloadFile, gradeBadge, h, icon, kpi, progress, searchInput, segmented, selectInput, statusBadge, toCSV } from "../ui.js";

const STATUS_FILTERS = ["ALL", "EXECUTED", "BROADCAST", "SIGNAL", "SKIPPED", "REJECTED", "EXPIRED"];

export async function mount(root, { scope }) {
  let items = [];
  const filters = { q: "", symbol: "ALL", status: "ALL", grade: "ALL", side: "ALL" };
  const k = {
    today: kpi({ label: "Setups today", iconName: "signal", toneName: "brand" }),
    aplus: kpi({ label: "A+ setups today", iconName: "zap", toneName: "warn" }),
    executed: kpi({ label: "Executed", iconName: "checkCircle", toneName: "up" }),
    filtered: kpi({ label: "Filtered out", iconName: "filter", toneName: "violet" }),
  };
  const symbols = ["ALL", ...(store.bootstrap?.symbols || [])];
  const search = searchInput({ placeholder: "Search symbol, strategy, reason…", onInput: (v) => { filters.q = v.toLowerCase(); render(); } });
  const symbolSel = selectInput(symbols.map((s) => ({ value: s, label: s === "ALL" ? "All symbols" : s })), "ALL", (v) => { filters.symbol = v; render(); });
  const statusSel = selectInput(STATUS_FILTERS.map((s) => ({ value: s, label: s === "ALL" ? "Any status" : s.charAt(0) + s.slice(1).toLowerCase() })), "ALL", (v) => { filters.status = v; render(); });
  const gradeSeg = segmented([{ value: "ALL", label: "All" }, { value: "A+", label: "A+" }, { value: "A", label: "A" }, { value: "B", label: "B" }], "ALL", (v) => { filters.grade = v; render(); }, { sm: true, label: "Grade" });
  const sideSeg = segmented([{ value: "ALL", label: "Both" }, { value: "BUY", label: "Buy" }, { value: "SELL", label: "Sell" }], "ALL", (v) => { filters.side = v; render(); }, { sm: true, label: "Direction" });
  symbolSel.style.width = "150px";
  statusSel.style.width = "150px";
  const exportBtn = h("button", { class: "btn btn-sm", onClick: exportCsv }, icon("download", 14), "Export CSV");
  const count = h("span", { class: "small dim" });

  const table = new DataTable({
    rowKey: (r) => r.signal_id || r.id,
    empty: "No signals match these filters.",
    sortBy: "created_at",
    onRowClick: (r) => table.toggleDetail(r),
    detail: (r) => detail(r),
    columns: [
      { key: "created_at", label: "Detected", render: (r) => h("span", { class: "small nowrap" }, fmtDateTime(r.created_at)) },
      { key: "symbol", label: "Symbol", render: (r) => h("span", { class: "strong" }, r.symbol) },
      { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
      { key: "strategy", label: "Setup", render: (r) => h("div", null, h("div", { class: "small strong" }, r.strategy), h("div", { class: "tiny dim" }, `${r.timeframe || ""} · ${r.order_type || ""}`)) },
      { key: "grade", label: "Grade", render: (r) => gradeBadge(r.grade) },
      { key: "score", label: "Score", render: (r) => h("div", { class: "score" }, progress(Math.min(100, (r.score || 0) * 10), r.score >= 8 ? "up" : r.score >= 6.5 ? "" : "warn"), h("span", { class: "score-val" }, fmtNum(r.score, 1))) },
      { key: "entry", label: "Entry", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.entry, digitsOf(r.symbol))) },
      { key: "stop_loss", label: "Stop", align: "right", render: (r) => h("span", { class: "num down" }, fmtPrice(r.stop_loss, digitsOf(r.symbol))) },
      { key: "tp1", label: "TP1", align: "right", render: (r) => h("span", { class: "num up" }, fmtPrice(r.tp1, digitsOf(r.symbol))) },
      { key: "tp2", label: "TP2", align: "right", render: (r) => h("span", { class: "num up" }, fmtPrice(r.tp2, digitsOf(r.symbol))) },
      { key: "rr", label: "R:R", align: "right", render: (r) => h("span", { class: "num" }, r.rr ? `1:${fmtNum(r.rr, 1)}` : "—") },
      { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
      { key: "reason", label: "Decision", sort: false, render: (r) => h("span", { class: "small muted ellipsis", style: { maxWidth: "240px", display: "inline-block" }, title: r.reason || "" }, r.reason || "—") },
      { key: "go", label: "", sort: false, align: "right", sig: (r) => `g${r.symbol}`, render: (r) => h("button", { class: "btn btn-xs btn-ghost", title: "Open chart", onClick: () => router.navigate(`/trade?symbol=${r.symbol}`) }, icon("candles", 13), "Chart") },
    ],
  });

  root.append(h("div", { class: "stack" },
    h("div", { class: "kpis k4" }, Object.values(k)),
    card({ title: "Signal feed", iconName: "signal", sub: "Click a row to see the confluence breakdown", actions: [count, exportBtn],
      body: [h("div", { class: "toolbar", style: { padding: "0 var(--pad) 12px" } }, h("div", { style: { flex: "1 1 240px" } }, search), symbolSel, statusSel, gradeSeg, sideSeg), table.el], flush: true })));

  function matches(r) {
    if (filters.symbol !== "ALL" && r.symbol !== filters.symbol) return false;
    if (filters.status !== "ALL" && String(r.status).toUpperCase() !== filters.status) return false;
    if (filters.grade !== "ALL" && String(r.grade).toUpperCase() !== filters.grade) return false;
    if (filters.side !== "ALL" && r.direction !== filters.side) return false;
    if (filters.q && !`${r.symbol} ${r.strategy} ${r.setup} ${r.reason} ${r.status}`.toLowerCase().includes(filters.q)) return false;
    return true;
  }

  function render() {
    const rows = items.filter(matches);
    table.update(rows);
    count.textContent = `${rows.length} of ${items.length}`;
    const today = dayKey(new Date());
    const todays = items.filter((r) => dayKey(r.created_at) === today);
    const executed = items.filter((r) => r.status === "EXECUTED");
    const filtered = items.filter((r) => ["SKIPPED", "REJECTED", "EXPIRED"].includes(String(r.status).toUpperCase()));
    k.today.set({ value: String(todays.length), sub: `${items.length} in the last ${items.length >= 500 ? "500 signals" : "history"}` });
    k.aplus.set({ value: String(todays.filter((r) => r.grade === "A+").length), sub: "Highest-confluence setups" });
    k.executed.set({ value: String(executed.length), sub: items.length ? `${fmtNum((executed.length / items.length) * 100, 0)}% of detected setups` : "—" });
    const reasons = {};
    for (const r of filtered) { const key = String(r.reason || "other").split(":")[0].slice(0, 40); reasons[key] = (reasons[key] || 0) + 1; }
    const top = Object.entries(reasons).sort((a, b) => b[1] - a[1])[0];
    k.filtered.set({ value: String(filtered.length), sub: top ? `Top reason: ${top[0]} (${top[1]})` : "Risk & news gates" });
  }

  function detail(r) {
    const conf = Array.isArray(r.confluences) ? r.confluences : [];
    return h("div", { class: "row-wrap gap-16", style: { alignItems: "flex-start" } },
      h("div", { style: { minWidth: "260px", flex: "1" } }, h("div", { class: "tiny dim upper mb-4" }, "Confluences"),
        conf.length ? h("div", { class: "confluences mt-8" }, conf.map((c) => h("span", { class: "confluence" }, c))) : h("div", { class: "small dim" }, "None recorded")),
      h("div", { style: { minWidth: "220px" } }, h("div", { class: "tiny dim upper" }, "Validity"),
        h("div", { class: "small mt-8" }, `Anchor ${fmtDateTime(r.anchor_time)}`), h("div", { class: "small" }, `Valid until ${fmtDateTime(r.valid_until)}`)),
      h("div", { style: { minWidth: "220px" } }, h("div", { class: "tiny dim upper" }, "Engine decision"), h("div", { class: "small mt-8" }, r.reason || "—")));
  }

  function exportCsv() {
    const rows = items.filter(matches);
    const csv = toCSV(rows, [
      { label: "detected", key: "created_at" }, { label: "symbol", key: "symbol" }, { label: "direction", key: "direction" }, { label: "strategy", key: "strategy" },
      { label: "timeframe", key: "timeframe" }, { label: "order_type", key: "order_type" }, { label: "grade", key: "grade" }, { label: "score", key: "score" },
      { label: "entry", key: "entry" }, { label: "stop_loss", key: "stop_loss" }, { label: "tp1", key: "tp1" }, { label: "tp2", key: "tp2" }, { label: "rr", key: "rr" },
      { label: "status", key: "status" }, { label: "reason", key: "reason" }, { label: "confluences", value: (r) => (r.confluences || []).join(" | ") },
    ]);
    downloadFile(`signals-${dayKey(new Date())}.csv`, csv, "text/csv");
  }

  async function load() {
    const res = await api.get("/api/signals?limit=500", { signal: scope.signal });
    items = res.items || [];
    render();
  }
  scope.every(10000, load);
}
