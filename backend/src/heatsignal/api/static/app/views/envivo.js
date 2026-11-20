// En vivo (#/envivo): velas de 5m y 15m CERRADAS armadas con los bars_1m propios (los de "Datos guardados") y el
// seguimiento de cada hipótesis abierta: cómo va, máximo a favor / en contra y alertas fijas por código.
// Fuente: /api/live (cache 20 s). Solo apoyo y monitoreo: no abre ni cierra hipótesis (la resolución es la del
// evaluador al vencimiento). Sin LLM.
import { el, svgEl, clear, api, errorBox, card, pageHead, loading, chip, table, fixed, price, signedPct, when, ago, int, stat, isNum, DASH } from "../core.js";

const ST = { ok: ["ok", "ok"], partial: ["warn", "parcial"], missing: ["bad", "sin datos"] };
const stChip = (s, title) => { const [k, t] = ST[s] || ["", s || DASH]; return chip(t, k, title); };
const atr = (v) => (isNum(v) ? (v >= 0 ? "+" : "−") + fixed(Math.abs(v), 2) + " ATR" : DASH);
const minutes = (m) => (isNum(m) ? (m >= 60 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : m + " min") : DASH);

/** Mini gráfico de velas (SVG, sin librerías) con las últimas velas cerradas. */
export function miniCandles(candles, { w = 180, h = 44 } = {}) {
  const cs = (candles || []).filter((c) => c && isNum(c[2]) && isNum(c[3]));
  if (cs.length < 2) return el("span", { class: "muted small" }, "sin velas");
  const hi = Math.max(...cs.map((c) => c[2])), lo = Math.min(...cs.map((c) => c[3]));
  const sy = (v) => h - 2 - ((v - lo) / Math.max(1e-12, hi - lo)) * (h - 4);
  const step = (w - 4) / cs.length;
  return svgEl("svg", { class: "spark", viewBox: `0 0 ${w} ${h}`, width: w, height: h, role: "img",
    "aria-label": cs.length + " velas: de " + price(cs[0][1]) + " a " + price(cs[cs.length - 1][4]) },
    ...cs.map((c, i) => {
      const x = 2 + i * step + step / 2, up = c[4] >= c[1];
      return svgEl("g", { class: up ? "up" : "down" },
        svgEl("line", { x1: x, x2: x, y1: sy(c[2]), y2: sy(c[3]), stroke: "currentColor", "stroke-width": 1 }),
        svgEl("rect", { x: x - step * 0.3, width: step * 0.6, y: Math.min(sy(c[1]), sy(c[4])),
          height: Math.max(1, Math.abs(sy(c[1]) - sy(c[4]))), fill: up ? "none" : "currentColor", stroke: "currentColor" }));
    }));
}

function tfRow(t) {
  if (!t) return [DASH, DASH, DASH, DASH, DASH, DASH];
  return [el("b", {}, t.timeframe), stChip(t.status, t.reason || ""),
    t.last_closed_open_ms ? when(t.last_closed_open_ms) : DASH,
    el("span", { class: "num" }, atr(t.dist_ema20_atr), el("span", { class: "muted small" }, " (pend. " + atr(t.ema20_slope_atr) + ")")),
    el("span", { class: "num" }, isNum(t.ret_3bars_pct) ? signedPct(t.ret_3bars_pct) : DASH),
    miniCandles(t.candles)];
}

function hypTable(hs) {
  if (!hs.length) return el("p", { class: "muted small" }, "Sin hipótesis abiertas en esta cripto.");
  return table([{ label: "Hipótesis" }, { label: "Dirección" }, { label: "Entrada → ahora" }, { label: "Va", cls: "num" },
    { label: "Máx. a favor", cls: "num" }, { label: "Máx. en contra", cls: "num" }, { label: "Vence en" }, { label: "Alertas (código)" }],
  hs.map((h) => [
    el("div", {}, el("a", { href: "#/hipotesis/" + encodeURIComponent(h.hypothesis_id) }, el("b", { class: "small" }, h.strategy || h.strategy_id.slice(0, 8))),
      el("div", { class: "muted small" }, (h.timeframe || "") + " · abierta " + ago(h.created_ms))),
    chip(h.direction, h.direction === "LONG" ? "ok" : "bad"),
    el("span", { class: "num small" }, price(h.entry_price) + " → " + price(h.last_price)),
    el("span", { class: "num " + (isNum(h.pnl_pct) ? (h.pnl_pct >= 0 ? "ok-t" : "bad-t") : "") }, isNum(h.pnl_pct) ? signedPct(h.pnl_pct) : DASH,
      isNum(h.pnl_atr15) ? el("div", { class: "muted small" }, atr(h.pnl_atr15) + " de 15m") : null),
    el("span", { class: "num" }, isNum(h.mfe_pct) ? signedPct(h.mfe_pct) : DASH),
    el("span", { class: "num" }, isNum(h.mae_pct) ? signedPct(h.mae_pct) : DASH),
    minutes(h.minutes_to_due),
    h.alerts && h.alerts.length ? el("div", { class: "chips" }, h.alerts.map((a) => chip(a, /a favor/.test(a) ? "ok" : "warn"))) : el("span", { class: "muted small" }, "ninguna")]));
}

function symbolCard(s) {
  const lv = s.live || {}, tfs = lv.tfs || {};
  const head = el("span", { class: "chips" }, stChip(lv.status), lv.last_price ? chip(price(lv.last_price), "", "último minuto con precio") : null,
    lv.last_minute_ms ? el("span", { class: "muted small" }, "minuto " + when(lv.last_minute_ms) + " (" + ago(lv.last_minute_ms) + ")") : null);
  return card(s.symbol, head,
    table([{ label: "Velas" }, { label: "Estado" }, { label: "Última cerrada" }, { label: "Cierre vs EMA20" }, { label: "Últimas 3", cls: "num" }, { label: "Últimas 12" }],
      [tfRow(tfs["15m"]), tfRow(tfs["5m"])]),
    el("p", { class: "small" }, "Precio vs VWAP del día: ", el("b", {}, atr(lv.dist_vwap_atr15)), " de 15m",
      isNum(lv.vwap_day) ? el("span", { class: "muted" }, " (VWAP " + price(lv.vwap_day) + ")") : null),
    (lv.data_gaps || []).length ? el("div", { class: "note warn small" }, lv.data_gaps.join(" · ")) : null,
    hypTable(s.hypotheses || []));
}

export async function mount(root, ctx) {
  const head = pageHead("En vivo", "Velas de 5m y 15m cerradas, armadas con las barras de 1m propias (las de Datos guardados), y cómo va cada hipótesis abierta. "
    + "Solo apoyo y monitoreo: no abre ni cierra hipótesis; la resolución es la del evaluador al vencimiento. Sin LLM.");
  const box = el("div", {}, loading());
  clear(root, head, box);
  async function load() {
    try {
      const d = await api("/api/live", null, ctx.signal);
      const syms = d.symbols || [];
      const withHyp = syms.filter((s) => (s.hypotheses || []).length);
      const rest = syms.filter((s) => !(s.hypotheses || []).length);
      clear(box,
        el("div", { class: "stats" },
          stat("Hipótesis abiertas", int(d.open_hypotheses), "seguidas minuto a minuto"),
          stat("Alertas", int(d.alerts), "reglas fijas de código", d.alerts ? "warn-t" : ""),
          stat("Actualizado", when(d.ts_ms, true), "se refresca cada 30 s")),
        el("p", { class: "small muted" }, d.nota || ""),
        ...withHyp.map(symbolCard), ...rest.map(symbolCard));
    } catch (e) { const b = errorBox(e, "las velas en vivo"); if (b) clear(box, b); }
  }
  await load();
  ctx.every(30000, load);
  return { refresh: load };
}
