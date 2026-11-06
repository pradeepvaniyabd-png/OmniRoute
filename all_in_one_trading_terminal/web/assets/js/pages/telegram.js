// Telegram broadcaster: status, credentials, test actions, message preview and broadcast log.

import { api } from "../api.js";
import { fmtDateTime, fmtNum, fmtRelative } from "../format.js";
import { DataTable, badge, card, field, h, icon, kpi, replace, searchInput, statusBadge, toast, toastError } from "../ui.js";

export async function mount(root, { scope }) {
  let data = null;
  let query = "";
  const k = {
    status: kpi({ label: "Broadcaster", iconName: "send", toneName: "brand" }),
    sent: kpi({ label: "Messages sent", iconName: "checkCircle", toneName: "up" }),
    failed: kpi({ label: "Failed", iconName: "xCircle", toneName: "down" }),
    queue: kpi({ label: "Queued", iconName: "clock", toneName: "warn" }),
  };

  const token = h("input", { class: "input mono", type: "password", placeholder: "123456789:AA… (leave empty to keep current)", autocomplete: "off", spellcheck: "false" });
  const channel = h("input", { class: "input mono", placeholder: "@your_vip_channel or -100…", autocomplete: "off", spellcheck: "false" });
  const admin = h("input", { class: "input mono", placeholder: "Your personal chat id", autocomplete: "off", spellcheck: "false" });
  const showToken = h("button", { class: "btn btn-sm btn-ghost btn-icon", type: "button", title: "Show / hide", onClick: () => { token.type = token.type === "password" ? "text" : "password"; } }, icon("eye", 15));
  const credsInfo = h("div", { class: "small dim" });
  const saveBtn = h("button", { class: "btn btn-primary", onClick: save }, icon("check", 15), "Save credentials");
  const testBtn = h("button", { class: "btn", onClick: testConnection }, icon("wifi", 15), "Test connection");
  const testResult = h("div");
  const credsCard = card({ title: "Bot connection", iconName: "key", sub: "Credentials are held in memory for this run — put them in .env to persist",
    body: h("div", { class: "stack", style: { gap: "12px" } }, credsInfo, field("Bot token (from @BotFather)", h("div", { class: "row" }, h("div", { class: "grow" }, token), showToken)),
      h("div", { class: "fields" }, field("VIP channel", channel, { hint: "Bot must be an admin of the channel" }), field("Admin chat", admin, { hint: "Private alerts: breaker, panic, errors" })),
      h("div", { class: "row-wrap" }, saveBtn, testBtn), testResult) });

  const signalBtn = h("button", { class: "btn btn-primary", onClick: () => action("/api/telegram/test-signal", "Test signal queued", signalBtn) }, icon("signal", 15), "Send test signal");
  const digestBtn = h("button", { class: "btn", onClick: () => action("/api/telegram/news-digest", "News digest queued", digestBtn) }, icon("calendar", 15), "Send today's news digest");
  const actionsCard = card({ title: "Broadcast actions", iconName: "zap", body: h("div", { class: "stack", style: { gap: "12px" } },
    h("div", { class: "row-wrap" }, signalBtn, digestBtn),
    h("div", { class: "info-box" }, icon("info", 16), h("div", null, "Automatic broadcasts: A+ setups, executions, TP1 / breakeven / trailing updates (as replies), closes with P&L, the morning red-folder digest and 15-minute pre-news warnings.")),
    h("div", { class: "tiny dim upper" }, "Signal card preview"),
    h("div", { class: "tg-preview" }, previewText())) });

  const log = new DataTable({
    rowKey: (r) => r.id, empty: "No broadcasts yet.", sortBy: "created_at",
    columns: [
      { key: "created_at", label: "Time", render: (r) => h("span", { class: "small nowrap" }, fmtDateTime(r.created_at, { seconds: true })) },
      { key: "category", label: "Type", render: (r) => badge(String(r.category || "—").replaceAll("_", " "), "outline", { sm: true }) },
      { key: "chat_id", label: "Chat", render: (r) => h("span", { class: "mono small dim" }, r.chat_id || "—") },
      { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
      { key: "telegram_message_id", label: "Msg id", align: "right", render: (r) => h("span", { class: "num small" }, r.telegram_message_id ?? "—") },
      { key: "preview", label: "Message", sort: false, render: (r) => h("span", { class: "small ellipsis", style: { maxWidth: "380px", display: "inline-block" }, title: r.preview || "" }, r.preview || "—") },
      { key: "error", label: "Error", sort: false, render: (r) => r.error ? h("span", { class: "small down ellipsis", style: { maxWidth: "240px", display: "inline-block" }, title: r.error }, r.error) : h("span", { class: "dim" }, "—") },
    ],
  });
  const search = searchInput({ placeholder: "Filter log…", onInput: (v) => { query = v.toLowerCase(); renderLog(); } });

  root.append(h("div", { class: "stack" },
    h("div", { class: "kpis k4" }, Object.values(k)),
    h("div", { class: "grid" }, h("div", { class: "span-6" }, credsCard), h("div", { class: "span-6" }, actionsCard)),
    card({ title: "Broadcast log", iconName: "book", sub: "Every message the dispatcher handled", actions: search, body: log.el, flush: true })));

  function render() {
    const s = data?.stats || {};
    const configured = Boolean(s.configured);
    k.status.set({ value: configured ? (s.running ? "Online" : "Stopped") : "Not set up", tone: configured && s.running ? "up" : configured ? "warn" : "down",
      sub: configured ? [s.channel_id ? badge(s.channel_id, "brand", { sm: true }) : h("span", { class: "dim" }, "No channel"), s.token_masked ? h("span", { class: "mono tiny dim" }, s.token_masked) : null] : "Add a bot token and channel" });
    k.sent.set({ value: fmtNum(s.sent ?? 0, 0), sub: s.last_sent_at ? `Last ${fmtRelative(s.last_sent_at)}` : "Nothing sent yet" });
    k.failed.set({ value: fmtNum(s.failed ?? 0, 0), sub: s.last_error ? h("span", { class: "ellipsis", title: s.last_error }, s.last_error) : "No errors", tone: s.failed ? "down" : "up" });
    k.queue.set({ value: fmtNum(s.queued ?? 0, 0), sub: s.dropped ? `${s.dropped} dropped` : "Rate-limited per chat" });
    replace(credsInfo, configured ? h("span", null, "Connected as ", h("b", { class: "mono" }, s.token_masked || "bot"), ` · source: ${s.source || "env"}`) : "Not configured — the terminal still trades; it just won't broadcast.");
    if (!channel.value && s.channel_id) channel.placeholder = s.channel_id;
    if (!admin.value && s.admin_chat_id) admin.placeholder = String(s.admin_chat_id);
    renderLog();
  }

  function renderLog() {
    const rows = (data?.broadcasts || []).filter((r) => !query || `${r.category} ${r.status} ${r.preview} ${r.error} ${r.chat_id}`.toLowerCase().includes(query));
    log.update(rows);
  }

  async function load() {
    data = await api.get("/api/telegram?limit=200", { signal: scope.signal });
    render();
  }

  async function save() {
    saveBtn.classList.add("loading");
    try {
      await api.post("/api/settings/telegram", { token: token.value.trim() || null, channel: channel.value.trim() || null, admin: admin.value.trim() || null });
      token.value = "";
      toast({ title: "Telegram credentials saved", message: "Use “Test connection” to verify the bot and channel.", level: "success" });
      await load();
    } catch (err) {
      toastError(err, "Could not save credentials");
    } finally {
      saveBtn.classList.remove("loading");
    }
  }

  async function testConnection() {
    testBtn.classList.add("loading");
    try {
      const res = await api.post("/api/telegram/test-connection", {});
      const entries = Object.entries(res || {}).filter(([key]) => key !== "ok");
      replace(testResult, h("div", { class: `info-box ${res.ok ? "" : "danger"}` }, icon(res.ok ? "checkCircle" : "alert", 16),
        h("div", { class: "stack", style: { gap: "4px" } }, h("b", null, res.ok ? "Telegram connection OK" : "Telegram connection failed"),
          entries.map(([key, value]) => h("div", { class: "small" }, h("span", { class: "dim" }, `${key.replaceAll("_", " ")}: `), typeof value === "object" ? JSON.stringify(value) : String(value))))));
    } catch (err) {
      replace(testResult, h("div", { class: "info-box danger" }, icon("alert", 16), err.message));
    } finally {
      testBtn.classList.remove("loading");
    }
  }

  async function action(path, title, button) {
    button.classList.add("loading");
    try {
      await api.post(path, {});
      toast({ title, level: "success" });
      setTimeout(load, 1500);
    } catch (err) {
      toastError(err);
    } finally {
      button.classList.remove("loading");
    }
  }

  scope.every(10000, load);
}

function previewText() {
  return [
    h("div", null, "🟢 ", h("b", null, "BUY XAUUSD"), "  ·  ⚡ TRADE EXECUTED"),
    h("div", null, h("i", null, "Liquidity Sweep → MSS → FVG"), "  ·  M5"),
    h("div", null, "━━━━━━━━━━━━━━━━━━━━"),
    h("div", null, "📍 ", h("b", null, "Entry:"), " ", h("code", null, "2,384.20"), " (Market)"),
    h("div", null, "🛑 ", h("b", null, "Stop Loss:"), " ", h("code", null, "2,379.60"), " (46.0 pips)"),
    h("div", null, "🎯 ", h("b", null, "TP1:"), " ", h("code", null, "2,391.10"), " · 1:1.5"),
    h("div", null, "🎯 ", h("b", null, "TP2:"), " ", h("code", null, "2,398.00"), " · 1:3.0"),
    h("div", null, "📦 ", h("b", null, "Lot Size:"), " 0.21  ·  Risk 0.50%"),
    h("div", null, "━━━━━━━━━━━━━━━━━━━━"),
    h("div", null, h("i", null, "⚠️ Not financial advice. Manage your risk.")),
  ];
}
