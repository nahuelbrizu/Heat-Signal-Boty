// Moneda: todo lo de UNA cripto en una pagina (#/moneda/XRP-USDT): mercado y velas, ultimo analisis del grafo y sus
// ciclos, hipotesis con su track record, noticias, datos guardados y laboratorio (resultados del lote y pedidos),
// con las acciones "Analizar ahora" y "Mandar al laboratorio".
import {
  el, clear, api, errorBox, when, ago, int, usd, price, funding, card, table, pageHead, loading, chip, seg, stat, statusChip, CYCLE_STATUS, OUTCOME,
  wilson, rateText, ciBar, base, norm, sym as toSym, extLink, fixed, signedFrac, isNum,
} from "../core.js";
import { candlePanel } from "../candles.js";
import { symbolAnalysis } from "../analysis-ui.js";
import { hypTable } from "../hyp-ui.js";
import { analyzeAction, labRequestAction, labRequestsBlock } from "../actions.js";

const DAY = 86400e3;

export async function mount(root, ctx) {
  const symbols = ctx.symbols();
  let saved = null;
  try { saved = localStorage.getItem("hs-coin"); } catch { /* sin storage */ }
  let symbol = toSym(ctx.route.parts[0] || saved || "");
  if (!symbols.some((s) => norm(s) === norm(symbol))) {
    if (!symbols.length) { clear(root, pageHead("Moneda"), el("p", { class: "empty" }, "No hay símbolos configurados.")); return; }
    if (ctx.route.parts[0]) root.append(el("div", { class: "note warn" }, ctx.route.parts[0] + " no está configurada."));
    symbol = symbols[0];
  }
  if (norm(ctx.route.parts[0] || "") !== norm(symbol)) ctx.replaceQuery({}, [symbol]);
  try { localStorage.setItem("hs-coin", symbol); } catch { /* sin storage */ }
  const coin = base(symbol);
  ctx.setTitle(coin);

  const box = (t) => el("div", {}, loading(t));
  const statsBox = box(), analysisBox = box(), cyclesBox = box(), hypBox = box(), newsBox = box(), dataBox = box(), labBox = box();
  const candles = candlePanel(ctx, symbol, { interval: "4h", tall: false });
  const reqs = labRequestsBlock(ctx, { symbol });
  const labActionSlot = el("div");

  clear(root,
    pageHead(el("span", {}, coin, " ", el("span", { class: "muted", style: { fontWeight: 500 } }, symbol)),
      "Todo lo de esta moneda en un lugar. Lenguaje histórico: lo que pasó, no lo que va a pasar.",
      seg("Moneda", symbols.map((s) => [s, base(s)]), symbol, (v) => ctx.navigate(ctx.href("moneda", [v])))),
    card("Acciones", "", el("div", { class: "grid" },
      el("div", {}, el("p", { class: "small muted" }, "Corre el grafo ahora solo para " + coin + " (analistas, debate y decisión). Tarda 1–2 min y usa llamadas al LLM."), analyzeAction(ctx, symbol)),
      el("div", {}, el("p", { class: "small muted" }, "Pide al laboratorio que pruebe estrategias del lote sobre " + coin + ". Lo corre tu PC; puede tardar horas."), labActionSlot))),
    statsBox,
    candles.node,
    el("div", { class: "grid two" },
      card("Último análisis del grafo", el("a", { href: "#/analisis" }, "todas las criptos →"), analysisBox),
      card("Ciclos que analizaron " + coin, el("a", { href: "#/ciclos" }, "historial completo →"), cyclesBox)),
    card("Hipótesis sobre " + coin, el("a", { href: ctx.href("hipotesis", [], { symbol }) }, "explorar con filtros →"), hypBox),
    el("div", { class: "grid two" },
      card("Noticias de " + coin, el("a", { href: ctx.href("noticias", [], { symbol: coin }) }, "buscar →"), newsBox),
      card("Datos guardados", el("a", { href: ctx.href("datos", [], { symbol }) }, "explorador de barras →"), dataBox)),
    card("Laboratorio", el("a", { href: "#/laboratorio" }, "lote completo →"), labBox,
      el("h3", { style: { margin: "14px 0 6px" } }, "Pedidos al laboratorio para " + coin), reqs.node),
  );

  // ------------------------------------------------------------ mercado
  async function loadMarket() {
    try {
      const m = await api("/api/market/" + encodeURIComponent(symbol), { trades: 0, liquidations: 0 }, ctx.signal);
      const l24 = (m.liquidations_window || {})["24h"] || {};
      const flow = Object.values(m.flow_5m || {}).reduce((a, f) => a + (f.delta_usd || 0), 0);
      clear(statsBox, el("div", { class: "stats" },
        stat("Mark medio", price(m.mark_mean), Object.keys(m.exchanges || {}).length + " exchanges"),
        stat("Funding medio (8 h)", funding(m.funding_8h_mean)),
        stat("Open interest", usd(m.oi_usd_total), m.oi_exchanges + " exchanges lo informan"),
        stat("Delta taker 5 min", el("span", { class: flow >= 0 ? "pos" : "neg" }, usd(flow))),
        stat("Liquidaciones 24 h", usd((l24.long_usd || 0) + (l24.short_usd || 0)), "parcial (Binance/OKX muestreadas) · n=" + int(l24.n)),
        stat("Hipótesis abiertas", int(m.open_hypotheses), el("a", { href: ctx.href("mercado", [symbol]) }, "mercado completo →"))));
    } catch (e) { const b = errorBox(e, "el mercado"); if (b) clear(statsBox, b); }
  }

  // ------------------------------------------------------------ analisis y ciclos
  async function loadAnalysis() {
    try {
      const d = await api("/api/analysis/latest", null, ctx.signal);
      const e = Object.entries(d.symbols || {}).find(([s]) => norm(s) === norm(symbol));
      if (!e) { clear(analysisBox, el("p", { class: "empty" }, "El grafo todavía no analizó " + coin + ". Podés pedirlo con \"Analizar ahora\".")); return; }
      const a = e[1];
      clear(analysisBox, el("p", { class: "small muted" }, when(a.ts_ms), " · ", ago(a.ts_ms), " · ", el("a", { href: ctx.href("ciclos", [a.run_id]) }, a.run_id)),
        symbolAnalysis(a.analysis, a.decision, ctx.href, { open: false }));
    } catch (e) { const b = errorBox(e, "el análisis"); if (b) clear(analysisBox, b); }
  }
  async function loadCycles() {
    try {
      const d = await api("/api/cycles", { limit: 300 }, ctx.signal);
      const mine = d.items.filter((c) => (c.decisions || []).some((x) => norm(x.symbol) === norm(symbol)) || String(c.detail || "").includes(symbol)).slice(0, 12);
      clear(cyclesBox, table([{ label: "Hora" }, { label: "Estado" }, { label: "Decisión" }],
        mine.map((c) => {
          const dd = (c.decisions || []).find((x) => norm(x.symbol) === norm(symbol));
          return [el("a", { href: ctx.href("ciclos", [c.run_id]) }, when(c.ts_ms)), statusChip(CYCLE_STATUS, c.status), dd ? statusChip(OUTCOME, dd.outcome) : "—"];
        }), { empty: "Ningún ciclo reciente analizó " + coin + ".", label: "Ciclos de " + coin, onRow: (i) => ctx.navigate(ctx.href("ciclos", [mine[i].run_id])) }),
        el("p", { class: "small muted", style: { marginTop: "6px" } }, "Entre los últimos " + int(d.items.length) + " ciclos."));
    } catch (e) { const b = errorBox(e, "los ciclos"); if (b) clear(cyclesBox, b); }
  }

  // ------------------------------------------------------------ hipotesis
  async function loadHyps() {
    try {
      const [list, w, l] = await Promise.all([
        api("/api/hypotheses", { symbol, limit: 15 }, ctx.signal),
        api("/api/hypotheses", { symbol, status: "resolved", win: "true", limit: 1 }, ctx.signal),
        api("/api/hypotheses", { symbol, status: "resolved", win: "false", limit: 1 }, ctx.signal)]);
      const n = w.total + l.total, ci = wilson(w.total, n);
      clear(hypBox, el("div", { class: "row", style: { marginBottom: "10px" } }, el("span", { class: "verdict" }, rateText(w.total, n, ci)),
        n ? el("div", { style: { width: "260px" } }, ciBar(w.total / n, ci)) : null, chip(int(list.total) + " en total")),
        el("p", { class: "small muted" }, "Todas las estrategias juntas sobre " + coin + ". PnL contra mantener y Brier, por estrategia."),
        hypTable(list.items, ctx, { empty: "Ninguna hipótesis sobre " + coin + " todavía." }));
    } catch (e) { const b = errorBox(e, "las hipótesis"); if (b) clear(hypBox, b); }
  }

  // ------------------------------------------------------------ noticias
  async function loadNews() {
    try {
      const d = await api("/api/news", { symbol: coin, limit: 8 }, ctx.signal);
      clear(newsBox, d.items.length ? el("div", {}, d.items.map((n) => el("article", { class: "news-item" },
        el("h3", {}, extLink(n.url, n.title)),
        el("div", { class: "small muted" }, n.primary ? chip("primaria", "info") : null, " ", n.source, " · ", ago(n.published_ms), n.stored ? "" : " · solo en memoria"))))
        : el("p", { class: "empty" }, "Sin noticias etiquetadas con " + coin + "."),
        el("p", { class: "small muted" }, int(d.total) + " en total (texto de terceros, escapado)."));
    } catch (e) { const b = errorBox(e, "las noticias"); if (b) clear(newsBox, b); }
  }

  // ------------------------------------------------------------ datos guardados
  async function loadData() {
    try {
      const until = Date.now(), since = until - DAY;
      const [liqs, bars] = await Promise.all([
        api("/api/history/liquidations", { symbol, since, until, limit: 1 }, ctx.signal),
        api("/api/history/bars", { symbol, tf: "1h", since, until, combine: "true" }, ctx.signal)]);
      const sum = (k) => bars.bars.reduce((a, b) => a + (b[k] || 0), 0);
      const first = bars.bars.find((b) => isNum(b.open)), last = [...bars.bars].reverse().find((b) => isNum(b.close));
      clear(dataBox, el("p", { class: "small muted" }, "Últimas 24 h guardadas (barras combinadas de 1 h)."),
        el("dl", { class: "kv" },
          el("dt", {}, "Barras de 1 h"), el("dd", {}, int(bars.count)),
          el("dt", {}, "Variación del precio"), el("dd", {}, first && last ? signedFrac(last.close / first.open - 1, 2) : "—"),
          el("dt", {}, "Compra / venta taker"), el("dd", {}, usd(sum("buy_usd")) + " / " + usd(sum("sell_usd"))),
          el("dt", {}, "Delta"), el("dd", { class: sum("delta_usd") >= 0 ? "pos" : "neg" }, usd(sum("delta_usd"))),
          el("dt", {}, "Liquidaciones guardadas"), el("dd", {}, int(liqs.totals.n) + " · longs " + usd(liqs.totals.long_usd) + " · shorts " + usd(liqs.totals.short_usd))),
        el("p", { class: "small warn-t", style: { marginTop: "6px" } }, "Liquidaciones de Binance y OKX muestreadas: los totales subestiman."));
    } catch (e) { const b = errorBox(e, "los datos guardados"); if (b) clear(dataBox, b); }
  }

  // ------------------------------------------------------------ laboratorio
  async function loadLab() {
    let lab = null;
    try { lab = await api("/api/lab", null, ctx.signal); } catch (e) {
      if (e.status !== 404) { const b = errorBox(e, "el laboratorio"); if (b) clear(labBox, b); }
      else clear(labBox, el("p", { class: "empty" }, "Todavía no se publicaron resultados del laboratorio."));
    }
    const items = (lab && lab.strategies) || [];
    clear(labActionSlot, labRequestAction(ctx, [symbol], items, () => reqs.reload()));
    if (!lab) return;
    const mine = items.filter((s) => (s.symbols || []).some((x) => norm(x) === norm(symbol)));
    clear(labBox, el("p", { class: "small muted" }, "Estrategias del lote que incluyen " + coin + " (los resultados son de la canasta completa de cada una)."),
      table([{ label: "Estrategia" }, { label: "Veredicto" }, { label: "Sharpe OOS", cls: "n" }, { label: "IC 95 %", cls: "n" }, { label: "DSR", cls: "n" },
        { label: "Holdout" }],
      mine.map((s) => [
        { v: el("div", {}, s.has_detail ? el("a", { href: ctx.href("laboratorio", [s.strategy_id]) }, el("b", {}, s.strategy_id)) : el("b", {}, s.strategy_id),
          s.description ? el("div", { class: "small muted" }, s.description) : null), cls: "wrap" },
        s.verdict && s.verdict.pasa ? chip("pasa a paper", "ok") : chip("no pasa", "warn"), fixed(s.sharpe, 2),
        s.sharpe_ci95 ? fixed(s.sharpe_ci95[0], 2) + " / " + fixed(s.sharpe_ci95[1], 2) : "—", fixed(s.dsr, 3),
        s.holdout === "ABIERTO" ? chip("abierto" + (s.holdout_result ? (s.holdout_result.confirma ? " · confirmó" : " · no confirmó") : ""), s.holdout_result && s.holdout_result.confirma ? "ok" : "warn") : chip("sellado", "info")]),
      { empty: "Ninguna estrategia del lote incluye " + coin + ". Podés pedirlo con \"Mandar al laboratorio\".", label: "Laboratorio de " + coin }));
  }

  loadMarket(); candles.reload(true); candles.reloadHyps(); loadAnalysis(); loadCycles(); loadHyps(); loadNews(); loadData(); loadLab();
  ctx.every(10000, loadMarket);
  let lastRun = ctx.summary && ctx.summary.analysis && ctx.summary.analysis.last_ran && ctx.summary.analysis.last_ran.run_id;
  let hk = null;
  return {
    refresh: () => { loadMarket(); candles.reload(false); },
    onSummary: (s) => {
      const id = s.analysis && s.analysis.last_ran && s.analysis.last_ran.run_id;
      if (id && id !== lastRun) { lastRun = id; loadAnalysis(); loadCycles(); }
      const h = s.counts && s.counts.hypotheses, k = h ? h.total + "|" + h.resolved : null;
      if (hk !== null && k !== hk) { loadHyps(); candles.reloadHyps(); }
      hk = k;
    },
    unmount: () => candles.dispose(),
  };
}
