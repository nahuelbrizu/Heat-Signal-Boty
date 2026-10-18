// Razonamiento del grafo para un simbolo: analistas, noticias, debate alcista/bajista, decision y reporte del Redactor.
// Todo el texto viene del LLM: se muestra escapado (nodos de texto), nunca como HTML.
import { el, chip, confChip, prose, statusChip, OUTCOME, TONE, analystParts, fixed, isNum, shortId } from "./core.js";
import { htfBrief } from "./htf-ui.js";

const ANALYSTS = [["liquidations", "Liquidaciones"], ["order_flow", "Order flow"], ["context", "Contexto de mercado"], ["news", "Noticias"]];

export function decisionLine(d, href) {
  if (!d) return el("p", { class: "muted" }, "Sin decisión registrada para este símbolo.");
  return el("div", { class: "row", style: { alignItems: "flex-start" } },
    statusChip(OUTCOME, d.outcome),
    el("div", { style: { flex: "1", minWidth: "0" } }, prose(d.reason || ""),
      d.strategy_id && href ? el("div", { class: "small muted" }, "estrategia ", el("a", { href: href("estrategias", [d.strategy_id]) }, shortId(d.strategy_id))) : null));
}

export function analystBlock(key, label, raw) {
  const a = analystParts(raw);
  if (!a) return el("div", { class: "analyst" }, el("h4", {}, label), el("p", { class: "muted small" }, "Sin salida del analista."));
  const extra = [];
  if (key === "news" && a.tone) { const [k, t] = TONE[a.tone] || ["", a.tone]; extra.push(chip("tono " + t, k)); }
  if (key === "news" && a.material_primary_negative) extra.push(chip("negativa material (fuente primaria)", "bad"));
  return el("div", { class: "analyst" }, el("h4", {}, label, confChip(a.confidence), ...extra), prose(a.text || ""));
}

export function debateBlock(debate) {
  if (!debate || !debate.length) return el("p", { class: "muted small" }, "Sin debate en este ciclo.");
  return el("div", { class: "debate" }, debate.map((t) => el("div", { class: "turn " + (t.side === "bull" ? "bull" : "bear") },
    el("div", { class: "row small", style: { marginBottom: "4px" } },
      el("b", { class: t.side === "bull" ? "buy" : "sell" }, t.side === "bull" ? "Alcista" : "Bajista"),
      el("span", { class: "muted" }, "ronda " + t.round), isNum(t.strength) ? chip("fuerza " + fixed(t.strength, 2)) : null),
    prose(t.text || ""))));
}

/** Analisis completo de un simbolo. a = report.symbols[sym]; d = decision. */
export function symbolAnalysis(a, d, href, { open = true } = {}) {
  if (!a) return el("p", { class: "muted" }, "No hay razonamiento guardado para este símbolo.");
  return el("div", {},
    htfBrief(a.htf),
    el("h3", { style: { margin: "4px 0 6px" } }, "Decisión"), decisionLine(d, href),
    a.report ? el("div", { style: { marginTop: "12px" } }, el("h3", {}, "Reporte del Redactor"), prose(a.report, "")) : null,
    el("details", { class: "box", style: { marginTop: "12px" }, open },
      el("summary", {}, el("b", {}, "Analistas")),
      ANALYSTS.map(([k, label]) => analystBlock(k, label, a[k]))),
    el("details", { class: "box", style: { marginTop: "10px" }, open },
      el("summary", {}, el("b", {}, "Debate alcista / bajista"), " ", el("span", { class: "muted small" }, "(" + ((a.debate || []).length) + " turnos)")),
      debateBlock(a.debate)));
}
