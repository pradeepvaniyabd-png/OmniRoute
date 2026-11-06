// Economic calendar: Forex Factory events, impact filters, kill-switch status and live countdowns.

import { api } from "../api.js";
import { dayKey, fmtCountdown, fmtDateTime, fmtDayHeading, fmtRelative, fmtTime, toDate, tzLabel } from "../format.js";
import { store } from "../store.js";
import { badge, card, emptyState, errorState, h, icon, replace, searchInput, segmented, setText, toast, toastError, toggleSwitch } from "../ui.js";
import { impactBars } from "./common.js";

const IMPACTS = ["High", "Medium", "Low", "Holiday"];

export async function mount(root, { scope }) {
  let data = null;
  const filters = { impacts: new Set(["High", "Medium"]), currencies: new Set(), mine: true, q: "", view: "week" };

  const heroCard = card({ title: "Next high-impact event", iconName: "flame", body: h("div") });
  const lockCard = card({ title: "News kill-switch", iconName: "lock", sub: "", body: h("div") });
  const feedCard = card({ title: "Calendar feed", iconName: "database", body: h("div") });
  const refreshBtn = h("button", { class: "btn btn-sm", onClick: refreshFeed }, icon("refresh", 14), "Refresh now");
  feedCard.actions.append(refreshBtn);

  const impactChips = h("div", { class: "chips" }, IMPACTS.map((imp) => {
    const chip = h("button", { class: `chip ${filters.impacts.has(imp) ? "active" : ""}`, type: "button", onClick: () => {
      if (filters.impacts.has(imp)) filters.impacts.delete(imp); else filters.impacts.add(imp);
      chip.classList.toggle("active", filters.impacts.has(imp));
      renderTable();
    } }, impactBars(imp), imp);
    return chip;
  }));
  const currencyChips = h("div", { class: "chips" });
  const mineToggle = toggleSwitch({ checked: filters.mine, label: "Only currencies of my symbols", onChange: (v) => { filters.mine = v; renderTable(); } });
  const viewSeg = segmented([{ value: "today", label: "Today" }, { value: "upcoming", label: "Upcoming" }, { value: "week", label: "This week" }], filters.view,
    (v) => { filters.view = v; renderTable(); }, { sm: true, label: "Range" });
  const search = searchInput({ placeholder: "Search events…", onInput: (v) => { filters.q = v.toLowerCase(); renderTable(); } });
  const tableBox = h("div", { class: "table-wrap" });
  const tableCard = card({ title: "Economic calendar", iconName: "calendar", sub: `Times shown in ${tzLabel(store.prefs.tz)}`, flush: true,
    actions: viewSeg,
    body: [h("div", { class: "stack", style: { padding: "0 var(--pad) 12px", gap: "10px" } },
      h("div", { class: "row-wrap gap-12" }, impactChips, h("div", { class: "divider-v" }), mineToggle, h("div", { style: { flex: "1 1 200px", maxWidth: "320px", marginLeft: "auto" } }, search)),
      currencyChips), tableBox] });

  root.append(h("div", { class: "stack" },
    h("div", { class: "grid" }, h("div", { class: "span-5" }, heroCard), h("div", { class: "span-4" }, lockCard), h("div", { class: "span-3" }, feedCard)),
    tableCard));

  // ---------------------------------------------------------------- render --
  function myCurrencies() {
    const set = new Set();
    for (const e of data?.events || []) if (e.affected?.length) set.add(e.currency);
    return set;
  }

  function renderHero() {
    const now = Date.now();
    const next = (data?.events || []).filter((e) => e.impact === "High" && e.time_utc && new Date(e.time_utc).getTime() > now - 60000 && (!filters.mine || e.affected?.length))
      .sort((a, b) => new Date(a.time_utc) - new Date(b.time_utc))[0];
    if (!next) {
      replace(heroCard.body, emptyState({ iconName: "checkCircle", title: "No high-impact events ahead", text: "Nothing red-folder is scheduled for your instruments this week." }));
      return;
    }
    const t = toDate(next.time_utc);
    const big = h("span", { class: "big" }, fmtCountdown((t - now) / 1000));
    big.dataset.t = String(t.getTime());
    big.classList.add("hero-cd");
    replace(heroCard.body, h("div", { class: "stack", style: { gap: "12px" } },
      h("div", { class: "hero-countdown" }, h("div", { class: "kpi-icon", style: { width: "52px", height: "52px", background: "var(--down-soft)", color: "var(--down)" } }, icon("flame", 24)),
        h("div", null, h("div", { class: "tiny dim upper" }, "Starts in"), big)),
      h("div", null, h("div", { class: "strong", style: { fontSize: "15px" } }, `${next.currency} · ${next.title}`),
        h("div", { class: "small muted mt-4" }, `${fmtDateTime(t)} · Forecast ${next.forecast || "—"} · Previous ${next.previous || "—"}`)),
      h("div", { class: "row-wrap" }, (next.affected || []).map((s) => badge(s, "warn", { sm: true })), next.tier1 ? badge("TIER-1", "down", { sm: true }) : null,
        h("span", { class: "small dim" }, `Kill-switch: −${data.window_min?.[0] ?? 15} / +${data.window_min?.[1] ?? 15} min`))));
  }

  function renderLocks() {
    const locks = data?.locks || {};
    const symbols = store.bootstrap?.symbols || [];
    setText(lockCard.sub, store.snapshot?.runtime?.news_auto_pause ? "Auto-pause is ON — new entries blocked during news windows" : "Auto-pause is OFF (Settings)");
    replace(lockCard.body, h("div", { class: "stack", style: { gap: "8px" } }, symbols.map((s) => {
      const l = locks[s] || {};
      return h("div", { class: `lock-chip ${l.active ? "on" : ""}` }, icon(l.active ? "lock" : "unlock", 14), h("span", { class: "strong" }, s),
        h("span", { class: "small muted ellipsis", style: { flex: "1" } }, l.active ? (l.label || l.event || "Locked") : l.event ? `Next: ${l.currency || ""} ${l.event}` : "Clear"));
    })));
  }

  function renderFeed() {
    const st = data?.status || {};
    replace(feedCard.body, h("div", null,
      kv("Source", st.source || "—"), kv("Last fetch", st.fetched_at ? fmtRelative(st.fetched_at) : "—"), kv("Events", String(st.events ?? 0)),
      kv("Next refresh", st.next_refresh_at ? fmtTime(st.next_refresh_at) : "—"),
      st.last_error ? h("div", { class: "info-box warn mt-8" }, icon("alert", 15), h("span", { class: "small" }, st.last_error)) : null));
  }

  function renderCurrencyChips() {
    const all = [...new Set((data?.events || []).map((e) => e.currency))].sort();
    replace(currencyChips, all.length ? [h("span", { class: "tiny dim upper", style: { alignSelf: "center" } }, "Currencies"), ...all.map((c) => {
      const chip = h("button", { class: `chip ${filters.currencies.has(c) ? "active" : ""}`, type: "button", onClick: () => {
        if (filters.currencies.has(c)) filters.currencies.delete(c); else filters.currencies.add(c);
        chip.classList.toggle("active", filters.currencies.has(c));
        renderTable();
      } }, c);
      return chip;
    })] : null);
  }

  function visible(e) {
    if (!filters.impacts.has(e.impact)) return false;
    if (filters.currencies.size && !filters.currencies.has(e.currency)) return false;
    if (filters.mine && !(e.affected?.length) && e.currency !== "ALL") return false;
    if (filters.q && !`${e.title} ${e.currency}`.toLowerCase().includes(filters.q)) return false;
    const t = toDate(e.time_utc);
    const now = Date.now();
    if (filters.view === "today" && dayKey(t || new Date(0)) !== dayKey(new Date())) return false;
    if (filters.view === "upcoming" && (!t || t.getTime() < now - 30 * 60000)) return false;
    return true;
  }

  function renderTable() {
    setText(tableCard.sub, `Times shown in ${tzLabel(store.prefs.tz)} · kill-switch window −${data?.window_min?.[0] ?? 15}/+${data?.window_min?.[1] ?? 15} min`);
    const events = (data?.events || []).filter(visible);
    if (!events.length) { replace(tableBox, emptyState({ iconName: "calendar", title: "No events match", text: "Adjust the impact or currency filters." })); return; }
    const now = Date.now();
    const today = dayKey(new Date());
    const nextHigh = events.find((e) => e.impact === "High" && e.time_utc && new Date(e.time_utc).getTime() > now);
    const tbody = h("tbody");
    let currentDay = null;
    for (const e of events) {
      const t = toDate(e.time_utc);
      const key = t ? dayKey(t) : "tbd";
      if (key !== currentDay) {
        currentDay = key;
        tbody.append(h("tr", null, h("td", { colspan: "9", style: { padding: "0", height: "auto" } },
          h("div", { class: `cal-day ${key === today ? "today" : ""}` }, h("span", null, t ? fmtDayHeading(t) : "Date to be confirmed"), key === today ? h("span", null, "Today") : null))));
      }
      const past = t && t.getTime() < now - 5 * 60000;
      const secs = t ? (t.getTime() - now) / 1000 : null;
      const cd = h("span", { class: "countdown" });
      if (t) cd.dataset.t = String(t.getTime());
      paintCountdown(cd, secs);
      tbody.append(h("tr", { class: `${past ? "past" : ""} ${nextHigh === e ? "next-up" : ""}` },
        h("td", { class: "nowrap" }, h("span", { class: "num" }, e.all_day ? "All day" : t ? fmtTime(t) : e.time_label || "TBD")),
        h("td", null, h("span", { class: "strong" }, e.currency)),
        h("td", null, impactBars(e.impact)),
        h("td", { class: "wrap", style: { minWidth: "240px" } }, h("span", { class: "strong" }, e.title), e.tier1 ? h("span", { style: { marginLeft: "8px" } }, badge("TIER-1", "down", { sm: true })) : null),
        h("td", { class: "right" }, h("span", { class: `num ${actualTone(e)}` }, e.actual || "—")),
        h("td", { class: "right" }, h("span", { class: "num muted" }, e.forecast || "—")),
        h("td", { class: "right" }, h("span", { class: "num dim" }, e.previous || "—")),
        h("td", null, h("div", { class: "row gap-4" }, (e.affected || []).map((s) => badge(s, "outline", { sm: true })))),
        h("td", { class: "right" }, cd)));
    }
    const table = h("table", { class: "table" },
      h("thead", null, h("tr", null, ["Time", "Cur", "Impact", "Event", "Actual", "Forecast", "Previous", "Affects", "Countdown"].map((label, i) => h("th", { class: i >= 4 && i <= 6 || i === 8 ? "right" : "" }, label)))),
      tbody);
    replace(tableBox, table);
  }

  function paintCountdown(el, secs) {
    if (secs === null) { setText(el, "—"); el.className = "countdown"; return; }
    if (secs > 0) { setText(el, fmtCountdown(secs)); el.className = `countdown ${secs < 3600 ? "soon" : ""}`; return; }
    if (secs > -15 * 60) { setText(el, "LIVE"); el.className = "countdown live"; return; }
    setText(el, "Done");
    el.className = "countdown";
  }

  function tick() {
    const now = Date.now();
    root.querySelectorAll(".countdown[data-t]").forEach((el) => paintCountdown(el, (Number(el.dataset.t) - now) / 1000));
    root.querySelectorAll(".hero-cd[data-t]").forEach((el) => setText(el, fmtCountdown(Math.max(0, (Number(el.dataset.t) - now) / 1000))));
  }

  function renderAll() {
    renderHero();
    renderLocks();
    renderFeed();
    renderCurrencyChips();
    renderTable();
  }

  async function load() {
    try {
      data = await api.get("/api/calendar", { signal: scope.signal, timeout: 20000 });
      renderAll();
    } catch (err) {
      if (err.name === "AbortError") return;
      if (!data) replace(tableBox, errorState(`Calendar unavailable: ${err.message}`, load));
    }
  }

  async function refreshFeed() {
    refreshBtn.classList.add("loading");
    try {
      const res = await api.post("/api/news/refresh", {});
      toast({ title: res.refreshed ? "Calendar refreshed" : "Calendar is up to date", message: res.status?.source ? `Source: ${res.status.source}` : "", level: "success" });
      await load();
    } catch (err) {
      toastError(err, "Refresh failed");
    } finally {
      refreshBtn.classList.remove("loading");
    }
  }

  scope.every(60000, load);
  scope.every(1000, tick, { immediate: false });
  scope.on("snapshot", (s) => {
    if (!data) return;
    const sig = JSON.stringify(s.news_locks || {});
    if (sig !== JSON.stringify(data.locks || {})) { data.locks = s.news_locks || {}; renderLocks(); }
  });
  scope.on("prefs", () => { if (data) renderTable(); });
}

function kv(key, value) {
  return h("div", { class: "kv" }, h("span", { class: "kv-key" }, key), h("span", { class: "kv-val small" }, value));
}

function actualTone(e) {
  const parse = (v) => { const n = parseFloat(String(v || "").replace(/[^0-9.+-]/g, "")); return Number.isFinite(n) ? n : null; };
  const a = parse(e.actual);
  const f = parse(e.forecast);
  if (a === null || f === null || a === f) return "";
  return a > f ? "up" : "down";
}
