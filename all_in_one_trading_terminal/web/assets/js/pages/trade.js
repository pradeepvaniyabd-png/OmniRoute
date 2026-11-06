// Trade Terminal: watchlist, SMC price chart, order ticket (Market / Limit / Stop) and position manager.

import { api, qs } from "../api.js";
import { PriceChart, chartsAvailable } from "../chart.js";
import { fmtDateTime, fmtLots, fmtMoney, fmtNum, fmtPct, fmtPrice, fmtRelative, tone } from "../format.js";
import { live, onChart } from "../live.js";
import { chartPrice, digitsOf, quote, setPref, store } from "../store.js";
import {
  DataTable, badge, confirmDialog, debounce, dirBadge, field, gradeBadge, h, icon, numberInput, openModal, popover, replace,
  segmented, setText, statusBadge, symbolDescription, symbolIcon, tabs, toast, toastError, toggleSwitch,
} from "../ui.js";
import { biasBadge, orderColumns, positionColumns } from "./common.js";

const OVERLAYS = [
  { key: "ema", label: "EMA trend line" }, { key: "volume", label: "Volume" }, { key: "fvg", label: "Fair value gaps" },
  { key: "ob", label: "Order blocks" }, { key: "sessions", label: "Session ranges" }, { key: "markers", label: "Sweeps & structure (BOS/MSS)" },
  { key: "signals", label: "Signals & projected trades" }, { key: "levels", label: "Position / order levels" },
];

let instance = null;

export function update(params) {
  instance?.fromParams(params);
}

export function unmount() {
  instance = null;
}

export async function mount(root, { scope, params }) {
  const boot = store.bootstrap || {};
  const symbols = boot.symbols || [];
  const timeframes = boot.chart_timeframes || ["M5", "M15", "H1"];
  const bars = boot.chart?.bars || 300;
  const prefs = store.prefs;
  const pick = (s) => (s && symbols.includes(String(s).toUpperCase()) ? String(s).toUpperCase() : null);
  const pickTf = (t) => (t && timeframes.includes(String(t).toUpperCase()) ? String(t).toUpperCase() : null);
  let symbol = pick(params.get("symbol")) || pick(prefs.chart.symbol) || symbols[0];
  let tf = pickTf(params.get("tf")) || pickTf(prefs.chart.tf) || boot.timeframes?.ltf || timeframes[0];

  // ------------------------------------------------------------ watchlist --
  const wlItems = new Map();
  const wlBody = h("div", { class: "card-body" });
  const wlFilter = h("input", { class: "input", type: "search", placeholder: "Filter symbols", spellcheck: "false" });
  wlFilter.addEventListener("input", () => {
    const q = wlFilter.value.trim().toUpperCase();
    for (const [s, item] of wlItems) item.el.hidden = Boolean(q) && !s.includes(q);
  });
  for (const s of symbols) {
    const price = h("div", { class: "wl-price" }, "—");
    const chg = h("div", { class: "wl-chg" }, "");
    const meta = h("div", { class: "wl-meta" }, symbolDescription(s));
    const lock = h("span", { class: "warn", hidden: true, title: "News lock" }, icon("lock", 12));
    const el = h("div", { class: "wl-item", role: "button", tabindex: "0", onClick: () => setSymbol(s), onKeydown: (e) => { if (e.key === "Enter") setSymbol(s); } },
      h("div", { class: "wl-sym" }, s, lock), price, meta, chg);
    wlItems.set(s, { el, price, chg, meta, lock });
    wlBody.append(el);
  }
  const wlCard = h("section", { class: "card wl" },
    h("div", { class: "card-head" }, h("h3", { class: "card-title" }, icon("globe", 16), "Watchlist"), badge(String(symbols.length), "", { sm: true })),
    h("div", { class: "wl-search" }, wlFilter), wlBody);

  // ---------------------------------------------------------------- chart --
  const headIcon = h("span");
  const symTitle = h("b");
  const symDesc = h("span", { class: "small dim" });
  const priceBig = h("span", { class: "num" }, "—");
  const priceChg = h("span", { class: "num small" });
  const tfSeg = segmented(timeframes.map((t) => ({ value: t, label: t })), tf, (v) => setTf(v), { sm: true, label: "Timeframe" });
  const indicatorsBtn = h("button", { class: "btn btn-sm", onClick: () => openIndicators() }, icon("layers", 14), "Indicators");
  const fitBtn = h("button", { class: "btn btn-sm btn-icon", title: "Reset view", "aria-label": "Reset view", onClick: () => chart?.fit() }, icon("fit", 15));
  const fsBtn = h("button", { class: "btn btn-sm btn-icon", title: "Fullscreen", "aria-label": "Fullscreen", onClick: () => toggleFullscreen() }, icon("maximize", 15));
  const legend = h("div", { class: "chart-legend" });
  const hint = h("div", { class: "chart-hint", hidden: true }, "Click the chart to set the order price");
  const msg = h("div", { class: "chart-msg" }, "Loading chart…");
  const chartBox = h("div", { class: "chart-box" }, legend, hint, msg);
  const strip = h("div", { class: "analysis-strip" });
  const chartPanel = h("section", { class: "card chart-panel" },
    h("div", { class: "chart-head" },
      h("div", { class: "row gap-12" }, headIcon, h("div", { class: "chart-symbol" }, symTitle, symDesc)),
      h("div", { class: "chart-price" }, priceBig, priceChg),
      h("div", { class: "chart-tools" }, tfSeg, indicatorsBtn, fitBtn, fsBtn)),
    chartBox, strip);

  // --------------------------------------------------------------- ticket --
  const runtime = store.snapshot?.runtime || {};
  const t = {
    side: "BUY", kind: "MARKET", entry: null,
    risk: Number(prefs.ticket.risk ?? runtime.risk_percent ?? boot.risk?.risk_percent ?? 0.5),
    slMode: ["atr", "pips", "structure"].includes(prefs.ticket.slMode) ? prefs.ticket.slMode : "atr",
    slAtr: Number(prefs.ticket.slValue ?? 1.5), slPips: Number(prefs.ticket.slPips ?? 50),
    tp1: Number(prefs.ticket.tp1 ?? boot.risk?.tp1_rr ?? 1.5), tp2: Number(prefs.ticket.tp2 ?? boot.risk?.tp2_rr ?? 3),
    override: false,
  };
  let preview = null;
  let previewToken = 0;
  const maxRisk = Number(boot.risk?.max_risk_percent || 5);

  const sellPrice = h("span", { class: "side-price" }, "—");
  const buyPrice = h("span", { class: "side-price" }, "—");
  const spreadVal = h("span", { class: "num" }, "—");
  const sellBtn = h("button", { class: "side-btn sell", onClick: () => setSide("SELL") }, h("span", { class: "side-label" }, "SELL"), sellPrice);
  const buyBtn = h("button", { class: "side-btn buy", onClick: () => setSide("BUY") }, h("span", { class: "side-label" }, "BUY"), buyPrice);
  const kindSeg = segmented([{ value: "MARKET", label: "Market" }, { value: "LIMIT", label: "Limit" }, { value: "STOP", label: "Stop" }], t.kind,
    (v) => { t.kind = v; t.entry = v === "MARKET" ? null : t.entry; syncTicket(); requestPreview(); }, { block: true, label: "Order type" });
  const entryInput = numberInput({ placeholder: "Order price", onInput: (v) => { t.entry = v; requestPreview(); } });
  const useMarket = h("button", { class: "btn btn-xs btn-ghost", type: "button", onClick: () => {
    const q = quote(symbol);
    const p = t.side === "BUY" ? q?.ask : q?.bid;
    if (p) { t.entry = p; entryInput.value = String(p); requestPreview(); }
  } }, "Use market");
  const entryField = field("Order price", entryInput, { extra: useMarket, hint: "Tip: click on the chart to set the price" });

  const riskValue = h("span", { class: "num small" }, "");
  const riskRange = h("input", { class: "range", type: "range", min: "0.1", max: String(maxRisk), step: "0.05", value: String(t.risk) });
  const riskInput = numberInput({ value: t.risk, step: "0.05", min: "0.05", max: String(maxRisk), suffix: "%" });
  const quick = h("div", { class: "quick-risk" }, [0.25, 0.5, 1, 2].filter((v) => v <= maxRisk).map((v) => h("button", { type: "button", onClick: () => setRisk(v) }, `${v}%`)));
  riskRange.addEventListener("input", () => setRisk(Number(riskRange.value)));
  riskInput.input.addEventListener("input", () => { const v = Number(riskInput.input.value); if (v > 0) setRisk(v, false); });

  const slSeg = segmented([{ value: "atr", label: "ATR ×" }, { value: "pips", label: "Pips" }, { value: "structure", label: "Structure" }], t.slMode,
    (v) => { t.slMode = v; setPref("ticket.slMode", v); syncTicket(); requestPreview(); }, { block: true, sm: true, label: "Stop-loss mode" });
  const slInput = numberInput({ step: "0.1", min: "0" });
  slInput.addEventListener("input", () => {
    const v = Number(slInput.value);
    if (!(v > 0)) return;
    if (t.slMode === "atr") { t.slAtr = v; setPref("ticket.slValue", v); } else { t.slPips = v; setPref("ticket.slPips", v); }
    requestPreview();
  });
  const slHint = h("div", { class: "field-hint" });
  const tp1Input = numberInput({ value: t.tp1, step: "0.1", min: "0.1", suffix: "R" });
  const tp2Input = numberInput({ value: t.tp2, step: "0.1", min: "0.1", suffix: "R" });
  tp1Input.input.addEventListener("input", () => { const v = Number(tp1Input.input.value); if (v > 0) { t.tp1 = v; setPref("ticket.tp1", v); requestPreview(); } });
  tp2Input.input.addEventListener("input", () => { const v = Number(tp2Input.input.value); if (v > 0) { t.tp2 = v; setPref("ticket.tp2", v); requestPreview(); } });

  const previewBox = h("div", { class: "preview" });
  const newsBox = h("div", { hidden: true });
  const submitBtn = h("button", { class: "btn btn-lg btn-block btn-buy", onClick: () => place() }, "Buy");
  const ticketFoot = h("div", { class: "tiny dim" });
  const ticketSymbol = badge(symbol, "brand", { sm: true });
  const ticket = h("section", { class: "card ticket" },
    h("div", { class: "card-head bordered" }, h("h3", { class: "card-title" }, icon("target", 16), "Order ticket"), ticketSymbol),
    h("div", { class: "card-body" },
      h("div", { class: "ticket-sides" }, sellBtn, h("div", { class: "spread-box" }, "spread", spreadVal), buyBtn),
      kindSeg, entryField,
      h("div", { class: "ticket-section" }, h("div", { class: "ticket-section-title" }, h("span", null, "Risk per trade"), riskValue), riskRange, h("div", { class: "row" }, h("div", { class: "grow" }, riskInput), quick)),
      h("div", { class: "ticket-section" }, h("div", { class: "ticket-section-title" }, h("span", null, "Stop loss")), slSeg, slInput, slHint),
      h("div", { class: "ticket-section" }, h("div", { class: "ticket-section-title" }, h("span", null, "Take profit (reward : risk)")),
        h("div", { class: "fields" }, field("TP1 (partial)", tp1Input), field("TP2 (final)", tp2Input))),
      previewBox, newsBox, submitBtn, ticketFoot));

  // ---------------------------------------------------------- bottom panel --
  let activeTab = "positions";
  const tabsEl = tabs([{ id: "positions", label: "Positions", icon: "layers", count: 0 }, { id: "orders", label: "Orders", icon: "clock", count: 0 },
    { id: "history", label: "History", icon: "book" }, { id: "signals", label: "Signals", icon: "signal" }], activeTab, (id) => setTab(id));
  const posTable = new DataTable({ rowKey: (r) => r.ticket, columns: positionColumns({ onEdit: (p) => editPosition(p) }), empty: "No open positions", onRowClick: (r) => setSymbol(r.canonical || r.symbol) });
  const ordTable = new DataTable({ rowKey: (r) => r.ticket, columns: orderColumns(), empty: "No pending orders", onRowClick: (r) => setSymbol(r.canonical || r.symbol) });
  const histTable = new DataTable({
    rowKey: (r) => r.id, empty: "No closed trades yet", sortBy: "closed_at",
    columns: [
      { key: "closed_at", label: "Closed", render: (r) => h("span", { class: "dim small nowrap" }, fmtDateTime(r.closed_at)) },
      { key: "symbol", label: "Symbol", render: (r) => h("span", { class: "strong" }, r.symbol) },
      { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
      { key: "volume", label: "Lots", align: "right", render: (r) => h("span", { class: "num" }, fmtLots(r.volume)) },
      { key: "fill_price", label: "Entry", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.fill_price ?? r.entry_price, digitsOf(r.symbol))) },
      { key: "exit_price", label: "Exit", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.exit_price, digitsOf(r.symbol))) },
      { key: "pnl", label: "P&L", align: "right", render: (r) => h("span", { class: `num strong ${tone(r.pnl)}` }, fmtMoney(r.pnl, { sign: true })) },
      { key: "pips", label: "Pips", align: "right", render: (r) => h("span", { class: `num ${tone(r.pips)}` }, fmtNum(r.pips, 1, { sign: true })) },
      { key: "r_multiple", label: "R", align: "right", render: (r) => h("span", { class: `num ${tone(r.r_multiple)}` }, r.r_multiple === null ? "—" : `${fmtNum(r.r_multiple, 2, { sign: true })}R`) },
      { key: "close_reason", label: "Reason", render: (r) => statusBadge(r.close_reason) },
      { key: "strategy", label: "Strategy", render: (r) => h("span", { class: "small muted" }, r.strategy) },
    ],
  });
  const sigTable = new DataTable({
    rowKey: (r) => r.signal_id || r.id, empty: "No signals for this symbol yet",
    columns: [
      { key: "created_at", label: "Time", render: (r) => h("span", { class: "dim small nowrap" }, fmtDateTime(r.created_at)) },
      { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
      { key: "strategy", label: "Setup", render: (r) => h("span", { class: "small" }, r.strategy) },
      { key: "grade", label: "Grade", render: (r) => gradeBadge(r.grade) },
      { key: "score", label: "Score", align: "right", render: (r) => h("span", { class: "num" }, fmtNum(r.score, 1)) },
      { key: "entry", label: "Entry", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.entry, digitsOf(r.symbol))) },
      { key: "stop_loss", label: "SL", align: "right", render: (r) => h("span", { class: "num down" }, fmtPrice(r.stop_loss, digitsOf(r.symbol))) },
      { key: "tp2", label: "TP", align: "right", render: (r) => h("span", { class: "num up" }, fmtPrice(r.tp2, digitsOf(r.symbol))) },
      { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
      { key: "reason", label: "Note", sort: false, render: (r) => h("span", { class: "small dim ellipsis", style: { maxWidth: "260px", display: "inline-block" }, title: r.reason || "" }, r.reason || "—") },
    ],
  });
  const panes = { positions: posTable.el, orders: ordTable.el, history: histTable.el, signals: sigTable.el };
  const paneBox = h("div");
  const bottom = h("section", { class: "card bottom-panel" }, tabsEl, h("div", { class: "card-body" }, paneBox));

  const acctBody = h("div", { class: "acct-mini" });
  const acctCard = h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h3", { class: "card-title" }, icon("wallet", 16), "Account"), h("span", { class: "acct-mode" })),
    h("div", { class: "card-body tight" }, acctBody));
  root.append(h("div", { class: "terminal" }, h("div", { class: "wl-col" }, wlCard, acctCard), chartPanel, ticket, bottom));

  // ---------------------------------------------------------- chart setup --
  let chart = null;
  if (chartsAvailable()) {
    try {
      chart = new PriceChart(chartBox, {
        onCrosshair: (info) => renderLegend(info),
        onClick: (price) => {
          if (t.kind === "MARKET") return;
          const d = digitsOf(symbol);
          t.entry = Number(price.toFixed(d));
          entryInput.value = String(t.entry);
          requestPreview();
        },
      });
      chart.setOverlays(prefs.chart.overlays || {});
      scope.add(() => chart?.destroy());
    } catch (err) {
      setText(msg, `Chart unavailable: ${err.message}`);
    }
  } else {
    setText(msg, "Chart library failed to load — check that /vendor files are served.");
  }

  scope.add(onChart("chart", (payload) => {
    if (!chart || payload.symbol !== symbol || payload.timeframe !== tf) return;
    applyPayload(payload);
  }));
  scope.add(onChart("chart_tick", (data) => chart?.tick(data)));
  scope.add(() => live.unsubscribeChart());

  function applyPayload(payload) {
    if (!payload.candles?.length) {
      msg.hidden = false;
      setText(msg, `No ${payload.timeframe} candles for ${payload.symbol} yet — the feed may still be loading.`);
      return;
    }
    msg.hidden = true;
    chart.setData(payload);
    renderLegend(chart.legendAt(null));
  }

  async function loadChart() {
    if (!chart) return;
    msg.hidden = false;
    setText(msg, "Loading chart…");
    const wanted = `${symbol}|${tf}`;
    try {
      const payload = await api.get(`/api/chart${qs({ symbol, tf, bars })}`, { signal: scope.signal, timeout: 30000 });
      if (`${payload.symbol}|${payload.timeframe}` === wanted) applyPayload(payload);
    } catch (err) {
      if (err.name !== "AbortError") setText(msg, err.message || "Chart data unavailable");
    }
  }

  function renderLegend(info) {
    if (!info) { replace(legend, h("span", null, `${symbol} · ${tf}`)); return; }
    replace(legend,
      h("span", { class: "strong" }, `${symbol} · ${tf}`),
      h("span", null, h("b", null, "O"), info.open), h("span", null, h("b", null, "H"), info.high), h("span", null, h("b", null, "L"), info.low),
      h("span", { class: info.up ? "up" : "down" }, h("b", null, "C"), info.close, ` (${info.change >= 0 ? "+" : ""}${info.change.toFixed(2)}%)`),
      info.ema ? h("span", null, h("b", null, "EMA"), info.ema) : null,
      info.volume ? h("span", null, h("b", null, "Vol"), info.volume) : null);
  }

  function openIndicators() {
    const overlays = { ...(store.prefs.chart.overlays || {}) };
    const rows = OVERLAYS.map((o) => h("div", { class: "row-between", style: { padding: "8px 14px" } }, h("span", { class: "small" }, o.label),
      toggleSwitch({ checked: overlays[o.key] !== false, onChange: (v) => {
        overlays[o.key] = v;
        setPref("chart.overlays", { ...overlays });
        chart?.setOverlays({ [o.key]: v });
      } })));
    popover(indicatorsBtn, h("div", { style: { minWidth: "270px", padding: "6px 0" } },
      h("div", { class: "popover-head" }, h("span", { class: "popover-title" }, "Chart overlays"), h("span", { class: "tiny dim" }, "SMC / ICT")), rows), { align: "right" });
  }

  function toggleFullscreen() {
    const on = !chartPanel.classList.contains("fullscreen");
    chartPanel.classList.toggle("fullscreen", on);
    replace(fsBtn, icon(on ? "minimize" : "maximize", 15));
  }
  scope.listen(document, "keydown", (e) => {
    if (e.key === "Escape" && chartPanel.classList.contains("fullscreen")) toggleFullscreen();
    const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
    if (typing || e.ctrlKey || e.metaKey || e.altKey || document.querySelector(".modal-backdrop")) return;
    if (e.key === "b" || e.key === "B") setSide("BUY");
    if (e.key === "s" || e.key === "S") setSide("SELL");
  });

  // ------------------------------------------------------- symbol / tf --
  function syncUrl() {
    history.replaceState({}, "", `/trade${qs({ symbol, tf })}`);
  }

  function renderHeader() {
    replace(headIcon, symbolIcon(symbol));
    setText(symTitle, symbol);
    setText(symDesc, `${symbolDescription(symbol)} · ${tf}`);
    setText(ticketSymbol, symbol);
    for (const [s, item] of wlItems) item.el.classList.toggle("active", s === symbol);
    document.title = `${symbol} ${fmtPrice(chartPrice(symbol), digitsOf(symbol))} · Trade Terminal`;
  }

  function setSymbol(next) {
    next = pick(next);
    if (!next || next === symbol) return;
    symbol = next;
    setPref("chart.symbol", symbol);
    t.entry = null;
    entryInput.value = "";
    syncUrl();
    renderHeader();
    chart?.resetKey();
    loadChart();
    live.subscribeChart(symbol, tf, bars);
    renderQuotes(store.snapshot);
    renderStrip(store.snapshot);
    requestPreview();
    loadSignals();
  }

  function setTf(next) {
    next = pickTf(next);
    if (!next || next === tf) return;
    tf = next;
    tfSeg.setValue(tf);
    setPref("chart.tf", tf);
    syncUrl();
    renderHeader();
    chart?.resetKey();
    loadChart();
    live.subscribeChart(symbol, tf, bars);
  }

  instance = {
    fromParams(p) {
      const s = pick(p.get("symbol"));
      const f = pickTf(p.get("tf"));
      if (f && f !== tf) setTf(f);
      if (s && s !== symbol) setSymbol(s);
    },
  };

  // ---------------------------------------------------------------- ticket --
  function setSide(side) {
    t.side = side;
    syncTicket();
    requestPreview();
  }

  function setRisk(v, syncInput = true) {
    const value = Math.max(0.05, Math.min(maxRisk, Number(v) || 0.05));
    t.risk = Number(value.toFixed(2));
    setPref("ticket.risk", t.risk);
    riskRange.value = String(t.risk);
    riskRange.style.setProperty("--fill", `${((t.risk - 0.1) / (maxRisk - 0.1)) * 100}%`);
    if (syncInput) riskInput.input.value = String(t.risk);
    quick.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.textContent === `${t.risk}%`));
    renderRiskValue();
    requestPreview();
  }

  function renderRiskValue() {
    const equity = store.snapshot?.account?.equity;
    setText(riskValue, equity ? `≈ ${fmtMoney((equity * t.risk) / 100)} of ${fmtMoney(equity, { compact: true })}` : `${t.risk}%`);
  }

  function syncTicket() {
    sellBtn.classList.toggle("active", t.side === "SELL");
    buyBtn.classList.toggle("active", t.side === "BUY");
    kindSeg.setValue(t.kind);
    entryField.hidden = t.kind === "MARKET";
    hint.hidden = t.kind === "MARKET";
    slSeg.setValue(t.slMode);
    slInput.hidden = t.slMode === "structure";
    slInput.value = String(t.slMode === "atr" ? t.slAtr : t.slPips);
    slInput.step = t.slMode === "atr" ? "0.1" : "1";
    setText(slHint, t.slMode === "atr" ? "Stop distance = ATR × multiple (volatility-adjusted)"
      : t.slMode === "pips" ? "Fixed stop distance in pips" : "Stop beyond the latest confirmed swing (SMC structure)");
    renderSubmit();
  }

  function body() {
    return { symbol, direction: t.side, order_kind: t.kind, entry_price: t.kind === "MARKET" ? null : t.entry, risk_percent: t.risk,
      sl_mode: t.slMode, sl_value: t.slMode === "atr" ? t.slAtr : t.slMode === "pips" ? t.slPips : 0, tp1_rr: t.tp1, tp2_rr: t.tp2 };
  }

  const requestPreview = debounce(() => runPreview(), 220);
  async function runPreview() {
    const token = ++previewToken;
    if (t.kind !== "MARKET" && !(t.entry > 0)) {
      preview = { ok: false, error: `Enter the ${t.kind.toLowerCase()} price (or click on the chart)` };
      renderPreview();
      return;
    }
    try {
      const res = await api.post("/api/trade/preview", body(), { signal: scope.signal });
      if (token !== previewToken) return;
      preview = res;
    } catch (err) {
      if (err.name === "AbortError") return;
      if (token !== previewToken) return;
      preview = { ok: false, error: err.message };
    }
    renderPreview();
  }

  function renderPreview() {
    const p = preview;
    const d = p?.digits ?? digitsOf(symbol);
    if (!p) { replace(previewBox, h("div", { class: "preview-row" }, h("span", null, "Calculating…"))); renderSubmit(); return; }
    const row = (label, value, cls = "") => h("div", { class: "preview-row" }, h("span", null, label), h("span", { class: `num ${cls}` }, value));
    if (!p.ok && !p.lots) {
      replace(previewBox, h("div", { class: "preview-row" }, h("span", { class: "preview-error" }, icon("alert", 15), p.error || "Order cannot be sized")));
    } else {
      replace(previewBox,
        h("div", { class: "preview-row" }, h("span", null, "Position size"), h("span", { class: "preview-lots" }, `${fmtLots(p.lots)} lots`)),
        row("Risk", `${fmtMoney(p.risk_amount)} · ${fmtNum(p.risk_percent, 2)}%`, "down"),
        row("Entry", `${fmtPrice(p.entry, d)}${t.kind === "MARKET" ? " (mkt)" : ""}`),
        row("Stop loss", `${fmtPrice(p.sl, d)} · ${fmtNum(p.sl_pips, 1)} pips`, "down"),
        row(`TP1 · ${fmtNum(p.tp1_rr, 1)}R`, fmtPrice(p.tp1, d), "up"),
        row(`TP2 · ${fmtNum(p.tp2_rr, 1)}R`, fmtPrice(p.tp2, d), "up"),
        row("Spread", `${fmtNum(p.spread_points, 1)} pts`),
        !p.ok && p.error ? h("div", { class: "preview-row" }, h("span", { class: "preview-error" }, icon("alert", 15), p.error)) : null);
    }
    if (p.news_lock) {
      newsBox.hidden = false;
      replace(newsBox, h("div", { class: "info-box warn" }, icon("lock", 16), h("div", { class: "stack", style: { gap: "8px" } },
        h("div", null, h("b", null, "News kill-switch active. "), p.news_label || ""),
        h("label", { class: "checkbox" }, h("input", { type: "checkbox", checked: t.override, onChange: (e) => { t.override = e.target.checked; renderSubmit(); } }), "Override and trade anyway"))));
    } else {
      newsBox.hidden = true;
      t.override = false;
    }
    renderSubmit();
  }

  function renderSubmit() {
    const p = preview;
    const buy = t.side === "BUY";
    const halted = store.snapshot?.drawdown?.halted;
    const blocked = !p?.ok || (p.news_lock && !t.override) || halted;
    submitBtn.className = `btn btn-lg btn-block ${buy ? "btn-buy" : "btn-sell"}`;
    submitBtn.disabled = Boolean(blocked);
    const kindLabel = t.kind === "MARKET" ? "at market" : `${t.kind.toLowerCase()} @ ${fmtPrice(t.entry, digitsOf(symbol))}`;
    replace(submitBtn, icon(buy ? "trendUp" : "trendDown", 17), halted ? "Trading halted (daily loss limit)" : `${buy ? "Buy" : "Sell"} ${p?.lots ? `${fmtLots(p.lots)} ` : ""}${symbol} ${kindLabel}`);
    const r = boot.risk || {};
    setText(ticketFoot, `Sent to ${store.snapshot?.mode === "MT5" ? "MetaTrader 5 (LIVE)" : "the paper account"}. Auto-protection: TP1 partial, breakeven at ${fmtNum(r.breakeven_trigger_r, 1)}R, ${r.trailing_mode || "ATR"} trailing from ${fmtNum(r.trailing_start_r, 1)}R.`);
  }

  async function place() {
    const p = preview;
    if (!p?.ok) return;
    const d = p.digits ?? digitsOf(symbol);
    const buy = t.side === "BUY";
    const details = h("div", { class: "preview" },
      [["Symbol", symbol], ["Order", `${t.side} ${t.kind}`], ["Volume", `${fmtLots(p.lots)} lots`], ["Entry", fmtPrice(p.entry, d)],
        ["Stop loss", fmtPrice(p.sl, d)], ["Take profit", `${fmtPrice(p.tp1, d)} / ${fmtPrice(p.tp2, d)}`], ["Risk", `${fmtMoney(p.risk_amount)} (${fmtNum(p.risk_percent, 2)}%)`]]
        .map(([k, v]) => h("div", { class: "preview-row" }, h("span", null, k), h("span", { class: "num" }, v))));
    const ok = await confirmDialog({
      title: `${buy ? "Buy" : "Sell"} ${symbol}?`,
      message: store.snapshot?.mode === "MT5" ? "This sends a REAL order to your MetaTrader 5 account." : "This order goes to the paper account.",
      details, confirmLabel: `${buy ? "Buy" : "Sell"} ${fmtLots(p.lots)} lots`, tone: "info", confirmVariant: buy ? "btn-buy" : "btn-sell",
    });
    if (!ok) return;
    submitBtn.classList.add("loading");
    try {
      const res = await api.post("/api/trade/place", { ...body(), override_news: Boolean(p.news_lock && t.override) });
      toast({ title: res.pending ? `${t.side} ${t.kind} order placed` : `${t.side} ${symbol} filled`,
        message: res.pending ? `#${res.order_ticket} · ${fmtLots(res.lots)} lots @ ${fmtPrice(res.price, d)}` : `#${res.ticket} · ${fmtLots(res.lots)} lots @ ${fmtPrice(res.price, d)}`, level: "success" });
      setTab(res.pending ? "orders" : "positions");
    } catch (err) {
      toastError(err, "Order rejected");
    } finally {
      submitBtn.classList.remove("loading");
      runPreview();
    }
  }

  function editPosition(pos) {
    const sym = pos.canonical || pos.symbol;
    const d = digitsOf(sym);
    const step = String(1 / 10 ** d);
    const sl = numberInput({ value: pos.sl ?? "", step });
    const tp = numberInput({ value: pos.tp ?? "", step });
    openModal({
      title: `Modify #${pos.ticket}`, iconName: "edit",
      body: [h("div", { class: "row gap-12" }, symbolIcon(sym), h("div", null, h("div", { class: "strong" }, `${sym} · ${pos.direction} ${fmtLots(pos.volume)} lots`),
        h("div", { class: "small dim" }, `Entry ${fmtPrice(pos.price_open, d)} · now ${fmtPrice(pos.price_current, d)} · P&L ${fmtMoney(pos.profit, { sign: true })}`))),
      h("div", { class: "fields" }, field("Stop loss", sl), field("Take profit", tp)),
      h("div", { class: "info-box" }, icon("info", 16), "The bot keeps managing the trade (TP1 partial, breakeven, trailing) from the new levels.")],
      actions: [{ label: "Cancel", variant: "btn-ghost" }, { label: "Save changes", variant: "btn-primary", onClick: async () => {
        const payload = {};
        if (sl.value !== "") payload.sl = Number(sl.value);
        if (tp.value !== "") payload.tp = Number(tp.value);
        await api.post(`/api/positions/${pos.ticket}/modify`, payload);
        toast({ title: "Stops updated", message: `#${pos.ticket} SL ${payload.sl ?? "—"} · TP ${payload.tp ?? "—"}`, level: "success" });
      } }],
    });
  }

  // --------------------------------------------------------- live updates --
  const lastPrices = new Map();
  function renderQuotes(s) {
    if (!s) return;
    for (const [sym, item] of wlItems) {
      const q = s.symbols?.[sym] || {};
      const price = chartPrice(sym);
      const prev = lastPrices.get(sym);
      setText(item.price, fmtPrice(price, q.digits ?? 5));
      if (prev !== undefined && price !== null && price !== prev) {
        item.price.classList.remove("up", "down");
        item.price.classList.add(price > prev ? "up" : "down");
        clearTimeout(item.timer);
        item.timer = setTimeout(() => item.price.classList.remove("up", "down"), 600);
      }
      if (price !== null) lastPrices.set(sym, price);
      const chg = q.change_24h_pct;
      setText(item.chg, chg === null || chg === undefined ? "" : fmtPct(chg));
      item.chg.className = `wl-chg ${tone(chg)}`;
      setText(item.meta, q.spread_points !== undefined ? `Spread ${fmtNum(q.spread_points, 1)} pts` : symbolDescription(sym));
      item.lock.hidden = !s.news_locks?.[sym]?.active;
    }
    const q = s.symbols?.[symbol] || {};
    const d = q.digits ?? 5;
    setText(sellPrice, fmtPrice(q.bid, d));
    setText(buyPrice, fmtPrice(q.ask, d));
    setText(spreadVal, q.spread_points === undefined ? "—" : fmtNum(q.spread_points, 1));
    setText(priceBig, fmtPrice(chartPrice(symbol), d));
    const chg = q.change_24h_pct;
    setText(priceChg, chg === null || chg === undefined ? "" : `${fmtPct(chg)} 24h`);
    priceChg.className = `num small ${tone(chg)}`;
    const p = chartPrice(symbol);
    if (p !== null) chart?.applyPrice(p);
    renderRiskValue();
  }

  function renderAccount(s) {
    const a = s?.account || {};
    const d = s?.drawdown || {};
    const cur = a.currency || "USD";
    const row = (k, v, cls = "") => h("div", { class: "kv" }, h("span", { class: "kv-key" }, k), h("span", { class: `kv-val num ${cls}` }, v));
    const today = a.equity !== undefined && d.start_equity ? a.equity - d.start_equity : null;
    replace(acctBody,
      row("Balance", fmtMoney(a.balance, { currency: cur })),
      row("Equity", fmtMoney(a.equity, { currency: cur })),
      row("Floating P&L", fmtMoney(a.profit, { currency: cur, sign: true }), tone(a.profit)),
      row("Today", fmtMoney(today, { currency: cur, sign: true }), tone(today)),
      row("Free margin", fmtMoney(a.free_margin, { currency: cur })),
      row("Margin level", a.margin_level ? `${fmtNum(a.margin_level, 0)}%` : "—"),
      row("Daily loss", `${fmtNum(d.drawdown_pct ?? 0, 2)}% / ${fmtNum(d.limit_pct, 1)}%`, d.halted ? "down" : ""));
    const mode = acctCard.querySelector(".acct-mode");
    replace(mode, badge(s?.mode === "MT5" ? "LIVE" : "PAPER", s?.mode === "MT5" ? "down" : "brand", { sm: true }));
  }

  function renderStrip(s) {
    const an = s?.symbols?.[symbol]?.analysis;
    if (!an) {
      replace(strip, h("div", { class: "analysis-cell" }, h("span", { class: "analysis-label" }, "SMC analysis"), h("span", { class: "analysis-value dim" }, "Waiting for the first closed bar…")));
      return;
    }
    const cell = (label, value) => h("div", { class: "analysis-cell" }, h("span", { class: "analysis-label" }, label), h("span", { class: "analysis-value" }, value));
    const zone = String(an.premium_discount || "—");
    replace(strip,
      cell("HTF bias", biasBadge(an.htf_bias)), cell("MTF bias", biasBadge(an.mtf_bias)), cell("LTF trend", biasBadge(an.ltf_trend)),
      cell("Price zone", badge(zone, zone.startsWith("DISC") ? "up" : zone.startsWith("PREM") ? "down" : "", { sm: true })),
      cell("Active FVG", String(an.active_fvgs ?? 0)), cell("Active OB", String(an.active_obs ?? 0)),
      cell("CRT", an.crt || "None"), cell("DXY", an.dxy_bias || "N/A"),
      cell("Killzone", (an.killzones || []).length ? an.killzones.join(", ") : "Off-session"),
      cell("Engine notes", h("span", { class: "small muted ellipsis", style: { maxWidth: "420px", display: "inline-block" }, title: (an.notes || []).join(" · ") }, (an.notes || []).join(" · ") || `Last bar ${fmtRelative(an.last_bar_time)}`)));
  }

  function renderBottom(s) {
    const positions = s?.positions || [];
    const orders = s?.orders || [];
    tabsEl.setCount("positions", positions.length);
    tabsEl.setCount("orders", orders.length);
    posTable.update(positions);
    ordTable.update(orders);
  }

  function setTab(id) {
    activeTab = id;
    tabsEl.setActive(id);
    replace(paneBox, panes[id]);
    if (id === "history") loadHistory();
    if (id === "signals") loadSignals();
  }

  async function loadHistory() {
    const res = await api.get("/api/trades?status=CLOSED&limit=100", { signal: scope.signal });
    histTable.update(res.items || []);
  }

  async function loadSignals() {
    const res = await api.get("/api/signals?limit=300", { signal: scope.signal });
    sigTable.update((res.items || []).filter((x) => x.symbol === symbol).slice(0, 60));
  }

  // ------------------------------------------------------------------ init --
  renderHeader();
  syncTicket();
  setRisk(t.risk);
  renderQuotes(store.snapshot);
  renderStrip(store.snapshot);
  renderBottom(store.snapshot);
  renderAccount(store.snapshot);
  setTab(activeTab);
  syncUrl();
  loadChart();
  live.subscribeChart(symbol, tf, bars);
  let lastStripSig = "";
  scope.on("snapshot", (s) => {
    renderQuotes(s);
    renderBottom(s);
    renderAccount(s);
    const sig = JSON.stringify(s.symbols?.[symbol]?.analysis || null);
    if (sig !== lastStripSig) { lastStripSig = sig; renderStrip(s); }
    renderSubmit();
  });
  scope.every(3000, () => { if (t.kind === "MARKET") return runPreview(); return null; }, { immediate: false });
  scope.every(15000, () => (activeTab === "history" ? loadHistory() : activeTab === "signals" ? loadSignals() : null), { immediate: false });
  runPreview();
}
