// Widgets shared by several pages (positions table, impact bars, key/value rows, bias badges).

import { breakeven, cancelOrder, closePosition } from "../actions.js";
import { fmtLots, fmtMoney, fmtNum, fmtPrice, fmtRelative, tone } from "../format.js";
import { store } from "../store.js";
import { badge, dirBadge, h, icon } from "../ui.js";

export function kv(key, value) {
  return h("div", { class: "kv" }, h("span", { class: "kv-key" }, key), h("span", { class: "kv-val" }, value));
}

export function biasBadge(bias) {
  const b = String(bias || "NEUTRAL").toUpperCase();
  if (b.startsWith("BULL") || b === "BUY") return badge("Bull", "up", { sm: true });
  if (b.startsWith("BEAR") || b === "SELL") return badge("Bear", "down", { sm: true });
  return badge("Neutral", "", { sm: true });
}

export function impactBars(impact) {
  const cls = String(impact || "").toLowerCase();
  return h("span", { class: `impact ${cls}`, title: `${impact} impact` }, h("i"), h("i"), h("i"));
}

export function digits(row) {
  const symbol = row.canonical || row.symbol;
  return store.snapshot?.symbols?.[symbol]?.digits ?? 5;
}

export function liveR(p) {
  if (!p.initial_sl || !p.price_open || !p.price_current) return null;
  const sign = p.direction === "BUY" ? 1 : -1;
  const risk = (p.price_open - p.initial_sl) * sign;
  if (risk <= 0) return null;
  return ((p.price_current - p.price_open) * sign) / risk;
}

export function positionColumns({ onEdit = null } = {}) {
  return [
    { key: "symbol", label: "Symbol", render: (r) => h("div", { class: "row gap-4" }, h("span", { class: "strong" }, r.canonical || r.symbol), r.bot ? null : badge("MANUAL", "outline", { sm: true })), sortValue: (r) => r.canonical || r.symbol },
    { key: "direction", label: "Side", render: (r) => dirBadge(r.direction) },
    { key: "volume", label: "Lots", align: "right", render: (r) => h("span", { class: "num" }, fmtLots(r.volume)) },
    { key: "price_open", label: "Entry", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.price_open, digits(r))) },
    { key: "price_current", label: "Price", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.price_current, digits(r))) },
    { key: "sl", label: "SL", align: "right", render: (r) => h("span", { class: "num down" }, fmtPrice(r.sl, digits(r))) },
    { key: "tp", label: "TP", align: "right", render: (r) => h("span", { class: "num up" }, fmtPrice(r.tp, digits(r))) },
    { key: "profit", label: "P&L", align: "right", render: (r) => h("span", { class: `num strong ${tone(r.profit)}` }, fmtMoney(r.profit, { sign: true })) },
    { key: "r", label: "R", align: "right", sortValue: liveR, render: (r) => { const v = liveR(r); return h("span", { class: `num ${tone(v)}` }, v === null ? "—" : `${v >= 0 ? "+" : ""}${fmtNum(v, 2)}R`); } },
    { key: "state", label: "Protection", sort: false, render: (r) => h("div", { class: "row gap-4" },
      r.tp1_hit ? badge("TP1", "up", { sm: true }) : null, r.be_moved ? badge("BE", "info", { sm: true }) : null, r.trailing ? badge("TRAIL", "violet", { sm: true }) : null,
      !r.tp1_hit && !r.be_moved && !r.trailing ? h("span", { class: "dim small" }, "Initial SL") : null) },
    { key: "time_utc", label: "Opened", render: (r) => h("span", { class: "dim small nowrap" }, fmtRelative(r.time_utc)) },
    { key: "actions", label: "", sort: false, align: "right", sig: (r) => `a${r.ticket}`, render: (r) => h("div", { class: "row-actions" },
      onEdit ? h("button", { class: "btn btn-xs btn-ghost", title: "Modify SL / TP", onClick: () => onEdit(r) }, icon("edit", 13), "Edit") : null,
      h("button", { class: "btn btn-xs", title: "Move stop to breakeven", onClick: () => breakeven(r) }, icon("shieldCheck", 13), "BE"),
      h("button", { class: "btn btn-xs btn-danger", title: "Close at market", onClick: () => closePosition(r) }, icon("x", 13), "Close")) },
  ];
}


export function orderColumns() {
  return [
    { key: "symbol", label: "Symbol", render: (r) => h("span", { class: "strong" }, r.canonical || r.symbol), sortValue: (r) => r.canonical || r.symbol },
    { key: "direction", label: "Side", render: (r) => h("div", { class: "row gap-4" }, dirBadge(r.direction), badge(r.kind, "outline", { sm: true })) },
    { key: "volume", label: "Lots", align: "right", render: (r) => h("span", { class: "num" }, fmtLots(r.volume)) },
    { key: "price", label: "Price", align: "right", render: (r) => h("span", { class: "num" }, fmtPrice(r.price, digits(r))) },
    { key: "sl", label: "SL", align: "right", render: (r) => h("span", { class: "num down" }, fmtPrice(r.sl, digits(r))) },
    { key: "tp", label: "TP", align: "right", render: (r) => h("span", { class: "num up" }, fmtPrice(r.tp, digits(r))) },
    { key: "time_utc", label: "Placed", render: (r) => h("span", { class: "dim small nowrap" }, fmtRelative(r.time_utc)) },
    { key: "actions", label: "", sort: false, align: "right", sig: (r) => `o${r.ticket}`, render: (r) => h("div", { class: "row-actions" },
      h("button", { class: "btn btn-xs btn-warn", title: "Cancel order", onClick: () => cancelOrder(r) }, icon("x", 13), "Cancel")) },
  ];
}
