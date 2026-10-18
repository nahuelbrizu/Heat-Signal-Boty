// Diario de lecciones (#/lecciones?strategy_id=&symbol=&daily_trend=&outcome=&source=): una lección corta por
// hipótesis resuelta, escrita por el LLM barato o por código (marcado). SOLO para leer: no vuelve a ningún prompt
// ni decisión (reinyectarlas cambiaría el sistema sin pre-registro). Fuente: /api/lessons.
import { el, clear, api, errorBox, card, pageHead, loading, chip, field, select, pager, prose, when, signed, shortId, DASH } from "../core.js";
import { trendChip, alignChip, SESSION_LABEL } from "../htf-ui.js";

const OUTCOME = { acierto: ["ok", "✓ acertó"], fallo: ["bad", "✗ falló"] };
const SOURCE = { llm: ["info", "LLM"], codigo: ["", "por código"] };
const WINDOW = { alcanza: ["ok", "la ventana alcanza"], dudoso: ["warn", "ventana dudosa"], no_alcanza: ["warn", "la ventana no alcanza"], sin_dato: ["", "ventana sin dato"] };
const pick = (map, v) => { const [k, t] = map[v] || ["", v || DASH]; return chip(t, k); };

export const RULE_TEXT = "Las lecciones son para vos: no entran a ningún prompt del grafo ni a ninguna decisión.";

/** Una lección (tarjeta). Se reusa en el detalle de la hipótesis. */
export function lessonBox(l, href) {
  if (!l) return el("p", { class: "muted small" }, "Todavía no tiene lección (se escribe cuando el job de evaluación la resuelve).");
  return el("article", { class: "box", style: { border: "1px solid var(--line)", borderRadius: "8px", padding: "10px 12px", marginBottom: "10px" } },
    el("div", { class: "row", style: { marginBottom: "6px", flexWrap: "wrap", gap: "6px" } },
      href ? el("a", { href: href("hipotesis", [l.hypothesis_id]) }, el("b", {}, l.symbol), " · ", shortId(l.hypothesis_id)) : el("b", {}, l.symbol),
      pick(OUTCOME, l.outcome), pick(WINDOW, l.window_verdict), pick(SOURCE, l.source),
      l.daily_trend ? trendChip(l.daily_trend) : null, l.trend_alignment ? alignChip(l.trend_alignment) : null,
      l.session ? chip(SESSION_LABEL[l.session] || l.session) : null),
    prose(l.text),
    el("div", { class: "small muted", style: { marginTop: "6px" } },
      "PnL neto ", signed(l.pnl_pct), " · movimiento real ", signed(l.real_move_pct), " · alpha ", signed(l.alpha_pct),
      " · escrita ", when(l.created_ms), l.model ? " · " + l.model : "", l.llm_error ? " · " + l.llm_error : ""));
}

export function renderOverview(d, href) {
  if (d.available === false) return [el("p", { class: "note" }, "Todavía no hay diario: el job de evaluación lo crea en su próxima corrida.")];
  if (!(d.items || []).length) return [el("p", { class: "empty" }, "No hay lecciones con estos filtros.")];
  return d.items.map((l) => lessonBox(l, href));
}

export function renderDetail(l) { return lessonBox(l, null); }

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const f = { strategy_id: q.get("strategy_id") || "", symbol: q.get("symbol") || "", daily_trend: q.get("daily_trend") || "",
    outcome: q.get("outcome") || "", source: q.get("source") || "", window_verdict: q.get("window_verdict") || "", offset: +(q.get("offset") || 0) };
  const LIMIT = 50;
  const filters = el("div", { class: "filters row", style: { flexWrap: "wrap", gap: "8px", marginBottom: "12px" } });
  const list = el("div", {}, loading()), pag = el("div");
  clear(root, pageHead("Lecciones", RULE_TEXT), el("div", { class: "note info small" },
    "Una lección corta por hipótesis resuelta: qué mostró el resultado frente a la tesis, si la ventana alcanza para juzgar (siempre n = 1) y qué contexto había. Lenguaje histórico."),
  card("Filtros", "", filters), list, pag);
  let strategies = [];
  try { strategies = (await api("/api/strategies", null, ctx.signal)).items || []; } catch { /* filtro sin nombres */ }
  const stratName = (id) => { const s = strategies.find((x) => x.strategy_id === id); return s ? s.description : shortId(id); };

  function drawFilters(facets) {
    const opt = (k, labels) => [["", "todas"], ...((facets && facets[k]) || []).map((x) => [x.value ?? "none", (labels ? labels(x.value) : (x.value ?? "sin registro")) + " (" + x.n + ")"])];
    const mk = (name, label, opts) => {
      const s = select(name, opts, f[name]);
      s.addEventListener("change", () => { f[name] = s.value; f.offset = 0; sync(); load(); });
      return field(label, s);
    };
    clear(filters,
      mk("strategy_id", "Estrategia", opt("strategy_id", stratName)),
      mk("symbol", "Moneda", opt("symbol")),
      mk("daily_trend", "Tendencia del día", opt("daily_trend")),
      mk("outcome", "Resultado", [["", "todos"], ["acierto", "acertó"], ["fallo", "falló"]]),
      mk("source", "Escrita por", [["", "todas"], ["llm", "LLM"], ["codigo", "código"]]),
      mk("window_verdict", "Ventana", [["", "todas"], ["alcanza", "alcanza"], ["dudoso", "dudosa"], ["no_alcanza", "no alcanza"], ["sin_dato", "sin dato"]]));
  }
  function sync() { ctx.replaceQuery({ ...f, offset: f.offset || "" }); }

  async function load() {
    try {
      const params = { limit: LIMIT, offset: f.offset };
      for (const k of ["strategy_id", "symbol", "daily_trend", "outcome", "source", "window_verdict"]) if (f[k]) params[k] = f[k];
      const d = await api("/api/lessons", params, ctx.signal);
      drawFilters(d.facets);
      clear(list, ...renderOverview(d, ctx.href));
      clear(pag, d.total > LIMIT ? pager(d.total, LIMIT, f.offset, (o) => { f.offset = o; sync(); load(); }) : "");
    } catch (e) { const b = errorBox(e, "las lecciones"); if (b) clear(list, b); }
  }
  await load();
  ctx.every(60000, load);
  return { refresh: load };
}
