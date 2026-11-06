// Tiny app store: bootstrap config, live snapshot, notifications and persisted preferences.

const PREF_KEY = "qst.prefs.v2";
const DEFAULT_PREFS = {
  theme: "dark",
  density: "comfortable",
  tz: null,
  sound: false,
  desktop: false,
  sidebar: "expanded",
  lastReadEventId: 0,
  chart: { symbol: null, tf: null, overlays: { ema: true, volume: true, fvg: true, ob: true, sessions: true, signals: true, levels: true, markers: true } },
  ticket: { risk: null, slMode: "atr", slValue: 1.5, slPips: 50, tp1: null, tp2: null },
  tools: { active: "position" },
};

function readPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
    return deepMerge(structuredClone(DEFAULT_PREFS), raw && typeof raw === "object" ? raw : {});
  } catch {
    return structuredClone(DEFAULT_PREFS);
  }
}

function deepMerge(target, source) {
  for (const [key, value] of Object.entries(source)) {
    if (value && typeof value === "object" && !Array.isArray(value) && target[key] && typeof target[key] === "object") {
      deepMerge(target[key], value);
    } else {
      target[key] = value;
    }
  }
  return target;
}

export const store = {
  bootstrap: null,
  snapshot: null,
  events: [],
  connection: "connecting",
  prefs: readPrefs(),
  lastSnapshotAt: 0,
};

const listeners = new Map();

export function on(topic, handler) {
  let set = listeners.get(topic);
  if (!set) listeners.set(topic, (set = new Set()));
  set.add(handler);
  return () => set.delete(handler);
}

export function emit(topic, payload) {
  const set = listeners.get(topic);
  if (!set) return;
  for (const handler of [...set]) {
    try { handler(payload); } catch (err) { console.error(`[store] ${topic} listener failed`, err); }
  }
}

export function setSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  store.snapshot = snapshot;
  store.lastSnapshotAt = Date.now();
  emit("snapshot", snapshot);
}

export function setConnection(state) {
  if (store.connection === state) return;
  store.connection = state;
  emit("connection", state);
}

export function addEvents(items, { silent = false } = {}) {
  if (!Array.isArray(items) || !items.length) return [];
  const known = new Set(store.events.map((e) => e.id));
  const fresh = items.filter((e) => e && !known.has(e.id));
  if (!fresh.length) return [];
  store.events = [...store.events, ...fresh].sort((a, b) => a.id - b.id).slice(-250);
  emit("events", { items: store.events, fresh: silent ? [] : fresh });
  return fresh;
}

export function unreadCount() {
  const last = store.prefs.lastReadEventId || 0;
  return store.events.filter((e) => e.id > last).length;
}

export function markAllRead() {
  const last = store.events.length ? store.events[store.events.length - 1].id : 0;
  setPref("lastReadEventId", Math.max(store.prefs.lastReadEventId || 0, last));
  emit("events", { items: store.events, fresh: [] });
}

export function setPref(path, value) {
  const keys = path.split(".");
  let node = store.prefs;
  for (const key of keys.slice(0, -1)) {
    if (!node[key] || typeof node[key] !== "object") node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
  try { localStorage.setItem(PREF_KEY, JSON.stringify(store.prefs)); } catch { /* private mode */ }
  emit("prefs", store.prefs);
}

export function quote(symbol) {
  return store.snapshot?.symbols?.[symbol] || null;
}

export function digitsOf(symbol, fallback = 5) {
  const q = quote(symbol);
  return Number.isInteger(q?.digits) ? q.digits : fallback;
}

export function midPrice(symbol) {
  const q = quote(symbol);
  if (!q || q.bid == null || q.ask == null) return null;
  return (q.bid + q.ask) / 2;
}

// Price used for live candles: bid on MT5 (broker charts are bid-based), mid on paper feeds.
export function chartPrice(symbol) {
  const q = quote(symbol);
  if (!q || q.bid == null) return null;
  return store.snapshot?.mode === "MT5" || q.ask == null ? q.bid : (q.bid + q.ask) / 2;
}

export function currency() {
  return store.snapshot?.account?.currency || store.bootstrap?.paper?.currency || "USD";
}
