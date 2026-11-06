// Shared actions used by the shell and by pages (kept out of app.js so pages never import the entry module).

import { api } from "./api.js";
import { emit, setPref, store } from "./store.js";
import { confirmDialog, toast, toastError } from "./ui.js";
import { wallClock } from "./format.js";

export const router = { navigate: (url) => { location.href = url; } };

export function resolvedTheme() {
  const pref = store.prefs.theme;
  if (pref === "system") return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  return pref === "light" ? "light" : "dark";
}

export function applyTheme() {
  const theme = resolvedTheme();
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.setAttribute("data-density", store.prefs.density === "compact" ? "compact" : "comfortable");
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#f3f5f9" : "#090c12");
  emit("theme", theme);
}

export function setTheme(theme) {
  setPref("theme", theme);
  applyTheme();
}

export function setDensity(density) {
  setPref("density", density);
  applyTheme();
}

export function fxMarketOpen(now = new Date()) {
  const ny = wallClock(now, "America/New_York");
  if (ny.weekday === 6) return false;
  if (ny.weekday === 5 && ny.h >= 17) return false;
  if (ny.weekday === 0 && ny.h < 17) return false;
  return true;
}

export async function toggleBot() {
  const enabled = store.snapshot?.runtime?.bot_enabled;
  try {
    const res = await api.post(enabled ? "/api/bot/pause" : "/api/bot/start", {});
    toast({ title: enabled ? "Bot paused" : "Bot started", message: enabled ? "No new entries — open positions stay protected." : `Engine status: ${res.status}`, level: enabled ? "warning" : "success" });
    return res;
  } catch (err) {
    toastError(err);
    return null;
  }
}

export async function panicClose() {
  const positions = store.snapshot?.positions?.length || 0;
  const orders = store.snapshot?.orders?.length || 0;
  const ok = await confirmDialog({
    title: "Panic close everything?",
    message: `This immediately closes ${positions} open position(s), cancels ${orders} pending order(s) and pauses the bot.`,
    confirmLabel: "Close everything", tone: "danger", requireText: "CLOSE ALL",
  });
  if (!ok) return null;
  try {
    const res = await api.post("/api/bot/panic", { confirm: true });
    toast({ title: "Panic close executed", message: `Closed ${res.closed}, cancelled ${res.cancelled}, failed ${res.failed}.`, level: res.failed ? "warning" : "success" });
    return res;
  } catch (err) {
    toastError(err, "Panic close failed");
    return null;
  }
}

export async function closePosition(position) {
  const ok = await confirmDialog({
    title: `Close ${position.canonical || position.symbol} #${position.ticket}?`,
    message: `${position.direction} ${position.volume} lots — the position is closed at market.`,
    confirmLabel: "Close position", tone: "danger",
  });
  if (!ok) return false;
  try {
    await api.post(`/api/positions/${position.ticket}/close`, {});
    toast({ title: "Close order sent", message: `#${position.ticket} closed at market`, level: "success" });
    return true;
  } catch (err) {
    toastError(err, "Close failed");
    return false;
  }
}

export async function breakeven(position) {
  try {
    const res = await api.post(`/api/positions/${position.ticket}/breakeven`, {});
    toast({ title: "Stop moved to breakeven", message: `#${position.ticket} SL → ${res.sl}`, level: "success" });
    return true;
  } catch (err) {
    toastError(err, "Breakeven not applied");
    return false;
  }
}

export async function cancelOrder(order) {
  const ok = await confirmDialog({ title: `Cancel order #${order.ticket}?`, message: `${order.direction} ${order.kind} ${order.volume} lots @ ${order.price}`, confirmLabel: "Cancel order", tone: "warn" });
  if (!ok) return false;
  try {
    await api.post(`/api/orders/${order.ticket}/cancel`, {});
    toast({ title: "Order cancelled", message: `#${order.ticket}`, level: "success" });
    return true;
  } catch (err) {
    toastError(err, "Cancel failed");
    return false;
  }
}
