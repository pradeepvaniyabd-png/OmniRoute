// DOM helpers and UI components. Dynamic text is always inserted as text nodes (never as HTML).

import { on } from "./store.js";

// ---------------------------------------------------------------- icons --
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7.5" height="9" rx="1.6"/><rect x="13.5" y="3" width="7.5" height="5.5" rx="1.6"/><rect x="13.5" y="11.5" width="7.5" height="9.5" rx="1.6"/><rect x="3" y="15" width="7.5" height="6" rx="1.6"/>',
  candles: '<path d="M7 3v4M7 17v4M17 3v2.5M17 15.5V21"/><rect x="4.5" y="7" width="5" height="10" rx="1.2"/><rect x="14.5" y="5.5" width="5" height="10" rx="1.2"/>',
  signal: '<circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.24a6 6 0 0 1 0-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14"/>',
  analytics: '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 6-7"/>',
  calendar: '<rect x="3" y="4.5" width="18" height="16.5" rx="2.2"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4M7.5 13.5h2M11 13.5h2M14.5 13.5h2M7.5 17h2M11 17h2"/>',
  tools: '<rect x="5" y="2.5" width="14" height="19" rx="2.2"/><path d="M8.5 6.5h7M8.5 11h.01M12 11h.01M15.5 11h.01M8.5 14.5h.01M12 14.5h.01M15.5 14.5h.01M8.5 18h.01M12 18h.01M15.5 18h.01"/>',
  send: '<path d="M21.6 3.4 2.9 10.6c-.8.3-.8 1.4.1 1.7l4.6 1.5 1.8 5.7c.2.7 1.1.9 1.6.4l2.7-2.6 4.9 3.6c.6.4 1.4.1 1.6-.6l3-15.2c.2-.9-.7-1.6-1.6-1.3Z"/><path d="m7.6 13.8 9.6-6.6-7 7.7"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2.2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M20.5 13.2A8.5 8.5 0 1 1 10.8 3.5a6.6 6.6 0 0 0 9.7 9.7Z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
  play: '<path d="M7 4.6v14.8a.8.8 0 0 0 1.2.7l11.6-7.4a.8.8 0 0 0 0-1.4L8.2 3.9A.8.8 0 0 0 7 4.6Z"/>',
  pause: '<rect x="6" y="4.5" width="4" height="15" rx="1"/><rect x="14" y="4.5" width="4" height="15" rx="1"/>',
  power: '<path d="M12 2.5v9"/><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  xCircle: '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  chevronsLeft: '<path d="m11 17-5-5 5-5M18 17l-5-5 5-5"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  arrowDown: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  trendUp: '<path d="m3 17 6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
  trendDown: '<path d="m3 7 6 6 4-4 8 8"/><path d="M14 17h7v-7"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
  shieldCheck: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7l10-5Z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.6-1.8"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  wifi: '<path d="M5 12.55a11 11 0 0 1 14 0M8.5 16.1a6 6 0 0 1 7 0M2 8.8a16 16 0 0 1 20 0M12 20h.01"/>',
  wifiOff: '<path d="M2 2l20 20M8.5 16.1a6 6 0 0 1 7 0M5 12.6a11 11 0 0 1 4.2-2.4M16.7 10.5a11 11 0 0 1 2.3 2M2 8.8a16 16 0 0 1 4.3-2.6M10.7 5.1A16 16 0 0 1 22 8.8M12 20h.01"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  crosshair: '<circle cx="12" cy="12" r="9"/><path d="M22 12h-4M6 12H2M12 6V2M12 22v-4"/>',
  wallet: '<path d="M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h14a2 2 0 0 1 2 2v3h-4a2 2 0 0 0 0 4h4v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5"/>',
  percent: '<path d="M19 5 5 19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
  scale: '<path d="M12 3v18M7 21h10M5 7h14"/><path d="m5 7-3 7a3 3 0 0 0 6 0L5 7ZM19 7l-3 7a3 3 0 0 0 6 0l-3-7Z"/>',
  grid: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8M17 6l3 3M14.5 8.5l2 2"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M3 3l18 18M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a10 10 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  flame: '<path d="M12 22c4 0 7-3 7-7 0-5-5-7-5-12-3 2-5 5-5 8-1-1-2-2-2-4-2 2-2 5-2 8 0 4 3 7 7 7Z"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5v14Z"/><path d="M6.5 17A2.5 2.5 0 0 0 4 19.5 2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  minimize: '<path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3"/>',
  fit: '<path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/>',
  coins: '<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18M7 6h1v4M16.71 13.88l.7.71-2.82 2.82"/>',
  ruler: '<path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.4 2.4 0 0 1 0-3.4l2.6-2.6a2.4 2.4 0 0 1 3.4 0Z"/><path d="m14.5 12.5 2-2M11.5 9.5l2-2M8.5 6.5l2-2M17.5 15.5l2-2"/>',
  divide: '<circle cx="12" cy="6" r="1.5"/><path d="M5 12h14"/><circle cx="12" cy="18" r="1.5"/>',
  sigma: '<path d="M18 7V4H6l6 8-6 8h12v-3"/>',
  spiral: '<path d="M12 12a1.5 1.5 0 0 1 1.5 1.5A3 3 0 0 1 10.5 16.5 4.5 4.5 0 0 1 6 12a6 6 0 0 1 6-6 7.5 7.5 0 0 1 7.5 7.5A9 9 0 0 1 10.5 22.4"/>',
  growth: '<path d="M3 21h18"/><path d="M5 17v-3M9 17V9M13 17v-5M17 17V5"/>',
  heart: '<path d="M19.5 12.6 12 20l-7.5-7.4A5 5 0 1 1 12 6a5 5 0 1 1 7.5 6.6Z"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  rotate: '<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.36 6.36L3 16M3 21v-5h5"/>',
  logo: '<path d="M6.5 4v3M6.5 15v5M12 3v4M12 13v8M17.5 6v3M17.5 16v3"/><rect x="4.5" y="7" width="4" height="8" rx="1"/><rect x="10" y="7" width="4" height="6" rx="1"/><rect x="15.5" y="9" width="4" height="7" rx="1"/>',
};

const SVG_NS = "http://www.w3.org/2000/svg";

export function icon(name, size = 16, className = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", name === "logo" ? "1.9" : "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  svg.innerHTML = ICONS[name] || ICONS.info; // constant markup only
  return svg;
}

export function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  return el;
}

// ------------------------------------------------------------------ DOM --
const PROPS = new Set(["value", "checked", "disabled", "selected", "indeterminate", "readOnly", "multiple"]);

export function h(tag, props = null, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
      else if (PROPS.has(key)) el[key] = value;
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function setText(el, text) {
  const value = text === null || text === undefined ? "" : String(text);
  if (el.textContent !== value) el.textContent = value;
}

export function toneClass(el, toneName, tones = ["up", "down", "warn"]) {
  for (const t of tones) el.classList.toggle(t, t === toneName);
}

// --------------------------------------------------------------- timing --
export function debounce(fn, wait = 250) {
  let timer;
  const wrapped = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

// Page lifecycle helper: everything registered here is torn down on navigation.
export class Scope {
  constructor() {
    this.disposers = [];
    this.controller = new AbortController();
    this.disposed = false;
  }
  get signal() { return this.controller.signal; }
  add(fn) { this.disposers.push(fn); return fn; }
  on(topic, handler) { return this.add(on(topic, handler)); }
  listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    return this.add(() => target.removeEventListener(type, handler, options));
  }
  every(ms, fn, { immediate = true } = {}) {
    let running = false;
    const tick = async () => {
      if (running || this.disposed || document.hidden) return;
      running = true;
      try { await fn(); } catch (err) { if (err?.name !== "AbortError") console.warn(err); } finally { running = false; }
    };
    if (immediate) tick();
    const id = setInterval(tick, ms);
    return this.add(() => clearInterval(id));
  }
  after(ms, fn) {
    const id = setTimeout(() => { if (!this.disposed) fn(); }, ms);
    return this.add(() => clearTimeout(id));
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.controller.abort();
    for (const fn of this.disposers.splice(0).reverse()) {
      try { fn(); } catch (err) { console.warn(err); }
    }
  }
}

// --------------------------------------------------------------- toasts --
const TOAST_ICONS = { success: "checkCircle", danger: "xCircle", warning: "alert", info: "info" };

export function toast({ title, message = "", level = "info", timeout = 5000 } = {}) {
  const stack = document.getElementById("toasts");
  if (!stack) return null;
  const close = () => {
    if (!el.isConnected) return;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  };
  const el = h("div", { class: `toast ${level}`, role: level === "danger" ? "alert" : "status" },
    h("span", { class: "toast-icon" }, icon(TOAST_ICONS[level] || "info", 18)),
    h("div", { class: "toast-body" }, h("div", { class: "toast-title" }, title || ""), message ? h("div", { class: "toast-msg" }, message) : null),
    h("button", { class: "toast-close", "aria-label": "Dismiss", onClick: close }, icon("x", 14)));
  stack.append(el);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  if (timeout) setTimeout(close, timeout);
  return close;
}

export function toastError(err, title = "Action failed") {
  toast({ title, message: err?.message || String(err), level: "danger", timeout: 7000 });
}

// --------------------------------------------------------------- modals --
let modalDepth = 0;

export function openModal({ title, iconName = null, iconTone = "", body, actions = [], size = "", dismissible = true, onClose } = {}) {
  const root = document.getElementById("overlay-root") || document.body;
  const previous = document.activeElement;
  const close = (result) => {
    if (!backdrop.isConnected) return;
    backdrop.remove();
    modalDepth = Math.max(0, modalDepth - 1);
    document.removeEventListener("keydown", onKey, true);
    if (previous && previous.focus) previous.focus();
    onClose?.(result);
  };
  const onKey = (event) => {
    if (event.key === "Escape" && dismissible) { event.stopPropagation(); close(false); }
  };
  const foot = actions.length
    ? h("div", { class: "modal-foot" }, actions.map((a) => {
        const btn = h("button", { class: `btn ${a.variant || ""}`, type: a.type || "button", disabled: a.disabled }, a.icon ? icon(a.icon, 15) : null, a.label);
        btn.addEventListener("click", async () => {
          if (!a.onClick) return close(a.value ?? false);
          btn.classList.add("loading");
          try {
            const keep = await a.onClick({ close, button: btn });
            if (keep !== true) close(a.value ?? true);
          } catch (err) {
            toastError(err);
          } finally {
            btn.classList.remove("loading");
          }
        });
        return btn;
      }))
    : null;
  const modal = h("div", { class: `modal ${size}`, role: "dialog", "aria-modal": "true", "aria-label": title || "Dialog" },
    h("div", { class: "modal-head" },
      h("h3", { class: "modal-title" }, iconName ? h("span", { class: `modal-icon ${iconTone}` }, icon(iconName, 18)) : null, title || ""),
      dismissible ? h("button", { class: "icon-btn", "aria-label": "Close", onClick: () => close(false) }, icon("x", 16)) : null),
    h("div", { class: "modal-body" }, body),
    foot);
  const backdrop = h("div", { class: "modal-backdrop" }, modal);
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop && dismissible) close(false); });
  document.addEventListener("keydown", onKey, true);
  root.append(backdrop);
  modalDepth += 1;
  requestAnimationFrame(() => {
    const focusable = modal.querySelector("input, select, textarea, .modal-foot .btn:last-child");
    focusable?.focus();
  });
  return { el: modal, close };
}

export function confirmDialog({ title, message, confirmLabel = "Confirm", tone = "danger", requireText = null, details = null, confirmVariant = null } = {}) {
  return new Promise((resolve) => {
    let input = null;
    const body = [h("div", null, message || "")];
    if (details) body.push(details);
    if (requireText) {
      input = h("input", { class: "input mono", placeholder: requireText, autocomplete: "off", spellcheck: "false" });
      body.push(h("div", { class: "field" }, h("label", { class: "field-label" }, h("span", null, "Type ", h("b", { class: "mono" }, requireText), " to confirm")), input));
    }
    const confirm = { label: confirmLabel, variant: confirmVariant || (tone === "danger" ? "btn-sell" : tone === "warn" ? "btn-warn" : "btn-primary"), value: true, disabled: Boolean(requireText) };
    const modal = openModal({ title, iconName: tone === "danger" ? "alert" : tone === "warn" ? "alert" : "info", iconTone: tone === "danger" ? "danger" : tone === "warn" ? "warn" : "",
      body, actions: [{ label: "Cancel", variant: "btn-ghost", value: false }, confirm], onClose: (result) => resolve(result === true) });
    if (input) {
      const button = modal.el.querySelector(".modal-foot .btn:last-child");
      input.addEventListener("input", () => { button.disabled = input.value.trim().toUpperCase() !== requireText.toUpperCase(); });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !button.disabled) button.click(); });
    }
  });
}

// ------------------------------------------------------------- popovers --
export function popover(anchor, content, { className = "", align = "right", onClose } = {}) {
  const root = document.getElementById("overlay-root") || document.body;
  const el = h("div", { class: `popover ${className}` }, content);
  root.append(el);
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth;
    let left = align === "right" ? r.right - w : r.left;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    el.style.left = `${left}px`;
    el.style.top = `${r.bottom + 8}px`;
  };
  place();
  const close = () => {
    if (!el.isConnected) return;
    el.remove();
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", key, true);
    window.removeEventListener("resize", place);
    onClose?.();
  };
  const outside = (event) => { if (!el.contains(event.target) && !anchor.contains(event.target)) close(); };
  const key = (event) => { if (event.key === "Escape") close(); };
  setTimeout(() => {
    document.addEventListener("mousedown", outside, true);
    document.addEventListener("keydown", key, true);
  });
  window.addEventListener("resize", place);
  return { el, close, place };
}

export function menu(anchor, items, options = {}) {
  let pop;
  const list = h("div", { class: "menu" }, items.map((item) => {
    if (item === "-") return h("div", { class: "menu-sep" });
    return h("button", { class: `menu-item ${item.danger ? "danger" : ""}`, onClick: () => { pop.close(); item.onClick?.(); } },
      item.icon ? icon(item.icon, 15) : null, item.label, item.checked ? h("span", { class: "menu-check" }, icon("check", 14)) : null);
  }));
  pop = popover(anchor, list, options);
  return pop;
}

// ----------------------------------------------------------- components --
export function badge(text, variant = "", { sm = false, title = null } = {}) {
  return h("span", { class: `badge ${variant ? `badge-${variant}` : ""} ${sm ? "sm" : ""}`, title }, text);
}

export function dirBadge(direction, { sm = true } = {}) {
  const buy = String(direction).toUpperCase() === "BUY";
  return badge(buy ? "BUY" : "SELL", buy ? "up" : "down", { sm });
}

export function gradeBadge(grade) {
  const g = String(grade || "").toUpperCase();
  const variant = g === "A+" ? "gold" : g === "A" ? "brand" : g === "B" ? "info" : "";
  return badge(g || "—", variant, { sm: true });
}

const STATUS_TONES = {
  RUNNING: "up", PAUSED: "warn", HALTED: "down", DISCONNECTED: "down", "NEWS LOCKED": "warn", STARTING: "info",
  OPEN: "brand", PENDING: "warn", CLOSED: "", CANCELLED: "", FILLED: "up",
  EXECUTED: "up", BROADCAST: "info", SIGNAL: "brand", NEW: "brand", SKIPPED: "warn", REJECTED: "down", EXPIRED: "",
  SENT: "up", FAILED: "down", QUEUED: "warn", RESERVED: "warn", DROPPED: "",
  TP: "up", SL: "down", MANUAL: "info", PANIC: "down", BREAKER: "down", STOP_OUT: "down", EXPERT: "", NEWS: "warn", RESET: "",
};

export function statusBadge(status, { sm = true } = {}) {
  const s = String(status || "—").toUpperCase();
  return badge(s.replaceAll("_", " "), STATUS_TONES[s] ?? "", { sm });
}

export function sparkline(values, { width = 96, height = 28, stroke = null, fill = true, strokeWidth = 1.6 } = {}) {
  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "spark", "aria-hidden": "true" });
  const data = (values || []).map(Number).filter(Number.isFinite);
  if (data.length < 2) return svg;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const step = width / (data.length - 1);
  const pts = data.map((v, i) => [i * step, height - 2 - ((v - min) / span) * (height - 4)]);
  const color = stroke || (data[data.length - 1] >= data[0] ? "var(--up)" : "var(--down)");
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  if (fill) {
    const id = `sg${Math.random().toString(36).slice(2, 9)}`;
    const defs = svgEl("defs");
    const grad = svgEl("linearGradient", { id, x1: "0", x2: "0", y1: "0", y2: "1" });
    grad.append(svgEl("stop", { offset: "0%", "stop-color": color, "stop-opacity": ".28" }), svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0" }));
    defs.append(grad);
    svg.append(defs, svgEl("path", { d: `${line}L${width},${height}L0,${height}Z`, fill: `url(#${id})`, stroke: "none" }));
  }
  svg.append(svgEl("path", { d: line, fill: "none", stroke: color, "stroke-width": strokeWidth, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  return svg;
}

export function progress(pct, toneName = "", { lg = false } = {}) {
  const bar = h("span");
  const el = h("div", { class: `progress ${toneName} ${lg ? "lg" : ""}` }, bar);
  el.set = (value, t = toneName) => {
    bar.style.width = `${Math.max(0, Math.min(100, Number(value) || 0))}%`;
    el.className = `progress ${t} ${lg ? "lg" : ""}`;
  };
  el.set(pct, toneName);
  return el;
}

export function segmented(options, value, onChange, { sm = false, block = false, label = "" } = {}) {
  const el = h("div", { class: `seg ${sm ? "sm" : ""} ${block ? "block" : ""}`, role: "radiogroup", "aria-label": label });
  let current = value;
  const buttons = options.map((opt) => {
    const btn = h("button", { type: "button", role: "radio", title: opt.title || null, onClick: () => {
      if (opt.disabled) return;
      el.setValue(opt.value);
      onChange?.(opt.value);
    } }, opt.icon ? icon(opt.icon, 14) : null, opt.label);
    btn.dataset.value = String(opt.value);
    return btn;
  });
  append(el, buttons);
  el.setValue = (v) => {
    current = v;
    for (const btn of buttons) {
      const active = btn.dataset.value === String(v);
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    }
  };
  el.getValue = () => current;
  el.setValue(value);
  return el;
}

export function toggleSwitch({ checked = false, onChange, label = null, disabled = false, title = null } = {}) {
  const input = h("input", { type: "checkbox", checked, disabled, role: "switch" });
  const el = h("label", { class: "switch", title }, input, h("span", { class: "switch-track" }), label ? h("span", { class: "switch-label" }, label) : null);
  input.addEventListener("change", () => onChange?.(input.checked, input));
  el.input = input;
  el.set = (v) => { input.checked = Boolean(v); };
  return el;
}

export function tabs(items, active, onChange) {
  const el = h("div", { class: "tabs", role: "tablist" });
  const buttons = new Map();
  for (const item of items) {
    const count = h("span", { class: "count", hidden: item.count === undefined || item.count === null }, item.count ?? "");
    const btn = h("button", { class: "tab", role: "tab", onClick: () => { el.setActive(item.id); onChange?.(item.id); } },
      item.icon ? icon(item.icon, 14) : null, item.label, count);
    btn.count = count;
    buttons.set(item.id, btn);
    el.append(btn);
  }
  el.setActive = (id) => {
    for (const [key, btn] of buttons) {
      btn.classList.toggle("active", key === id);
      btn.setAttribute("aria-selected", key === id ? "true" : "false");
    }
  };
  el.setCount = (id, n) => {
    const btn = buttons.get(id);
    if (!btn) return;
    btn.count.hidden = n === null || n === undefined;
    setText(btn.count, n ?? "");
  };
  el.setActive(active);
  return el;
}

export function emptyState({ iconName = "info", title = "Nothing here yet", text = "", action = null } = {}) {
  return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon(iconName, 20)),
    h("div", { class: "empty-title" }, title), text ? h("div", { class: "empty-text" }, text) : null, action);
}

export function errorState(message, onRetry) {
  return h("div", { class: "error-state" }, icon("alert", 18), h("div", { class: "grow" }, message || "Something went wrong"),
    onRetry ? h("button", { class: "btn btn-sm", onClick: onRetry }, icon("refresh", 14), "Retry") : null);
}

export function skeletonLines(count = 4) {
  return h("div", { class: "skel-lines" }, Array.from({ length: count }, (_, i) =>
    h("div", { class: "skeleton", style: { height: "14px", width: `${92 - (i % 3) * 18}%` } })));
}

export function card({ title = null, iconName = null, sub = null, actions = null, body = null, flush = false, className = "", head = true } = {}) {
  const subEl = sub !== null ? h("div", { class: "card-sub" }, sub) : null;
  const actionsEl = h("div", { class: "card-actions" }, actions);
  const bodyEl = h("div", { class: `card-body ${flush ? "flush" : ""}` }, body);
  const el = h("section", { class: `card ${className}` },
    head ? h("div", { class: "card-head" }, h("div", { class: "card-titles" },
      h("h3", { class: "card-title" }, iconName ? icon(iconName, 16) : null, title), subEl), actionsEl) : null,
    bodyEl);
  el.body = bodyEl;
  el.actions = actionsEl;
  el.sub = subEl;
  return el;
}

export function kpi({ label, iconName = "activity", toneName = "brand" } = {}) {
  const value = h("div", { class: "kpi-value" }, "—");
  const sub = h("div", { class: "kpi-sub" });
  const iconBox = h("span", { class: "kpi-icon" }, icon(iconName, 16));
  let currentIcon = iconName;
  const el = h("div", { class: `kpi tone-${toneName}` },
    h("div", { class: "kpi-head" }, h("span", { class: "kpi-label" }, label), iconBox), value, sub);
  el.set = ({ value: v, sub: s, valueTone = null, tone: t = null, iconName: nextIcon = null } = {}) => {
    if (nextIcon && nextIcon !== currentIcon) { currentIcon = nextIcon; replace(iconBox, icon(nextIcon, 16)); }
    if (v !== undefined) setText(value, v);
    value.classList.toggle("up", valueTone === "up");
    value.classList.toggle("down", valueTone === "down");
    if (s !== undefined) replace(sub, s);
    if (t) el.className = `kpi tone-${t}`;
  };
  return el;
}

const SYMBOL_COLORS = {
  XAU: "linear-gradient(135deg,#f6c453,#b7791f)", XAG: "linear-gradient(135deg,#cbd5e1,#64748b)",
  BTC: "linear-gradient(135deg,#f7931a,#c2410c)", ETH: "linear-gradient(135deg,#8ea2ff,#4f5bd5)",
  EUR: "linear-gradient(135deg,#3b82f6,#1d4ed8)", GBP: "linear-gradient(135deg,#a78bfa,#6d28d9)",
  JPY: "linear-gradient(135deg,#fb7185,#be123c)", AUD: "linear-gradient(135deg,#34d399,#047857)",
  NZD: "linear-gradient(135deg,#2dd4bf,#0f766e)", CAD: "linear-gradient(135deg,#f87171,#b91c1c)",
  CHF: "linear-gradient(135deg,#fca5a5,#dc2626)", USD: "linear-gradient(135deg,#4ade80,#15803d)",
  US3: "linear-gradient(135deg,#60a5fa,#1e3a8a)", NAS: "linear-gradient(135deg,#22d3ee,#0e7490)",
  SOL: "linear-gradient(135deg,#c084fc,#14b8a6)", XRP: "linear-gradient(135deg,#94a3b8,#334155)",
};

export function symbolIcon(symbol) {
  const s = String(symbol || "?").toUpperCase();
  const key = Object.keys(SYMBOL_COLORS).find((k) => s.startsWith(k));
  let background = key ? SYMBOL_COLORS[key] : null;
  if (!background) {
    let hash = 0;
    for (const ch of s) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    const hue = hash % 360;
    background = `linear-gradient(135deg,hsl(${hue} 70% 58%),hsl(${(hue + 40) % 360} 65% 40%))`;
  }
  return h("span", { class: "sym-icon", style: { background } }, s.slice(0, 3));
}

const SYMBOL_DESCRIPTIONS = {
  XAUUSD: "Gold / US Dollar", XAGUSD: "Silver / US Dollar", BTCUSD: "Bitcoin / US Dollar", ETHUSD: "Ethereum / US Dollar",
  EURUSD: "Euro / US Dollar", GBPUSD: "British Pound / US Dollar", USDJPY: "US Dollar / Japanese Yen", AUDUSD: "Australian Dollar / US Dollar",
  USDCAD: "US Dollar / Canadian Dollar", USDCHF: "US Dollar / Swiss Franc", NZDUSD: "New Zealand Dollar / US Dollar", GBPJPY: "Pound / Yen",
  EURJPY: "Euro / Yen", EURGBP: "Euro / Pound", US30: "Dow Jones 30", NAS100: "Nasdaq 100", SPX500: "S&P 500", US500: "S&P 500", SOLUSD: "Solana / US Dollar",
};
export function symbolDescription(symbol) {
  const s = String(symbol || "").toUpperCase();
  const key = Object.keys(SYMBOL_DESCRIPTIONS).find((k) => s.startsWith(k));
  return key ? SYMBOL_DESCRIPTIONS[key] : "Instrument";
}

export function symbolCell(symbol, sub = null) {
  return h("div", { class: "sym-cell" }, symbolIcon(symbol),
    h("div", { style: { minWidth: "0" } }, h("div", { class: "sym-name" }, symbol), h("div", { class: "sym-desc ellipsis" }, sub ?? symbolDescription(symbol))));
}

export function field(label, control, { hint = null, extra = null } = {}) {
  return h("div", { class: "field" }, h("label", { class: "field-label" }, h("span", null, label), extra), control, hint ? h("div", { class: "field-hint" }, hint) : null);
}

export function numberInput({ value = "", step = "any", min = null, max = null, placeholder = "", onInput = null, className = "", suffix = null, prefix = null } = {}) {
  const input = h("input", { class: `input num ${className}`, type: "number", inputmode: "decimal", step, min, max, placeholder, value: value ?? "" });
  if (onInput) input.addEventListener("input", () => onInput(input.value === "" ? null : Number(input.value), input));
  if (!suffix && !prefix) return input;
  const group = h("div", { class: "input-group" }, prefix ? h("span", { class: "input-addon" }, prefix) : null, input, suffix ? h("span", { class: "input-addon" }, suffix) : null);
  group.input = input;
  return group;
}

export function selectInput(options, value, onChange, { className = "" } = {}) {
  const el = h("select", { class: `select ${className}` }, options.map((o) => h("option", { value: o.value, selected: String(o.value) === String(value) }, o.label)));
  if (onChange) el.addEventListener("change", () => onChange(el.value));
  return el;
}

export function searchInput({ placeholder = "Search…", onInput, value = "" } = {}) {
  const input = h("input", { class: "input", type: "search", placeholder, value, spellcheck: "false", autocomplete: "off" });
  const debounced = debounce((v) => onInput?.(v), 120);
  input.addEventListener("input", () => debounced(input.value.trim()));
  const el = h("div", { class: "search-input" }, icon("search", 15), input);
  el.input = input;
  return el;
}

// ----------------------------------------------------------- data table --
export class DataTable {
  constructor({ columns, rowKey = (r, i) => i, empty = "No data", onRowClick = null, rowClass = null, sortBy = null, sortDir = "desc", maxHeight = false, detail = null } = {}) {
    this.columns = columns;
    this.rowKey = rowKey;
    this.emptyText = empty;
    this.onRowClick = onRowClick;
    this.rowClass = rowClass;
    this.sortBy = sortBy;
    this.sortDir = sortDir;
    this.detail = detail;
    this.expanded = new Set();
    this.rows = [];
    this.rowEls = new Map();
    this.thead = h("thead");
    this.tbody = h("tbody");
    this.table = h("table", { class: "table" }, this.thead, this.tbody);
    this.empty = h("div", { class: "table-empty", hidden: true });
    this.el = h("div", { class: `table-wrap ${maxHeight ? "max-h" : ""}` }, this.table, this.empty);
    this.renderHead();
  }

  renderHead() {
    const tr = h("tr");
    for (const col of this.columns) {
      const th = h("th", { class: [col.align || "", col.sortable !== false && col.sort !== false ? "sortable" : "", this.sortBy === col.key ? `sorted ${this.sortDir}` : ""].join(" "), style: col.width ? { width: col.width } : null }, col.label ?? "");
      if (col.sortable !== false && col.sort !== false && col.key) {
        th.addEventListener("click", () => {
          if (this.sortBy === col.key) this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
          else { this.sortBy = col.key; this.sortDir = col.defaultDir || "desc"; }
          this.renderHead();
          this.update(this.rows, { force: true });
        });
      }
      tr.append(th);
    }
    this.thead.replaceChildren(tr);
  }

  sorted(rows) {
    if (!this.sortBy) return rows;
    const col = this.columns.find((c) => c.key === this.sortBy);
    if (!col) return rows;
    const get = col.sortValue || ((r) => r[col.key]);
    const dir = this.sortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const x = get(a);
      const y = get(b);
      if (x === y) return 0;
      if (x === null || x === undefined || x === "") return 1;
      if (y === null || y === undefined || y === "") return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
    });
  }

  setEmpty(text) { this.emptyText = text; if (!this.rows.length) setText(this.empty, text); }

  update(rows, { force = false } = {}) {
    this.rows = rows || [];
    const ordered = this.sorted(this.rows);
    this.empty.hidden = ordered.length > 0;
    this.table.hidden = ordered.length === 0;
    if (!ordered.length) { setText(this.empty, this.emptyText); }
    const seen = new Set();
    const fragment = [];
    ordered.forEach((row, index) => {
      const key = String(this.rowKey(row, index));
      seen.add(key);
      let entry = this.rowEls.get(key);
      if (!entry || force) {
        if (entry) entry.tr.remove();
        entry = { tr: h("tr"), cells: [], sigs: [] };
        if (this.onRowClick) {
          entry.tr.classList.add("clickable");
          entry.tr.addEventListener("click", (event) => {
            if (event.target.closest("button, a, input, select, label")) return;
            this.onRowClick(entry.row, event);
          });
        }
        for (const col of this.columns) {
          const td = h("td", { class: `${col.align || ""} ${col.className || ""}` });
          entry.cells.push(td);
          entry.sigs.push(undefined);
          entry.tr.append(td);
        }
        this.rowEls.set(key, entry);
      }
      entry.row = row;
      this.columns.forEach((col, i) => {
        const sig = col.sig ? col.sig(row) : col.render ? JSON.stringify(row) : JSON.stringify(col.key ? row[col.key] : null);
        if (sig === entry.sigs[i] && !col.alwaysRender) return;
        entry.sigs[i] = sig;
        const content = col.render ? col.render(row) : col.format ? col.format(row[col.key], row) : row[col.key];
        replace(entry.cells[i], content ?? "—");
        if (col.cellClass) entry.cells[i].className = `${col.align || ""} ${col.className || ""} ${col.cellClass(row) || ""}`;
      });
      entry.tr.className = `${this.onRowClick ? "clickable" : ""} ${this.rowClass ? this.rowClass(row) || "" : ""}`;
      fragment.push(entry.tr);
      if (this.detail && this.expanded.has(key)) {
        if (!entry.detailTr) entry.detailTr = h("tr", { class: "detail" }, h("td", { colspan: this.columns.length }));
        replace(entry.detailTr.firstChild, this.detail(row));
        fragment.push(entry.detailTr);
      } else if (entry.detailTr) {
        entry.detailTr.remove();
        entry.detailTr = null;
      }
    });
    for (const [key, entry] of this.rowEls) {
      if (!seen.has(key)) { entry.tr.remove(); entry.detailTr?.remove(); this.rowEls.delete(key); }
    }
    // Re-order only when needed to keep focus/hover stable.
    let node = this.tbody.firstChild;
    for (const tr of fragment) {
      if (node === tr) { node = node.nextSibling; continue; }
      this.tbody.insertBefore(tr, node);
    }
    while (node) { const next = node.nextSibling; node.remove(); node = next; }
  }

  toggleDetail(row) {
    const key = String(this.rowKey(row));
    if (this.expanded.has(key)) this.expanded.delete(key); else this.expanded.add(key);
    this.update(this.rows);
  }
}

// -------------------------------------------------------------- export --
export async function copyText(text, label = "Copied to clipboard") {
  try {
    await navigator.clipboard.writeText(text);
    toast({ title: label, level: "success", timeout: 2200 });
  } catch {
    toast({ title: "Copy failed", message: "Clipboard access was blocked by the browser", level: "warning" });
  }
}

export function downloadFile(filename, content, type = "text/plain") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function toCSV(rows, columns) {
  const esc = (v) => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    const guarded = /^[=+\-@\t\r]/.test(s) ? `'${s}` : s; // spreadsheet formula-injection guard
    return /[",\n]/.test(guarded) ? `"${guarded.replaceAll('"', '""')}"` : guarded;
  };
  const head = columns.map((c) => esc(c.label)).join(",");
  const body = rows.map((r) => columns.map((c) => esc(typeof c.value === "function" ? c.value(r) : r[c.key])).join(",")).join("\n");
  return `${head}\n${body}\n`;
}

export function flash(el, direction) {
  if (!el || !direction) return;
  el.classList.remove("flash-up", "flash-down");
  void el.offsetWidth; // restart the transition
  el.classList.add(direction === "up" ? "flash-up" : "flash-down");
  clearTimeout(el._flashTimer);
  el._flashTimer = setTimeout(() => el.classList.remove("flash-up", "flash-down"), 450);
}

export function beep(level = "info") {
  try {
    const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = level === "danger" ? 330 : level === "success" ? 880 : 620;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.36);
  } catch { /* audio unavailable */ }
}
