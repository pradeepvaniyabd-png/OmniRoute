// TradingView Lightweight Charts™ wrappers: the SMC price chart and small analytics charts.

import { fmtNum, fmtPrice, getDisplayTz, tzOffsetSeconds } from "./format.js";
import { on } from "./store.js";

const LWC = () => window.LightweightCharts;

export function chartsAvailable() { return Boolean(LWC()?.createChart); }

export function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return {
    bg: v("--surface", "#111620"), text: v("--text-2", "#a7afc0"), text3: v("--text-3", "#6c768b"), grid: v("--chart-grid", "rgba(255,255,255,.05)"),
    border: v("--border", "#1f2635"), up: v("--up", "#18c787"), down: v("--down", "#f2475d"), brand: v("--brand", "#4d7cff"),
    warn: v("--warn", "#f5a524"), info: v("--info", "#38bdf8"), violet: v("--violet", "#a78bfa"), gold: v("--gold", "#e8b24a"),
  };
}

function alpha(color, a) {
  if (!color) return `rgba(128,128,128,${a})`;
  if (color.startsWith("#")) {
    const hex = color.length === 4 ? color.slice(1).split("").map((c) => c + c).join("") : color.slice(1, 7);
    const n = parseInt(hex, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const m = color.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(",").map((x) => x.trim());
    return `rgba(${r},${g},${b},${a})`;
  }
  return color;
}

function baseOptions(colors) {
  const L = LWC();
  return {
    autoSize: true,
    layout: { background: { type: "solid", color: colors.bg }, textColor: colors.text, fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    crosshair: { mode: L.CrosshairMode.Normal, vertLine: { color: alpha(colors.text3, 0.6), labelBackgroundColor: colors.brand }, horzLine: { color: alpha(colors.text3, 0.6), labelBackgroundColor: colors.brand } },
    rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.08, bottom: 0.12 } },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false, rightOffset: 10, barSpacing: 8, minBarSpacing: 2 },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
  };
}

const shift = (t) => (Number.isFinite(t) ? t + tzOffsetSeconds(t, getDisplayTz()) : t);

// ------------------------------------------------------------ primitive --
// Draws rectangles (FVG / order blocks / session ranges / projected trade boxes) behind candles.
class BoxPrimitive {
  constructor() {
    this.boxes = [];
    this.times = [];
    this.barSeconds = 300;
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
    const self = this;
    this.view = { zOrder: () => "bottom", renderer: () => ({ draw: (target) => self.draw(target) }) };
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = this.series = this.requestUpdate = null; }
  updateAllViews() {}
  paneViews() { return [this.view]; }
  set(boxes, times, barSeconds) {
    this.boxes = boxes;
    this.times = times;
    this.barSeconds = barSeconds || 300;
    this.requestUpdate?.();
  }
  logicalOf(t) {
    const times = this.times;
    const n = times.length;
    if (!n) return null;
    if (t <= times[0]) return (t - times[0]) / this.barSeconds;
    if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / this.barSeconds;
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
    return lo + (t - times[lo]) / (times[hi] - times[lo] || 1);
  }
  // logicalToCoordinate() only accepts whole bar indices (fractions return 0), so interpolate.
  xAt(ts, logical) {
    const i = Math.floor(logical);
    const a = ts.logicalToCoordinate(i);
    const frac = logical - i;
    if (a === null || frac === 0) return a;
    const b = ts.logicalToCoordinate(i + 1);
    return b === null ? a : a + (b - a) * frac;
  }
  draw(target) {
    if (!this.chart || !this.series || !this.boxes.length) return;
    const ts = this.chart.timeScale();
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      for (const box of this.boxes) {
        const l1 = this.logicalOf(box.from);
        if (l1 === null) continue;
        let x1 = this.xAt(ts, l1 - 0.5);
        let x2;
        if (box.bars) x2 = this.xAt(ts, l1 + box.bars);
        else if (box.to === null || box.to === undefined) x2 = mediaSize.width;
        else x2 = this.xAt(ts, this.logicalOf(box.to) + 0.5);
        const y1 = this.series.priceToCoordinate(box.top);
        const y2 = this.series.priceToCoordinate(box.bottom);
        if ([x1, x2, y1, y2].some((v) => v === null || v === undefined)) continue;
        x1 = Math.max(-10, x1);
        x2 = Math.min(mediaSize.width + 10, x2);
        if (x2 <= 0 || x1 >= mediaSize.width || x2 - x1 < 1) continue;
        const top = Math.min(y1, y2);
        const height = Math.max(1, Math.abs(y2 - y1));
        ctx.fillStyle = box.fill;
        ctx.fillRect(x1, top, x2 - x1, height);
        if (box.stroke) {
          ctx.save();
          ctx.strokeStyle = box.stroke;
          ctx.lineWidth = 1;
          if (box.dashed) ctx.setLineDash([4, 3]);
          ctx.strokeRect(Math.round(x1) + 0.5, Math.round(top) + 0.5, Math.round(x2 - x1), Math.round(height));
          ctx.restore();
        }
        if (box.label && x2 - x1 > 34 && height > 9) {
          ctx.font = "600 10px Inter, system-ui, sans-serif";
          ctx.fillStyle = box.labelColor || box.stroke || "#999";
          ctx.textBaseline = "top";
          ctx.fillText(box.label, Math.max(x1, 0) + 5, top + 3);
        }
      }
    });
  }
}

// --------------------------------------------------------------- price chart --
export class PriceChart {
  constructor(container, { onCrosshair = null, onClick = null } = {}) {
    if (!chartsAvailable()) throw new Error("Chart library failed to load");
    const L = LWC();
    this.container = container;
    this.colors = themeColors();
    this.chart = L.createChart(container, baseOptions(this.colors));
    this.candles = this.chart.addCandlestickSeries(this.candleOptions());
    this.volume = this.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    this.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 }, visible: false });
    this.ema = this.chart.addLineSeries({ color: this.colors.warn, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    this.boxes = new BoxPrimitive();
    this.candles.attachPrimitive(this.boxes);
    this.priceLines = [];
    this.payload = null;
    this.lastBar = null;
    this.digits = 5;
    this.overlays = { ema: true, volume: true, fvg: true, ob: true, sessions: true, signals: true, levels: true, markers: true };
    this.firstLoad = true;
    this.key = null;
    if (onCrosshair) this.chart.subscribeCrosshairMove((param) => onCrosshair(this.legendAt(param)));
    if (onClick) {
      this.chart.subscribeClick((param) => {
        if (!param.point) return;
        const price = this.candles.coordinateToPrice(param.point.y);
        if (price !== null && Number.isFinite(price)) onClick(price);
      });
    }
    this.offTheme = on("theme", () => this.applyTheme());
  }

  candleOptions() {
    const c = this.colors;
    return { upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down, priceLineColor: c.brand, priceLineStyle: 2 };
  }

  applyTheme() {
    this.colors = themeColors();
    this.chart.applyOptions(baseOptions(this.colors));
    this.candles.applyOptions(this.candleOptions());
    this.ema.applyOptions({ color: this.colors.warn });
    if (this.payload) this.render(this.payload, { keepView: true });
  }

  setOverlays(overlays) {
    this.overlays = { ...this.overlays, ...overlays };
    if (this.payload) this.render(this.payload, { keepView: true });
  }

  setData(payload) {
    const key = `${payload.symbol}|${payload.timeframe}|${getDisplayTz()}`;
    const keepView = key === this.key;
    this.key = key;
    this.payload = payload;
    this.render(payload, { keepView });
  }

  render(payload, { keepView = false } = {}) {
    const digits = Number.isInteger(payload.digits) ? payload.digits : 5;
    if (digits !== this.digits || this.firstLoad) {
      this.digits = digits;
      this.candles.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: 1 / 10 ** digits } });
      this.ema.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: 1 / 10 ** digits } });
    }
    const range = keepView ? this.chart.timeScale().getVisibleLogicalRange() : null;
    const candles = (payload.candles || []).map((c) => ({ ...c, time: shift(c.time) }));
    this.candles.setData(candles);
    this.lastBar = candles.length ? { ...candles[candles.length - 1] } : null;
    const c = this.colors;
    this.volume.setData(this.overlays.volume ? (payload.volume || []).map((v) => ({ time: shift(v.time), value: v.value, color: alpha(v.up ? c.up : c.down, 0.35) })) : []);
    this.ema.setData(this.overlays.ema ? (payload.ema || []).filter((p) => p.value !== null).map((p) => ({ time: shift(p.time), value: p.value })) : []);
    this.renderMarkers(payload);
    this.renderBoxes(payload, candles);
    this.renderLevels(payload);
    if (range && !this.firstLoad) this.chart.timeScale().setVisibleLogicalRange(range);
    else if (candles.length) {
      const total = candles.length;
      this.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - 150), to: total + 8 });
    }
    this.firstLoad = false;
  }

  renderMarkers(payload) {
    if (!this.overlays.markers && !this.overlays.signals) { this.candles.setMarkers([]); return; }
    const isSignal = (m) => m.text?.startsWith("BUY") || m.text?.startsWith("SELL");
    const tradeable = (m) => /^(BUY|SELL) (A\+|A|B)\b/.test(m.text || "");
    const markers = (payload.markers || [])
      .filter((m) => (isSignal(m) ? this.overlays.signals && tradeable(m) : this.overlays.markers))
      .map((m) => ({ ...m, time: shift(m.time), size: m.text?.startsWith("BUY") || m.text?.startsWith("SELL") ? 1.4 : 0.8 }))
      .sort((a, b) => a.time - b.time);
    this.candles.setMarkers(markers);
  }

  renderBoxes(payload, candles) {
    const c = this.colors;
    const boxes = [];
    const times = candles.map((x) => x.time);
    const barSeconds = times.length > 1 ? Math.max(60, Math.round((times[times.length - 1] - times[Math.max(0, times.length - 20)]) / Math.min(19, times.length - 1))) : 300;
    if (this.overlays.sessions) {
      const palette = { ASIA: c.warn, ASIAN: c.warn, TOKYO: c.warn, LONDON: c.brand, LDN: c.brand, NY: c.violet, "NEW YORK": c.violet, NEWYORK: c.violet };
      for (const s of payload.sessions || []) {
        const color = palette[String(s.name).toUpperCase()] || c.info;
        boxes.push({ from: shift(s.from), to: shift(s.to), top: s.high, bottom: s.low, fill: alpha(color, 0.05), stroke: alpha(color, 0.45), dashed: true,
          label: `${s.name}${s.complete ? "" : " (live)"}`, labelColor: alpha(color, 0.9) });
      }
    }
    const fresh = (payload.zones || []).filter((z) => !z.mitigated).sort((a, b) => b.from - a.from);
    const zones = [...fresh.filter((z) => z.kind === "FVG").slice(0, 4), ...fresh.filter((z) => z.kind !== "FVG").slice(0, 3)];
    for (const z of zones) {
      const isFvg = z.kind === "FVG";
      if ((isFvg && !this.overlays.fvg) || (!isFvg && !this.overlays.ob)) continue;
      const buy = z.direction === "BUY";
      const color = isFvg ? (buy ? c.up : c.down) : (buy ? c.info : c.violet);
      boxes.push({ from: shift(z.from), to: z.to === null ? null : shift(z.to), top: z.top, bottom: z.bottom,
        fill: alpha(color, z.mitigated ? 0.06 : 0.13), stroke: alpha(color, z.mitigated ? 0.3 : 0.55), dashed: z.mitigated,
        label: `${isFvg ? "FVG" : "OB"} ${buy ? "▲" : "▼"}`, labelColor: alpha(color, 0.95) });
    }
    if (this.overlays.signals) {
      const recentFrom = times.length > 40 ? times[times.length - 40] : -Infinity;
      const tradeable = (payload.signals || []).filter((s) => ["A+", "A", "B"].includes(String(s.grade).toUpperCase()) && s.anchor
        && s.entry !== null && s.stop_loss !== null && shift(s.anchor) >= recentFrom);
      const latest = tradeable.sort((a, b) => b.anchor - a.anchor)[0];
      if (latest) {
        const from = shift(latest.anchor);
        const target = latest.tp2 ?? latest.tp1;
        boxes.push({ from, bars: 18, top: latest.entry, bottom: latest.stop_loss, fill: alpha(c.down, 0.14), stroke: alpha(c.down, 0.5), label: "SL", labelColor: c.down });
        if (target !== null && target !== undefined) {
          boxes.push({ from, bars: 18, top: target, bottom: latest.entry, fill: alpha(c.up, 0.12), stroke: alpha(c.up, 0.5), label: `${latest.grade} ${latest.direction} → ${fmtPrice(target, this.digits)}`, labelColor: c.up });
        }
      }
    }
    this.boxes.set(boxes, times, barSeconds);
  }

  renderLevels(payload) {
    for (const line of this.priceLines) this.candles.removePriceLine(line);
    this.priceLines = [];
    if (!this.overlays.levels) return;
    const L = LWC();
    const c = this.colors;
    const styles = {
      entry: { color: c.brand, lineStyle: L.LineStyle.Solid, lineWidth: 1 },
      sl: { color: c.down, lineStyle: L.LineStyle.Dashed, lineWidth: 1 },
      tp1: { color: c.up, lineStyle: L.LineStyle.Dotted, lineWidth: 1 },
      tp: { color: c.up, lineStyle: L.LineStyle.Dashed, lineWidth: 1 },
      order: { color: c.warn, lineStyle: L.LineStyle.LargeDashed, lineWidth: 1 },
    };
    for (const level of payload.levels || []) {
      if (level.price === null || level.price === undefined) continue;
      const style = styles[level.kind] || styles.entry;
      this.priceLines.push(this.candles.createPriceLine({ price: level.price, ...style, axisLabelVisible: true, title: level.title || level.kind.toUpperCase() }));
    }
  }

  tick(data) {
    if (!this.payload || !data?.candle || data.symbol !== this.payload.symbol || data.timeframe !== this.payload.timeframe) return;
    const bar = { ...data.candle, time: shift(data.candle.time) };
    if (this.lastBar && bar.time < this.lastBar.time) return;
    this.candles.update(bar);
    this.lastBar = { ...bar };
    if (data.volume && this.overlays.volume) {
      const c = this.colors;
      this.volume.update({ time: shift(data.volume.time), value: data.volume.value, color: alpha(data.volume.up ? c.up : c.down, 0.35) });
    }
    if (data.ema && data.ema.value !== null && this.overlays.ema) this.ema.update({ time: shift(data.ema.time), value: data.ema.value });
  }

  // Keep the forming candle in step with live quotes between server pushes.
  applyPrice(price) {
    if (!this.lastBar || !Number.isFinite(price)) return;
    const bar = this.lastBar;
    const next = { ...bar, close: price, high: Math.max(bar.high, price), low: Math.min(bar.low, price) };
    if (next.close === bar.close && next.high === bar.high && next.low === bar.low) return;
    this.lastBar = next;
    this.candles.update(next);
  }

  legendAt(param) {
    let bar = null;
    if (param && param.time !== undefined && param.seriesData) bar = param.seriesData.get(this.candles) || null;
    if (!bar) bar = this.lastBar;
    if (!bar) return null;
    const emaPoint = param?.seriesData?.get(this.ema);
    const volPoint = param?.seriesData?.get(this.volume);
    const change = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : 0;
    return { open: fmtPrice(bar.open, this.digits), high: fmtPrice(bar.high, this.digits), low: fmtPrice(bar.low, this.digits), close: fmtPrice(bar.close, this.digits),
      change, up: bar.close >= bar.open, ema: emaPoint ? fmtPrice(emaPoint.value, this.digits) : null, volume: volPoint ? fmtNum(volPoint.value, 0) : null };
  }

  fit() {
    const total = this.payload?.candles?.length || 0;
    if (total) this.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - 150), to: total + 8 });
    this.chart.priceScale("right").applyOptions({ autoScale: true });
  }

  resetKey() { this.key = null; this.firstLoad = true; }

  destroy() {
    this.offTheme?.();
    try { this.chart.remove(); } catch { /* already removed */ }
  }
}

// --------------------------------------------------------------- mini charts --
export class MiniChart {
  constructor(container, { valueFormatter = (v) => fmtNum(v, 2), timeVisible = true, rightScale = true } = {}) {
    if (!chartsAvailable()) throw new Error("Chart library failed to load");
    this.container = container;
    this.valueFormatter = valueFormatter;
    this.colors = themeColors();
    const opts = baseOptions(this.colors);
    opts.timeScale = { ...opts.timeScale, timeVisible, rightOffset: 2, barSpacing: 6 };
    opts.rightPriceScale = { ...opts.rightPriceScale, visible: rightScale, scaleMargins: { top: 0.12, bottom: 0.08 } };
    opts.localization = { priceFormatter: (v) => this.valueFormatter(v) };
    opts.handleScroll = false;
    opts.handleScale = false;
    this.chart = LWC().createChart(container, opts);
    this.series = [];
    this.specs = [];
    this.offTheme = on("theme", () => this.refreshTheme());
  }

  clear() {
    for (const s of this.series) this.chart.removeSeries(s);
    this.series = [];
    this.specs = [];
  }

  add(kind, data, options = {}) {
    const c = this.colors;
    const color = options.color ? (c[options.color] || options.color) : c.brand;
    let series;
    if (kind === "area") {
      series = this.chart.addAreaSeries({ lineColor: color, topColor: alpha(color, 0.32), bottomColor: alpha(color, 0.01), lineWidth: 2, priceLineVisible: false, lastValueVisible: options.lastValue ?? true });
    } else if (kind === "line") {
      series = this.chart.addLineSeries({ color, lineWidth: options.width || 1.5, lineStyle: options.dashed ? 2 : 0, priceLineVisible: false, lastValueVisible: options.lastValue ?? false, crosshairMarkerVisible: true });
    } else if (kind === "baseline") {
      series = this.chart.addBaselineSeries({ baseValue: { type: "price", price: options.base ?? 0 }, topLineColor: c.up, topFillColor1: alpha(c.up, 0.28), topFillColor2: alpha(c.up, 0.02),
        bottomLineColor: c.down, bottomFillColor1: alpha(c.down, 0.02), bottomFillColor2: alpha(c.down, 0.28), lineWidth: 2, priceLineVisible: false });
    } else {
      series = this.chart.addHistogramSeries({ color, priceLineVisible: false, lastValueVisible: false, base: 0 });
      data = data.map((d) => ({ ...d, color: d.color || (d.value >= 0 ? alpha(c.up, 0.85) : alpha(c.down, 0.85)) }));
    }
    series.applyOptions({ priceFormat: { type: "custom", formatter: (v) => this.valueFormatter(v), minMove: 0.01 } });
    series.setData(data);
    this.series.push(series);
    this.specs.push({ kind, data, options });
    return series;
  }

  fit() { this.chart.timeScale().fitContent(); }

  refreshTheme() {
    this.colors = themeColors();
    const opts = baseOptions(this.colors);
    this.chart.applyOptions({ layout: opts.layout, grid: opts.grid, crosshair: opts.crosshair, rightPriceScale: { borderColor: this.colors.border }, timeScale: { borderColor: this.colors.border } });
    const specs = this.specs;
    this.clear();
    for (const s of specs) this.add(s.kind, s.data.map(({ color, ...rest }) => rest), s.options);
    this.fit();
  }

  destroy() {
    this.offTheme?.();
    try { this.chart.remove(); } catch { /* already removed */ }
  }
}

export const toChartTime = (iso) => {
  const t = Math.floor(new Date(iso).getTime() / 1000);
  return Number.isFinite(t) ? shift(t) : null;
};
