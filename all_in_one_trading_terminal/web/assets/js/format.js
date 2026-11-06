// Number, money, price and time formatting shared by every page.

export const ZONES = [
  { id: "Asia/Kolkata", label: "IST", name: "India (IST)" },
  { id: "local", label: "Local", name: "Browser local time" },
  { id: "UTC", label: "UTC", name: "UTC" },
  { id: "America/New_York", label: "NY", name: "New York (ET)" },
  { id: "Europe/London", label: "LDN", name: "London" },
];

const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
let displayTz = "Asia/Kolkata";

export function setDisplayTz(tz) { displayTz = tz || "Asia/Kolkata"; }
export function getDisplayTz() { return displayTz === "local" ? LOCAL_TZ : displayTz; }
export function tzLabel(tz = displayTz) {
  const zone = ZONES.find((z) => z.id === tz);
  if (zone) return zone.id === "local" ? shortTzName(LOCAL_TZ) : zone.label;
  return shortTzName(tz);
}
function shortTzName(tz) {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    return part ? part.value : tz;
  } catch { return tz; }
}

const numberFormats = new Map();
function nf(min, max, extra = {}) {
  const key = `${min}|${max}|${JSON.stringify(extra)}`;
  let f = numberFormats.get(key);
  if (!f) {
    f = new Intl.NumberFormat("en-US", { minimumFractionDigits: min, maximumFractionDigits: max, ...extra });
    numberFormats.set(key, f);
  }
  return f;
}

export const isNum = (v) => typeof v === "number" && Number.isFinite(v);
export const toNum = (v) => (v === null || v === undefined || v === "" ? null : Number.isFinite(+v) ? +v : null);

export function fmtNum(v, digits = 2, { min = digits, sign = false } = {}) {
  v = toNum(v);
  if (v === null) return "—";
  const s = nf(Math.min(min, digits), digits).format(v);
  return sign && v > 0 ? `+${s}` : s;
}

export function fmtInt(v) { return fmtNum(v, 0); }

export function fmtPrice(v, digits = 5) {
  v = toNum(v);
  if (v === null) return "—";
  const d = Math.max(0, Math.min(8, digits ?? 5));
  return nf(d, d).format(v);
}

const CURRENCY_SYMBOLS = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", INR: "₹", AUD: "A$", CAD: "C$", CHF: "CHF ", USDT: "₮" };
let defaultCurrency = "USD";
export function setCurrency(code) { if (code) defaultCurrency = String(code).toUpperCase(); }
export function currencySymbol(code = defaultCurrency) { return CURRENCY_SYMBOLS[code] ?? `${code} `; }

export function fmtMoney(v, { currency = defaultCurrency, digits = 2, sign = false, compact = false } = {}) {
  v = toNum(v);
  if (v === null) return "—";
  const abs = Math.abs(v);
  const body = compact && abs >= 10000 ? fmtCompact(abs) : nf(digits, digits).format(abs);
  const prefix = v < 0 ? "-" : sign && v > 0 ? "+" : "";
  return `${prefix}${currencySymbol(currency)}${body}`;
}

export function fmtPct(v, digits = 2, { sign = true } = {}) {
  v = toNum(v);
  if (v === null) return "—";
  const s = nf(digits, digits).format(v);
  return `${sign && v > 0 ? "+" : ""}${s}%`;
}

export function fmtCompact(v) {
  v = toNum(v);
  if (v === null) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function fmtLots(v) {
  v = toNum(v);
  if (v === null) return "—";
  return nf(2, 2).format(v);
}

export function tone(v, eps = 1e-9) {
  v = toNum(v);
  if (v === null || Math.abs(v) < eps) return "";
  return v > 0 ? "up" : "down";
}

// ------------------------------------------------------------------ time --
export function toDate(value) {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value);
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

const dateFormats = new Map();
function dtf(options) {
  const tz = getDisplayTz();
  const key = `${tz}|${JSON.stringify(options)}`;
  let f = dateFormats.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hourCycle: "h23", ...options });
    dateFormats.set(key, f);
  }
  return f;
}

export function fmtTime(value, { seconds = false } = {}) {
  const d = toDate(value);
  if (!d) return "—";
  return dtf({ hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }).format(d);
}

export function fmtDate(value, { year = false, weekday = false } = {}) {
  const d = toDate(value);
  if (!d) return "—";
  return dtf({ day: "2-digit", month: "short", ...(year ? { year: "numeric" } : {}), ...(weekday ? { weekday: "short" } : {}) }).format(d);
}

export function fmtDateTime(value, { seconds = false, year = false } = {}) {
  const d = toDate(value);
  if (!d) return "—";
  return `${fmtDate(d, { year })} ${fmtTime(d, { seconds })}`;
}

export function dayKey(value) {
  const d = toDate(value);
  if (!d) return "";
  const parts = Object.fromEntries(dtf({ year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function fmtDayHeading(value) {
  const d = toDate(value);
  if (!d) return "—";
  return dtf({ weekday: "long", day: "numeric", month: "long" }).format(d);
}

export function fmtRelative(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return "—";
  const diff = (now - d.getTime()) / 1000;
  const abs = Math.abs(diff);
  const suffix = diff >= 0 ? "ago" : "from now";
  if (abs < 5) return "just now";
  if (abs < 60) return `${Math.round(abs)}s ${suffix}`;
  if (abs < 3600) return `${Math.round(abs / 60)}m ${suffix}`;
  if (abs < 86400) return `${Math.round(abs / 3600)}h ${suffix}`;
  return `${Math.round(abs / 86400)}d ${suffix}`;
}

export function fmtCountdown(seconds) {
  seconds = toNum(seconds);
  if (seconds === null) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function fmtDuration(minutes) {
  minutes = toNum(minutes);
  if (minutes === null) return "—";
  if (minutes < 1) return `${Math.round(minutes * 60)}s`;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  if (h < 24) return `${h}h ${Math.round(minutes % 60)}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

// Offset (seconds) of a time zone at an instant — used to show chart times in the chosen zone.
const offsetFormats = new Map();
const offsetCache = new Map();
export function tzOffsetSeconds(epochSeconds, tz = getDisplayTz()) {
  if (tz === "UTC") return 0;
  const bucket = Math.floor(epochSeconds / 1800);
  const key = `${tz}|${bucket}`;
  const hit = offsetCache.get(key);
  if (hit !== undefined) return hit;
  let f = offsetFormats.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    offsetFormats.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(epochSeconds * 1000)).map((x) => [x.type, x.value]));
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
  const offset = Math.round((asUtc - Math.floor(epochSeconds)) / 60) * 60;
  if (offsetCache.size > 5000) offsetCache.clear();
  offsetCache.set(key, offset);
  return offset;
}

// Wall-clock {h, m, weekday} of an instant in a zone.
const wallFormats = new Map();
export function wallClock(date, tz) {
  let f = wallFormats.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    wallFormats.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(date).map((x) => [x.type, x.value]));
  const days = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  return { h: +p.hour % 24, m: +p.minute, s: +p.second, weekday: days[p.weekday] ?? 0 };
}

export function minutesOfDay(date, tz) {
  const w = wallClock(date, tz);
  return w.h * 60 + w.m + w.s / 60;
}

export function hhmm(minutes) {
  const m = ((Math.round(minutes) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}
