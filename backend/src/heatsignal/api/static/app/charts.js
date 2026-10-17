// Graficos: lightweight-charts 4.2.0 (vendorizado, global window.LightweightCharts) para series de tiempo
// y SVG propio y liviano para el resto (curva costo->Sharpe, calibracion, descomposicion del PnL).
import { el, svgEl, num, fixed, frac, isNum } from "./core.js";

export const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Los graficos muestran hora LOCAL (igual que las tablas): lightweight-charts dibuja en UTC, se corre el offset. */
export const toChartTime = (ms) => Math.floor(ms / 1000) - new Date(ms).getTimezoneOffset() * 60;

const charts = new Set();
const darkMq = window.matchMedia("(prefers-color-scheme: dark)");
darkMq.addEventListener("change", () => { for (const c of charts) c._retheme(); });

function themeOptions() {
  return {
    layout: { background: { color: css("--surface") }, textColor: css("--muted"), fontFamily: css("--sans") || "system-ui" },
    grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
    rightPriceScale: { borderColor: css("--line") }, leftPriceScale: { borderColor: css("--line") },
    timeScale: { borderColor: css("--line") },
    crosshair: { vertLine: { labelBackgroundColor: css("--muted") }, horzLine: { labelBackgroundColor: css("--muted") } },
  };
}

/**
 * Crea un grafico de lightweight-charts en `node`. onTheme(chart) se llama al crear y al cambiar claro/oscuro
 * para recolorear las series. Devuelve null si la libreria no cargo.
 */
export function makeChart(node, options = {}, onTheme) {
  if (!window.LightweightCharts) { node.append(el("p", { class: "err" }, "No cargó la librería de gráficos (/static/lightweight-charts.js).")); return null; }
  const chart = window.LightweightCharts.createChart(node, {
    autoSize: true, localization: { locale: "es-AR" }, timeScale: { timeVisible: true, secondsVisible: false },
    handleScroll: { vertTouchDrag: false }, ...options,
  });
  chart._retheme = () => { chart.applyOptions(themeOptions()); if (onTheme) onTheme(chart); };
  chart.applyOptions(themeOptions());
  charts.add(chart);
  chart._dispose = () => { charts.delete(chart); try { chart.remove(); } catch { /* ya removido */ } };
  return chart;
}
export const disposeChart = (c) => c && c._dispose && c._dispose();

export function pricePrecision(v) {
  if (!isNum(v) || v <= 0) return 2;
  return v >= 1000 ? 1 : v >= 10 ? 2 : v >= 1 ? 4 : 6;
}

// ------------------------------------------------------------------ SVG

const W = 640;

function scale(d0, d1, r0, r1) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}
function niceTicks(lo, hi, n = 5) {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

/**
 * Lineas XY. series: [{name, color, points: [[x, y]]}]; marks: [{x, label, color}] lineas verticales.
 */
export function xyChart({ series, xLabel, yLabel, xFmt = (v) => num(v, 1), yFmt = (v) => num(v, 2), marks = [], height = 280, zeroLine = true, ariaLabel }) {
  const W = 440, H = height, m = { l: 52, r: 16, t: 14, b: 38 };
  const pts = series.flatMap((s) => s.points).filter((p) => isNum(p[0]) && isNum(p[1]));
  if (!pts.length) return el("p", { class: "empty" }, "Sin puntos para graficar.");
  let [x0, x1] = [Math.min(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[0]))];
  for (const mk of marks) { x0 = Math.min(x0, mk.x); x1 = Math.max(x1, mk.x); }
  let [y0, y1] = [Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[1]))];
  if (zeroLine) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  const pad = (y1 - y0) * 0.08 || 1; y0 -= pad; y1 += pad;
  const sx = scale(x0, x1, m.l, W - m.r), sy = scale(y0, y1, H - m.b, m.t);
  const g = svgEl("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": ariaLabel || yLabel || "gráfico" });
  for (const t of niceTicks(y0, y1)) {
    g.append(svgEl("line", { class: "grid-l", x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t) }),
      svgEl("text", { x: m.l - 6, y: sy(t) + 4, "text-anchor": "end" }, yFmt(t)));
  }
  for (const t of niceTicks(x0, x1, 6)) g.append(svgEl("text", { x: sx(t), y: H - m.b + 16, "text-anchor": "middle" }, xFmt(t)));
  if (zeroLine && y0 < 0 && y1 > 0) g.append(svgEl("line", { class: "axis", x1: m.l, x2: W - m.r, y1: sy(0), y2: sy(0) }));
  g.append(svgEl("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: H - m.b }),
    svgEl("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
  if (xLabel) g.append(svgEl("text", { x: (m.l + W - m.r) / 2, y: H - 4, "text-anchor": "middle" }, xLabel));
  if (yLabel) g.append(svgEl("text", { x: 12, y: (m.t + H - m.b) / 2, transform: `rotate(-90 12 ${(m.t + H - m.b) / 2})`, "text-anchor": "middle" }, yLabel));
  for (const mk of marks) {
    g.append(svgEl("line", { x1: sx(mk.x), x2: sx(mk.x), y1: m.t, y2: H - m.b, stroke: mk.color || "currentColor", "stroke-dasharray": "4 3", "stroke-width": 1.5 }),
      svgEl("text", { x: sx(mk.x) + 4, y: m.t + 10, style: `fill:${mk.color}` }, mk.label));
  }
  for (const s of series) {
    const p = s.points.filter((q) => isNum(q[0]) && isNum(q[1]));
    if (!p.length) continue;
    g.append(svgEl("polyline", { fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round",
      points: p.map((q) => sx(q[0]).toFixed(1) + "," + sy(q[1]).toFixed(1)).join(" ") }));
    for (const q of p.length <= 40 ? p : []) {
      g.append(svgEl("circle", { cx: sx(q[0]), cy: sy(q[1]), r: 3, fill: s.color },
        svgEl("title", {}, `${s.name}: ${xFmt(q[0])} → ${yFmt(q[1])}`)));
    }
  }
  return g;
}

/**
 * Diagrama de confiabilidad (10 buckets): barra = tasa de acierto observada, bigote = IC 95 %, punto = confidence medio,
 * diagonal = calibracion perfecta. El ancho/opacidad no esconde n: n va escrito arriba de cada bucket.
 */
export function calibrationChart(buckets) {
  const H = 300, m = { l: 46, r: 12, t: 22, b: 44 };
  const sx = scale(0, 1, m.l, W - m.r), sy = scale(0, 1, H - m.b, m.t);
  const g = svgEl("svg", { class: "viz cal", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Diagrama de calibración: tasa de acierto observada por tramo de confidence" });
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    g.append(svgEl("line", { class: "grid-l", x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t) }),
      svgEl("text", { x: m.l - 6, y: sy(t) + 4, "text-anchor": "end" }, frac(t, 0)));
  }
  for (let i = 0; i <= 10; i += 2) g.append(svgEl("text", { x: sx(i / 10), y: H - m.b + 16, "text-anchor": "middle" }, fixed(i / 10, 1)));
  g.append(svgEl("text", { x: (m.l + W - m.r) / 2, y: H - 6, "text-anchor": "middle" }, "confidence declarado"));
  g.append(svgEl("line", { x1: sx(0), y1: sy(0), x2: sx(1), y2: sy(1), stroke: css("--faint"), "stroke-dasharray": "5 4" }));
  const accent = css("--accent");
  for (const b of buckets) {
    const x = sx(b.lower) + 3, w = sx(b.upper) - sx(b.lower) - 6;
    g.append(svgEl("text", { x: x + w / 2, y: m.t - 8, "text-anchor": "middle" }, "n=" + b.n));
    if (!b.n || !isNum(b.hit_rate)) continue;
    const y = sy(b.hit_rate);
    g.append(svgEl("rect", { x, y, width: w, height: H - m.b - y, fill: accent, opacity: 0.28, rx: 3 },
      svgEl("title", {}, `confidence ${fixed(b.lower, 1)}–${fixed(b.upper, 1)}: acertó ${frac(b.hit_rate, 0)} de ${b.n}` +
        (b.ci95 ? ` (IC 95 % ${frac(b.ci95[0], 0)}–${frac(b.ci95[1], 0)})` : ""))));
    g.append(svgEl("line", { x1: x, x2: x + w, y1: y, y2: y, stroke: accent, "stroke-width": 2 }));
    if (b.ci95) {
      const cx = x + w / 2;
      g.append(svgEl("line", { x1: cx, x2: cx, y1: sy(b.ci95[0]), y2: sy(b.ci95[1]), stroke: css("--ink"), "stroke-width": 1.2 }),
        svgEl("line", { x1: cx - 5, x2: cx + 5, y1: sy(b.ci95[0]), y2: sy(b.ci95[0]), stroke: css("--ink") }),
        svgEl("line", { x1: cx - 5, x2: cx + 5, y1: sy(b.ci95[1]), y2: sy(b.ci95[1]), stroke: css("--ink") }));
    }
    if (isNum(b.mean_confidence)) g.append(svgEl("circle", { cx: sx(b.mean_confidence), cy: y, r: 3.5, fill: css("--s2") }));
  }
  return g;
}

/** Barras horizontales con signo (descomposicion del PnL). items: [{label, value}] en fraccion. */
export function signedBars(items, fmt = (v) => frac(v, 1)) {
  const W = 440, rowH = 32, H = items.length * rowH + 10, m = { l: 96, r: 64 };
  const max = Math.max(1e-9, ...items.map((i) => Math.abs(i.value || 0)));
  const sx = scale(-max, max, m.l, W - m.r), z = sx(0);
  const g = svgEl("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Descomposición del retorno" });
  g.append(svgEl("line", { class: "axis", x1: z, x2: z, y1: 0, y2: H }));
  items.forEach((it, i) => {
    const y = 5 + i * rowH, v = it.value || 0, x = Math.min(z, sx(v)), w = Math.abs(sx(v) - z);
    g.append(svgEl("text", { x: m.l - 8, y: y + 18, "text-anchor": "end", style: it.bold ? "font-weight:700" : "" }, it.label),
      svgEl("rect", { x, y: y + 5, width: Math.max(1, w), height: rowH - 12, rx: 3, fill: v >= 0 ? css("--buy") : css("--sell"), opacity: it.bold ? 0.9 : 0.6 }),
      svgEl("text", { x: v >= 0 ? sx(v) + 6 : z + 6, y: y + 18, "text-anchor": "start" }, fmt(v)));  // negativos: valor a la derecha del 0 (no pisa las etiquetas)
  });
  return g;
}

export function legend(items) {
  return el("div", { class: "legend" }, items.map(([color, text, box]) =>
    el("span", {}, el("span", { class: "sw" + (box ? " box" : ""), style: { background: color } }), text)));
}
