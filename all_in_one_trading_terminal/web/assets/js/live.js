// Live feed: WebSocket push with automatic reconnect and a REST polling fallback.

import { api } from "./api.js";
import { addEvents, setConnection, setSnapshot, store } from "./store.js";

const listeners = { chart: new Set(), chart_tick: new Set() };

class LiveFeed {
  constructor() {
    this.ws = null;
    this.attempt = 0;
    this.reconnectTimer = null;
    this.pollTimer = null;
    this.pingTimer = null;
    this.staleTimer = null;
    this.chartSub = null;
    this.lastMessageAt = 0;
    this.stopped = false;
    this.firstEventsBatch = true;
  }

  start() {
    this.stopped = false;
    this.connect();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && (!this.ws || this.ws.readyState > 1)) this.connect(true);
    });
    window.addEventListener("online", () => this.connect(true));
  }

  connect(immediate = false) {
    if (this.stopped) return;
    clearTimeout(this.reconnectTimer);
    if (this.ws && this.ws.readyState <= 1) return;
    this.reconnectTimer = setTimeout(() => this.open(), immediate ? 0 : 50);
  }

  open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const lastId = store.events.length ? store.events[store.events.length - 1].id : 0;
    let ws;
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws${lastId ? `?after=${lastId}` : ""}`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    setConnection(this.attempt ? "reconnecting" : "connecting");
    ws.onopen = () => {
      this.attempt = 0;
      this.stopPolling();
      setConnection("live");
      if (this.chartSub) this.send({ op: "chart", ...this.chartSub });
      clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => this.send({ op: "ping" }), 20000);
      this.watchStale();
    };
    ws.onmessage = (event) => {
      this.lastMessageAt = Date.now();
      if (store.connection !== "live") setConnection("live");
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      this.route(message);
    };
    ws.onclose = (event) => {
      clearInterval(this.pingTimer);
      if (this.ws === ws) this.ws = null;
      if (event.code === 4401) {
        location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
        return;
      }
      this.scheduleReconnect();
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  route(message) {
    switch (message.type) {
      case "snapshot":
        setSnapshot(message.data);
        break;
      case "events":
        addEvents(message.items, { silent: this.firstEventsBatch && store.events.length === 0 });
        this.firstEventsBatch = false;
        break;
      case "chart":
      case "chart_tick":
        for (const fn of listeners[message.type]) fn(message.data);
        break;
      case "error":
        console.warn("[live]", message.error);
        break;
      default:
        break;
    }
  }

  watchStale() {
    clearInterval(this.staleTimer);
    this.staleTimer = setInterval(() => {
      if (!this.ws || this.ws.readyState !== 1) return;
      if (Date.now() - this.lastMessageAt > 12000) {
        setConnection("stale");
        try { this.ws.close(); } catch { /* ignore */ }
      }
    }, 4000);
  }

  scheduleReconnect() {
    if (this.stopped) return;
    this.attempt += 1;
    setConnection(this.attempt > 2 ? "offline" : "reconnecting");
    if (this.attempt >= 3) this.startPolling();
    const delay = Math.min(15000, 800 * 2 ** Math.min(this.attempt, 5)) + Math.random() * 400;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  startPolling() {
    if (this.pollTimer) return;
    const poll = async () => {
      try {
        setSnapshot(await api.get("/api/snapshot", { timeout: 8000 }));
        const lastId = store.events.length ? store.events[store.events.length - 1].id : 0;
        const events = await api.get(`/api/events?after=${lastId}`, { timeout: 8000 });
        addEvents(events.items, { silent: this.firstEventsBatch && store.events.length === 0 });
        this.firstEventsBatch = false;
        if (store.connection === "offline") setConnection("polling");
        if (this.chartSub) {
          const { symbol, tf, bars } = this.chartSub;
          const chart = await api.get(`/api/chart?symbol=${encodeURIComponent(symbol)}&tf=${tf}&bars=${bars}`, { timeout: 10000 });
          for (const fn of listeners.chart) fn(chart);
        }
      } catch {
        setConnection("offline");
      }
    };
    poll();
    this.pollTimer = setInterval(poll, 3000);
  }

  stopPolling() {
    clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  send(message) {
    if (this.ws && this.ws.readyState === 1) {
      try { this.ws.send(JSON.stringify(message)); return true; } catch { return false; }
    }
    return false;
  }

  subscribeChart(symbol, tf, bars) {
    this.chartSub = { symbol, tf, bars };
    this.send({ op: "chart", symbol, tf, bars });
  }

  unsubscribeChart() {
    this.chartSub = null;
    this.send({ op: "unchart" });
  }
}

export const live = new LiveFeed();

export function onChart(type, fn) {
  listeners[type].add(fn);
  return () => listeners[type].delete(fn);
}
