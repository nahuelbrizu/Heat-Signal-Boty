// Veredicto del CIO (graph-v2-cio) y "¿Acierta el CIO?". El CIO decide sobre el debate (postura, peso, tomar un
// candidato verificado por código o no operar) y el CRO (reglas fijas, sin LLM) puede vetar. Todo el texto del LLM
// se muestra escapado (el() usa nodos de texto). Fuentes: /api/cycles/{run_id} (cio_verdicts) y /api/cio/accuracy.
import { el, card, table, chip, fixed, int, signed, when, shortId, rateText, ciBar, brierText, isNum, DASH } from "./core.js";

const POSTURA = { alcista: ["ok", "▲ alcista"], bajista: ["bad", "▼ bajista"], empate: ["", "empate"] };
const FILL = { alcista: "var(--ok)", bajista: "var(--bad)", empate: "var(--muted)" };
const pick = (map, v) => { const [k, t] = map[v] || ["", v || DASH]; return chip(t, k); };

function pesoBar(peso, postura) {
  if (!isNum(peso)) return el("span", { class: "muted" }, "sin peso");
  return el("div", { style: { minWidth: "90px" } },
    el("div", { class: "bar", role: "img", "aria-label": "peso " + fixed(peso, 2) + " de 1" },
      el("span", { style: { left: "0", width: Math.max(1, peso * 100) + "%", background: FILL[postura] || "var(--muted)" } })),
    el("span", { class: "small muted" }, fixed(peso, 2)));
}

function decisionCell(v, href) {
  if (v.decision === "tomar") {
    return el("span", {}, chip("tomó", "ok"), " ",
      href && v.strategy_id ? el("a", { href: href("estrategias", [v.strategy_id]) }, shortId(v.strategy_id)) : shortId(v.strategy_id),
      " ", v.direction || "");
  }
  return el("span", {}, chip("no operar"), v.cio_rejection ? el("div", { class: "small bad-t" }, "rechazo por código: " + v.cio_rejection) : null);
}

/** Tabla "Veredicto del CIO" de un ciclo (una fila por símbolo). */
export function cioVerdictTable(rows, href) {
  return table(
    [{ label: "Símbolo" }, { label: "Postura" }, { label: "Peso" }, { label: "Decisión" }, { label: "Razones" }, { label: "Veto del CRO" }, { label: "Registro" }],
    (rows || []).map((v) => [
      href ? el("a", { href: href("mercado", [v.symbol]) }, v.symbol) : v.symbol,
      pick(POSTURA, v.postura),
      pesoBar(v.peso, v.postura),
      decisionCell(v, href),
      { v: el("ul", { class: "small", style: { margin: "0", paddingLeft: "16px" } }, (v.razones || []).map((r) => el("li", {}, r))), cls: "wrap" },
      v.cro_veto ? { v: el("span", { class: "small" }, chip("vetó", "warn"), " ", v.cro_veto), cls: "wrap" } : (v.decision === "tomar" ? chip("aprobó", "ok") : el("span", { class: "muted" }, DASH)),
      { v: el("span", { class: "small muted" }, v.outcome ? v.outcome + (v.outcome_reason ? ": " + v.outcome_reason : "") : DASH), cls: "wrap" },
    ]),
    { empty: "Este ciclo no tiene veredicto del CIO (anterior a graph-v2-cio o el grafo no corrió).", label: "Veredicto del CIO" });
}

export function cioVerdictCard(rows, href) {
  const v = (rows || [])[0];
  return card("Veredicto del CIO", v ? v.graph_version : "", el("p", { class: "small muted" },
    "El CIO escucha a los analistas y el debate y decide por símbolo; solo puede tomar un candidato que el código dio por cumplido. El CRO (reglas fijas: hueco real de datos, evento macro, tope por sesión, exposición correlacionada) puede vetar."),
  cioVerdictTable(rows, href));
}

/** Tarjeta "¿Acierta el CIO?" (descriptiva: no cambia nada del sistema). */
export function cioAccuracyBody(d, href) {
  if (!d || d.available === false) return [el("p", { class: "note" }, "Todavía no hay veredictos del CIO (empiezan con graph-v2-cio).")];
  const base = el("p", { class: "small muted" },
    "Base: veredictos con postura alcista o bajista; retorno del símbolo en las ", int(d.horizon_h), " h siguientes con velas de 1h cerradas (mark). ",
    "Acierto = la postura coincide con el signo del retorno. Brier = (peso − acierto)², el peso tomado como probabilidad de acierto; referencia = decir siempre la tasa observada. ",
    "Veredictos: ", int(d.verdicts), " · empates: ", int((d.by_postura || {}).empate || 0), " · vetos del CRO: ", int(d.cro_vetoes),
    " · rechazos por código: ", int(d.cio_rejections), " · esperando ventana: ", int(d.pending), " · sin datos: ", int(d.sin_datos), ".");
  const rate = d.n ? d.hits / d.n : null;
  const parts = [
    el("p", {}, el("b", {}, rateText(d.hits, d.n, d.ci))),
    d.n ? ciBar(rate, d.ci) : null,
    el("p", {}, brierText(d.brier, d.brier_ref), isNum(d.mean_peso) ? el("span", { class: "small muted" }, " · peso medio " + fixed(d.mean_peso, 2)) : null),
    Object.keys(d.by_postura_eval || {}).length ? el("p", { class: "small" }, Object.entries(d.by_postura_eval).map(([p, x]) =>
      el("span", { style: { marginRight: "12px" } }, pick(POSTURA, p), " ", rateText(x.hits, x.n, x.ci)))) : null,
    base,
  ];
  if ((d.recent || []).length) {
    parts.push(table([{ label: "Hora" }, { label: "Símbolo" }, { label: "Postura" }, { label: "Peso", cls: "n" }, { label: "Retorno 4 h", cls: "n" }, { label: "Resultado" }],
      d.recent.map((r) => [
        href ? el("a", { href: href("ciclos", [r.run_id]) }, when(r.ts_ms)) : when(r.ts_ms), r.symbol, pick(POSTURA, r.postura), fixed(r.peso, 2),
        r.status === "ok" ? signed(r.return_pct) : el("span", { class: "muted" }, "sin datos"),
        r.status !== "ok" ? chip("sin dato de precio", "warn") : r.hit ? chip("✓ acertó", "ok") : chip("✗ falló", "bad"),
      ]), { label: "Últimos veredictos evaluados" }));
  }
  return parts;
}

export function cioAccuracyCard(d, href) {
  return card("¿Acierta el CIO?", "descriptivo: no cambia nada automáticamente", ...cioAccuracyBody(d, href));
}

// Para el arnés de render de los tests (tests/js/render_view.mjs).
export function renderOverview(d, href) { return [cioAccuracyCard(d, href)]; }
export function renderDetail(rows) { return cioVerdictCard(rows, null); }
