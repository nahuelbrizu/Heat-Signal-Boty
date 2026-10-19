// Contexto multi-temporalidad: velas de 1h CERRADAS (nuestra base; REST de Binance solo para huecos, declarado) y
// tendencia del dia UTC con la regla fija htf-trend-v1 (features/htf.py). Lo usan Moneda, Analisis e Hipotesis.
import { el, clear, api, errorBox, chip, stat, table, int, fixed, price, signedPct, frac, isNum, rateText, ciBar, signed, when, ago, DASH } from "./core.js";
import { candlePanel } from "./candles.js";
import { css } from "./charts.js";

export const TREND = { alcista: ["ok", "▲ alcista"], bajista: ["bad", "▼ bajista"], lateral: ["", "↔ lateral"], sin_datos: ["warn", "sin datos"], sin_registro: ["", "sin registro"] };
export const ALIGN = { a_favor: ["ok", "a favor"], en_contra: ["warn", "en contra"], lateral: ["", "tendencia lateral"], sin_datos: ["warn", "sin datos"], sin_registro: ["", "sin registro"] };
const SETUP_TEXT = { pullback_ema20_1h_a_favor: "retroceso a la EMA20 1h a favor", continuacion_a_favor: "continuación a favor" };

export function trendChip(t, title) {
  if (!t) return el("span", { class: "muted", title: "hipótesis previa al contexto 1h" }, DASH);
  const [k, txt] = TREND[t] || ["", t];
  return chip(txt, k, title || "tendencia del día UTC (htf-trend-v1, calculada por código)");
}
export function alignChip(a) {
  if (!a) return el("span", { class: "muted", title: "hipótesis previa al contexto 1h" }, DASH);
  const [k, txt] = ALIGN[a] || ["", a];
  return chip(txt, k, "dirección de la hipótesis respecto de la tendencia del día");
}
export const SESSION_LABEL = { asia: "Asia", europa: "Europa", eeuu: "EE.UU.", cierre: "Cierre UTC", sin_registro: "sin registro" };
export function sessionText(h) {
  if (!h.session) return el("span", { class: "muted", title: "hipótesis previa al diario de sesiones" }, DASH);
  return el("span", { title: "última sesión analizada al emitir · día " + (h.session_day || "") }, SESSION_LABEL[h.session] || h.session);
}
export function tfText(h) {
  if (!h.timeframe) return el("span", { class: "muted", title: "estrategia sin temporalidad fija (horizonte en segundos)" }, DASH);
  return h.timeframe + (isNum(h.horizon_bars) ? " · " + h.horizon_bars + (h.horizon_bars === 1 ? " vela" : " velas") : "");
}
const LATERAL_TEXT = { falsa_ruptura_asia: "falsa ruptura del rango de Asia", rechazo_dia_anterior: "rechazo del extremo de ayer", extremo_vwap: "extremo vs VWAP (ADX < 20)" };
/** Banderas de días laterales (htf-levels-v1): "SHORT falsa ruptura…" o "ninguna". */
export function lateralText(lv) {
  if (!lv) return "sin datos";
  return Object.entries(LATERAL_TEXT).filter(([k]) => lv[k]).map(([k, t]) => lv[k] + " · " + t).join(" · ") || "ninguna";
}
function setupsText(s) {
  if (!s) return "ninguno";
  const out = Object.entries(SETUP_TEXT).filter(([k]) => s[k]).map(([, t]) => t);
  if (s.extremo_rango_diario) out.push("cierre en el " + s.extremo_rango_diario + " del rango diario");
  return out.join(" · ") || "ninguno";
}

/** Linea corta para el analisis de un ciclo (a.htf = brief de run_view). */
export function htfBrief(b) {
  if (!b) return null;
  return el("div", { class: "row small", style: { margin: "4px 0 8px" } }, el("b", {}, "Tendencia del día"), trendChip(b.daily_trend, b.trend_reason),
    el("span", { class: "muted" }, "desde la apertura UTC " + signedPct(b.change_from_open_pct, 2) + " · posición en el rango " + (isNum(b.pos_in_day_range) ? frac(b.pos_in_day_range, 0) : DASH) +
      " · vs EMA20 1h " + (isNum(b.dist_ema20_1h_atr) ? fixed(b.dist_ema20_1h_atr, 2) + " ATR" : DASH) + " · setups: " + setupsText(b.setups) + (b.daily_trend === "lateral" && b.levels ? " · lateral: " + lateralText(b.levels) : "")));
}

/** Tarjeta "Velas de 1h y tendencia del día" de una moneda. Devuelve {node, reload(fit), reloadHyps(), dispose()}. */
export function htfPanel(ctx, symbol, { title = "Velas de 1h cerradas y tendencia del día" } = {}) {
  let last = null;
  const statsBox = el("div", {}, el("p", { class: "muted" }, "Cargando…"));
  const gapsBox = el("div");
  async function fetchCtx() {
    last = await api("/api/htf/" + encodeURIComponent(symbol), { candles: 48 }, ctx.signal);
    return last;
  }
  const chart = candlePanel(ctx, symbol, {
    title, interval: "1h", intervals: [["1h", 48]], tall: false, small: true,
    source: async () => (await fetchCtx()).candles_1h.map((c) => ({ t: c.open_ms / 1000, o: c.open, h: c.high, l: c.low, c: c.close })),
    legendItems: [[css("--buy"), "▲ hipótesis LONG"], [css("--sell"), "▼ hipótesis SHORT"], [css("--s1"), "EMA20 1h"], [css("--s4"), "apertura del día UTC"],
      [css("--s5"), "máximo / mínimo del día"], [css("--s2"), "rango de Asia (00–08 UTC)"], [css("--s3"), "máx / mín de ayer"], [css("--warn"), "VWAP del día"]],
    note: "Últimas 48 velas de 1h CERRADAS de nuestra base (klines 1m de Binance + barras propias; REST de Binance solo para huecos, declarado). Hora local. ",
  });
  const node = el("div", {}, chart.node, el("section", { class: "card" }, statsBox, gapsBox));

  function render() {
    const h = last;
    if (!h) return;
    const lv = h.levels;
    chart.setLevels([
      { price: h.ema20_1h, title: "EMA20 1h", color: css("--s1"), style: 0 },
      { price: h.day_open, title: "apertura UTC", color: css("--s4") },
      { price: h.day_high, title: "máx día", color: css("--s5") },
      { price: h.day_low, title: "mín día", color: css("--s5") },
      ...(lv ? [
        { price: lv.asia_high, title: "máx Asia", color: css("--s2") },
        { price: lv.asia_low, title: "mín Asia", color: css("--s2") },
        { price: lv.prev_day_high, title: "máx ayer", color: css("--s3") },
        { price: lv.prev_day_low, title: "mín ayer", color: css("--s3") },
        { price: lv.vwap, title: "VWAP", color: css("--warn"), style: 0 },
      ].filter((x) => isNum(x.price)) : []),
    ]);
    const src = Object.entries(h.sources || {}).map(([k, v]) => k + " " + v).join(" · ") || "ninguna";
    clear(statsBox,
      el("div", { class: "row", style: { marginBottom: "8px" } }, el("b", {}, "Tendencia del día UTC"), trendChip(h.daily_trend),
        chip(h.htf_model_id, "info", "versión de la regla (features/htf.py)"), h.status !== "ok" ? chip("datos " + (h.status === "partial" ? "parciales" : "faltantes"), "warn") : null,
        el("span", { class: "small muted" }, h.trend_reason || "")),
      el("div", { class: "stats" },
        stat("Desde la apertura UTC", signed(h.change_from_open_pct), isNum(h.change_from_open_atr) ? fixed(h.change_from_open_atr, 2) + " ATR(1h) · regla: ±1,5" : DASH),
        stat("Posición en el rango del día", isNum(h.pos_in_day_range) ? frac(h.pos_in_day_range, 0) : DASH, "máx " + price(h.day_high) + " · mín " + price(h.day_low)),
        stat("Cierre vs EMA20 1h", isNum(h.dist_ema20_1h_atr) ? fixed(h.dist_ema20_1h_atr, 2) + " ATR" : DASH, "pendiente " + (isNum(h.ema20_1h_slope_atr) ? fixed(h.ema20_1h_slope_atr, 2) : DASH) + " ATR en 4 velas"),
        stat("Cierre vs EMA10 diaria", signed(h.close_vs_ema10_1d_pct), "EMA10 " + price(h.ema10_1d) + " · SMA20 " + price(h.sma20_1d) + " · " + int(h.daily_closes) + " días"),
        stat("ATR14 de 1h", price(h.atr14_1h), isNum(h.atr14_1h_pct) ? fixed(h.atr14_1h_pct, 3) + " % del precio" : DASH),
        stat("Setups (código)", setupsText(h.setups), int(h.day_hours_closed) + " velas cerradas hoy · última " + when(h.last_closed_open_ms))),
      levelsStats(h),
      el("p", { class: "small muted", style: { marginTop: "6px" } }, "Regla fija: alcista si el cambio desde la apertura ≥ +1,5 ATR(1h), el cierre está sobre la EMA10 diaria y la EMA20 de 1h sube; bajista al revés; si no, lateral. Fuentes de las 48 velas: " + src + ". Calculado " + ago(h.ts_ms) + "."));
    clear(gapsBox, (h.data_gaps || []).length ? el("div", { class: "note warn" }, el("b", {}, "Huecos declarados: "), h.data_gaps.join("; ")) : null);
  }

  async function reload(fit) {
    try { await chart.reload(fit); render(); }
    catch (e) { const b = errorBox(e, "el contexto de 1h"); if (b) clear(statsBox, b); }
    if (!last) { try { await fetchCtx(); render(); } catch (e) { const b = errorBox(e, "el contexto de 1h"); if (b) clear(statsBox, b); } }
  }
  return { node, reload, reloadHyps: () => chart.reloadHyps(), dispose: () => chart.dispose(), chart, last: () => last };
}

/** Niveles para días laterales (htf-levels-v1): rango de Asia, extremos de ayer, VWAP, ADX, mechas y banderas. */
function levelsStats(h) {
  const lv = h.levels;
  if (!lv) return el("p", { class: "small muted" }, "Niveles de días laterales: no calculados.");
  const sd = "sin datos";
  const pct = (x) => (isNum(x) ? frac(x, 0) : DASH);
  return el("div", {},
    el("div", { class: "row", style: { margin: "10px 0 6px" } }, el("b", {}, "Niveles para días laterales"), chip(lv.levels_model_id, "info", "versión de los niveles (features/htf.py)"),
      el("span", { class: "small muted" }, h.daily_trend === "lateral" ? "día lateral: las 3 estrategias de lateralidad pueden aplicar" : "solo aplican con tendencia lateral")),
    el("div", { class: "stats" },
      stat("Rango de Asia (00–08 UTC)", isNum(lv.asia_high) ? price(lv.asia_low) + " – " + price(lv.asia_high) : sd, int(lv.asia_hours) + "/8 velas"),
      stat("Máx / mín de ayer (UTC)", isNum(lv.prev_day_high) ? price(lv.prev_day_high) + " / " + price(lv.prev_day_low) : sd, "día UTC anterior completo"),
      stat("VWAP del día", isNum(lv.vwap) ? price(lv.vwap) : sd, isNum(lv.dist_vwap_atr) ? "cierre a " + fixed(lv.dist_vwap_atr, 2) + " ATR · regla: ≥ 2" : int(lv.vwap_minutes) + " min de 1m"),
      stat("ADX14 1h", isNum(lv.adx14_1h) ? fixed(lv.adx14_1h, 1) : sd, "regla VWAP: < 20"),
      stat("Última vela: mechas", pct(lv.upper_wick_frac) + " sup · " + pct(lv.lower_wick_frac) + " inf", "del rango " + price(lv.last_range) + " · regla rechazo: ≥ 50 %"),
      stat("Banderas laterales (código)", lateralText(h.daily_trend === "lateral" ? lv : null) === "sin datos" ? "ninguna (día no lateral)" : lateralText(lv), "falsa ruptura solo en Europa/EE.UU.")),
    (lv.sin_datos || []).length ? el("p", { class: "small muted" }, "Sin datos: " + lv.sin_datos.join("; ")) : null);
}

/** Tabla de tasas por segmento (respuesta de /api/hypotheses/by-trend). */
function segTable(items, keyCols, label) {
  return table([...keyCols.map(([, l]) => ({ label: l })), { label: "n", cls: "n", title: "resueltas" }, { label: "Tasa de acierto (IC 95 % Wilson)" }, { label: "", title: "intervalo" },
    { label: "PnL neto medio", cls: "n" }, { label: "Mantener medio", cls: "n", title: "buy & hold en la misma ventana" }, { label: "Alfa medio", cls: "n" },
    { label: "Pend.", cls: "n" }, { label: "Sin dato", cls: "n", title: "expired_no_data" }],
  items.map((s) => [...keyCols.map(([k]) => (k === "session" ? (SESSION_LABEL[s[k]] || s[k]) : k === "daily_trend" ? trendChip(s[k] === "sin_registro" ? null : s[k]) : alignChip(s[k] === "sin_registro" ? null : s[k]))),
    int(s.n), rateText(s.wins, s.n, s.ci95), s.n ? el("div", { style: { minWidth: "120px" } }, ciBar(s.rate, s.ci95)) : DASH,
    signed(s.mean_pnl_pct), signed(s.mean_benchmark_pnl_pct), signed(s.mean_alpha_pct), int(s.pending), int(s.expired_no_data)]),
  { empty: "Sin hipótesis.", label });
}

export function trendCuts(d) {
  const o = d.overall || {};
  return el("div", {},
    el("p", { class: "small muted" }, "Total con estos filtros: ", rateText(o.wins, o.n, o.ci95), ". ", "Tendencia y alineación se registran al emitir cada hipótesis (", d.rule, "); \"sin registro\" = hipótesis anteriores al contexto 1h. Con n chico el intervalo es ancho: no hay conclusión."),
    el("h3", { style: { margin: "10px 0 4px" } }, "Por tendencia del día"), segTable(d.by_daily_trend || [], [["daily_trend", "Tendencia"]], "Tasa por tendencia del día"),
    el("h3", { style: { margin: "14px 0 4px" } }, "A favor / en contra de la tendencia"), segTable(d.by_alignment || [], [["trend_alignment", "Dirección"]], "Tasa a favor o en contra"),
    el("h3", { style: { margin: "14px 0 4px" } }, "Por sesión de la que salió"), segTable(d.by_session || [], [["session", "Sesión"]], "Tasa por sesión"),
    (d.by_trend_and_alignment || []).length ? el("details", { class: "box", style: { marginTop: "10px" } }, el("summary", {}, el("b", {}, "Cruce tendencia × dirección")),
      segTable(d.by_trend_and_alignment, [["daily_trend", "Tendencia"], ["trend_alignment", "Dirección"]], "Cruce tendencia y dirección")) : null);
}
