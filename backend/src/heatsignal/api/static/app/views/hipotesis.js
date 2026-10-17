// Hipotesis: explorador con filtros y paginacion (/api/hypotheses; los filtros viven en la URL, se pueden compartir)
// y detalle (/api/hypotheses/{id}) con "¿acerto?", precio real al vencer, movimiento real, PnL neto vs mantener y su ciclo.
import {
  el, clear, api, errorBox, when, int, dur, fixed, price, card, table, pageHead, loading, statusChip, HYP_STATUS, STRATEGY_STATUS,
  kv, pager, field, select, dirSpan, signed, shortId, prose, wilson, rateText, ciBar, chip, isNum, verdictChip, sym as toSym,
} from "../core.js";
import { hypTable } from "../hyp-ui.js";
import { symbolAnalysis } from "../analysis-ui.js";

export async function mount(root, ctx) {
  if (ctx.route.parts[0]) return detail(root, ctx, ctx.route.parts[0]);
  return list(root, ctx);
}

const toLocalInput = (ms) => { if (!ms) return ""; const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
const fromLocalInput = (v) => { if (!v) return ""; const t = new Date(v).getTime(); return Number.isNaN(t) ? "" : String(t); };

async function list(root, ctx) {
  const q = ctx.route.query;
  const f = {
    strategy_id: q.get("strategy_id") || "", symbol: q.get("symbol") || "", status: q.get("status") || "", win: q.get("win") || "",
    since: q.get("since") || "", until: q.get("until") || "", order: q.get("order") || "desc",
    limit: Math.min(500, Math.max(1, parseInt(q.get("limit") || "50", 10) || 50)), offset: Math.max(0, parseInt(q.get("offset") || "0", 10) || 0),
  };
  const form = el("form", { class: "filters", role: "search", "aria-label": "Filtros de hipótesis" });
  const body = el("div", {}, loading());
  const summaryBox = el("div");
  clear(root, pageHead("Hipótesis", "Todas, incluidos los fallos (anti-sesgo de supervivencia). Cada una se resuelve contra el precio real al vencer."),
    card(null, null, form, summaryBox, body));

  let strategies = [];
  try { strategies = (await api("/api/strategies", null, ctx.signal)).items; } catch { /* filtro sin nombres */ }
  const symbols = ctx.symbols();

  function drawForm() {
    const sStrat = select("strategy_id", [["", "todas"], ...strategies.map((s) => [s.strategy_id, s.description])], f.strategy_id);
    const sSym = select("symbol", [["", "todos"], ...symbols.map((s) => [s, s])], f.symbol ? toSym(f.symbol) : "");
    const sSt = select("status", [["", "todos"], ["pending", "pendientes"], ["resolved", "resueltas"], ["expired_no_data", "sin datos de precio"]], f.status);
    const sWin = select("win", [["", "todos"], ["true", "acertó"], ["false", "falló"]], f.win);
    const iSince = el("input", { type: "datetime-local", name: "since", value: toLocalInput(+f.since || null) });
    const iUntil = el("input", { type: "datetime-local", name: "until", value: toLocalInput(+f.until || null) });
    const sOrd = select("order", [["desc", "más nuevas primero"], ["asc", "más viejas primero"]], f.order);
    const sLim = select("limit", [["25", "25"], ["50", "50"], ["100", "100"], ["200", "200"]], String(f.limit));
    clear(form, field("Estrategia", sStrat), field("Símbolo", sSym), field("Estado", sSt), field("¿Acertó?", sWin),
      field("Creada desde (hora local)", iSince), field("hasta", iUntil), field("Orden", sOrd), field("Por página", sLim),
      el("button", { class: "btn primary", type: "submit" }, "Aplicar"),
      el("button", { class: "btn", type: "button", onclick: () => { Object.assign(f, { strategy_id: "", symbol: "", status: "", win: "", since: "", until: "", order: "desc", offset: 0 }); sync(); } }, "Limpiar"));
    form.onsubmit = (ev) => {
      ev.preventDefault();
      Object.assign(f, { strategy_id: sStrat.value, symbol: sSym.value, status: sSt.value, win: sWin.value, since: fromLocalInput(iSince.value),
        until: fromLocalInput(iUntil.value), order: sOrd.value, limit: +sLim.value, offset: 0 });
      sync();
    };
  }
  const sync = () => { ctx.replaceQuery({ ...f, order: f.order === "desc" ? "" : f.order, limit: f.limit === 50 ? "" : f.limit, offset: f.offset || "" }); drawForm(); load(); };

  async function load() {
    clear(body, loading());
    try {
      const params = { ...f };
      if (params.win && params.status === "") params.status = "";
      const d = await api("/api/hypotheses", params, ctx.signal);
      // Tasa del FILTRO actual (no solo la pagina): se pide el conteo de aciertos/fallos con el mismo filtro.
      clear(body, hypTable(d.items, ctx, { empty: "Ninguna hipótesis con esos filtros." }),
        pager(d.total, f.limit, f.offset, (o) => { f.offset = o; sync(); }));
      filterRate();
    } catch (e) { const b = errorBox(e, "las hipótesis"); if (b) clear(body, b); }
  }

  async function filterRate() {
    if (f.status && f.status !== "resolved") { clear(summaryBox); return; }
    if (f.win) { clear(summaryBox); return; }
    try {
      const base = { ...f, limit: 1, offset: 0, status: "resolved" };
      const [w, l] = await Promise.all([api("/api/hypotheses", { ...base, win: "true" }, ctx.signal), api("/api/hypotheses", { ...base, win: "false" }, ctx.signal)]);
      const n = w.total + l.total, ci = wilson(w.total, n);
      clear(summaryBox, el("div", { class: "note info" }, el("b", {}, "Con estos filtros: "), rateText(w.total, n, ci), ".",
        n ? el("div", { style: { maxWidth: "360px", marginTop: "6px" } }, ciBar(w.total / n, ci)) : null));
    } catch { clear(summaryBox); }
  }

  drawForm();
  await load();
  let key = null;
  return {
    refresh: load,
    onSummary: (s) => { const h = s.counts && s.counts.hypotheses; const k = h ? h.total + "|" + h.resolved : null; if (key !== null && k !== key && f.offset === 0) load(); key = k; },
  };
}

async function detail(root, ctx, id) {
  clear(root, loading());
  let h;
  try { h = await api("/api/hypotheses/" + encodeURIComponent(id), null, ctx.signal); }
  catch (e) { clear(root, pageHead("Hipótesis", null, null, el("a", { href: "#/hipotesis" }, "← Hipótesis")), errorBox(e, "la hipótesis") || ""); return; }
  ctx.setTitle("Hipótesis " + shortId(h.hypothesis_id));

  let big;
  if (h.status === "pending") big = el("div", { class: "bigq " + (h.overdue ? "warn" : "pend") }, h.overdue ? "Vencida: espera al job de resolución (cada 5 min)" : "Pendiente: vence " + new Date(h.due_ms).toLocaleString("es-AR"));
  else if (h.status === "expired_no_data" || h.win === null || h.win === undefined) big = el("div", { class: "bigq warn" }, "Sin dato de precio al vencer: no cuenta como acierto ni como fallo");
  else big = el("div", { class: "bigq " + (h.win ? "ok" : "bad") }, h.win ? "✓ Acertó" : "✗ Falló", el("span", { style: { fontWeight: 500, fontSize: "14px" } }, " · PnL neto ", signed(h.pnl_pct)));

  const cyc = h.cycle;
  const st = h.strategy;
  clear(root,
    pageHead(el("span", {}, dirSpan(h.direction), " ", h.symbol), el("span", { class: "mono small" }, h.hypothesis_id),
      el("span", { class: "row" }, statusChip(HYP_STATUS, h.status), verdictChip(h)), el("a", { href: "#/hipotesis" }, "← Hipótesis")),
    big,
    el("div", { class: "grid two", style: { marginTop: "16px" } },
      card("Contra la realidad", "",
        kv([
          ["Precio de entrada", price(h.entry_price)],
          ["Precio real al vencer", h.exit_price !== null && h.exit_price !== undefined ? price(h.exit_price) : "—"],
          ["Hora del precio de salida", h.exit_price_at ? when(Date.parse(h.exit_price_at), true) : "—"],
          ["Movimiento real del precio", signed(h.real_move_pct)],
          ["PnL bruto", signed(h.gross_pnl_pct)],
          ["Costos", isNum(h.cost_bps) ? fixed(h.cost_bps, 1) + " bps ida y vuelta" : "—"],
          ["PnL neto", signed(h.pnl_pct)],
          ["Mantener (buy & hold) en la misma ventana", signed(h.benchmark_pnl_pct)],
          ["Neto vs mantener (alpha)", signed(h.alpha_pct)],
        ]),
        el("p", { class: "small muted", style: { marginTop: "8px" } }, "Movimiento real = cuánto se movió el precio entre la entrada y el vencimiento, sin importar la dirección apostada.")),
      card("La hipótesis", "",
        kv([
          ["Dirección", dirSpan(h.direction)], ["Confidence", fixed(h.confidence, 2)], ["Horizonte", dur(h.horizon_s)],
          ["Creada", when(h.created_ms, true)], ["Vence", when(h.due_ms, true)], ["Resuelta", h.resolved_ms ? when(h.resolved_ms, true) : "—"],
          ["Disparador (ATR normalizado)", isNum(h.atr_normalized_trigger) ? fixed(h.atr_normalized_trigger, 2) : "—"],
          ["Modelo de heatmap", h.heatmap_model_id || "—"],
          ["Estrategia", st ? el("a", { href: ctx.href("estrategias", [st.strategy_id]) }, st.description) : el("a", { href: ctx.href("estrategias", [h.strategy_id]) }, shortId(h.strategy_id))],
          ["Ciclo que la generó", h.run_id ? el("a", { href: ctx.href("ciclos", [h.run_id]) }, h.run_id) : "—"],
        ]),
        el("h3", { style: { marginTop: "12px" } }, "Justificación"), prose(h.rationale || "—"))),
    st ? card("Estrategia", el("a", { href: ctx.href("estrategias", [st.strategy_id]) }, "ver track record →"),
      el("p", {}, el("b", {}, st.description), " ", statusChip(STRATEGY_STATUS, st.status)),
      el("pre", { class: "mono small", style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 } },
        "entrada: " + JSON.stringify(st.entry_conditions) + "\nsalida: " + JSON.stringify(st.exit_conditions))) : null,
    card("Razonamiento del ciclo", cyc ? el("span", {}, el("a", { href: ctx.href("ciclos", [cyc.run_id]) }, cyc.run_id), " · ", when(cyc.ts_ms)) : "",
      cyc ? symbolAnalysis(cyc.analysis, cyc.decision, ctx.href, { open: false })
        : el("p", { class: "muted" }, h.run_id ? el("span", {}, "El ciclo no está en el historial; puede estar en los checkpoints: ", el("a", { href: ctx.href("ciclos", [h.run_id]) }, "abrir " + h.run_id)) : "Esta hipótesis no tiene ciclo asociado.")),
  );
}
