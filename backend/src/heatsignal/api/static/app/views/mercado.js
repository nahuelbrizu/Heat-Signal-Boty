// Mercado / Cripto: velas (/api/klines) con hipotesis y clusters, snapshot por exchange (/api/market/{symbol}, cada 10 s
// con la vista abierta), features en vivo con calidad de datos (/api/features, a demanda) y ultimo analisis.
import {
  el, clear, api, errorBox, price, usd, funding, int, num, pct, dur, ago, when, chip, card, kv, table, stat, seg, pageHead,
  sideSpan, signedPct, isNum, sym as toSym, norm, loading, base, extLink, timeOnly,
} from "../core.js";
import { candlePanel, INTERVALS } from "../candles.js";
import { decisionLine } from "../analysis-ui.js";
import { analyzeAction } from "../actions.js";

const MARKET_POLL_MS = 10000;

export async function mount(root, ctx) {
  const symbols = ctx.symbols();
  let symbol = ctx.route.parts[0] ? toSym(ctx.route.parts[0]) : null;
  if (!symbol || !symbols.some((s) => norm(s) === norm(symbol))) {
    if (!symbols.length) { clear(root, pageHead("Mercado"), el("p", { class: "empty" }, "No hay símbolos configurados.")); return; }
    if (symbol) root.append(el("div", { class: "note warn" }, "El símbolo " + symbol + " no está configurado; se muestra " + symbols[0] + "."));
    symbol = symbols[0];
  }
  let interval = INTERVALS.some(([i]) => i === ctx.route.query.get("tf")) ? ctx.route.query.get("tf") : "1h";
  ctx.setTitle("Mercado " + symbol);

  const candles = candlePanel(ctx, symbol, { interval, onInterval: (v) => { interval = v; ctx.replaceQuery({ tf: v }); } });
  const statsBox = el("div", { class: "stats" }, loading());
  const exBox = el("div"), flowBox = el("div"), liqBox = el("div"), tradesBox = el("div"), liqListBox = el("div"), lastBox = el("div");
  const featBox = el("div", {}, loading("Calculando features…"));

  clear(root,
    pageHead(el("span", {}, symbol, " ", el("span", { class: "muted", style: { fontWeight: 500 } }, "perpetuo")),
      "Ventana en memoria (se vacía con cada reinicio) + velas de Binance pedidas en el momento.",
      seg("Símbolo", symbols.map((s) => [s, base(s)]), symbol, (v) => ctx.navigate(ctx.href("mercado", [v], { tf: interval })))),
    statsBox,
    candles.node,
    el("div", { class: "grid two" }, card("Por exchange", "último valor no nulo de cada campo del ticker", exBox), card("Flujo taker · últimos 5 min", "", flowBox)),
    card("Liquidaciones en la ventana", "1 h y 24 h", liqBox),
    el("div", { class: "grid two" }, card("Últimos trades", "más nuevo primero", tradesBox), card("Últimas liquidaciones", "por hora del evento", liqListBox)),
    card("Features en vivo", el("button", { class: "btn small", type: "button", onclick: () => loadFeatures() }, "Recalcular"), featBox),
    card("Último análisis del grafo", el("a", { href: ctx.href("moneda", [symbol]) }, "todo de " + base(symbol) + " →"), lastBox,
      el("hr", { class: "sep" }), analyzeAction(ctx, symbol)),
  );

  // ---------------------------------------------------------------- snapshot de mercado
  async function loadMarket() {
    try {
      const m = await api("/api/market/" + encodeURIComponent(symbol), { trades: 50, liquidations: 50 }, ctx.signal);
      renderMarket(m);
    } catch (e) {
      const box = errorBox(e, "el mercado");
      if (box) clear(statsBox, box);
    }
  }

  function renderMarket(m) {
    const exs = Object.entries(m.exchanges || {});
    const flowTot = Object.values(m.flow_5m || {}).reduce((a, f) => a + (f.delta_usd || 0), 0);
    const l1 = (m.liquidations_window || {})["1h"] || {}, l24 = (m.liquidations_window || {})["24h"] || {};
    clear(statsBox,
      stat("Mark medio", price(m.mark_mean), exs.length + " exchanges"),
      stat("Funding medio (tasa 8 h)", funding(m.funding_8h_mean), "media entre exchanges que lo informan"),
      stat("Open interest", usd(m.oi_usd_total), m.oi_exchanges + " exchange" + (m.oi_exchanges === 1 ? "" : "s") + " · Binance no lo trae por WS"),
      stat("Delta taker 5 min", el("span", { class: flowTot >= 0 ? "pos" : "neg" }, usd(flowTot)), "compra − venta"),
      stat("Liquidaciones 1 h", usd((l1.long_usd || 0) + (l1.short_usd || 0)), `longs ${usd(l1.long_usd)} · shorts ${usd(l1.short_usd)} · n=${int(l1.n)}`),
      stat("Liquidaciones 24 h", usd((l24.long_usd || 0) + (l24.short_usd || 0)), `longs ${usd(l24.long_usd)} · shorts ${usd(l24.short_usd)} · n=${int(l24.n)}`),
      stat("Hipótesis abiertas", int(m.open_hypotheses), el("a", { href: ctx.href("hipotesis", [], { symbol, status: "pending" }) }, "ver")));

    const hasLast = exs.some(([, e]) => isNum(e.last));  // el ultimo precio solo lo mandan algunos exchanges
    clear(exBox, table([{ label: "Exchange" }, { label: "Mark", cls: "n" }, ...(hasLast ? [{ label: "Último", cls: "n" }] : []), { label: "Funding 8 h", cls: "n" },
      { label: "OI", cls: "n" }, { label: "Antigüedad", cls: "n" }],
      exs.map(([ex, e]) => [el("b", {}, ex), price(e.mark), ...(hasLast ? [price(e.last)] : []),
        el("span", { title: "funding de hace " + dur(e.funding_age_s) }, funding(e.funding_8h), e.funding_age_s > 600 ? el("span", { class: "warn-t small" }, " (" + dur(e.funding_age_s) + ")") : null),
        isNum(e.oi_usd) ? usd(e.oi_usd) : el("span", { class: "muted", title: "no publicado por WS" }, "n/d"),
        el("span", { class: e.age_s > 30 ? "bad-t" : "" }, dur(e.age_s))]),
      { empty: "Sin tickers en la ventana.", label: "Mark, funding y OI por exchange" }),
      el("p", { class: "small muted", style: { marginTop: "6px" } }, "Funding: tasa del período de 8 h (0,0100 % = 1 bp). OI de Binance: no viene por WebSocket."));

    clear(flowBox, table([{ label: "Exchange" }, { label: "Compra", cls: "n" }, { label: "Venta", cls: "n" }, { label: "Delta", cls: "n" }, { label: "Trades", cls: "n" }],
      Object.entries(m.flow_5m || {}).map(([ex, f]) => [el("b", {}, ex), usd(f.buy_usd), usd(f.sell_usd),
        el("span", { class: f.delta_usd >= 0 ? "pos" : "neg" }, usd(f.delta_usd)), int(f.n)]),
      { empty: "Sin trades en los últimos 5 min.", label: "Flujo taker" }));

    const lw = m.liquidations_window || {};
    const exNames = [...new Set(Object.values(lw).flatMap((w) => Object.keys(w.by_exchange || {})))].sort();
    clear(liqBox,
      el("div", { class: "note warn" }, "Cobertura parcial: Binance manda como mucho 1 liquidación por segundo por símbolo y OKX no informa el total; solo Bybit es completo. No compares montos entre exchanges como si fueran totales."),
      table([{ label: "Ventana" }, { label: "Longs liquidados", cls: "n" }, { label: "Shorts liquidados", cls: "n" }, { label: "n", cls: "n" },
        ...exNames.map((x) => ({ label: x, cls: "n" }))],
        ["1h", "24h"].filter((k) => lw[k]).map((k) => [k === "1h" ? "última hora" : "últimas 24 h",
          el("span", { class: "sell" }, usd(lw[k].long_usd)), el("span", { class: "buy" }, usd(lw[k].short_usd)), int(lw[k].n),
          ...exNames.map((x) => { const b = (lw[k].by_exchange || {})[x]; return b ? usd(b.long_usd + b.short_usd) + " · " + b.n : "—"; })]),
        { label: "Liquidaciones por ventana" }));

    clear(tradesBox, table([{ label: "Hora" }, { label: "Exchange" }, { label: "Lado" }, { label: "Precio", cls: "n" }, { label: "USD", cls: "n" }],
      (m.last_trades || []).map((t) => [el("time", { datetime: new Date(t.ts_ms).toISOString() }, timeOnly(t.ts_ms)), t.exchange, sideSpan(t.side), price(t.price), el("span", {}, usd(t.usd), t.block ? chip("block", "info") : null)]),
      { empty: "Sin trades en la ventana.", label: "Últimos trades", scroll: true }));
    clear(liqListBox, table([{ label: "Hora" }, { label: "Exchange" }, { label: "Liquidado" }, { label: "Precio", cls: "n" }, { label: "USD", cls: "n" }, { label: "Cobertura" }],
      (m.last_liquidations || []).map((l) => [when(l.ts_ms, true), l.exchange,
        el("span", { class: l.position === "long" ? "sell" : "buy" }, l.position === "long" ? "long" : "short"),
        price(l.price), usd(l.notional_usd), l.coverage === "partial" ? chip("parcial", "warn") : chip("completa", "ok")]),
      { empty: "Sin liquidaciones en las últimas 24 h de ventana.", label: "Últimas liquidaciones", scroll: true }));

    const la = m.last_analysis;
    if (!la) clear(lastBox, el("p", { class: "empty" }, "El grafo todavía no analizó este símbolo."));
    else clear(lastBox, el("p", { class: "small muted" }, el("a", { href: ctx.href("ciclos", [la.run_id]) }, la.run_id), " · ", when(la.ts_ms)),
      decisionLine(la.decision, ctx.href),
      la.analysis && la.analysis.report ? el("p", { class: "prose", style: { marginTop: "8px" } }, la.analysis.report) : null,
      el("p", { style: { marginTop: "8px" } }, el("a", { href: ctx.href("ciclos", [la.run_id]) }, "Ver razonamiento completo →")));
  }

  // ---------------------------------------------------------------- features
  async function loadFeatures() {
    clear(featBox, loading("Calculando features…"));
    try {
      const d = await api("/api/features", null, ctx.signal);
      const fs = Object.values(d.features).find((f) => norm(f.symbol) === norm(symbol));
      candles.setFeatures(fs || null);
      clear(featBox, fs ? featureView(fs, d.ts_ms) : el("p", { class: "empty" }, "Sin features para " + symbol + "."));
    } catch (e) {
      const b = errorBox(e, "las features");
      if (b) clear(featBox, b);
    }
  }

  await loadMarket();
  candles.reload(true);
  candles.reloadHyps();
  loadFeatures();
  ctx.every(MARKET_POLL_MS, loadMarket);
  return {
    refresh: () => { loadMarket(); candles.reload(false); },
    onSummary: (s, prev) => {
      const c = s.counts && s.counts.hypotheses, p = prev && prev.counts && prev.counts.hypotheses;
      if (c && p && (c.total !== p.total || c.resolved !== p.resolved)) candles.reloadHyps();
    },
    unmount: () => candles.dispose(),
  };
}

// ------------------------------------------------------------------ features en vivo

const COV_COLS = [["trades", "Trades"], ["liquidations", "Liqs"], ["ticker", "Ticker"], ["funding", "Funding"], ["open_interest", "OI"]];
const COV = { ok: ["✓", "ok", "con datos"], partial: ["parcial", "warn", "llega muestreado: subestima el total"],
  stale: ["viejo", "warn", "el último dato es viejo"], missing: ["—", "bad", "sin datos"], na: ["n/a", "", "el exchange no lo publica"] };

function progress(label, have, need, unit, doneText) {
  const ratio = need > 0 ? Math.min(1, (have || 0) / need) : 0, done = (have || 0) >= need;
  return el("div", { class: "prog-row" }, el("span", {}, label),
    el("div", { class: "bar", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": String(need), "aria-valuenow": String(Math.round(have || 0)) },
      el("span", { style: { left: 0, width: (ratio * 100).toFixed(1) + "%", background: done ? "var(--ok)" : "var(--warn)" } })),
    el("span", { class: "v " + (done ? "ok-t" : "muted") }, done ? doneText : int(have) + " / " + int(need) + " " + unit));
}

function featureView(fs, ts) {
  const c = fs.context, of = fs.order_flow, hm = fs.heatmap, q = fs.quality;
  const imb = of.imbalance || 0;
  const imbBar = el("div", { class: "bar", title: "imbalance " + num(imb, 3), role: "img", "aria-label": "Imbalance de order flow " + num(imb, 3) },
    el("span", { class: "mid" }),
    el("span", { style: { background: imb >= 0 ? "var(--buy)" : "var(--sell)", [imb >= 0 ? "left" : "right"]: "50%", width: Math.min(50, Math.abs(imb) * 50) + "%" } }));
  return el("div", {},
    el("p", { class: "small muted" }, "Calculado ", when(ts, true), " desde la ventana en memoria: lo mismo que vería el grafo ahora."),
    el("div", { class: "grid" },
      el("div", {}, el("h3", { style: { marginBottom: "6px" } }, "Contexto ", chip(c.regime === "unknown" ? "régimen sin datos" : "régimen " + c.regime, "info")),
        kv([["Mark", price(c.mark_price)], ["ATR (barras de " + (q ? q.bar_s : "?") + " s, 14)", isNum(c.atr) ? price(c.atr) + " · " + pct(c.atr_pct, 3) : "—"],
          ["Open interest", usd(c.open_interest_usd) + (isNum(c.oi_change_pct) ? " · " + signedPct(c.oi_change_pct) : "")],
          ...Object.entries(c.funding_rate_by_exchange || {}).map(([ex, v]) => ["Funding 8 h · " + ex, funding(v)])])),
      el("div", {}, el("h3", { style: { marginBottom: "6px" } }, "Order flow · últimos " + dur(of.window_s)),
        kv([["Compra taker", usd(of.buy_volume_usd)], ["Venta taker", usd(of.sell_volume_usd)],
          ["CVD", el("span", { class: of.cvd_usd >= 0 ? "pos" : "neg" }, usd(of.cvd_usd))], ["Imbalance", num(imb, 3)],
          ["Trades", int(of.n_trades)], ["Block trades (excluidos)", usd(of.block_trade_volume_usd)]]),
        el("div", { style: { marginTop: "8px" } }, imbBar))),
    el("h3", { style: { margin: "16px 0 6px" } }, "Clusters de liquidación ", el("span", { class: "muted small" }, hm.heatmap_model_id + " · ventana " + dur(hm.window_s))),
    table([{ label: "Rango" }, { label: "Notional", cls: "n" }, { label: "Eventos", cls: "n" }, { label: "Lado dominante" }, { label: "Distancia (ATR)", cls: "n" }, { label: "Fuentes" }],
      hm.clusters.map((cl) => [price(cl.price_low) + " – " + price(cl.price_high), usd(cl.notional_usd), int(cl.n_events),
        String(cl.dominant_side).toLowerCase() === "sell" ? el("span", { class: "sell" }, "longs liquidados") : String(cl.dominant_side).toLowerCase() === "buy" ? el("span", { class: "buy" }, "shorts liquidados") : "—",
        num(cl.distance_atr, 2),
        el("span", {}, cl.sources.join(", "), cl.partial_coverage_share > 0 ? chip(pct(cl.partial_coverage_share * 100, 0) + " parcial", "warn") : null)]),
      { empty: "Sin liquidaciones en la ventana.", label: "Clusters" }),
    q ? el("div", {},
      el("h3", { style: { margin: "16px 0 6px" } }, "Calidad de datos"),
      progress("Ventana en memoria", q.warmup_have_s === null ? q.window_s : q.warmup_have_s, q.window_s, "s", "completa"),
      progress("Velas para ATR", q.bars_closed, q.atr_bars_needed, "velas", "listo"),
      progress("Velas para régimen", q.bars_closed, q.regime_bars_needed, "velas", "listo"),
      el("div", { style: { marginTop: "8px" } }, table([{ label: "Exchange" }, ...COV_COLS.map(([, t]) => ({ label: t }))],
        Object.entries(q.coverage || {}).map(([ex, cv]) => [el("b", {}, ex), ...COV_COLS.map(([k]) => {
          const [t, cls, tip] = COV[cv[k]] || ["?", "", ""];
          return el("span", { class: "chip " + cls, title: tip }, t);
        })]), { label: "Cobertura por exchange", caption: "✓ con datos · parcial = muestreado (subestima) · viejo = último dato atrasado · n/a = el exchange no lo publica" }))) : null,
    fs.data_gaps && fs.data_gaps.length ? el("details", { class: "box", style: { marginTop: "12px" } },
      el("summary", {}, "Huecos y sesgos declarados (" + fs.data_gaps.length + ")"),
      el("ul", { class: "warn-t", style: { margin: 0, paddingLeft: "18px" } }, fs.data_gaps.map((g) => el("li", {}, g)))) : null,
    fs.news_model_id ? el("div", {},
      el("h3", { style: { margin: "16px 0 6px" } }, "Noticias que ve el grafo ", el("span", { class: "muted small" }, (fs.news || []).length + " · " + fs.news_model_id)),
      (fs.news || []).length ? el("ul", { style: { margin: 0, paddingLeft: "18px" } }, fs.news.map((it) => el("li", {},
        it.primary ? chip("primaria", "info") : null, " ", extLink(it.url, it.title),
        el("span", { class: "muted small" }, " · " + it.source + " · " + ago(it.published_ms))))) : el("p", { class: "empty" }, "Sin noticias en las últimas 48 h.")) : null);
}
