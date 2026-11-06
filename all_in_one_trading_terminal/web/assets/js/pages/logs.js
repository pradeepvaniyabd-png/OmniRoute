// System logs: live engine log stream with level filter, search, copy and download.

import { api, qs } from "../api.js";
import { dayKey, fmtDateTime } from "../format.js";
import { card, copyText, downloadFile, emptyState, h, icon, replace, searchInput, segmented, setText, toggleSwitch } from "../ui.js";

const LEVELS = [{ value: "", label: "All" }, { value: "INFO", label: "Info+" }, { value: "WARNING", label: "Warnings" }, { value: "ERROR", label: "Errors" }];

export async function mount(root, { scope }) {
  let items = [];
  let level = "";
  let query = "";
  let liveOn = true;
  const view = h("div", { class: "logview", role: "log", "aria-live": "off" });
  const count = h("span", { class: "small dim" });
  const levelSeg = segmented(LEVELS, level, (v) => { level = v; load(); }, { sm: true, label: "Level" });
  const search = searchInput({ placeholder: "Search log messages…", onInput: (v) => { query = v.toLowerCase(); render(); } });
  const liveToggle = toggleSwitch({ checked: liveOn, label: "Live", onChange: (v) => { liveOn = v; } });
  const copyBtn = h("button", { class: "btn btn-sm", onClick: () => copyText(asText(filtered()), "Log copied") }, icon("copy", 14), "Copy");
  const dlBtn = h("button", { class: "btn btn-sm", onClick: () => downloadFile(`terminal-log-${dayKey(new Date())}.txt`, asText(filtered())) }, icon("download", 14), "Download");

  root.append(card({ title: "Engine log", iconName: "terminal", sub: "Newest first — scanner, protector, news, Telegram and web server", flush: true,
    actions: [count, liveToggle],
    body: [h("div", { class: "toolbar", style: { padding: "0 var(--pad) 12px" } }, levelSeg, h("div", { style: { flex: "1 1 260px" } }, search), copyBtn, dlBtn), view] }));

  function filtered() {
    return items.filter((r) => !query || `${r.message} ${r.source || r.logger || ""}`.toLowerCase().includes(query));
  }

  function highlight(text) {
    if (!query) return text;
    const lower = text.toLowerCase();
    const out = [];
    let i = 0;
    while (i < text.length) {
      const j = lower.indexOf(query, i);
      if (j < 0) { out.push(text.slice(i)); break; }
      if (j > i) out.push(text.slice(i, j));
      out.push(h("mark", null, text.slice(j, j + query.length)));
      i = j + query.length;
    }
    return out;
  }

  function render() {
    const rows = filtered();
    setText(count, `${rows.length} lines`);
    if (!rows.length) { replace(view, emptyState({ iconName: "terminal", title: "No log lines", text: query ? "Nothing matches the search." : "Logs appear as the engine runs." })); return; }
    replace(view, rows.map((r) => {
      const lvl = String(r.level || "INFO").toUpperCase();
      return h("div", { class: `log-line ${lvl}` }, h("span", { class: "log-time" }, fmtDateTime(r.ts || r.time, { seconds: true })),
        h("span", { class: `log-level ${lvl}` }, lvl), h("span", { class: "log-src", title: r.source || r.logger || "" }, r.source || r.logger || "—"),
        h("span", { class: "log-msg" }, highlight(String(r.message ?? ""))));
    }));
  }

  async function load() {
    const res = await api.get(`/api/logs${qs({ limit: 800, level: level || null })}`, { signal: scope.signal });
    items = (res.items || []).slice().reverse();
    render();
  }

  scope.every(3000, () => (liveOn ? load() : null));
}

function asText(rows) {
  return rows.map((r) => `${r.ts || r.time || ""} ${String(r.level || "").padEnd(8)} ${String(r.source || r.logger || "").padEnd(18)} ${r.message ?? ""}`).join("\n");
}
