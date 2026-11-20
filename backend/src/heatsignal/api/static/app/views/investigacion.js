// Investigación semanal (#/investigacion/<run_id>): informes del agente de Claude Managed Agents que busca evidencia
// y propone familias nuevas (research/managed.py). Desde la fase 2 (2026-11-21) tambien propone hasta 5 estrategias
// por BLOQUES (estrategias.json): el sistema las valida, las registra (origen agente) y las manda al laboratorio; aca
// se ve que paso con cada una. Nada entra al grafo. Fuente: /api/research y /api/research/{run_id}.
import { el, clear, api, errorBox, card, pageHead, loading, chip, prose, table, extLink, parseMs, when, DASH } from "../core.js";

const STATUS = { ok: ["ok", "informe listo"], en_curso: ["info", "en curso"], sin_informe: ["warn", "sin informe"], error: ["bad", "falló"] };
const OUTCOME = { satisfied: ["ok", "cumple la rúbrica"], max_iterations_reached: ["warn", "no llegó a cumplirla"], failed: ["bad", "rúbrica: falló"], interrupted: ["warn", "interrumpida"] };
const pick = (map, v) => { const [k, t] = map[v] || ["", v || DASH]; return chip(t, k); };

export const RULE_TEXT = "El agente investiga y propone. Las estrategias que arma con los bloques fijos (nunca código) se validan, se registran con su hash antes de ver resultados y van al laboratorio, que las prueba como a todas. Nada entra al grafo.";

function links(text) {
  const urls = [...new Set((String(text || "").match(/https?:\/\/[^\s)>\]]+/g) || []).map((u) => u.replace(/[.,;:]+$/, "")))];
  return urls.length ? el("ol", { class: "small" }, ...urls.map((u) => el("li", {}, extLink(u, u)))) : null;
}

function reportCard(r) {
  if (!r) return el("p", { class: "empty" }, "Elegí una corrida.");
  const head = el("div", { class: "row", style: { flexWrap: "wrap", gap: "6px", marginBottom: "8px" } },
    pick(STATUS, r.status), r.outcome ? pick(OUTCOME, r.outcome) : null,
    chip(r.trigger === "manual" ? "corrida manual" : "programada"),
    r.denied_calls ? chip(r.denied_calls + " llamadas negadas (sin aprobación)", "warn") : null);
  const body = r.text ? prose(r.text, "report") : el("p", { class: "note" },
    r.status === "error" ? "La corrida no pudo arrancar: " + (r.error || DASH)
      : r.status === "en_curso" ? "Todavía está corriendo. El job local lo baja cuando termine."
        : "La sesión terminó sin dejar /mnt/session/outputs/informe.md.");
  const src = r.text ? links(r.text) : null;
  return card("Informe " + (r.created_at ? when(parseMs(r.created_at)) : DASH),
    r.trace_url ? extLink(r.trace_url, "ver la sesión en Anthropic") : "",
    head, strategiesBlock(r.strategies), body, src ? el("details", {}, el("summary", {}, "Enlaces del informe"), src) : null);
}

/** Estrategias que el agente propuso en bloques: registradas y mandadas al laboratorio, repetidas o rechazadas. */
function strategiesBlock(xs) {
  if (xs == null) return el("p", { class: "small muted" }, "Esta corrida no propuso estrategias en bloques.");
  if (!xs.length) return null;
  return el("div", {}, el("b", { class: "small" }, "Estrategias propuestas (" + xs.length + ")"),
    table([{ label: "Nombre" }, { label: "Resultado" }, { label: "Regla / motivo" }], xs.map((x) => [
      x.strategy_id ? el("a", { href: "#/laboratorio/" + encodeURIComponent(x.strategy_id) }, el("span", { class: "small" }, x.strategy_id))
        : el("span", { class: "small" }, x.name || DASH),
      !x.ok ? chip("rechazada", "bad") : x.added === false ? chip("ya estaba registrada", "") : x.lab_request ? chip("al laboratorio", "ok") : chip("registrada (sin pedido al lab)", "warn"),
      el("span", { class: "small" }, x.ok ? (x.describe || "") + (x.lab_error ? " · " + x.lab_error : "") : (x.error || ""))])));
}

export async function mount(root, ctx) {
  const listBox = el("div", {}, loading()), repBox = el("div");
  clear(root, pageHead("Investigación semanal", RULE_TEXT), listBox, repBox);

  async function load() {
    try {
      const d = await api("/api/research", null, ctx.signal);
      if (!d.configured) {
        clear(listBox, el("p", { class: "note" }, "Todavía no está creado el agente: python -m heatsignal.research.managed provision"));
        clear(repBox);
        return;
      }
      const items = d.items || [];
      const sel = ctx.route.parts[0] || (items.find((x) => x.status === "ok") || items[0] || {}).run_id;
      const sched = d.paused ? chip("horario pausado", "warn") : chip("lunes 02:00 UTC", "ok");
      clear(listBox, card("Corridas", el("span", {}, sched, " · revisado ", d.updated_at ? when(parseMs(d.updated_at)) : DASH),
        items.length ? table([{ label: "Fecha" }, { label: "Tipo" }, { label: "Estado" }, { label: "Rúbrica" }],
          items.map((x) => [x.created_at ? when(parseMs(x.created_at)) : DASH, x.trigger === "manual" ? "manual" : "programada",
            pick(STATUS, x.status), x.outcome ? pick(OUTCOME, x.outcome) : DASH]),
          { onRow: (i) => ctx.navigate(ctx.href("investigacion", [items[i].run_id])),
            rowLabel: (i) => "Abrir el informe del " + (items[i].created_at || "") })
          : el("p", { class: "empty" }, "Todavía no corrió ninguna vez.")));
      if (!sel) { clear(repBox); return; }
      clear(repBox, loading());
      const r = await api("/api/research/" + encodeURIComponent(sel), null, ctx.signal);
      clear(repBox, reportCard(r));
    } catch (e) { const b = errorBox(e, "la investigación semanal"); if (b) clear(listBox, b); }
  }
  await load();
  ctx.every(300000, load);
  return { refresh: load };
}
