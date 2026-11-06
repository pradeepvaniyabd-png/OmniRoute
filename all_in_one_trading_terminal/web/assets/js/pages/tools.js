// Trading tools: risk calculators, Fibonacci/OTE, compounding, drawdown, sessions, correlation and ADR volatility.

import { api, qs } from "../api.js";
import { fxMarketOpen } from "../actions.js";
import { fmtCountdown, fmtLots, fmtMoney, fmtNum, fmtPct, fmtPrice, getDisplayTz, hhmm, minutesOfDay, tzLabel, tzOffsetSeconds } from "../format.js";
import { setPref, store } from "../store.js";
import { badge, card, emptyState, errorState, field, h, icon, numberInput, progress, replace, segmented, selectInput, setText, skeletonLines, svgEl } from "../ui.js";

const TOOLS = [
  { id: "position", label: "Position size", desc: "Lots from risk and stop distance", icon: "scale" },
  { id: "pip", label: "Pip value", desc: "Money value per pip / point", icon: "coins" },
  { id: "rr", label: "Risk : reward", desc: "R multiple & break-even win rate", icon: "target" },
  { id: "margin", label: "Margin", desc: "Required margin & leverage", icon: "wallet" },
  { id: "pnl", label: "Profit / loss", desc: "Result of an entry and exit", icon: "trendUp" },
  { id: "fib", label: "Fibonacci & OTE", desc: "Retracements, ICT OTE, targets", icon: "spiral" },
  { id: "compound", label: "Compounding", desc: "Account growth projection", icon: "growth" },
  { id: "drawdown", label: "Drawdown & recovery", desc: "Gain needed, losing streaks", icon: "trendDown" },
  { id: "sessions", label: "Market sessions", desc: "Sessions, killzones, IST candles", icon: "clock" },
  { id: "correlation", label: "Correlation matrix", desc: "How your symbols move together", icon: "grid" },
  { id: "volatility", label: "Volatility (ADR)", desc: "Average daily range & usage", icon: "activity" },
];

export async function mount(root, { scope }) {
  let specs = null;
  let active = TOOLS.some((t) => t.id === store.prefs.tools?.active) ? store.prefs.tools.active : "position";
  let disposeTool = null;
  const navItems = new Map();
  const nav = h("div", { class: "card tools-nav" }, TOOLS.map((tool) => {
    const btn = h("button", { class: "tools-item", type: "button", onClick: () => open(tool.id) },
      h("span", { class: "tools-ico" }, icon(tool.icon, 16)), h("span", null, tool.label, h("small", null, tool.desc)));
    navItems.set(tool.id, btn);
    return btn;
  }));
  const panel = h("div", { class: "stack" });
  root.append(h("div", { class: "tools" }, nav, panel));

  const cur = () => specs?.account_currency || store.snapshot?.account?.currency || "USD";
  const specOf = (symbol) => specs?.symbols?.find((s) => s.symbol === symbol) || null;
  const available = () => (specs?.symbols || []).filter((s) => s.available);
  const mid = (s) => (s?.bid && s?.ask ? (s.bid + s.ask) / 2 : s?.bid ?? null);
  const val = (input) => { const el = input.input || input; return el.value === "" ? null : Number(el.value); };
  const setVal = (input, v) => { const el = input.input || input; el.value = v === null || v === undefined || !Number.isFinite(Number(v)) ? "" : String(v); };
  const onInputs = (inputs, fn) => inputs.forEach((i) => (i.input || i).addEventListener("input", fn));

  function symbolPicker(onChange) {
    const list = available();
    const initial = list.find((s) => s.symbol === store.prefs.chart?.symbol)?.symbol || list[0]?.symbol || "";
    const sel = selectInput(list.length ? list.map((s) => ({ value: s.symbol, label: s.symbol })) : [{ value: "", label: "No symbols available" }], initial, onChange);
    return sel;
  }

  function results(items) {
    return h("div", { class: "result-grid" }, items.map((r) => h("div", { class: `result ${r.hero ? "hero" : ""}` },
      h("span", { class: "result-label" }, r.label), h("span", { class: `result-value ${r.tone || ""}` }, r.value ?? "—"), r.sub ? h("span", { class: "tiny dim" }, r.sub) : null)));
  }

  function needSpecs(box) {
    if (!specs) { replace(box, skeletonLines(4)); return false; }
    if (!available().length) { replace(box, errorState("Symbol specifications are unavailable — the broker connection is offline.")); return false; }
    return true;
  }

  // ------------------------------------------------------- position size --
  function positionTool() {
    const out = h("div");
    if (!needSpecs(out)) return { el: card({ title: "Position size calculator", iconName: "scale", body: out }) };
    const runtimeRisk = store.snapshot?.runtime?.risk_percent ?? 1;
    const sym = symbolPicker(() => { prefill(); calc(); });
    const balance = numberInput({ value: Math.round(specs.equity ?? specs.balance ?? 10000), step: "1", prefix: cur() });
    let riskMode = "pct";
    const riskPct = numberInput({ value: runtimeRisk, step: "0.05", suffix: "%" });
    const riskAmt = numberInput({ value: 100, step: "1", prefix: cur() });
    const riskSeg = segmented([{ value: "pct", label: "% of balance" }, { value: "amount", label: "Fixed amount" }], riskMode, (v) => { riskMode = v; sync(); calc(); }, { sm: true });
    let slMode = "pips";
    const slPips = numberInput({ value: 50, step: "0.1", suffix: "pips" });
    const entry = numberInput({ step: "any" });
    const slPrice = numberInput({ step: "any" });
    const slSeg = segmented([{ value: "pips", label: "Stop in pips" }, { value: "price", label: "Entry & stop price" }], slMode, (v) => { slMode = v; sync(); calc(); }, { sm: true });
    const riskPctField = field("Risk", riskPct);
    const riskAmtField = field("Risk amount", riskAmt);
    const pipsField = field("Stop-loss distance", slPips);
    const priceFields = h("div", { class: "fields" }, field("Entry price", entry), field("Stop-loss price", slPrice));
    const note = h("div");
    function prefill() {
      const s = specOf(sym.value);
      const m = mid(s);
      if (m) { setVal(entry, Number(m.toFixed(s.digits))); setVal(slPrice, Number((m - s.pip * 50).toFixed(s.digits))); }
    }
    function sync() {
      riskPctField.hidden = riskMode !== "pct";
      riskAmtField.hidden = riskMode !== "amount";
      pipsField.hidden = slMode !== "pips";
      priceFields.hidden = slMode !== "price";
    }
    function calc() {
      const s = specOf(sym.value);
      if (!s) return;
      const bal = val(balance) || 0;
      const risk = riskMode === "pct" ? bal * (val(riskPct) || 0) / 100 : val(riskAmt) || 0;
      const pips = slMode === "pips" ? val(slPips) : (val(entry) && val(slPrice) ? Math.abs(val(entry) - val(slPrice)) / s.pip : null);
      const perLot = pips && s.pip_value ? pips * s.pip_value : null;
      const raw = perLot ? risk / perLot : 0;
      const step = s.volume_step || 0.01;
      const lots = Math.floor(raw / step + 1e-9) * step;
      const tooSmall = raw > 0 && lots < (s.volume_min || step);
      const lotsShown = tooSmall ? 0 : Math.min(lots, s.volume_max || lots);
      replace(out, results([
        { label: "Position size", value: `${fmtLots(lotsShown)} lots`, hero: true, sub: `Exact: ${fmtNum(raw, 4)} lots · step ${step}` },
        { label: "Money at risk", value: fmtMoney(risk, { currency: cur() }), tone: "down", sub: bal ? `${fmtNum((risk / bal) * 100, 2)}% of balance` : "" },
        { label: "Stop distance", value: pips ? `${fmtNum(pips, 1)} pips` : "—" },
        { label: "Pip value (1 lot)", value: fmtMoney(s.pip_value, { currency: cur(), digits: 2 }) },
        { label: "Units", value: fmtNum(lotsShown * (s.contract_size || 0), 0) },
        { label: "Margin (est.)", value: s.margin_per_lot ? fmtMoney(lotsShown * s.margin_per_lot, { currency: cur() }) : "—", sub: specs.leverage ? `at 1:${specs.leverage}` : "" },
      ]));
      replace(note, tooSmall ? h("div", { class: "info-box warn mt-12" }, icon("alert", 16),
        `Below the broker minimum of ${s.volume_min} lots — trading the minimum would risk ${fmtMoney((s.volume_min || step) * perLot, { currency: cur() })}.`) : null);
    }
    onInputs([balance, riskPct, riskAmt, slPips, entry, slPrice], calc);
    sync();
    prefill();
    calc();
    return { el: card({ title: "Position size calculator", iconName: "scale", sub: "Uses your broker's live tick value — the same math the bot uses",
      body: h("div", { class: "tool-grid" },
        h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), field("Account balance", balance), riskSeg, riskPctField, riskAmtField, slSeg, pipsField, priceFields),
        h("div", null, out, note)) }), refresh: calc };
  }

  // ----------------------------------------------------------- pip value --
  function pipTool() {
    const out = h("div");
    if (!needSpecs(out)) return { el: card({ title: "Pip value", iconName: "coins", body: out }) };
    const sym = symbolPicker(() => calc());
    const lots = numberInput({ value: 1, step: "0.01", suffix: "lots" });
    function calc() {
      const s = specOf(sym.value);
      if (!s) return;
      const l = val(lots) || 0;
      const rows = [0.01, 0.1, 0.5, 1, 2, 5, 10].map((x) => h("tr", null, h("td", null, h("span", { class: "num" }, fmtLots(x))),
        h("td", { class: "right" }, h("span", { class: "num" }, fmtMoney(x * s.pip_value, { currency: cur() }))),
        h("td", { class: "right" }, h("span", { class: "num" }, fmtMoney(x * s.pip_value * 10, { currency: cur() }))),
        h("td", { class: "right" }, h("span", { class: "num" }, fmtMoney(x * s.pip_value * 100, { currency: cur() })))));
      replace(out, results([
        { label: `Pip value · ${fmtLots(l)} lots`, value: fmtMoney(l * s.pip_value, { currency: cur(), digits: 2 }), hero: true, sub: `1 pip = ${s.pip} price units` },
        { label: "Point value", value: fmtMoney(l * s.value_per_unit * s.point, { currency: cur(), digits: 4 }), sub: `1 point = ${s.point}` },
        { label: "Contract size", value: fmtNum(s.contract_size, 0) },
        { label: "Tick value", value: fmtMoney(s.tick_value, { currency: cur(), digits: 4 }), sub: `per ${s.tick_size} move · 1 lot` },
      ]), h("div", { class: "table-wrap mt-16" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", null, "Lots"), h("th", { class: "right" }, "1 pip"), h("th", { class: "right" }, "10 pips"), h("th", { class: "right" }, "100 pips"))), h("tbody", null, rows))));
    }
    onInputs([lots], calc);
    calc();
    return { el: card({ title: "Pip value calculator", iconName: "coins", sub: "Account-currency value of price moves", body: h("div", { class: "tool-grid" }, h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), field("Position size", lots)), out) }), refresh: calc };
  }

  // ---------------------------------------------------------------- R:R --
  function rrTool() {
    const out = h("div");
    let side = "BUY";
    const sym = symbolPicker(() => { prefill(); calc(); });
    const sideSeg = segmented([{ value: "BUY", label: "Long" }, { value: "SELL", label: "Short" }], side, (v) => { side = v; prefill(); calc(); }, { sm: true });
    const entry = numberInput({ step: "any" });
    const stop = numberInput({ step: "any" });
    const target = numberInput({ step: "any" });
    const lots = numberInput({ value: 0.1, step: "0.01", suffix: "lots" });
    function prefill() {
      const s = specOf(sym.value);
      const m = mid(s);
      if (!m) return;
      const sign = side === "BUY" ? 1 : -1;
      setVal(entry, Number(m.toFixed(s.digits)));
      setVal(stop, Number((m - sign * s.pip * 30).toFixed(s.digits)));
      setVal(target, Number((m + sign * s.pip * 90).toFixed(s.digits)));
    }
    function calc() {
      const s = specOf(sym.value);
      const e = val(entry); const sl = val(stop); const tp = val(target);
      if (!e || !sl || !tp) { replace(out, emptyState({ iconName: "target", title: "Enter entry, stop and target" })); return; }
      const sign = side === "BUY" ? 1 : -1;
      const risk = (e - sl) * sign;
      const reward = (tp - e) * sign;
      if (risk <= 0 || reward <= 0) {
        replace(out, h("div", { class: "info-box danger" }, icon("alert", 16), side === "BUY" ? "For a long trade the stop must be below entry and the target above it." : "For a short trade the stop must be above entry and the target below it."));
        return;
      }
      const rr = reward / risk;
      const pip = s?.pip || 1;
      const l = val(lots) || 0;
      replace(out, results([
        { label: "Reward : risk", value: `1 : ${fmtNum(rr, 2)}`, hero: true, tone: rr >= 2 ? "up" : rr >= 1 ? "" : "down", sub: rr >= 2 ? "Meets the 1:2 minimum" : "Below a 1:2 target" },
        { label: "Break-even win rate", value: `${fmtNum(100 / (1 + rr), 1)}%`, sub: "Win rate needed to not lose money" },
        { label: "Risk", value: `${fmtNum(risk / pip, 1)} pips`, tone: "down", sub: s && l ? fmtMoney((risk / pip) * s.pip_value * l, { currency: cur() }) : "" },
        { label: "Reward", value: `${fmtNum(reward / pip, 1)} pips`, tone: "up", sub: s && l ? fmtMoney((reward / pip) * s.pip_value * l, { currency: cur() }) : "" },
      ]));
    }
    onInputs([entry, stop, target, lots], calc);
    if (specs) prefill();
    calc();
    return { el: card({ title: "Risk : reward calculator", iconName: "target", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), sideSeg, h("div", { class: "fields" }, field("Entry", entry), field("Stop loss", stop)), field("Take profit", target), field("Position size (optional)", lots)), out) }) };
  }

  // -------------------------------------------------------------- margin --
  function marginTool() {
    const out = h("div");
    if (!needSpecs(out)) return { el: card({ title: "Margin calculator", iconName: "wallet", body: out }) };
    const sym = symbolPicker(() => { prefill(); calc(); });
    const lots = numberInput({ value: 1, step: "0.01", suffix: "lots" });
    const leverage = numberInput({ value: specs.leverage || 100, step: "1", prefix: "1 :" });
    const price = numberInput({ step: "any" });
    function prefill() { const s = specOf(sym.value); const m = mid(s); if (m) setVal(price, Number(m.toFixed(s.digits))); }
    function calc() {
      const s = specOf(sym.value);
      if (!s) return;
      const l = val(lots) || 0; const lev = val(leverage) || 1; const p = val(price) || 0;
      const notional = l * p * (s.value_per_unit || 0);
      const margin = notional / lev;
      const equity = specs.equity || specs.balance || 0;
      replace(out, results([
        { label: "Required margin", value: fmtMoney(margin, { currency: cur() }), hero: true, sub: `at 1:${fmtNum(lev, 0)} leverage (estimate)` },
        { label: "Notional value", value: fmtMoney(notional, { currency: cur(), compact: true }) },
        { label: "Margin vs equity", value: equity ? `${fmtNum((margin / equity) * 100, 2)}%` : "—", tone: equity && margin / equity > 0.5 ? "down" : "" },
        { label: "Max lots (equity)", value: margin && equity ? fmtLots(Math.floor(((equity / margin) * l) / (s.volume_step || 0.01)) * (s.volume_step || 0.01)) : "—", sub: "Before margin call" },
      ]), h("div", { class: "info-box mt-12" }, icon("info", 16), "Brokers can apply different margin rates to metals, indices and crypto — confirm in MT5 (right-click symbol → Specification)."));
    }
    onInputs([lots, leverage, price], calc);
    prefill();
    calc();
    return { el: card({ title: "Margin calculator", iconName: "wallet", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), field("Position size", lots), field("Leverage", leverage), field("Price", price)), out) }) };
  }

  // ----------------------------------------------------------------- P&L --
  function pnlTool() {
    const out = h("div");
    if (!needSpecs(out)) return { el: card({ title: "Profit / loss", iconName: "trendUp", body: out }) };
    let side = "BUY";
    const sym = symbolPicker(() => { prefill(); calc(); });
    const sideSeg = segmented([{ value: "BUY", label: "Long" }, { value: "SELL", label: "Short" }], side, (v) => { side = v; calc(); }, { sm: true });
    const lots = numberInput({ value: 0.1, step: "0.01", suffix: "lots" });
    const entry = numberInput({ step: "any" });
    const exit = numberInput({ step: "any" });
    function prefill() { const s = specOf(sym.value); const m = mid(s); if (m) { setVal(entry, Number(m.toFixed(s.digits))); setVal(exit, Number((m + s.pip * 40).toFixed(s.digits))); } }
    function calc() {
      const s = specOf(sym.value);
      if (!s) return;
      const sign = side === "BUY" ? 1 : -1;
      const e = val(entry); const x = val(exit); const l = val(lots) || 0;
      if (!e || !x) { replace(out, emptyState({ iconName: "trendUp", title: "Enter entry and exit prices" })); return; }
      const pnl = (x - e) * sign * l * (s.value_per_unit || 0);
      const pips = ((x - e) * sign) / s.pip;
      const bal = specs.balance || 0;
      replace(out, results([
        { label: "Profit / loss", value: fmtMoney(pnl, { currency: cur(), sign: true }), hero: true, tone: pnl >= 0 ? "up" : "down" },
        { label: "Pips", value: fmtNum(pips, 1, { sign: true }), tone: pips >= 0 ? "up" : "down" },
        { label: "Return on balance", value: bal ? fmtPct((pnl / bal) * 100, 2) : "—" },
        { label: "Per pip", value: fmtMoney(l * s.pip_value, { currency: cur() }) },
      ]));
    }
    onInputs([lots, entry, exit], calc);
    prefill();
    calc();
    return { el: card({ title: "Profit / loss calculator", iconName: "trendUp", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), sideSeg, field("Position size", lots), h("div", { class: "fields" }, field("Entry", entry), field("Exit", exit))), out) }) };
  }

  // ----------------------------------------------------------- fibonacci --
  function fibTool() {
    const out = h("div");
    let trend = "UP";
    const symbols = (specs?.symbols || []).map((s) => s.symbol);
    const sym = selectInput((symbols.length ? symbols : store.bootstrap?.symbols || []).map((s) => ({ value: s, label: s })), store.prefs.chart?.symbol || symbols[0], () => calc());
    const trendSeg = segmented([{ value: "UP", label: "Swing low → high" }, { value: "DOWN", label: "Swing high → low" }], trend, (v) => { trend = v; calc(); }, { sm: true });
    const high = numberInput({ step: "any" });
    const low = numberInput({ step: "any" });
    const loadBtn = h("button", { class: "btn btn-sm", type: "button", onClick: loadSwing }, icon("crosshair", 14), "Use last 5 days H1 swing");
    async function loadSwing() {
      loadBtn.classList.add("loading");
      try {
        const chart = await api.get(`/api/chart${qs({ symbol: sym.value, tf: "H1", bars: 120 })}`, { signal: scope.signal });
        const candles = chart.candles || [];
        if (!candles.length) return;
        let hi = -Infinity; let lo = Infinity; let hiAt = 0; let loAt = 0;
        candles.forEach((c, i) => { if (c.high > hi) { hi = c.high; hiAt = i; } if (c.low < lo) { lo = c.low; loAt = i; } });
        const digits = specOf(sym.value)?.digits ?? store.snapshot?.symbols?.[sym.value]?.digits ?? 5;
        setVal(high, Number(hi.toFixed(digits)));
        setVal(low, Number(lo.toFixed(digits)));
        trend = hiAt > loAt ? "UP" : "DOWN";
        trendSeg.setValue(trend);
        calc();
      } catch { /* optional */ } finally { loadBtn.classList.remove("loading"); }
    }
    function calc() {
      const hi = val(high); const lo = val(low);
      const d = specOf(sym.value)?.digits ?? store.snapshot?.symbols?.[sym.value]?.digits ?? 5;
      if (!hi || !lo || hi <= lo) { replace(out, emptyState({ iconName: "spiral", title: "Enter a swing high above the swing low", text: "Or load the recent H1 swing for the selected symbol." })); return; }
      const range = hi - lo;
      const retr = (r) => (trend === "UP" ? hi - range * r : lo + range * r);
      const ext = (e) => (trend === "UP" ? lo + range * e : hi - range * e);
      const levels = [
        { r: 0, label: "0.0 — swing extreme", kind: "" }, { r: 0.236, label: "0.236", kind: "" }, { r: 0.382, label: "0.382", kind: "" },
        { r: 0.5, label: "0.5 — equilibrium", kind: "eq" }, { r: 0.618, label: "0.618 — OTE start", kind: "ote" }, { r: 0.705, label: "0.705 — OTE sweet spot", kind: "ote" },
        { r: 0.786, label: "0.786 — OTE end", kind: "ote" }, { r: 1, label: "1.0 — swing origin", kind: "" },
      ];
      const targets = [{ e: 1.272, label: "−0.272 target" }, { e: 1.618, label: "−0.618 target" }, { e: 2, label: "−1.0 target" }, { e: 2.618, label: "−1.618 target" }];
      const live = store.snapshot?.symbols?.[sym.value];
      const price = live?.bid && live?.ask ? (live.bid + live.ask) / 2 : null;
      const pos = price ? (trend === "UP" ? (hi - price) / range : (price - lo) / range) : null;
      const where = pos === null ? null : pos >= 0.618 && pos <= 0.786 ? ["Inside the OTE zone", "warn"] : pos > 0.5 ? [trend === "UP" ? "Discount (buy zone)" : "Premium (sell zone)", "up"] : [trend === "UP" ? "Premium" : "Discount", "down"];
      replace(out,
        price ? h("div", { class: "info-box mt-0", style: { marginBottom: "12px" } }, icon("crosshair", 16), h("span", null, `Live ${fmtPrice(price, d)} = ${fmtNum((pos || 0) * 100, 1)}% retracement · `, h("b", { class: where?.[1] || "" }, where?.[0] || ""))) : null,
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", null, h("tr", null, h("th", null, "Level"), h("th", { class: "right" }, "Price"), h("th", null, "Zone"))),
          h("tbody", null,
            levels.map((l) => h("tr", { class: l.kind }, h("td", null, h("span", { class: "strong small" }, l.label)), h("td", { class: "right" }, h("span", { class: "num" }, fmtPrice(retr(l.r), d))),
              h("td", null, l.kind === "ote" ? badge("OTE", "gold", { sm: true }) : l.kind === "eq" ? badge("EQ", "brand", { sm: true }) : l.r < 0.5 ? badge(trend === "UP" ? "Premium" : "Discount", "", { sm: true }) : badge(trend === "UP" ? "Discount" : "Premium", "", { sm: true })))),
            targets.map((t) => h("tr", null, h("td", null, h("span", { class: "small" }, t.label)), h("td", { class: "right" }, h("span", { class: "num up" }, fmtPrice(ext(t.e), d))), h("td", null, badge("Target", "up", { sm: true }))))))));
    }
    onInputs([high, low], calc);
    calc();
    if (specs) loadSwing();
    return { el: card({ title: "Fibonacci retracement & ICT OTE", iconName: "spiral", sub: "Optimal Trade Entry = 62–79% retracement of the dealing range", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Symbol", sym), trendSeg, h("div", { class: "fields" }, field("Swing high", high), field("Swing low", low)), loadBtn), out) }) };
  }

  // ---------------------------------------------------------- compounding --
  function compoundTool() {
    const out = h("div");
    const start = numberInput({ value: Math.round(specs?.balance || 10000), step: "1", prefix: cur() });
    const rate = numberInput({ value: 2, step: "0.1", suffix: "%" });
    const periods = numberInput({ value: 24, step: "1" });
    const add = numberInput({ value: 0, step: "1", prefix: cur() });
    let unit = "month";
    const unitSeg = segmented([{ value: "trade", label: "Per trade" }, { value: "week", label: "Per week" }, { value: "month", label: "Per month" }], unit, (v) => { unit = v; calc(); }, { sm: true });
    function calc() {
      const s = val(start) || 0; const r = (val(rate) || 0) / 100; const n = Math.min(600, Math.max(1, Math.round(val(periods) || 1))); const c = val(add) || 0;
      const series = [s];
      let bal = s;
      const rows = [];
      for (let i = 1; i <= n; i += 1) {
        const profit = bal * r;
        bal = bal + profit + c;
        series.push(bal);
        if (i <= 12 || i === n || i % Math.ceil(n / 12) === 0) rows.push([i, profit, bal]);
      }
      const contributed = s + c * n;
      replace(out, results([
        { label: "Final balance", value: fmtMoney(bal, { currency: cur(), compact: bal > 1e6 }), hero: true, tone: "up", sub: `after ${n} ${unit}s` },
        { label: "Total growth", value: fmtPct(((bal - contributed) / (s || 1)) * 100, 1) },
        { label: "Profit", value: fmtMoney(bal - contributed, { currency: cur(), compact: true }) },
        { label: "Deposits", value: fmtMoney(contributed, { currency: cur(), compact: true }) },
      ]), growthChart(series), h("div", { class: "table-wrap mt-12 max-h" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", null, unit), h("th", { class: "right" }, "Profit"), h("th", { class: "right" }, "Balance"))),
        h("tbody", null, rows.map(([i, p, b]) => h("tr", null, h("td", null, `#${i}`), h("td", { class: "right" }, h("span", { class: "num up" }, fmtMoney(p, { currency: cur(), sign: true }))), h("td", { class: "right" }, h("span", { class: "num" }, fmtMoney(b, { currency: cur() })))))))));
    }
    onInputs([start, rate, periods, add], calc);
    calc();
    return { el: card({ title: "Compounding calculator", iconName: "growth", sub: "Projection only — real returns vary and drawdowns happen", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Starting balance", start), unitSeg, h("div", { class: "fields" }, field("Return per period", rate), field("Periods", periods)), field("Deposit per period", add)), out) }) };
  }

  // ------------------------------------------------------------ drawdown --
  function drawdownTool() {
    const out = h("div");
    const dd = numberInput({ value: 20, step: "0.5", suffix: "%" });
    const risk = numberInput({ value: store.snapshot?.runtime?.risk_percent ?? 1, step: "0.05", suffix: "%" });
    const losses = numberInput({ value: 5, step: "1" });
    function calc() {
      const d = Math.min(99.9, Math.max(0, val(dd) || 0));
      const r = (val(risk) || 0) / 100; const n = Math.max(0, Math.round(val(losses) || 0));
      const streakDd = (1 - (1 - r) ** n) * 100;
      const table = [5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90].map((x) => h("tr", { class: Math.abs(x - d) < 0.01 ? "highlight" : "" },
        h("td", null, h("span", { class: "num down" }, `-${x}%`)), h("td", { class: "right" }, h("span", { class: "num up" }, `+${fmtNum((x / (100 - x)) * 100, 1)}%`)),
        h("td", null, progress(Math.min(100, (x / (100 - x)) * 100 / 9), x >= 50 ? "down" : x >= 25 ? "warn" : "up"))));
      replace(out, results([
        { label: "Gain needed to recover", value: `+${fmtNum((d / (100 - d)) * 100, 1)}%`, hero: true, tone: "warn", sub: `from a ${fmtNum(d, 1)}% drawdown` },
        { label: `${n} losses in a row`, value: `-${fmtNum(streakDd, 2)}%`, tone: "down", sub: `risking ${fmtNum(r * 100, 2)}% per trade` },
        { label: "Recovery after streak", value: `+${fmtNum((streakDd / (100 - streakDd)) * 100, 2)}%` },
      ]), h("div", { class: "table-wrap mt-16" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "Drawdown"), h("th", { class: "right" }, "Gain to recover"), h("th", null, ""))), h("tbody", null, table))));
    }
    onInputs([dd, risk, losses], calc);
    calc();
    return { el: card({ title: "Drawdown & recovery", iconName: "trendDown", sub: "Why the daily loss limit protects the account", body: h("div", { class: "tool-grid" },
      h("div", { class: "stack", style: { gap: "12px" } }, field("Drawdown", dd), h("div", { class: "divider" }), field("Risk per trade", risk), field("Consecutive losses", losses)), out) }) };
  }

  // ------------------------------------------------------------ sessions --
  function sessionsTool() {
    const body = h("div");
    const status = h("div", { class: "row-wrap gap-12" });
    function windowToDisplay(startH, startM, endH, endM, tz) {
      const nowSec = Date.now() / 1000;
      const off = (tzOffsetSeconds(nowSec, getDisplayTz()) - tzOffsetSeconds(nowSec, tz)) / 60;
      const a = (((startH * 60 + startM + off) % 1440) + 1440) % 1440;
      let len = endH * 60 + endM - (startH * 60 + startM);
      if (len <= 0) len += 1440;
      return { start: a, len };
    }
    const parse = (s) => String(s).split(":").map(Number);
    function render() {
      const now = new Date();
      const nowMin = minutesOfDay(now, getDisplayTz());
      const fxOpen = fxMarketOpen(now);
      const sessions = [
        { name: "Sydney", color: "var(--warn)", ...windowToDisplay(7, 0, 16, 0, "Australia/Sydney") },
        { name: "Tokyo", color: "var(--info)", ...windowToDisplay(9, 0, 18, 0, "Asia/Tokyo") },
        { name: "London", color: "var(--brand)", ...windowToDisplay(8, 0, 17, 0, "Europe/London") },
        { name: "New York", color: "var(--violet)", ...windowToDisplay(8, 0, 17, 0, "America/New_York") },
      ];
      const kzTz = store.bootstrap?.killzone_tz || "America/New_York";
      const killzones = (store.bootstrap?.killzones || []).map((k) => { const [a, b] = parse(k.start); const [c, d] = parse(k.end); return { name: k.name, color: "var(--down)", ...windowToDisplay(a, b, c, d, kzTz) }; });
      const candles = (store.bootstrap?.time_candles_ist || []).map((t) => { const [a, b] = parse(t); return { label: t, ...windowToDisplay(a, b, a, b + 15, "Asia/Kolkata") }; });
      const inside = (w) => { const end = w.start + w.len; return (nowMin >= w.start && nowMin < end) || (end > 1440 && nowMin < end - 1440); };
      const openNow = fxOpen ? sessions.filter(inside).map((s) => s.name) : [];
      const kzNow = fxOpen ? killzones.filter(inside).map((k) => k.name) : [];
      const nextOpen = sessions.map((s) => ({ name: s.name, mins: ((s.start - nowMin) + 1440) % 1440 })).filter((s) => !openNow.includes(s.name)).sort((a, b) => a.mins - b.mins)[0];
      replace(status,
        h("div", { class: "lock-chip on", style: { borderColor: fxOpen ? "transparent" : "" } }, icon(fxOpen ? "globe" : "lock", 14), fxOpen ? `Open: ${openNow.join(" + ") || "between sessions"}` : "FX market closed (weekend)"),
        kzNow.length ? h("div", { class: "lock-chip on" }, icon("target", 14), `Killzone: ${kzNow.join(", ")}`) : h("div", { class: "lock-chip" }, icon("target", 14), "No killzone active"),
        nextOpen ? h("div", { class: "lock-chip" }, icon("clock", 14), `${nextOpen.name} opens in ${fmtCountdown(nextOpen.mins * 60)}`) : null);
      const bar = (w, label) => {
        const segs = [];
        const end = w.start + w.len;
        segs.push([w.start, Math.min(end, 1440)]);
        if (end > 1440) segs.push([0, end - 1440]);
        return segs.map(([a, b]) => { const el = h("div", { class: "tl-bar" }, label); el.style.left = `${(a / 1440) * 100}%`; el.style.width = `${((b - a) / 1440) * 100}%`; el.style.background = w.color; return el; });
      };
      const scale = h("div", { class: "tl-scale" }, [0, 3, 6, 9, 12, 15, 18, 21, 24].map((hr) => { const el = h("span", { class: "tl-tick" }, `${String(hr % 24).padStart(2, "0")}:00`); el.style.left = `${(hr / 24) * 100}%`; return el; }));
      const nowLine = h("div", { class: "tl-now", "data-label": hhmm(nowMin) });
      const timeline = h("div", { class: "timeline" }, scale,
        sessions.map((s) => h("div", { class: "tl-row" }, h("span", { class: "tl-name" }, h("span", { class: "dot", style: { background: s.color } }), s.name), h("div", { class: "tl-track" }, bar(s, `${hhmm(s.start)}–${hhmm(s.start + s.len)}`)))),
        killzones.length ? h("div", { class: "tl-row" }, h("span", { class: "tl-name" }, h("span", { class: "dot dot-down" }), "ICT killzones"), h("div", { class: "tl-track" }, killzones.flatMap((k) => bar(k, k.name)))) : null,
        candles.length ? h("div", { class: "tl-row" }, h("span", { class: "tl-name" }, h("span", { class: "dot dot-warn" }), "IST candles"), h("div", { class: "tl-track" }, candles.flatMap((c) => bar({ ...c, color: "var(--gold)" }, c.label)))) : null,
        nowLine);
      replace(body, status, timeline);
      requestAnimationFrame(() => {
        const track = body.querySelector(".tl-track");
        if (!track) return;
        const left = track.offsetLeft;
        const width = track.offsetWidth;
        nowLine.style.left = `${left + (nowMin / 1440) * width}px`;
      });
    }
    render();
    const id = setInterval(render, 30000);
    return { el: card({ title: "Market sessions & killzones", iconName: "clock", sub: `Shown in ${tzLabel(store.prefs.tz)} — DST handled automatically`, body }), dispose: () => clearInterval(id) };
  }

  // --------------------------------------------------------- correlation --
  function correlationTool() {
    const out = h("div", null, skeletonLines(5));
    let tf = "H1";
    let bars = 200;
    const tfSeg = segmented([{ value: "M15", label: "M15" }, { value: "H1", label: "H1" }, { value: "H4", label: "H4" }], tf, (v) => { tf = v; load(); }, { sm: true });
    const barsSel = selectInput([100, 200, 500].map((b) => ({ value: b, label: `${b} bars` })), bars, (v) => { bars = Number(v); load(); });
    barsSel.style.width = "120px";
    async function load() {
      replace(out, skeletonLines(5));
      try {
        const res = await api.get(`/api/tools/correlation${qs({ tf, bars })}`, { signal: scope.signal, timeout: 30000 });
        const syms = res.symbols || [];
        if (syms.length < 2) { replace(out, emptyState({ iconName: "grid", title: "Add more symbols to compare", text: "Correlation needs at least two instruments — add them to SYMBOLS in .env." })); return; }
        if (!res.matrix?.length) { replace(out, emptyState({ iconName: "grid", title: "Not enough overlapping data yet" })); return; }
        const cell = (v) => {
          const td = h("td", null, fmtNum(v, 2));
          const strength = Math.min(1, Math.abs(v));
          td.style.background = `color-mix(in srgb, ${v >= 0 ? "var(--up)" : "var(--down)"} ${Math.round(strength * 60)}%, var(--surface-2))`;
          td.style.color = strength > 0.55 ? "#fff" : "var(--text)";
          return td;
        };
        replace(out, h("div", { class: "table-wrap" }, h("table", { class: "corr" },
          h("thead", null, h("tr", null, h("th"), syms.map((s) => h("th", null, s)))),
          h("tbody", null, res.matrix.map((row, i) => h("tr", null, h("th", { style: { textAlign: "right" } }, syms[i]), row.map(cell)))))),
          h("div", { class: "legend mt-12" }, h("span", { class: "legend-item" }, h("span", { class: "legend-swatch", style: { background: "var(--up)" } }), "+1 move together"),
            h("span", { class: "legend-item" }, h("span", { class: "legend-swatch", style: { background: "var(--down)" } }), "−1 move opposite"),
            h("span", { class: "legend-item dim" }, `${res.bars} ${res.timeframe} returns`)),
          h("div", { class: "info-box mt-12" }, icon("info", 16), "Above ±0.8, two positions behave like one — size them as a single combined risk."));
      } catch (err) {
        if (err.name !== "AbortError") replace(out, errorState(err.message, load));
      }
    }
    load();
    return { el: card({ title: "Correlation matrix", iconName: "grid", sub: "Pearson correlation of bar returns", actions: [tfSeg, barsSel], body: out }) };
  }

  // ---------------------------------------------------------- volatility --
  function volatilityTool() {
    const out = h("div", null, skeletonLines(5));
    async function load() {
      try {
        const res = await api.get("/api/tools/volatility", { signal: scope.signal, timeout: 30000 });
        const items = res.items || [];
        if (!items.length) { replace(out, emptyState({ iconName: "activity", title: "No volatility data yet" })); return; }
        replace(out, h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", null, h("tr", null, ["Symbol", "ADR (14d)", "ADR pips", "Today range", "Used", "Remaining", "ATR (strategy TF)"].map((x, i) => h("th", { class: i ? "right" : "" }, x)))),
          h("tbody", null, items.map((r) => {
            const used = r.used_pct;
            const remaining = r.adr_pips !== null && r.today_pips !== null ? Math.max(0, r.adr_pips - r.today_pips) : null;
            return h("tr", null,
              h("td", null, h("span", { class: "strong" }, r.symbol)),
              h("td", { class: "right" }, h("span", { class: "num" }, fmtPrice(r.adr, r.digits ?? 2))),
              h("td", { class: "right" }, h("span", { class: "num" }, fmtNum(r.adr_pips, 1))),
              h("td", { class: "right" }, h("span", { class: "num" }, `${fmtNum(r.today_pips, 1)} pips`)),
              h("td", { class: "right", style: { minWidth: "150px" } }, used === null ? "—" : h("div", { class: "score" }, progress(Math.min(100, used), used >= 100 ? "down" : used >= 70 ? "warn" : "up"), h("span", { class: "score-val", style: { width: "44px" } }, `${fmtNum(used, 0)}%`))),
              h("td", { class: "right" }, h("span", { class: "num muted" }, remaining === null ? "—" : `${fmtNum(remaining, 1)} pips`)),
              h("td", { class: "right" }, h("span", { class: "num" }, r.atr_ltf_pips === null ? "—" : `${fmtNum(r.atr_ltf_pips, 1)} pips`)));
          })))),
          h("div", { class: "info-box mt-12" }, icon("info", 16), "When today's range has already used 100%+ of the ADR, extension targets are less likely — favour TP1 partials."));
      } catch (err) {
        if (err.name !== "AbortError") replace(out, errorState(err.message, load));
      }
    }
    load();
    return { el: card({ title: "Volatility — average daily range", iconName: "activity", sub: "14-day ADR from H1 candles", body: out }) };
  }

  const builders = { position: positionTool, pip: pipTool, rr: rrTool, margin: marginTool, pnl: pnlTool, fib: fibTool, compound: compoundTool,
    drawdown: drawdownTool, sessions: sessionsTool, correlation: correlationTool, volatility: volatilityTool };
  let current = null;

  function open(id) {
    active = id;
    setPref("tools.active", id);
    for (const [key, btn] of navItems) btn.classList.toggle("active", key === id);
    disposeTool?.();
    current = builders[id]();
    disposeTool = current.dispose || null;
    replace(panel, current.el);
  }
  scope.add(() => disposeTool?.());

  async function loadSpecs(first = false) {
    try {
      specs = await api.get("/api/tools/specs", { signal: scope.signal });
      if (first) open(active);
      else current?.refresh?.();
    } catch (err) {
      if (err.name === "AbortError") return;
      if (first) { specs = { symbols: [] }; open(active); }
    }
  }
  open(active);
  await loadSpecs(true);
  scope.every(15000, () => loadSpecs(false), { immediate: false });
}

function growthChart(series) {
  const width = 640;
  const height = 170;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", class: "mini-chart mt-16" });
  if (series.length < 2) return svg;
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const pts = series.map((v, i) => [(i / (series.length - 1)) * width, height - 8 - ((v - min) / span) * (height - 16)]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const defs = svgEl("defs");
  const grad = svgEl("linearGradient", { id: "cg", x1: "0", x2: "0", y1: "0", y2: "1" });
  grad.append(svgEl("stop", { offset: "0%", "stop-color": "var(--up)", "stop-opacity": ".35" }), svgEl("stop", { offset: "100%", "stop-color": "var(--up)", "stop-opacity": "0" }));
  defs.append(grad);
  svg.append(defs, svgEl("path", { d: `${line}L${width},${height}L0,${height}Z`, fill: "url(#cg)" }),
    svgEl("path", { d: line, fill: "none", stroke: "var(--up)", "stroke-width": "2.2", "vector-effect": "non-scaling-stroke" }));
  return svg;
}
