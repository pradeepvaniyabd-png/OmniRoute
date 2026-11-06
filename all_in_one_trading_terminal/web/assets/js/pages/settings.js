// Settings: engine controls, strategies, risk limits (read-only), display preferences, paper reset, danger zone.

import { api } from "../api.js";
import { applyTheme, panicClose, setDensity, setTheme, toggleBot } from "../actions.js";
import { ZONES, fmtMoney, fmtNum, setDisplayTz } from "../format.js";
import { setPref, store } from "../store.js";
import { badge, beep, card, confirmDialog, field, h, icon, numberInput, replace, segmented, selectInput, setText, toast, toastError, toggleSwitch } from "../ui.js";

export async function mount(root, { scope }) {
  const boot = store.bootstrap || {};
  const risk = boot.risk || {};
  const maxRisk = Number(risk.max_risk_percent || 5);

  // engine ---------------------------------------------------------------
  const botState = h("div", { class: "row gap-8" });
  const botBtn = h("button", { class: "btn btn-sm", onClick: toggleBot });
  const execSeg = segmented([{ value: "auto", label: "Auto-trade" }, { value: "signal_only", label: "Signal only" }], store.snapshot?.runtime?.execution_mode || "auto",
    (v) => post("/api/settings/execution", { mode: v }, v === "auto" ? "Auto-execution enabled" : "Signal-only mode — setups are broadcast, not traded"), { sm: true });
  const riskInput = numberInput({ value: store.snapshot?.runtime?.risk_percent ?? risk.risk_percent ?? 0.5, step: "0.05", min: "0.05", max: String(maxRisk), suffix: "%" });
  const riskSave = h("button", { class: "btn btn-sm btn-primary", onClick: async () => {
    const v = Number(riskInput.input.value);
    if (!(v > 0) || v > maxRisk) { toast({ title: "Invalid risk", message: `Choose between 0.05% and ${maxRisk}%`, level: "warning" }); return; }
    const res = await post("/api/settings/risk", { risk_percent: v }, null);
    if (res) toast({ title: "Risk per trade updated", message: `${res.risk_percent}% of equity per trade`, level: "success" });
  } }, "Save");
  const kzToggle = toggleSwitch({ checked: Boolean(store.snapshot?.runtime?.require_killzone), onChange: (v) => post("/api/settings/killzone", { enabled: v }, v ? "Killzone filter ON" : "Killzone filter OFF") });
  const newsToggle = toggleSwitch({ checked: Boolean(store.snapshot?.runtime?.news_auto_pause), onChange: (v) => post("/api/settings/news-pause", { enabled: v }, v ? "News auto-pause ON" : "News auto-pause OFF") });
  const engineCard = card({ title: "Trading engine", iconName: "zap", sub: "Changes apply instantly and persist across restarts", body: h("div", null,
    setting("Automated trading", "Start or pause the bot. Open positions stay protected while paused.", h("div", { class: "row gap-12" }, botState, botBtn)),
    setting("Execution mode", "Auto-trade sends orders; signal-only broadcasts A+ setups without trading.", execSeg),
    setting("Risk per trade", `Percent of equity risked per setup (max ${maxRisk}%).`, h("div", { class: "row" }, h("div", { style: { width: "130px" } }, riskInput), riskSave)),
    setting("Require ICT killzone", "Only take entries inside the configured killzones.", kzToggle),
    setting("News auto-pause", "Block new entries during the Forex Factory blackout window.", newsToggle)) });

  // strategies -------------------------------------------------------------
  const enabled = new Set(store.snapshot?.runtime?.enabled_strategies || []);
  const strategyCards = h("div", { class: "stack", style: { gap: "10px" } });
  function renderStrategies() {
    replace(strategyCards, (boot.strategies || []).map((s) => {
      const on = enabled.has(s.code);
      return h("div", { class: `strategy-card ${on ? "on" : ""}` },
        toggleSwitch({ checked: on, onChange: async (v) => {
          const next = new Set(enabled);
          if (v) next.add(s.code); else next.delete(s.code);
          if (!next.size) { toast({ title: "Keep at least one strategy enabled", level: "warning" }); renderStrategies(); return; }
          const res = await post("/api/settings/strategies", { strategies: [...next] }, `${s.label} ${v ? "enabled" : "disabled"}`);
          if (res) { enabled.clear(); (res.strategies || []).forEach((c) => enabled.add(c)); }
          renderStrategies();
        } }),
        h("div", null, h("div", { class: "strong" }, s.label), h("div", { class: "small muted mt-4" }, s.description), h("div", { class: "tiny dim mt-4 mono" }, s.code)));
    }));
  }
  renderStrategies();
  const strategiesCard = card({ title: "Strategies", iconName: "layers", sub: "SMC / ICT setup detectors", body: strategyCards });

  // limits (read-only) --------------------------------------------------
  const limitsCard = card({ title: "Risk limits", iconName: "shield", sub: "Set in .env — restart the terminal to change", body: h("div", null,
    kv("Max daily loss", `${fmtNum(risk.max_daily_loss_percent, 2)}% (circuit breaker)`), kv("Daily reset", `${String(risk.daily_reset_hour ?? 0).padStart(2, "0")}:00 ${risk.daily_reset_timezone || ""}`),
    kv("Max open positions", String(risk.max_open_positions ?? "—")), kv("Max per symbol", String(risk.max_positions_per_symbol ?? "—")),
    kv("Max trades per day", String(risk.max_trades_per_day ?? "—")), kv("Default targets", `TP1 ${fmtNum(risk.tp1_rr, 1)}R · TP2 ${fmtNum(risk.tp2_rr, 1)}R`),
    kv("Breakeven", `at ${fmtNum(risk.breakeven_trigger_r, 1)}R`), kv("Trailing stop", `${risk.trailing_mode || "—"} from ${fmtNum(risk.trailing_start_r, 1)}R`),
    kv("Timeframes", `${boot.timeframes?.htf} bias · ${boot.timeframes?.mtf} confirm · ${boot.timeframes?.ltf} entry`),
    kv("Symbols", (boot.symbols || []).join(", "))) });

  // display ----------------------------------------------------------------
  const themeSeg = segmented([{ value: "dark", label: "Dark", icon: "moon" }, { value: "light", label: "Light", icon: "sun" }, { value: "system", label: "System" }], store.prefs.theme || "dark", (v) => setTheme(v), { sm: true });
  const densitySeg = segmented([{ value: "comfortable", label: "Comfortable" }, { value: "compact", label: "Compact" }], store.prefs.density || "comfortable", (v) => setDensity(v), { sm: true });
  const tzSel = selectInput(ZONES.map((z) => ({ value: z.id, label: z.name })), store.prefs.tz || "Asia/Kolkata", (v) => {
    setPref("tz", v);
    setDisplayTz(v);
    applyTheme();
    toast({ title: "Time zone updated", message: "Charts, tables and clocks now use the new zone.", level: "success" });
  });
  tzSel.style.width = "220px";
  const soundToggle = toggleSwitch({ checked: Boolean(store.prefs.sound), onChange: (v) => { setPref("sound", v); if (v) beep("success"); } });
  const desktopToggle = toggleSwitch({ checked: Boolean(store.prefs.desktop), onChange: async (v, input) => {
    if (v && "Notification" in window && Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { input.checked = false; toast({ title: "Notifications blocked", message: "Allow notifications for this site in the browser settings.", level: "warning" }); return; }
    }
    setPref("desktop", v);
  } });
  const displayCard = card({ title: "Display & alerts", iconName: "sliders", sub: "Saved in this browser", body: h("div", null,
    setting("Theme", "Dark is easier on the eyes during the NY session.", themeSeg),
    setting("Density", "Compact fits more rows on smaller screens.", densitySeg),
    setting("Time zone", "Used for charts, the calendar and every timestamp.", tzSel),
    setting("Sound alerts", "Play a tone for executions, closes and risk alerts.", soundToggle),
    setting("Desktop notifications", "Show OS notifications while the tab is in the background.", desktopToggle)) });

  // paper / danger --------------------------------------------------------
  const blocks = [];
  if (boot.broker_mode === "paper") {
    const balance = numberInput({ value: boot.paper?.starting_balance ?? 10000, step: "100", prefix: boot.paper?.currency || "USD" });
    const clear = h("input", { type: "checkbox" });
    const resetBtn = h("button", { class: "btn btn-warn", onClick: async () => {
      const amount = Number(balance.input.value);
      if (!(amount > 0)) { toast({ title: "Enter a positive balance", level: "warning" }); return; }
      const ok = await confirmDialog({ title: "Reset the paper account?", message: `All paper positions and orders are closed and the balance is set to ${fmtMoney(amount, { currency: boot.paper?.currency })}.${clear.checked ? " The paper trade journal and equity curve are deleted." : ""}`, confirmLabel: "Reset account", tone: "warn", requireText: "RESET" });
      if (!ok) return;
      try {
        const res = await api.post("/api/paper/reset", { confirm: true, balance: amount, clear_history: clear.checked });
        toast({ title: "Paper account reset", message: `New balance ${fmtMoney(res.balance, { currency: boot.paper?.currency })}`, level: "success" });
      } catch (err) { toastError(err, "Reset failed"); }
    } }, icon("rotate", 15), "Reset paper account");
    blocks.push(card({ title: "Paper account", iconName: "wallet", sub: `Simulated fills on live market data · leverage 1:${boot.paper?.leverage ?? "—"}`, body: h("div", { class: "stack", style: { gap: "12px" } },
      field("Starting balance", balance), h("label", { class: "checkbox" }, clear, "Also delete paper trade history and equity curve"), h("div", null, resetBtn)) }));
  }
  blocks.push(card({ title: "Danger zone", iconName: "alert", className: "danger-zone", body: h("div", null,
    setting("Panic close", "Close every position, cancel every pending order and pause the bot.", h("button", { class: "btn btn-danger", onClick: panicClose }, icon("power", 15), "Panic close all"))) }));

  const about = card({ title: "About", iconName: "info", body: h("div", null,
    kv("Version", `Quant Super-Terminal ${boot.version || ""}`), kv("Broker mode", boot.mode === "MT5" ? badge("LIVE MT5", "down", { sm: true }) : badge("PAPER", "brand", { sm: true })),
    kv("Login", boot.auth_enabled ? "Password protected" : "Local access (no password)"),
    kv("Charts", h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer" }, "TradingView Lightweight Charts™")),
    kv("Fonts", "Inter, JetBrains Mono (SIL OFL)")) });

  root.append(h("div", { class: "settings-grid" },
    h("div", { class: "stack" }, engineCard, strategiesCard, limitsCard),
    h("div", { class: "stack" }, displayCard, ...blocks, about)));

  function renderEngine(s) {
    const r = s?.runtime || {};
    const on = Boolean(r.bot_enabled);
    replace(botState, h("span", { class: `dot ${on ? "dot-up pulse" : "dot-warn"}` }), h("span", { class: "small strong" }, s?.bot_status || (on ? "RUNNING" : "PAUSED")));
    botBtn.className = `btn btn-sm ${on ? "btn-warn" : "btn-success"}`;
    replace(botBtn, icon(on ? "pause" : "play", 14), on ? "Pause" : "Start");
    if (r.execution_mode && execSeg.getValue() !== r.execution_mode) execSeg.setValue(r.execution_mode);
    kzToggle.set(r.require_killzone);
    newsToggle.set(r.news_auto_pause);
    const sig = (r.enabled_strategies || []).slice().sort().join(",");
    if (sig !== [...enabled].sort().join(",")) { enabled.clear(); (r.enabled_strategies || []).forEach((c) => enabled.add(c)); renderStrategies(); }
  }
  renderEngine(store.snapshot);
  scope.on("snapshot", renderEngine);

  async function post(path, body, success) {
    try {
      const res = await api.post(path, body);
      if (success) toast({ title: success, level: "success" });
      return res;
    } catch (err) {
      toastError(err);
      return null;
    }
  }
}

function setting(title, desc, control) {
  return h("div", { class: "setting" }, h("div", { class: "setting-text" }, h("div", { class: "setting-title" }, title), h("div", { class: "setting-desc" }, desc)), control);
}

function kv(key, value) {
  return h("div", { class: "kv" }, h("span", { class: "kv-key" }, key), h("span", { class: "kv-val small" }, value));
}


