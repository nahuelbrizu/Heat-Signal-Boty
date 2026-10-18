// Salidas: ficha (stop, objetivo, tiempo, otra) de lab/exits.py y conteo de cierres por motivo. Lo usan Laboratorio,
// Bot paper, Paper (Freqtrade), Hipótesis y Semanal. Render puro; todo texto de datos entra como nodo de texto (el()).
import { el, chip, card, kv, int, frac, table, isNum, DASH } from "./core.js";

export const MOTIVO = { stop: "stop", objetivo: "objetivo", tiempo: "tiempo", senal: "señal", rebalanceo: "rebalanceo",
  abierta: "abierta al final", sin_dato: "sin registro" };
const MOTIVO_COLOR = { stop: "var(--bad)", objetivo: "var(--ok)", tiempo: "var(--s1)", senal: "var(--s3)", rebalanceo: "var(--s4)",
  stop_loss: "var(--bad)", roi: "var(--ok)", hyp_due: "var(--s1)", exit_signal: "var(--s3)" };
const MOTIVO_KIND = { stop: "bad", objetivo: "ok", tiempo: "info", senal: "", rebalanceo: "" };
export const FT_REASON = { exit_signal: "señal", stop_loss: "stop", stoploss_on_exchange: "stop (exchange)", trailing_stop_loss: "stop móvil",
  roi: "objetivo (ROI)", hyp_due: "tiempo (vencimiento)", hyp_tag_invalido: "tag inválido", force_exit: "cierre forzado",
  liquidation: "liquidación", emergency_exit: "salida de emergencia" };

/** Chip con el motivo de un cierre (stop, objetivo, tiempo, senal, rebalanceo). */
export function motivoChip(m) {
  if (!m) return el("span", { class: "muted", title: "cierre anterior al registro de motivos" }, DASH);
  return chip(MOTIVO[m] || m, MOTIVO_KIND[m] || "");
}

/** Chips cortos de la ficha: "stop 1,5 ATR · objetivo 3 ATR · máx. 30 d" o "sin stop: canal". */
export function exitChips(f, { small = false } = {}) {
  if (!f) return el("span", { class: "muted small", title: "estrategia sin ficha de salidas" }, DASH);
  const kind = (t) => t.startsWith("sin stop") ? "warn" : t.startsWith("stop") ? "ok" : t.startsWith("objetivo") ? "info" : "";
  return el("div", { class: "chips" + (small ? " small" : ""), title: f.nota || "" }, (f.chips || []).map((t) => chip(t, kind(t))),
    (f.componentes || []).map((c) => el("div", { class: "small muted" }, c.strategy_id + ": " + (c.resumen || DASH))));
}

const PART = [["stop", "Stop"], ["take_profit", "Objetivo"]];
function partText(p) {
  if (!p) return el("span", { class: "muted" }, "no tiene");
  return el("span", {}, el("b", {}, p.tipo === "atr" ? p.valor + " ATR" : p.tipo === "sigma" ? p.valor + " σ" : p.tipo === "z" ? "|z| " + p.valor
    : p.tipo === "retroceso" ? Math.round(p.valor * 100) + " % de retroceso" : p.tipo === "ema" ? "EMA" + p.valor : p.tipo === "vwap" ? "VWAP" : p.tipo),
  p.base ? " — " + p.base : "", p.param ? el("span", { class: "mono small muted" }, " (" + p.param + ")") : "",
  el("div", { class: "small muted" }, p.velas_1m ? "evaluado en velas de 1m" + (p.empate === "stop" ? "; si en la misma vela se tocan stop y objetivo, cuenta el stop" : "")
    : "evaluado al cierre de la vela de la regla"));
}

/** Filas de la ficha (stop, objetivo, tiempo, otra, nota). */
export function exitRows(f) {
  if (!f) return el("p", { class: "muted small" }, "Sin ficha de salidas para esta estrategia.");
  const t = f.tiempo, o = f.otra;
  return kv([...PART.map(([k, l]) => [l, partText(f[k])]),
    ["Tiempo", t ? el("span", {}, el("b", {}, "máx. " + (f.tiempo_texto || t.valor + " " + t.unidad)), t.fuente ? " — " + t.fuente : "",
      t.param ? el("span", { class: "mono small muted" }, " (" + t.param + ")") : "") : el("span", { class: "muted" }, "sin tope")],
    ["Otra salida", o ? el("span", {}, el("b", {}, o.tipo === "canal" ? "canal" : o.tipo === "senal" ? "señal" : o.tipo === "composicion" ? "cambio de composición"
      : o.tipo === "programada" ? "giro programado" : o.tipo), " — " + (o.detalle || "")) : el("span", { class: "muted" }, "no")],
    ["En una línea", f.nota || DASH]]);
}

/** Barras horizontales: una por motivo, con n y %. counts = {motivo: n}. */
export function reasonBars(counts, labels = MOTIVO) {
  const entries = Object.entries(counts || {}).filter(([, n]) => isNum(n));
  const total = entries.reduce((a, [, n]) => a + n, 0);
  if (!total) return el("p", { class: "muted small" }, "Ningún cierre todavía.");
  return el("div", { class: "stack", style: { gap: "6px" }, role: "list", "aria-label": "Cierres por motivo" },
    entries.map(([m, n]) => el("div", { role: "listitem", style: { display: "grid", gridTemplateColumns: "130px 1fr 110px", gap: "8px", alignItems: "center" } },
      el("span", {}, labels[m] || m),
      el("div", { class: "bar" }, el("span", { style: { left: "0", width: (100 * n / total).toFixed(1) + "%", background: MOTIVO_COLOR[m] || "var(--s5)" } })),
      el("span", { class: "small", style: { textAlign: "right" } }, int(n) + " · " + frac(n / total, 0)))));
}

/** Tarjeta "Cómo sale" del laboratorio: ficha + atribución del desarrollo (lab.diagnostics salidas). */
export function exitsCard(f, s) {
  const comps = s && s.componentes ? Object.entries(s.componentes) : [];
  return card("Cómo sale", f ? exitChips(f) : null,
    exitRows(f),
    el("h3", { style: { marginTop: "12px" } }, "Cierres del desarrollo por motivo"),
    s ? el("div", {},
      el("p", { class: "small muted" }, int(s.trades) + " operaciones cerradas en " + ((s.window || []).map((x) => String(x).slice(0, 10)).join(" → ") || "el desarrollo") +
        " (holdout: " + (s.holdout || "no usado") + ")" + (isNum(s.abiertas_al_final) && s.abiertas_al_final ? " · " + int(s.abiertas_al_final) + " abiertas al final" : "") +
        (isNum(s.ajustes) && s.ajustes ? " · " + int(s.ajustes) + " ajustes de tamaño (no son cierres)" : "") + "."),
      reasonBars(s.por_motivo),
      el("div", { class: "chips", style: { marginTop: "8px" } },
        s.verificacion && s.verificacion.coincide === false ? chip("la reconstrucción NO coincide con la regla", "bad") :
          s.verificacion && s.verificacion.llamadas ? chip("reconstrucción verificada (" + int(s.verificacion.llamadas) + " llamadas)", "ok", s.metodo || "") : chip(s.metodo || "transiciones de la señal", "info"),
        chip("diagnóstico: no suma intentos", "info")),
      comps.length ? table([{ label: "Componente" }, ...Object.keys(MOTIVO).filter((m) => m in (s.por_motivo || {})).map((m) => ({ label: MOTIVO[m], cls: "n" }))],
        comps.map(([c, r]) => [el("b", {}, c), ...Object.keys(MOTIVO).filter((m) => m in (s.por_motivo || {})).map((m) => int((r.por_motivo || {})[m]))]),
      { label: "Cierres por componente" }) : null)
      : el("p", { class: "muted small" }, "Todavía no se corrió la atribución. En la PC: python -m heatsignal.lab.diagnostics salidas <id> (o ambos) y después lab.publish --upload."));
}

/** Stop y ROI configurados de un bot de Freqtrade + conteo de exit_reason. */
export function ftExitsBlock(x, counts) {
  if (!x && !counts) return null;
  return el("div", {},
    x ? el("div", { class: "chips" }, chip(x.stop_text || "stop ?", x.stoploss !== null && x.stoploss > -0.98 ? "ok" : "warn", "stoploss = " + x.stoploss),
      chip(x.roi_text || "ROI ?", "", "minimal_roi = " + JSON.stringify(x.minimal_roi || {})),
      chip(x.source === "api" ? "leído de la API" : "de la estrategia (API sin respuesta)", "info")) : null,
    x && x.nota ? el("p", { class: "small muted" }, x.nota) : null,
    counts && Object.keys(counts).length ? reasonBars(counts, FT_REASON) : null);
}

const pctTxt = (v) => isNum(v) ? (Math.round(v * 100) / 100).toLocaleString("es-AR") + " %" : DASH;
const atrTxt = (v) => isNum(v) ? (Math.round(v * 100) / 100).toLocaleString("es-AR") + " ATR" : DASH;

/** Hipótesis del grafo: regla de salida (por tiempo) + MAE/MFE + corte descriptivo de stops (NO pre-registrado). */
export function excursionCard(h) {
  const r = h.exit_rule, x = h.excursion;
  const stops = (x && x.stops) || [];
  return card("Cómo sale y cuánto fue en contra", r ? chip(r.texto, "info", r.nota) : null,
    r ? el("p", { class: "small" }, r.nota) : null,
    x ? el("div", {},
      kv([["Fue en contra (MAE)", el("span", {}, el("b", { class: "neg" }, pctTxt(x.mae_pct)), " · " + atrTxt(x.mae_atr))],
        ["Fue a favor (MFE)", el("span", {}, el("b", { class: "pos" }, pctTxt(x.mfe_pct)), " · " + atrTxt(x.mfe_atr))],
        ["ATR usado", isNum(x.atr) ? el("span", {}, String(x.atr), el("span", { class: "small muted" }, " · " + (x.atr_source || ""))) : el("span", { class: "muted" }, x.atr_source || "sin ATR")],
        ["Velas", int(x.n_candles) + (x.source === "klines_1m" ? " de 1m (base histórica)" : " de 1h (Binance)") + (x.partial_before ? " · la primera empieza antes de la emisión" : "")]]),
      stops.length ? el("div", { style: { marginTop: "10px" } },
        el("div", { class: "note warn" }, el("b", {}, "Solo descriptivo, no pre-registrado: "), x.descriptivo || "no se usó para decidir nada."),
        table([{ label: "¿Un stop de…" }, { label: "…la hubiera sacado antes?" }],
          stops.map((s) => [s.atr.toLocaleString("es-AR") + " ATR", s.hit ? chip("sí", "warn", "el precio fue " + s.atr + " ATR en contra antes del vencimiento") : chip("no", "ok")]),
          { label: "Corte descriptivo de stops" })) : null,
      x.note ? el("p", { class: "small muted" }, x.note) : null)
      : el("p", { class: "muted small" }, h.status === "resolved" ? "El MAE/MFE todavía no se calculó (lo hace el job de evaluación cada 5 min)." : "El MAE/MFE se calcula cuando se resuelve."));
}

/** Resumen del corte descriptivo por estrategia (/api/hypotheses/excursions). */
export function excursionSummaryCard(d) {
  const rows = (d && d.strategies) || [];
  if (!rows.length) return null;
  const xs = [...new Set(rows.flatMap((r) => (r.stops || []).map((s) => s.atr)))].sort((a, b) => a - b);
  return card("Cuánto fueron en contra (MAE/MFE)", chip("descriptivo, no pre-registrado", "warn", d.nota || ""),
    table([{ label: "Estrategia" }, { label: "Con dato", cls: "n" }, { label: "MAE mediano", cls: "n" }, { label: "MFE mediano", cls: "n" },
      ...xs.map((x) => ({ label: "Stop " + x.toLocaleString("es-AR") + " ATR", cls: "n", title: "cuántas hubiera sacado antes y cuántas de esas terminaron acertando" }))],
    rows.map((r) => [el("b", {}, r.name || r.strategy_id), int(r.n), atrTxt(r.mae_atr_median), atrTxt(r.mfe_atr_median),
      ...xs.map((x) => { const s = (r.stops || []).find((q) => q.atr === x); return s ? int(s.hit) + " de " + int(s.n) + (s.hit ? " · " + int(s.hit_won) + " acertaban" : "") : DASH; })]),
    { label: "MAE/MFE por estrategia" }),
    el("p", { class: "small muted" }, d.nota || ""));
}
