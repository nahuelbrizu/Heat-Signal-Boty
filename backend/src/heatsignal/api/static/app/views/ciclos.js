// Historial de ciclos (/api/cycles, filtro por estado y paginacion) y detalle de un ciclo (/api/cycles/{run_id}):
// decisiones, analistas, noticias, debate, reporte del Redactor e hipotesis emitidas.
import {
  el, clear, api, errorBox, when, ago, int, card, table, pageHead, loading, statusChip, CYCLE_STATUS, OUTCOME, seg, pager, chip,
  shortId, prose,
} from "../core.js";
import { symbolAnalysis } from "../analysis-ui.js";
import { hypTable } from "../hyp-ui.js";

const LIMIT = 50;
const STATUSES = [["", "todos"], ["ran", "corrió"], ["error", "error"], ["warmup", "calentando"], ["no_strategies", "sin estrategias"], ["no_llm", "sin LLM"]];

export async function mount(root, ctx) {
  if (ctx.route.parts[0]) return detail(root, ctx, ctx.route.parts[0]);
  return list(root, ctx);
}

async function list(root, ctx) {
  const q = ctx.route.query;
  let status = q.get("status") || "", offset = Math.max(0, parseInt(q.get("offset") || "0", 10) || 0);
  const filt = el("div"), body = el("div", {}, loading());
  clear(root, pageHead("Ciclos del grafo", "Cada ciclo arma features desde la ventana, corre los analistas y decide si alguna estrategia abre hipótesis.",
    el("a", { class: "btn", href: "#/analisis" }, "Último análisis por cripto →")),
  card(null, null, filt, body));
  let lastTotal = null;

  const drawFilt = () => clear(filt, el("div", { class: "filters" }, seg("Filtrar por estado", STATUSES, status, (v) => { status = v; offset = 0; sync(); load(); })));
  const sync = () => { ctx.replaceQuery({ status, offset: offset || "" }); drawFilt(); };
  drawFilt();

  async function load() {
    try {
      const d = await api("/api/cycles", { status, limit: LIMIT, offset }, ctx.signal);
      lastTotal = ctx.summary && ctx.summary.counts && ctx.summary.counts.cycles ? ctx.summary.counts.cycles.total : null;
      const counts = Object.entries(d.counts || {}).map(([k, v]) => (CYCLE_STATUS[k] || [0, k])[1] + " " + v).join(" · ");
      clear(body,
        el("p", { class: "small muted" }, "En total: " + (counts || "ninguno")),
        table([{ label: "Hora" }, { label: "Estado" }, { label: "Hipótesis", cls: "n" }, { label: "Decisiones" }, { label: "Detalle" }, { label: "Razonamiento" }],
          d.items.map((c) => [
            el("a", { href: ctx.href("ciclos", [c.run_id]) }, when(c.ts_ms)),
            statusChip(CYCLE_STATUS, c.status), int(c.n_hypotheses),
            el("div", { class: "chips", style: { minWidth: "220px" } }, (c.decisions || []).map((x) => el("span", { class: "chip " + (OUTCOME[x.outcome] || [""])[0], title: x.reason }, x.symbol + ": " + (OUTCOME[x.outcome] || [0, x.outcome])[1]))),
            { v: el("span", { class: "small " + (c.status === "error" ? "bad-t" : "muted") }, c.detail || ""), cls: "wrap" },
            c.has_report ? chip("sí", "ok") : chip("no"),
          ]),
          { empty: "No hay ciclos con ese filtro.", label: "Historial de ciclos", onRow: (i) => ctx.navigate(ctx.href("ciclos", [d.items[i].run_id])) }),
        pager(d.total, LIMIT, offset, (o) => { offset = o; sync(); load(); }));
    } catch (e) { const b = errorBox(e, "los ciclos"); if (b) clear(body, b); }
  }
  await load();
  return {
    refresh: load,
    onSummary: (s) => { const t = s.counts && s.counts.cycles && s.counts.cycles.total; if (t !== null && t !== undefined && t !== lastTotal && offset === 0) load(); },
  };
}

async function detail(root, ctx, runId) {
  ctx.setTitle("Ciclo " + runId);
  clear(root, loading());
  let d;
  try { d = await api("/api/cycles/" + encodeURIComponent(runId), null, ctx.signal); }
  catch (e) { clear(root, pageHead("Ciclo " + runId, null, null, el("a", { href: "#/ciclos" }, "← Ciclos")), errorBox(e, "el ciclo") || ""); return; }

  const rep = d.report;
  const syms = rep && rep.symbols ? Object.keys(rep.symbols) : [];
  const decisions = d.decisions || [];
  const allSyms = [...new Set([...syms, ...decisions.map((x) => x.symbol)])];
  let current = allSyms[0];
  const symBox = el("div");
  const drawSym = () => {
    const dec = decisions.find((x) => x.symbol === current);
    clear(symBox,
      allSyms.length > 1 ? el("div", { style: { marginBottom: "12px" } }, seg("Símbolo", allSyms.map((s) => [s, s]), current, (v) => { current = v; drawSym(); })) : null,
      current ? symbolAnalysis(rep && rep.symbols ? rep.symbols[current] : null, dec, ctx.href) : el("p", { class: "empty" }, "Sin símbolos en este ciclo."));
  };
  drawSym();

  clear(root,
    pageHead(el("span", { class: "mono" }, d.run_id), el("span", {}, when(d.ts_ms, true), " · ", ago(d.ts_ms)),
      el("span", { class: "row" }, statusChip(CYCLE_STATUS, d.status), chip(int(d.n_hypotheses) + " hipótesis")),
      el("a", { href: "#/ciclos" }, "← Ciclos")),
    d.detail ? el("div", { class: "note " + (d.status === "error" ? "bad" : "") }, prose(d.detail)) : null,
    (d.notes || []).map((n) => el("div", { class: "note warn" }, n)),
    d.report_source === "checkpoint" ? el("div", { class: "note info" }, "Razonamiento leído de los checkpoints del grafo (no estaba en el historial de ciclos).") : null,
    rep ? card(rep.title || "Resumen del ciclo", rep.status && rep.status !== "ok" ? chip("reporte: " + rep.status, "warn") : "", rep.summary ? prose(rep.summary) : el("p", { class: "muted" }, "Sin resumen.")) : null,
    card("Decisiones", "", table([{ label: "Símbolo" }, { label: "Resultado" }, { label: "Motivo" }, { label: "Estrategia" }],
      decisions.map((x) => [el("a", { href: ctx.href("mercado", [x.symbol]) }, x.symbol), el("span", { class: "chip " + (OUTCOME[x.outcome] || [""])[0] }, (OUTCOME[x.outcome] || [0, x.outcome])[1]),
        { v: prose(x.reason || ""), cls: "wrap" }, x.strategy_id ? el("a", { href: ctx.href("estrategias", [x.strategy_id]) }, shortId(x.strategy_id)) : "—"]),
      { empty: "Este ciclo no registró decisiones.", label: "Decisiones" })),
    rep ? card("Razonamiento por símbolo", "texto generado por el LLM, mostrado tal cual (escapado)", symBox) : null,
    card("Hipótesis emitidas en este ciclo", "", hypTable(d.hypotheses || [], ctx)),
  );
}
