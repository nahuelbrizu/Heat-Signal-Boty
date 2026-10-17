// Ultimo analisis del grafo por cripto (/api/analysis/latest). Se recarga cuando cambia summary.analysis.last_ran.run_id.
import { el, clear, api, errorBox, when, ago, card, pageHead, loading, statusChip, OUTCOME, CYCLE_STATUS, chip } from "../core.js";
import { symbolAnalysis } from "../analysis-ui.js";

export async function mount(root, ctx) {
  const body = el("div", {}, loading());
  clear(root, pageHead("Análisis del grafo", "Último análisis de cada cripto. Con rotación, cada ciclo cubre solo algunas: la hora de cada tarjeta dice cuándo fue.",
    el("a", { class: "btn", href: "#/ciclos" }, "Historial de ciclos →")), body);
  let lastRun = null;

  async function load() {
    try {
      const d = await api("/api/analysis/latest", null, ctx.signal);
      lastRun = d.last_ran && d.last_ran.run_id;
      render(d);
    } catch (e) { const b = errorBox(e, "el análisis"); if (b) clear(body, b); }
  }

  function render(d) {
    const entries = Object.entries(d.symbols || {}).sort((a, b) => (b[1].ts_ms || 0) - (a[1].ts_ms || 0));
    const lr = d.last_ran;
    clear(body,
      lr ? el("div", { class: "note info" }, "Último ciclo con razonamiento: ", el("a", { href: ctx.href("ciclos", [lr.run_id]) }, lr.run_id), " ",
        statusChip(CYCLE_STATUS, lr.status), " · ", String(lr.n_hypotheses), " hipótesis · ", lr.detail || "") : null,
      entries.length ? el("div", { class: "stack" }, entries.map(([sym, e]) => card(
        el("span", {}, el("a", { href: ctx.href("moneda", [sym]) }, sym), " ", e.decision ? statusChip(OUTCOME, e.decision.outcome) : chip("sin decisión")),
        el("span", {}, when(e.ts_ms), " · ", ago(e.ts_ms), " · ", el("a", { href: ctx.href("ciclos", [e.run_id]) }, e.run_id)),
        symbolAnalysis(e.analysis, e.decision, ctx.href, { open: false }))))
        : el("p", { class: "empty" }, "Todavía no hay análisis guardados (el grafo no corrió o está deshabilitado)."));
  }

  await load();
  return {
    onSummary: (s) => { const id = s.analysis && s.analysis.last_ran && s.analysis.last_ran.run_id; if (id && id !== lastRun) load(); },
    refresh: load,
  };
}
