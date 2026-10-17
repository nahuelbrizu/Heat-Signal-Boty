// Estrategias pre-registradas con track record (/api/strategies) y detalle (/api/strategies/{id}): condiciones,
// historial de estados, calibracion en 10 buckets e hipotesis paginadas.
import {
  el, clear, api, errorBox, when, int, dur, frac, fixed, card, table, pageHead, loading, statusChip, STRATEGY_STATUS, rateText, ciBar,
  brierText, signed, kv, stat, pager, shortId, chip, isNum,
} from "../core.js";
import { calibrationChart, legend, css } from "../charts.js";
import { hypTable } from "../hyp-ui.js";

export async function mount(root, ctx) {
  if (ctx.route.parts[0]) return detail(root, ctx, ctx.route.parts[0]);
  return list(root, ctx);
}

function trackCells(tr) {
  const t = tr || {};
  return [
    el("div", { style: { minWidth: "190px" } }, el("div", { class: "small" }, rateText(t.wins, t.resolved, t.ci95)), t.resolved ? ciBar(t.rate, t.ci95) : null),
    signed(t.mean_pnl_pct), signed(t.mean_benchmark_pnl_pct), signed(t.mean_alpha_pct),
    isNum(t.brier) ? el("span", { class: isNum(t.brier_reference) && t.brier < t.brier_reference ? "pos" : "warn-t", title: "referencia " + fixed(t.brier_reference, 3) },
      fixed(t.brier, 3) + " / " + fixed(t.brier_reference, 3)) : "—",
  ];
}

async function list(root, ctx) {
  const body = el("div", {}, loading());
  clear(root, pageHead("Estrategias", "Pre-registradas antes de ver resultados. Toda hipótesis se guarda, incluidos los fallos."), body);
  let key = null;
  async function load() {
    try {
      const d = await api("/api/strategies", null, ctx.signal);
      clear(body,
        el("div", { class: "note" }, "Cómo leer: la tasa de acierto va siempre con n e IC 95 % (Wilson). PnL neto = después de costos (10 bps ida y vuelta). ",
          "\"Mantener\" = buy & hold en la misma ventana; alpha = neto − mantener. Brier menor que su referencia = el confidence aporta información."),
        card(null, null, table([{ label: "Estrategia" }, { label: "Estado" }, { label: "Símbolo" }, { label: "Horizonte", cls: "n" }, { label: "Hipótesis", cls: "n" },
          { label: "Acierto (n, IC 95 %)" }, { label: "PnL neto medio", cls: "n" }, { label: "Mantener medio", cls: "n" }, { label: "Alpha medio", cls: "n" },
          { label: "Brier / ref.", cls: "n", title: "Brier del confidence vs referencia (menor es mejor)" }],
        d.items.map((s) => [
          { v: el("div", {}, el("a", { href: ctx.href("estrategias", [s.strategy_id]) }, el("b", {}, s.description)), el("div", { class: "small muted mono" }, shortId(s.strategy_id) + " · " + (s.author || ""))), cls: "wrap" },
          statusChip(STRATEGY_STATUS, s.status), s.symbol || "—", dur(s.horizon_s),
          el("span", {}, int(s.hypotheses.total), el("div", { class: "small muted" }, int(s.hypotheses.pending) + " pend. · " + int(s.hypotheses.expired_no_data) + " sin datos")),
          ...trackCells(s.track_record)]),
        { empty: "Ninguna todavía. Registrala con: python -m heatsignal.evaluation.register_strategy archivo.json", label: "Estrategias",
          onRow: (i) => ctx.navigate(ctx.href("estrategias", [d.items[i].strategy_id])) })),
        el("p", { class: "small muted" }, "Agregados con " + fixed(d.stats_age_s, 0) + " s de antigüedad (cache de 10 s)."));
    } catch (e) { const b = errorBox(e, "las estrategias"); if (b) clear(body, b); }
  }
  await load();
  return {
    refresh: load,
    onSummary: (s) => { const h = s.counts && s.counts.hypotheses; const k = h ? h.total + "|" + h.resolved : null; if (key !== null && k !== key) load(); key = k; },
  };
}

const LIMIT = 50;

async function detail(root, ctx, id) {
  clear(root, loading());
  let s;
  try { s = await api("/api/strategies/" + encodeURIComponent(id), { limit: 1 }, ctx.signal); }
  catch (e) { clear(root, pageHead("Estrategia", null, null, el("a", { href: "#/estrategias" }, "← Estrategias")), errorBox(e, "la estrategia") || ""); return; }
  ctx.setTitle(s.description);
  const tr = s.track_record || {};
  const hypBox = el("div", {}, loading());
  let offset = 0;

  async function loadHyps() {
    try {
      const d = await api("/api/hypotheses", { strategy_id: s.strategy_id, limit: LIMIT, offset }, ctx.signal);
      clear(hypBox, hypTable(d.items, ctx, { empty: "Esta estrategia todavía no emitió hipótesis." }),
        pager(d.total, LIMIT, offset, (o) => { offset = o; loadHyps(); }),
        el("p", { class: "small" }, el("a", { href: ctx.href("hipotesis", [], { strategy_id: s.strategy_id }) }, "Abrir en el explorador de hipótesis (filtros) →")));
    } catch (e) { const b = errorBox(e, "las hipótesis"); if (b) clear(hypBox, b); }
  }

  const cal = s.calibration || [];
  clear(root,
    pageHead(s.description, el("span", { class: "mono small" }, s.strategy_id), el("span", { class: "row" }, statusChip(STRATEGY_STATUS, s.status), s.symbol ? chip(s.symbol) : null),
      el("a", { href: "#/estrategias" }, "← Estrategias")),
    el("div", { class: "stats" },
      stat("Acierto", tr.resolved ? frac(tr.rate, 0) : "—", rateText(tr.wins, tr.resolved, tr.ci95)),
      stat("PnL neto medio", signed(tr.mean_pnl_pct), "bruto " + (isNum(tr.mean_gross_pnl_pct) ? fixed(tr.mean_gross_pnl_pct, 2) + " %" : "—")),
      stat("Mantener (buy & hold) medio", signed(tr.mean_benchmark_pnl_pct), "misma ventana que cada hipótesis"),
      stat("Alpha medio", signed(tr.mean_alpha_pct), "neto − mantener"),
      stat("Confidence medio", isNum(tr.mean_confidence) ? fixed(tr.mean_confidence, 2) : "—", brierText(tr.brier, tr.brier_reference)),
      stat("Hipótesis", int(s.hypotheses.total), `${int(s.hypotheses.pending)} pendientes · ${int(s.hypotheses.resolved)} resueltas · ${int(s.hypotheses.expired_no_data)} sin datos`)),
    tr.resolved ? card("Intervalo de la tasa de acierto", rateText(tr.wins, tr.resolved, tr.ci95), ciBar(tr.rate, tr.ci95),
      tr.resolved < 30 ? el("p", { class: "small warn-t", style: { marginTop: "8px" } }, "Muestra chica (n < 30): el intervalo es ancho y la tasa todavía no distingue suerte de señal.") : null) : null,
    el("div", { class: "grid two" },
      card("Condiciones", "pre-registradas", el("h3", {}, "Entrada"), jsonBlock(s.entry_conditions), el("h3", { style: { marginTop: "10px" } }, "Salida"), jsonBlock(s.exit_conditions),
        kv([["Horizonte", dur(s.horizon_s)], ["Autor", s.author || "—"], ["Registrada", when(s.registered_ms)],
          ["Primera hipótesis", s.first_hypothesis_ms ? when(s.first_hypothesis_ms) : "—"], ["Última hipótesis", s.last_hypothesis_ms ? when(s.last_hypothesis_ms) : "—"]])),
      card("Historial de estados", "", table([{ label: "Estado" }, { label: "Desde" }],
        (s.status_history || []).map((h) => [statusChip(STRATEGY_STATUS, h.status), when(h.changed_ms, true)]), { label: "Historial de estados" }))),
    card("Calibración del confidence", "10 tramos de igual ancho · diagrama de confiabilidad",
      cal.some((b) => b.n) ? el("div", {}, calibrationChart(cal),
        legend([[css("--accent"), "tasa de acierto observada en el tramo (barra) · bigote = IC 95 %", true], [css("--s2"), "confidence medio del tramo", true], [css("--faint"), "diagonal = calibración perfecta"]]),
        el("p", { class: "small muted", style: { marginTop: "6px" } }, "Con pocos casos por tramo el bigote cubre casi todo: no sacar conclusiones de tramos con n chico."))
        : el("p", { class: "empty" }, "Sin hipótesis resueltas: todavía no hay calibración."),
      el("details", { class: "box", style: { marginTop: "10px" } }, el("summary", {}, "Tabla de los 10 tramos"),
        table([{ label: "Tramo" }, { label: "n", cls: "n" }, { label: "Confidence medio", cls: "n" }, { label: "Acierto", cls: "n" }, { label: "IC 95 %", cls: "n" }],
          cal.map((b) => [fixed(b.lower, 1) + "–" + fixed(b.upper, 1), int(b.n), isNum(b.mean_confidence) ? fixed(b.mean_confidence, 2) : "—",
            isNum(b.hit_rate) ? frac(b.hit_rate, 0) : "—", b.ci95 ? frac(b.ci95[0], 0) + "–" + frac(b.ci95[1], 0) : "—"]), { label: "Calibración" }))),
    card("Hipótesis de esta estrategia", "más nuevas primero", hypBox),
  );
  await loadHyps();
  return { refresh: loadHyps };
}

function jsonBlock(obj) {
  return el("pre", { class: "mono small", style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", background: "var(--surface-2)", padding: "8px 10px", borderRadius: "8px", margin: "4px 0 0" } },
    JSON.stringify(obj || {}, null, 2));
}
