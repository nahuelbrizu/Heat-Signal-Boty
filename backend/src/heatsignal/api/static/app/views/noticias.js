// Noticias (/api/news): buscador con filtros en la URL. Texto de terceros: siempre escapado, links solo http(s).
import { el, clear, api, errorBox, when, ago, card, pageHead, loading, chip, field, select, pager, extLink, base, int } from "../core.js";

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const f = { q: q.get("q") || "", symbol: q.get("symbol") || "", source: q.get("source") || "", primary: q.get("primary") || "",
    since: q.get("since") || "", until: q.get("until") || "", limit: 50, offset: Math.max(0, parseInt(q.get("offset") || "0", 10) || 0) };
  const form = el("form", { class: "filters", role: "search", "aria-label": "Buscar noticias" });
  const body = el("div", {}, loading());
  const sources = new Set(Object.keys((ctx.summary && ctx.summary.freshness && ctx.summary.freshness.news_feeds) || {}));
  clear(root, pageHead("Noticias", "Guardadas en la base histórica + las de la ventana en memoria (48 h) que todavía no se guardaron. El RSS se consulta cada 5 min."),
    el("div", { class: "note" }, "Texto de terceros, mostrado tal cual (escapado). El etiquetado por símbolo es por palabras clave (newsrss-v1): puede tener falsos positivos."),
    card(null, null, form, body));

  const toLocal = (ms) => (ms ? new Date(+ms - new Date(+ms).getTimezoneOffset() * 60000).toISOString().slice(0, 10) : "");
  function drawForm() {
    const iq = el("input", { type: "search", name: "q", value: f.q, placeholder: "texto en título o resumen", minlength: "2", maxlength: "100" });
    const ss = select("symbol", [["", "todos"], ...ctx.symbols().map((s) => [base(s), base(s)])], f.symbol);
    const so = select("source", [["", "todas"], ...[...sources].sort().map((s) => [s, s])], f.source);
    const sp = select("primary", [["", "todas"], ["true", "solo primarias (emisor/regulador)"], ["false", "solo medios"]], f.primary);
    const isn = el("input", { type: "date", name: "since", value: toLocal(f.since) });
    const iun = el("input", { type: "date", name: "until", value: toLocal(f.until) });
    clear(form, field("Buscar", iq), field("Activo", ss), field("Fuente", so), field("Tipo", sp), field("Desde", isn), field("Hasta (excluido)", iun),
      el("button", { class: "btn primary", type: "submit" }, "Buscar"),
      el("button", { class: "btn", type: "button", onclick: () => { Object.assign(f, { q: "", symbol: "", source: "", primary: "", since: "", until: "", offset: 0 }); sync(); } }, "Limpiar"));
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const d2ms = (v) => (v ? String(new Date(v + "T00:00").getTime()) : "");
      Object.assign(f, { q: iq.value.trim(), symbol: ss.value, source: so.value, primary: sp.value, since: d2ms(isn.value), until: d2ms(iun.value), offset: 0 });
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
            n.stored ? null : chip("solo en memoria", "warn", "todavía no llegó a la base: se pierde si el proceso se reinicia")),
          n.summary ? el("p", { class: "clamp", style: { marginTop: "4px" } }, n.summary) : null)))
          : el("p", { class: "empty" }, "No hay noticias con esos filtros."),
        pager(d.total, f.limit, f.offset, (o) => { f.offset = o; sync(); window.scrollTo(0, 0); }));
    } catch (e) { const b = errorBox(e, "las noticias"); if (b) clear(body, b); }
  }
  drawForm();
  await load();
  return { refresh: load };
}
