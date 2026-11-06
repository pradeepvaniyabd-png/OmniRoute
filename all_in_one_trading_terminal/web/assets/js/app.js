// App shell: sidebar, topbar, ticker, banners, notifications, command palette and the router.

import { api } from "./api.js";
import { applyTheme, fxMarketOpen, panicClose, resolvedTheme, router, setTheme, toggleBot } from "./actions.js";
import { live } from "./live.js";
import { addEvents, chartPrice, markAllRead, on, setPref, setSnapshot, store, unreadCount } from "./store.js";
import { fmtCountdown, fmtPct, fmtPrice, fmtRelative, fmtTime, setCurrency, setDisplayTz, toDate, tzLabel, wallClock } from "./format.js";
import { Scope, badge, beep, errorState, flash, h, icon, menu, openModal, popover, replace, setText, sparkline, symbolIcon, toast, toastError } from "./ui.js";

export const PAGES = [
  { id: "dashboard", path: "/dashboard", label: "Dashboard", icon: "dashboard", group: "Trading", sub: "Account, risk and market overview", load: () => import("./pages/dashboard.js") },
  { id: "trade", path: "/trade", label: "Trade Terminal", icon: "candles", group: "Trading", sub: "Live chart, order ticket and positions", load: () => import("./pages/trade.js") },
  { id: "signals", path: "/signals", label: "Signals", icon: "signal", group: "Trading", sub: "SMC / ICT setups detected by the engine", load: () => import("./pages/signals.js") },
  { id: "analytics", path: "/analytics", label: "Analytics", icon: "analytics", group: "Insights", sub: "Performance, drawdown and trade journal", load: () => import("./pages/analytics.js") },
  { id: "calendar", path: "/calendar", label: "Economic Calendar", icon: "calendar", group: "Insights", sub: "Forex Factory events and news kill-switch", load: () => import("./pages/calendar.js") },
  { id: "tools", path: "/tools", label: "Trading Tools", icon: "tools", group: "Insights", sub: "Calculators and market intelligence", load: () => import("./pages/tools.js") },
  { id: "telegram", path: "/telegram", label: "Telegram", icon: "send", group: "System", sub: "VIP channel broadcaster", load: () => import("./pages/telegram.js") },
  { id: "settings", path: "/settings", label: "Settings", icon: "sliders", group: "System", sub: "Engine, risk and display preferences", load: () => import("./pages/settings.js") },
  { id: "logs", path: "/logs", label: "System Logs", icon: "terminal", group: "System", sub: "Live engine log stream", load: () => import("./pages/logs.js") },
];
const NOT_FOUND = { id: "notfound", path: null, label: "Not found", icon: "info", sub: "", load: () => import("./pages/notfound.js") };

const SESSIONS = [
  { name: "Sydney", tz: "Australia/Sydney", open: 7, close: 16 },
  { name: "Tokyo", tz: "Asia/Tokyo", open: 9, close: 18 },
  { name: "London", tz: "Europe/London", open: 8, close: 17 },
  { name: "New York", tz: "America/New_York", open: 8, close: 17 },
];

const els = {};
let current = null;
let renderToken = 0;
let nextNews = null;

// ------------------------------------------------------------------ shell --
function buildShell() {
  const app = document.getElementById("app");
  app.dataset.sidebar = store.prefs.sidebar === "collapsed" ? "collapsed" : "expanded";
  const groups = [...new Set(PAGES.map((p) => p.group))];
  els.navItems = new Map();
  const nav = h("nav", { class: "nav", "aria-label": "Main" }, groups.map((group) => h("div", { class: "nav-group" },
    h("div", { class: "nav-label" }, group),
    PAGES.filter((p) => p.group === group).map((p) => {
      const badgeEl = h("span", { class: "nav-badge", hidden: true });
      const link = h("a", { class: "nav-item", href: p.path, "data-link": "", title: p.label },
        h("span", { class: "nav-icon" }, icon(p.icon, 18)), h("span", { class: "nav-text" }, p.label), badgeEl,
        h("span", { class: "nav-kbd" }, `Alt ${PAGES.indexOf(p) + 1}`));
      link.badgeEl = badgeEl;
      els.navItems.set(p.id, link);
      return link;
    }))));

  els.botDot = h("span", { class: "dot" });
  els.botState = h("span", null, "Starting");
  els.botReason = h("div", { class: "bot-reason" }, "Connecting to the engine…");
  els.modeBadge = badge(store.bootstrap?.mode || "—", store.bootstrap?.mode === "MT5" ? "down" : "brand", { sm: true });
  els.botToggle = h("button", { class: "btn btn-sm btn-success btn-block", onClick: toggleBot }, icon("play", 14), "Start bot");
  const panic = h("button", { class: "btn btn-sm btn-danger btn-icon", title: "Panic close all", "aria-label": "Panic close all", onClick: panicClose }, icon("power", 15));
  const botCard = h("div", { class: "bot-card" },
    h("div", { class: "bot-card-head" }, h("span", { class: "bot-state" }, els.botDot, els.botState), els.modeBadge),
    els.botReason, h("div", { class: "row" }, els.botToggle, panic));
  els.miniBot = h("button", { class: "icon-btn", title: "Bot status", onClick: toggleBot }, h("span", { class: "dot" }));
  const collapse = h("button", { class: "sidebar-collapse", onClick: toggleSidebar, title: "Collapse sidebar" }, icon("chevronsLeft", 16), h("span", { class: "collapse-text" }, "Collapse"));

  const sidebar = h("aside", { class: "sidebar", id: "sidebar" },
    h("a", { class: "sidebar-brand", href: "/dashboard", "data-link": "" },
      h("span", { class: "brand-mark" }, icon("logo", 20)),
      h("span", { class: "brand-text" }, h("span", { class: "brand-name" }, "Quant Super-Terminal"), h("span", { class: "brand-tag" }, "SMC · ICT · Algo Execution"))),
    nav,
    h("div", { class: "sidebar-foot" }, botCard, h("div", { class: "mini-bot" }, els.miniBot), collapse));

  els.title = h("h1", { class: "page-title" }, "Dashboard");
  els.sub = h("div", { class: "page-sub" }, "");
  els.newsChip = h("button", { class: "news-chip calm", onClick: () => navigate("/calendar") }, icon("calendar", 14), h("span", null, "Calendar"));
  els.sessions = h("div", { class: "sessions" }, SESSIONS.map((s) => {
    const chip = h("span", { class: "session-chip", title: `${s.name} session (${s.open}:00–${s.close}:00 local)` }, h("span", { class: "dot" }), s.name === "New York" ? "NY" : s.name);
    s.el = chip;
    return chip;
  }));
  els.clockTime = h("span", { class: "clock-time" }, "--:--:--");
  els.clockZone = h("span", { class: "clock-zone" }, tzLabel());
  els.conn = h("div", { class: "conn-pill", title: "Live connection" }, h("span", { class: "dot" }), h("span", null, "Connecting"));
  els.themeBtn = h("button", { class: "icon-btn", title: "Toggle theme", "aria-label": "Toggle theme", onClick: toggleTheme });
  els.bellCount = h("span", { class: "bell-count", hidden: true });
  els.bell = h("button", { class: "icon-btn", title: "Notifications", "aria-label": "Notifications", onClick: openNotifications }, icon("bell", 18), els.bellCount);
  els.userBtn = h("button", { class: "icon-btn", title: "Account", "aria-label": "Account menu", onClick: openUserMenu }, icon("user", 18));
  const topbar = h("header", { class: "topbar" },
    h("button", { class: "icon-btn menu-toggle", "aria-label": "Open menu", onClick: () => toggleDrawer(true) }, icon("menu", 20)),
    h("div", { class: "topbar-left" }, h("div", { class: "page-heading" }, els.title, els.sub)),
    h("button", { class: "topbar-search", onClick: openPalette, "aria-label": "Search" }, icon("search", 15), h("span", null, "Search…"), h("span", { class: "kbd" }, "Ctrl K")),
    h("div", { class: "topbar-right" }, els.newsChip, els.sessions, h("div", { class: "clock" }, els.clockTime, els.clockZone), els.conn, els.themeBtn, els.bell, els.userBtn));

  els.ticks = new Map();
  els.ticker = h("div", { class: "ticker", role: "list" }, (store.bootstrap?.symbols || []).map((symbol) => {
    const price = h("span", { class: "tick-price" }, "—");
    const chg = h("span", { class: "tick-chg" }, "");
    const spark = h("span", { class: "tick-spark" });
    const lock = h("span", { class: "tick-lock", hidden: true, title: "News lock" }, icon("lock", 13));
    const el = h("a", { class: "tick", href: `/trade?symbol=${symbol}`, "data-link": "", role: "listitem" },
      symbolIcon(symbol), h("span", { class: "tick-sym" }, symbol), price, chg, spark, lock);
    els.ticks.set(symbol, { el, price, chg, spark, lock, last: null });
    return el;
  }));
  els.ticker.querySelectorAll(".sym-icon").forEach((n) => { n.style.width = "22px"; n.style.height = "22px"; n.style.fontSize = "8.5px"; n.style.borderRadius = "6px"; });

  els.banners = h("div", { class: "banners", id: "banners" });
  els.view = h("main", { class: "content", id: "view", tabindex: "-1" });
  const year = new Date().getFullYear();
  const footer = h("footer", { class: "footer" },
    h("span", null, `© ${year} Quant Super-Terminal · v${store.bootstrap?.version || ""} · ${store.bootstrap?.mode === "MT5" ? "Live MT5" : "Paper trading"}`),
    h("span", null, "Charts by ", h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer" }, "TradingView Lightweight Charts™"),
      " · Trading involves substantial risk. Not financial advice."));
  const main = h("div", { class: "main" }, topbar, els.ticker, els.banners, els.view, footer);
  els.scrim = h("div", { class: "drawer-scrim", hidden: true, onClick: () => toggleDrawer(false) });
  app.replaceChildren(sidebar, main, els.scrim);
  els.app = app;
  applyTheme();
  renderThemeButton();
}

function toggleSidebar() {
  const collapsed = els.app.dataset.sidebar !== "collapsed";
  els.app.dataset.sidebar = collapsed ? "collapsed" : "expanded";
  setPref("sidebar", collapsed ? "collapsed" : "expanded");
  setTimeout(() => window.dispatchEvent(new Event("resize")), 260);
}

function toggleDrawer(open) {
  els.app.classList.toggle("drawer-open", open);
  els.scrim.hidden = !open;
}

function toggleTheme() {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}

function renderThemeButton() {
  if (els.themeBtn) replace(els.themeBtn, icon(resolvedTheme() === "light" ? "moon" : "sun", 18));
}

// ------------------------------------------------------------ live updates --
const BOT_TONES = { RUNNING: "dot-up", PAUSED: "dot-warn", "NEWS LOCKED": "dot-warn", HALTED: "dot-down", DISCONNECTED: "dot-down", STARTING: "dot-info" };

function renderSnapshot(s) {
  if (s.account?.currency) setCurrency(s.account.currency);
  const status = s.bot_status || "STARTING";
  els.botDot.className = `dot ${BOT_TONES[status] || ""} ${status === "RUNNING" ? "pulse" : ""}`;
  els.miniBot.firstChild.className = els.botDot.className;
  els.miniBot.title = `Bot ${status} — ${s.status_reason || ""}`;
  setText(els.botState, status.charAt(0) + status.slice(1).toLowerCase());
  setText(els.botReason, s.status_reason || "");
  const enabled = Boolean(s.runtime?.bot_enabled);
  if (els.botToggle.dataset.enabled !== String(enabled)) {
    els.botToggle.dataset.enabled = String(enabled);
    els.botToggle.className = `btn btn-sm btn-block ${enabled ? "btn-warn" : "btn-success"}`;
    replace(els.botToggle, icon(enabled ? "pause" : "play", 14), enabled ? "Pause bot" : "Start bot");
  }
  for (const [symbol, t] of els.ticks) {
    const q = s.symbols?.[symbol] || {};
    const price = chartPrice(symbol);
    if (price !== null) {
      if (t.last !== null && price !== t.last) flash(t.el, price > t.last ? "up" : "down");
      t.last = price;
      setText(t.price, fmtPrice(price, q.digits ?? 5));
    }
    const chg = q.change_24h_pct;
    setText(t.chg, chg === null || chg === undefined ? "" : fmtPct(chg));
    t.chg.className = `tick-chg ${chg > 0 ? "up" : chg < 0 ? "down" : "dim"}`;
    t.lock.hidden = !s.news_locks?.[symbol]?.active;
  }
  const lockCount = Object.values(s.news_locks || {}).filter((l) => l.active).length;
  const calBadge = els.navItems.get("calendar")?.badgeEl;
  if (calBadge) { calBadge.hidden = !lockCount; setText(calBadge, lockCount ? `${lockCount} lock` : ""); }
  const posBadge = els.navItems.get("trade")?.badgeEl;
  if (posBadge) {
    const n = (s.positions || []).length;
    posBadge.hidden = !n;
    posBadge.style.background = "var(--brand-soft)";
    posBadge.style.color = "var(--brand)";
    setText(posBadge, n ? String(n) : "");
  }
  renderBanners(s);
  renderConnection();
}

let bannerSig = "";
function renderBanners(s) {
  const items = [];
  if (store.connection === "offline") items.push({ tone: "warning", icon: "wifiOff", title: "Live connection lost.", text: "Showing the last known data — reconnecting automatically." });
  if (s && s.broker_connected === false) items.push({ tone: "danger", icon: "alert", title: "Broker disconnected.", text: s.broker_error || "Reconnecting automatically…" });
  if (s?.drawdown?.halted) items.push({ tone: "danger", icon: "shield", title: "Daily loss limit reached.", text: s.drawdown.halt_reason || "New trades are blocked until the next trading day." });
  const locks = Object.entries(s?.news_locks || {}).filter(([, l]) => l.active);
  if (locks.length) items.push({ tone: "warning", icon: "lock", title: "News kill-switch active:", text: locks.map(([sym, l]) => `${sym} — ${l.label || l.event || "high-impact news"}`).join(" · "), link: "/calendar" });
  const sig = JSON.stringify(items);
  if (sig === bannerSig) return;
  bannerSig = sig;
  replace(els.banners, items.map((b) => h("div", { class: `banner ${b.tone}`, role: "status" }, icon(b.icon, 16),
    h("span", { class: "banner-text" }, h("b", null, b.title), " ", b.text),
    b.link ? h("a", { class: "btn btn-sm btn-ghost", href: b.link, "data-link": "" }, "View") : null)));
}

const CONN = {
  connecting: ["dot-info", "Connecting"], live: ["dot-up pulse", "Live"], reconnecting: ["dot-warn", "Reconnecting"],
  stale: ["dot-warn", "Stale"], polling: ["dot-warn", "Polling"], offline: ["dot-down", "Offline"],
};
function renderConnection() {
  const [cls, label] = CONN[store.connection] || CONN.connecting;
  const ping = store.snapshot?.ping_ms;
  els.conn.firstChild.className = `dot ${cls}`;
  setText(els.conn.lastChild, store.connection === "live" && ping !== null && ping !== undefined ? `${label} · ${Math.round(ping)}ms` : label);
  els.conn.title = store.connection === "live" ? "Streaming live data over WebSocket" : "Live connection is not streaming";
}

function tickClock() {
  const now = new Date();
  setText(els.clockTime, fmtTime(now, { seconds: true }));
  setText(els.clockZone, tzLabel(store.prefs.tz));
  const fxOpen = fxMarketOpen(now);
  for (const s of SESSIONS) {
    const w = wallClock(now, s.tz);
    const minutes = w.h * 60 + w.m;
    const open = fxOpen && minutes >= s.open * 60 && minutes < s.close * 60;
    s.el.classList.toggle("open", open);
    s.el.firstChild.className = `dot ${open ? "dot-up" : ""}`;
  }
  renderNewsChip(now);
}

function renderNewsChip(now) {
  const locks = Object.entries(store.snapshot?.news_locks || {}).filter(([, l]) => l.active);
  if (locks.length) {
    els.newsChip.className = "news-chip";
    replace(els.newsChip, icon("lock", 14), h("span", { class: "ellipsis" }, `News lock · ${locks.map(([s]) => s).join(", ")}`));
    return;
  }
  const event = nextNews;
  const t = event ? toDate(event.time_utc) : null;
  if (!t || t.getTime() < now.getTime() - 60000) {
    els.newsChip.className = "news-chip calm";
    replace(els.newsChip, icon("calendar", 14), h("span", null, "No high-impact news soon"));
    return;
  }
  const seconds = (t.getTime() - now.getTime()) / 1000;
  els.newsChip.className = `news-chip ${seconds > 3600 ? "calm" : ""}`;
  replace(els.newsChip, icon("flame", 14), h("span", { class: "ellipsis" }, `${event.currency} ${event.title} · ${seconds > 0 ? fmtCountdown(seconds) : "now"}`));
}

async function refreshNextNews() {
  try {
    const cal = await api.get("/api/calendar", { timeout: 15000 });
    const now = Date.now();
    nextNews = (cal.events || []).filter((e) => e.impact === "High" && e.time_utc && new Date(e.time_utc).getTime() > now - 60000)
      .sort((a, b) => new Date(a.time_utc) - new Date(b.time_utc))[0] || null;
  } catch { /* keep previous */ }
}

// ---------------------------------------------------------- notifications --
const EVENT_ICONS = { trade: "zap", signal: "signal", risk: "shield", news: "calendar", bot: "activity", broker: "wifi" };

function renderBell() {
  const n = unreadCount();
  els.bellCount.hidden = n === 0;
  setText(els.bellCount, n > 99 ? "99+" : String(n));
}

function eventRow(e, unread) {
  const level = e.level === "danger" ? "danger" : e.level === "warning" ? "warning" : e.level === "success" ? "success" : "info";
  return h("div", { class: `notif-item ${unread ? "unread" : ""}` },
    h("span", { class: `feed-icon ${level}` }, icon(EVENT_ICONS[e.kind] || "info", 15)),
    h("div", { class: "feed-body" }, h("div", { class: "row-between" }, h("span", { class: "feed-title" }, e.title), h("span", { class: "feed-time" }, fmtRelative(e.ts))),
      e.detail ? h("div", { class: "feed-detail" }, e.detail) : null));
}

function openNotifications() {
  const last = store.prefs.lastReadEventId || 0;
  const items = [...store.events].reverse().slice(0, 60);
  const list = h("div", { class: "notif-list" }, items.length ? items.map((e) => eventRow(e, e.id > last))
    : h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon("bell", 20)), h("div", { class: "empty-title" }, "You're all caught up"), h("div", { class: "empty-text" }, "Trades, signals, risk alerts and news locks appear here.")));
  const content = h("div", null, h("div", { class: "popover-head" }, h("span", { class: "popover-title" }, "Notifications"),
    h("a", { class: "btn btn-xs btn-ghost", href: "/logs", "data-link": "" }, "Open logs")), list);
  popover(els.bell, content, { className: "notif-panel" });
  markAllRead();
}

function handleFreshEvents(fresh) {
  for (const e of fresh.slice(-3)) {
    const level = ["success", "danger", "warning"].includes(e.level) ? e.level : "info";
    toast({ title: e.title, message: e.detail, level, timeout: level === "danger" ? 9000 : 5500 });
    if (store.prefs.sound) beep(level);
    if (store.prefs.desktop && document.hidden && "Notification" in window && Notification.permission === "granted") {
      try { new Notification(e.title, { body: e.detail || "", tag: `qst-${e.id}`, icon: "/favicon.svg" }); } catch { /* blocked */ }
    }
  }
}

function openUserMenu() {
  const items = [
    { label: store.bootstrap?.mode === "MT5" ? "Live MT5 account" : "Paper trading account", icon: "wallet", onClick: () => navigate("/dashboard") },
    { label: "Settings", icon: "sliders", onClick: () => navigate("/settings") },
    { label: "Keyboard shortcuts", icon: "grid", onClick: showShortcuts },
  ];
  if (store.bootstrap?.auth_enabled) {
    items.push("-", { label: "Sign out", icon: "logout", danger: true, onClick: logout });
  }
  menu(els.userBtn, items);
}

async function logout() {
  try { await api.post("/api/logout", {}); } catch { /* ignore */ }
  location.href = "/login";
}

function showShortcuts() {
  const rows = [["Ctrl / ⌘ + K", "Command palette"], ["/", "Command palette"], ["Alt + 1…9", "Jump to a page"], ["Esc", "Close dialogs"], ["B / S", "Buy / sell side (Trade Terminal)"]];
  openModal({ title: "Keyboard shortcuts", iconName: "grid", body: h("div", null, rows.map(([k, v]) => h("div", { class: "kv" }, h("span", { class: "kv-key" }, v), h("span", { class: "kbd" }, k)))),
    actions: [{ label: "Close", variant: "btn-primary" }] });
}

// -------------------------------------------------------- command palette --
function paletteCommands() {
  const cmds = PAGES.map((p, i) => ({ group: "Pages", label: p.label, icon: p.icon, hint: `Alt ${i + 1}`, keywords: p.sub, run: () => navigate(p.path) }));
  for (const symbol of store.bootstrap?.symbols || []) {
    cmds.push({ group: "Symbols", label: `${symbol} chart`, icon: "candles", hint: "Trade", keywords: symbol, run: () => navigate(`/trade?symbol=${symbol}`) });
  }
  const enabled = store.snapshot?.runtime?.bot_enabled;
  cmds.push(
    { group: "Actions", label: enabled ? "Pause bot" : "Start bot", icon: enabled ? "pause" : "play", run: toggleBot },
    { group: "Actions", label: "Toggle dark / light theme", icon: "sun", run: toggleTheme },
    { group: "Actions", label: "Refresh economic calendar", icon: "refresh", run: async () => {
      try { await api.post("/api/news/refresh", {}); toast({ title: "Calendar refreshed", level: "success" }); refreshNextNews(); } catch (err) { toastError(err); }
    } },
    { group: "Actions", label: "Send Telegram test signal", icon: "send", run: async () => {
      try { await api.post("/api/telegram/test-signal", {}); toast({ title: "Test signal queued", level: "success" }); } catch (err) { toastError(err); }
    } },
    { group: "Actions", label: "Panic close all positions", icon: "power", run: panicClose },
  );
  if (store.bootstrap?.auth_enabled) cmds.push({ group: "Actions", label: "Sign out", icon: "logout", run: logout });
  return cmds;
}

function openPalette() {
  if (document.querySelector(".palette")) return;
  const commands = paletteCommands();
  const input = h("input", { class: "palette-input", placeholder: "Type a page, symbol or action…", spellcheck: "false", autocomplete: "off" });
  const list = h("div", { class: "palette-list", role: "listbox" });
  let filtered = commands;
  let active = 0;
  const close = () => backdrop.remove();
  const run = (cmd) => { close(); cmd?.run(); };
  const draw = () => {
    const groups = new Map();
    filtered.forEach((c, i) => { if (!groups.has(c.group)) groups.set(c.group, []); groups.get(c.group).push([c, i]); });
    replace(list, filtered.length ? [...groups].map(([group, rows]) => [h("div", { class: "palette-group" }, group), rows.map(([c, i]) => {
      const item = h("div", { class: `palette-item ${i === active ? "active" : ""}`, role: "option", onMousedown: (e) => { e.preventDefault(); run(c); }, onMousemove: () => { if (active !== i) { active = i; draw(); } } },
        icon(c.icon, 16), c.label, c.hint ? h("span", { class: "hint" }, c.hint) : null);
      return item;
    })]) : h("div", { class: "empty" }, h("div", { class: "empty-title" }, "No matches"), h("div", { class: "empty-text" }, "Try a symbol like XAUUSD or a page name.")));
    list.querySelector(".palette-item.active")?.scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    filtered = !q ? commands : commands.filter((c) => `${c.label} ${c.keywords || ""} ${c.group}`.toLowerCase().includes(q));
    active = 0;
    draw();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(filtered.length - 1, active + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); run(filtered[active]); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  const palette = h("div", { class: "palette", role: "dialog", "aria-label": "Command palette" },
    h("div", { class: "palette-search" }, icon("search", 18), input, h("span", { class: "kbd" }, "Esc")), list,
    h("div", { class: "palette-foot" }, h("span", null, "↑↓ navigate"), h("span", null, "↵ open"), h("span", null, "Esc close")));
  const backdrop = h("div", { class: "modal-backdrop palette-backdrop" }, palette);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  (document.getElementById("overlay-root") || document.body).append(backdrop);
  draw();
  input.focus();
}

// ------------------------------------------------------------------ router --
export async function navigate(url, { replace: replaceState = false } = {}) {
  const target = new URL(url, location.origin);
  if (target.origin !== location.origin) { location.href = url; return; }
  const next = target.pathname + target.search + target.hash;
  const now = location.pathname + location.search + location.hash;
  if (replaceState) history.replaceState({}, "", next);
  else if (next !== now) history.pushState({}, "", next);
  toggleDrawer(false);
  await renderRoute();
}

function pageFor(pathname) {
  const path = pathname === "/" ? "/dashboard" : pathname.replace(/\/+$/, "");
  return PAGES.find((p) => p.path === path) || NOT_FOUND;
}

async function renderRoute() {
  const page = pageFor(location.pathname);
  const sameModule = current && current.page.id === page.id && current.module?.update;
  if (sameModule) {
    try { current.module.update(new URLSearchParams(location.search)); return; } catch (err) { console.warn(err); }
  }
  if (current) {
    try { current.module?.unmount?.(); } catch (err) { console.warn(err); }
    current.scope.dispose();
    current = null;
  }
  const token = ++renderToken;
  for (const [id, link] of els.navItems) link.classList.toggle("active", id === page.id);
  setText(els.title, page.label);
  setText(els.sub, page.sub || "");
  document.title = `${page.label} · Quant Super-Terminal`;
  els.view.replaceChildren(h("div", { class: "stack" }, h("div", { class: "skeleton", style: { height: "92px" } }), h("div", { class: "skeleton", style: { height: "320px" } })));
  let module;
  try {
    module = await page.load();
  } catch (err) {
    console.error(err);
    if (token === renderToken) els.view.replaceChildren(errorState("This page could not be loaded — check that the server is running.", () => renderRoute()));
    return;
  }
  if (token !== renderToken) return;
  const scope = new Scope();
  current = { page, module, scope };
  els.view.replaceChildren();
  els.view.classList.remove("view-enter");
  void els.view.offsetWidth;
  els.view.classList.add("view-enter");
  window.scrollTo({ top: 0 });
  try {
    await module.mount(els.view, { scope, params: new URLSearchParams(location.search), navigate });
  } catch (err) {
    console.error(err);
    if (token === renderToken) els.view.replaceChildren(errorState(`This page failed to render: ${err.message || err}`, () => renderRoute()));
  }
}

function wireGlobalEvents() {
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-link]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target) return;
    const href = link.getAttribute("href");
    if (!href || !href.startsWith("/") || href.startsWith("//")) return;
    event.preventDefault();
    navigate(href);
  });
  window.addEventListener("popstate", () => renderRoute());
  document.addEventListener("keydown", (event) => {
    const typing = event.target.closest?.("input, textarea, select, [contenteditable]");
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); return; }
    if (!typing && event.key === "/" && !document.querySelector(".modal-backdrop")) { event.preventDefault(); openPalette(); return; }
    if (event.altKey && /^[1-9]$/.test(event.key)) {
      const page = PAGES[Number(event.key) - 1];
      if (page) { event.preventDefault(); navigate(page.path); }
    }
  });
  window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => { if (store.prefs.theme === "system") applyTheme(); });
  on("snapshot", renderSnapshot);
  on("connection", () => { renderConnection(); renderBanners(store.snapshot); });
  on("events", ({ fresh }) => { renderBell(); if (fresh?.length) handleFreshEvents(fresh); });
  on("prefs", (prefs) => { setDisplayTz(prefs.tz); });
  on("theme", renderThemeButton);
  setInterval(tickClock, 1000);
  setInterval(refreshNextNews, 120000);
  setInterval(refreshSparklines, 60000);
}

async function refreshSparklines() {
  if (document.hidden) return;
  try {
    const data = await api.get("/api/sparklines?points=60", { timeout: 10000 });
    for (const [symbol, t] of els.ticks) {
      const values = data.series?.[symbol] || [];
      replace(t.spark, values.length > 2 ? sparkline(values, { width: 54, height: 18, fill: false, strokeWidth: 1.4 }) : null);
    }
  } catch { /* optional */ }
}

function showBootError(err) {
  const splash = document.getElementById("boot-splash");
  if (!splash) return;
  replace(splash, h("div", { class: "boot-error" }, h("div", { class: "empty-icon" }, icon("wifiOff", 22)),
    h("div", { class: "empty-title" }, "Cannot reach the trading engine"),
    h("div", null, err?.message || "The server did not respond."),
    h("button", { class: "btn btn-primary", onClick: () => location.reload() }, icon("refresh", 15), "Retry")));
}

async function boot() {
  let bootstrap;
  try {
    bootstrap = await api.get("/api/bootstrap");
  } catch (err) {
    showBootError(err);
    return;
  }
  store.bootstrap = bootstrap;
  if (!store.prefs.tz) setPref("tz", bootstrap.chart?.timezone || "Asia/Kolkata");
  setDisplayTz(store.prefs.tz);
  if (bootstrap.paper?.currency) setCurrency(bootstrap.paper.currency);
  buildShell();
  wireGlobalEvents();
  try { setSnapshot(await api.get("/api/snapshot")); } catch { /* the live feed retries */ }
  try {
    const events = await api.get("/api/events?after=0&limit=80");
    addEvents(events.items, { silent: true });
  } catch { /* optional */ }
  renderBell();
  tickClock();
  live.start();
  refreshNextNews();
  refreshSparklines();
  await renderRoute();
  window.addEventListener("error", (e) => console.error("[ui]", e.error || e.message));
  window.addEventListener("unhandledrejection", (e) => console.error("[ui]", e.reason));
}

router.navigate = navigate;
boot();
