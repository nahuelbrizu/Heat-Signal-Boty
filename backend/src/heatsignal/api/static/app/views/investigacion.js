// Investigación semanal (#/investigacion/<run_id>): informes del agente de Claude Managed Agents que busca evidencia
// y propone familias nuevas (research/managed.py). SOLO para leer: nada entra al grafo ni registra estrategias; una
// propuesta se pre-registra a mano. Fuente: /api/research y /api/research/{run_id}.
import { el, clear, api, errorBox, card, pageHead, loading, chip, prose, table, extLink, parseMs, when, DASH } from "../core.js";

const STATUS = { ok: ["ok", "informe listo"], en_curso: ["info", "en curso"], sin_informe: ["warn", "sin informe"], error: ["bad", "falló"] };
const OUTCOME = { satisfied: ["ok", "cumple la rúbrica"], max_iterations_reached: ["warn", "no llegó a cumplirla"], failed: ["bad", "rúbrica: falló"], interrupted: ["warn", "interrumpida"] };
const pick = (map, v) => { const [k, t] = map[v] || ["", v || DASH]; return chip(t, k); };

export const RULE_TEXT = "El agente solo propone: nada de esto entra al grafo ni se registra solo. Una propuesta se pre-registra a mano, antes de ver resultados.";

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
    head, body, src ? el("details", {}, el("summary", {}, "Enlaces del informe"), src) : null);
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
