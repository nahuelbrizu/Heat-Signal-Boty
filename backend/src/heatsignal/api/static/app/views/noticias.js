// Noticias (/api/news): buscador con filtros en la URL. Texto de terceros: siempre escapado, links solo http(s).
// Desde 2026-10-27 tambien medios generalistas (mundo / economia / tecnologia) con temas por palabras clave
// (newstopics-v1) y, arriba, el bloque "Mundo" tal como lo recibe el Analista de Contexto (/api/news/world).
import { el, clear, api, errorBox, when, ago, card, pageHead, loading, chip, field, select, pager, extLink, base, int } from "../core.js";

const KIND = { cripto: "cripto", mundo: "mundo", economia: "economía", tecnologia: "tecnología" };
const TOPIC = { geopolitica: "guerras y geopolítica", politica: "política y gobiernos", economia: "economía", tecnologia: "tecnología" };

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const f = { q: q.get("q") || "", symbol: q.get("symbol") || "", source: q.get("source") || "", primary: q.get("primary") || "",
    kind: q.get("kind") || "", topic: q.get("topic") || "",
    since: q.get("since") || "", until: q.get("until") || "", limit: 50, offset: Math.max(0, parseInt(q.get("offset") || "0", 10) || 0) };
  const form = el("form", { class: "filters", role: "search", "aria-label": "Buscar noticias" });
  const body = el("div", {}, loading());
  const worldBox = el("div", {});
  const sources = new Set(Object.keys((ctx.summary && ctx.summary.freshness && ctx.summary.freshness.news_feeds) || {}));
  clear(root, pageHead("Noticias", "Guardadas en la base histórica + las de la ventana en memoria (48 h) que todavía no se guardaron. El RSS se consulta cada 5 min."),
    el("div", { class: "note" }, "Texto de terceros, mostrado tal cual (escapado). El etiquetado por símbolo es por palabras clave (newsrss-v1): puede tener falsos positivos."),
    worldBox, card(null, null, form, body));

  const toLocal = (ms) => (ms ? new Date(+ms - new Date(+ms).getTimezoneOffset() * 60000).toISOString().slice(0, 10) : "");
  function drawForm() {
    const iq = el("input", { type: "search", name: "q", value: f.q, placeholder: "texto en título o resumen", minlength: "2", maxlength: "100" });
    const ss = select("symbol", [["", "todos"], ...ctx.symbols().map((s) => [base(s), base(s)])], f.symbol);
    const so = select("source", [["", "todas"], ...[...sources].sort().map((s) => [s, s])], f.source);
    const sp = select("primary", [["", "todas"], ["true", "solo primarias (emisor/regulador)"], ["false", "solo medios"]], f.primary);
    const sk = select("kind", [["", "todas"], ...Object.entries(KIND)], f.kind);
    const st = select("topic", [["", "todos"], ...Object.entries(TOPIC)], f.topic);
    const isn = el("input", { type: "date", name: "since", value: toLocal(f.since) });
    const iun = el("input", { type: "date", name: "until", value: toLocal(f.until) });
    clear(form, field("Buscar", iq), field("Activo", ss), field("Fuente", so), field("Tipo", sp), field("Clase", sk), field("Tema", st),
      field("Desde", isn), field("Hasta (excluido)", iun),
      el("button", { class: "btn primary", type: "submit" }, "Buscar"),
      el("button", { class: "btn", type: "button", onclick: () => { Object.assign(f, { q: "", symbol: "", source: "", primary: "", kind: "", topic: "", since: "", until: "", offset: 0 }); sync(); } }, "Limpiar"));
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const d2ms = (v) => (v ? String(new Date(v + "T00:00").getTime()) : "");
      Object.assign(f, { q: iq.value.trim(), symbol: ss.value, source: so.value, primary: sp.value, kind: sk.value, topic: st.value,
        since: d2ms(isn.value), until: d2ms(iun.value), offset: 0 });
      sync();
    };
  }
  const sync = () => { ctx.replaceQuery({ ...f, limit: "", offset: f.offset || "" }); drawForm(); load(); };

  async function load() {
    if (f.q && f.q.length < 2) { clear(body, el("div", { class: "note warn" }, "La búsqueda necesita al menos 2 caracteres.")); return; }
    clear(body, loading());
    try {
      const d = await api("/api/news", f, ctx.signal);
      d.items.forEach((n) => sources.add(n.source));
      clear(body,
        el("p", { class: "small muted" }, int(d.total) + " noticias · " + int(d.sources.db) + " en la base, " + int(d.sources.window_only) + " solo en memoria"),
        d.items.length ? el("div", {}, d.items.map((n) => el("article", { class: "news-item" },
          el("h3", {}, extLink(n.url, n.title)),
          el("div", { class: "row small muted", style: { gap: "4px 8px" } },
            n.primary ? chip("fuente primaria", "info") : null, el("span", {}, n.source), "·", when(n.published_ms), el("span", {}, "(" + ago(n.published_ms) + ")"),
            ...(n.symbols || []).map((s) => chip(s)), n.market_wide ? chip("todo el mercado") : null,
            n.kind && n.kind !== "cripto" ? chip(KIND[n.kind] || n.kind) : null,
            ...(n.topics || []).map((t) => chip(TOPIC[t] || t, "info", "tema por palabras clave (newstopics-v1)")),
            n.stored ? null : chip("solo en memoria", "warn", "todavía no llegó a la base: se pierde si el proceso se reinicia")),
          n.summary ? el("p", { class: "clamp", style: { marginTop: "4px" } }, n.summary) : null)))
          : el("p", { class: "empty" }, "No hay noticias con esos filtros."),
        pager(d.total, f.limit, f.offset, (o) => { f.offset = o; sync(); window.scrollTo(0, 0); }));
    } catch (e) { const b = errorBox(e, "las noticias"); if (b) clear(body, b); }
  }
  async function loadWorld() {
    try {
      const w = await api("/api/news/world", null, ctx.signal);
      const items = w.items || [];
      clear(worldBox, card("Mundo: lo que recibe el grafo", "titulares de las últimas 48 h de medios generalistas, a lo sumo 4 por tema; va una vez por corrida al Analista de Contexto",
        items.length ? el("div", {}, items.map((n) => el("div", { class: "small", style: { margin: "4px 0" } },
          ...(n.topics || []).map((t) => chip(TOPIC[t] || t, "info")), " ", when(n.published_ms), " · ", el("span", { class: "muted" }, n.source), " · ", extLink(n.url, n.title))))
          : el("p", { class: "empty" }, "Sin titulares de guerras, gobiernos, economía o tecnología en la ventana."),
        (w.feeds_sin_respuesta || []).length ? el("div", { class: "note warn" }, "Feeds sin respuesta reciente: " + w.feeds_sin_respuesta.join(", ")) : null,
        el("p", { class: "small muted" }, "El tema sale de palabras clave (puede haber falsos positivos, p. ej. \"president\" de una empresa). Es contexto: no cambia ninguna estrategia ni el veredicto del CRO.")));
    } catch (e) {
      clear(worldBox, e.status === 503 ? el("div", { class: "note" }, "Bloque Mundo no disponible: las noticias están apagadas en este proceso.") : (errorBox(e, "el bloque Mundo") || null));
    }
  }
  drawForm();
  await Promise.all([load(), loadWorld()]);
  return { refresh: () => Promise.all([load(), loadWorld()]) };
}
