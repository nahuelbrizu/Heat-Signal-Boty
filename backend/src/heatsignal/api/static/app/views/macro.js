// Macro y eventos (#/macro): series de FRED con su fecha de publicación (point-in-time), calendario de FOMC / CPI /
// empleo, probabilidades de Polymarket con mini gráfico de 7 días y estado de cada fuente. Debajo, el bloque exacto
// que recibe el Analista de Contexto. Fuente: /api/macro (solo lectura). Lenguaje histórico: datos publicados.
import { el, svgEl, clear, api, errorBox, card, pageHead, loading, chip, kv, table, prose, num, fixed, when, ago, isNum, DASH } from "../core.js";

const STATE = { ok: ["ok", "ok"], apagado: ["warn", "apagado"], sin_datos: ["info", "sin datos todavía"], atrasado: ["warn", "atrasado"], error: ["bad", "error"], aviso: ["warn", "aviso"] };
const KIND = { FOMC: "FOMC", CPI: "CPI", NFP: "Empleo (NFP)" };
const SRC_NAME = { fred: "FRED (St. Louis Fed)", polymarket: "Polymarket", calendar: "Calendario versionado" };

function stateChip(s) { const [k, t] = STATE[s] || ["", s || DASH]; return chip(t, k); }
const pp = (v) => (isNum(v) ? (v >= 0 ? "+" : "−") + fixed(Math.abs(v) * 100, 0) + " pp" : DASH);
const pubDate = (s) => (s.realtime_clipped ? "≤ " : "") + s.realtime_start;

/** Mini gráfico SVG (sin librerías): línea de la probabilidad 0–100 %. */
export function spark(points, { w = 140, h = 34 } = {}) {
  const pts = (points || []).filter((p) => isNum(p[0]) && isNum(p[1]));
  if (pts.length < 2) return el("span", { class: "muted small" }, "sin serie");
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0] || x0 + 1;
  const sx = (x) => 2 + ((x - x0) / Math.max(1, x1 - x0)) * (w - 4), sy = (p) => h - 2 - p * (h - 4);
  const last = pts[pts.length - 1];
  return svgEl("svg", { class: "spark", viewBox: `0 0 ${w} ${h}`, width: w, height: h, role: "img",
    "aria-label": "probabilidad en 7 días: de " + fixed(pts[0][1] * 100, 0) + " % a " + fixed(last[1] * 100, 0) + " %" },
    svgEl("line", { class: "grid-l", x1: 0, x2: w, y1: sy(0.5), y2: sy(0.5) }),
    svgEl("polyline", { fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linejoin": "round",
      points: pts.map((p) => sx(p[0]).toFixed(1) + "," + sy(p[1]).toFixed(1)).join(" ") }),
    svgEl("circle", { cx: sx(last[0]), cy: sy(last[1]), r: 2.4, fill: "currentColor" }));
}

function sourcesCard(d) {
  const rows = Object.entries(d.sources || {}).map(([k, s]) => {
    const lastOk = s.last_ok, err = s.last_error;
    return [SRC_NAME[k] || k, stateChip(s.state),
      k === "fred" ? (s.key_loaded ? chip("key cargada", "ok") : chip("sin FRED_API_KEY", "warn")) : k === "polymarket" ? "pública, sin key" : (s.calendar_id || DASH),
      lastOk ? el("span", {}, when(lastOk.finished_ms), " (" + ago(lastOk.finished_ms) + ")") : (k === "calendar" ? "verificado " + (s.verified_at || DASH) : DASH),
      el("span", { class: "small" }, s.detail || "", err ? el("span", {}, s.detail ? " · " : "", "último error ", when(err.finished_ms), ": " + String(err.detail || "").slice(0, 160)) : (s.detail ? "" : DASH))];
  });
  return card("Estado de las fuentes", d.db_note || "", table(
    [{ label: "Fuente" }, { label: "Estado" }, { label: "Key" }, { label: "Último dato OK" }, { label: "Detalle / errores" }], rows, { label: "estado de fuentes macro" }));
}

function fredCard(d) {
  const off = d.sources && d.sources.fred && d.sources.fred.state === "apagado";
  if (!(d.fred || []).length) {
    return card("Series de FRED", "point-in-time",
      el("p", { class: off ? "note warn" : "empty" }, off ? "FRED está apagado: falta FRED_API_KEY en el .env de la VM (key gratuita en fred.stlouisfed.org)." : "Todavía no hay valores publicados guardados."));
  }
  const rows = d.fred.map((s) => {
    const ch = s.change_kind === "pct"
      ? (isNum(s.change_1) ? fixed(s.change_1, 2) + " % m/m" : DASH) + (isNum(s.change_12) ? " · " + fixed(s.change_12, 1) + " % a/a" : "")
      : (isNum(s.change_5) ? (s.change_5 >= 0 ? "+" : "") + fixed(s.change_5, 2) + " en 5 obs." : DASH);
    return [el("span", {}, el("b", {}, s.series_id), el("div", { class: "small muted" }, s.name)),
      { v: num(s.value, 2) + (s.units === "%" ? " %" : ""), cls: "num" }, s.obs_date, pubDate(s), ch, s.frequency];
  });
  return card("Series de FRED", "valor = último vintage publicado hasta ahora; ≤ = publicado en esa fecha o antes",
    table([{ label: "Serie" }, { label: "Valor", cls: "num" }, { label: "Observación" }, { label: "Publicado" }, { label: "Variación" }, { label: "Frecuencia" }], rows, { label: "series de FRED" }),
    el("p", { class: "small muted", style: { marginTop: "8px" } }, "Un valor existe en el sistema solo desde el día siguiente a su publicación (hora de Chicago): las revisiones no reescriben el pasado."));
}

function calendarCard(d) {
  const c = d.calendar;
  if (!c) return card("Calendario de eventos", "", el("p", { class: "note bad" }, d.calendar_error || "calendario no disponible"));
  const up = c.upcoming || [];
  const rows = (c.next || []).map((e) => [chip(KIND[e.kind] || e.kind, e.kind === "FOMC" ? "info" : ""), e.label, when(e.ts_ms), e.ts_at.slice(0, 16).replace("T", " ") + " UTC", e.tentative ? chip("tentativa", "warn") : ""]);
  return card("Calendario de eventos", "fuentes oficiales, archivo versionado " + c.calendar_id,
    up.length ? el("div", { class: "note info" }, el("b", {}, "En las próximas " + c.window_h + " h: "), up.map((e, i) => [i ? "; " : "", e.label, " (", when(e.ts_ms), ")"]))
      : el("p", { class: "small muted" }, "Ningún evento en las próximas " + c.window_h + " h."),
    c.warning ? el("div", { class: "note warn" }, c.warning) : null,
    table([{ label: "Tipo" }, { label: "Evento" }, { label: "Hora local" }, { label: "UTC" }, { label: "" }], rows, { label: "próximos eventos" }),
    el("p", { class: "small muted", style: { marginTop: "8px" } }, "Fuentes: ",
      Object.entries(c.sources || {}).map(([k, u]) => (KIND[k] || k) + " " + u).join(" · ")));
}

function polyCard(d) {
  const rows = (d.polymarket || []).map((r) => [
    el("span", {}, el("div", { class: "small muted" }, r.label || r.config_key || ""), r.question || r.slug || r.market_id),
    { v: fixed(r.prob * 100, 0) + " %", cls: "num" },
    { v: el("span", { title: r.change_24h_source === "api" ? "variación informada por la API (no tenemos lectura propia de hace 24 h)" : "contra nuestra lectura de hace ~24 h" }, pp(r.change_24h), r.change_24h_source === "api" ? " (API)" : ""), cls: "num" },
    el("span", { class: "spark-wrap" }, spark(r.series)),
    { v: isNum(r.volume) ? "$" + num(r.volume, 0) : DASH, cls: "num" },
    { v: isNum(r.liquidity) ? "$" + num(r.liquidity, 0) : DASH, cls: "num" },
    r.end_date ? String(r.end_date).slice(0, 10) : DASH]);
  return card("Polymarket", d.polymarket_read_ms ? el("span", {}, "leído ", when(d.polymarket_read_ms), " (" + ago(d.polymarket_read_ms) + ")") : "sin lecturas",
    table([{ label: "Mercado" }, { label: "Prob.", cls: "num" }, { label: "24 h", cls: "num" }, { label: "7 días" }, { label: "Volumen", cls: "num" }, { label: "Liquidez", cls: "num" }, { label: "Vence" }], rows,
      { label: "mercados de Polymarket", empty: "Sin lecturas recientes (la lista está en macro/config/polymarket_markets.json)." }),
    el("p", { class: "small muted", style: { marginTop: "8px" } }, "Probabilidad implícita = precio del \"Yes\" cuando se leyó. Es lo que el mercado pagaba, no un pronóstico de HeatSignal."));
}

export function renderOverview(d) {
  return [sourcesCard(d), el("div", { class: "grid two" }, fredCard(d), calendarCard(d)), polyCard(d)];
}

export function renderDetail(d) {
  return card("Bloque que recibe el grafo", d.context_id + " · " + d.block_chars + " caracteres · ~" + d.block_tokens_est + " tokens (estimado)",
    el("p", { class: "small muted" }, "Va UNA vez, al final del mensaje del Analista de Contexto de Mercado. No suma llamadas al LLM."),
    el("pre", { class: "mono small", style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 } }, d.block || ""));
}

export async function mount(root, ctx) {
  const body = el("div", {}, loading()), block = el("div");
  clear(root, pageHead("Macro y eventos", "Contexto macro point-in-time: datos publicados y probabilidades leídas, nunca pronósticos."), body, block);
  async function load() {
    try {
      const d = await api("/api/macro", null, ctx.signal);
      clear(body, ...renderOverview(d));
      clear(block, renderDetail(d));
    } catch (e) { const b = errorBox(e, "el contexto macro"); if (b) clear(body, b); }
  }
  await load();
  ctx.every(5 * 60000, load);
  return { refresh: load };
}
