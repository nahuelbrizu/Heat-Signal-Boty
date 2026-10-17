// Paper trading: Freqtrade en dry-run de donchian-ensemble-v1 (/api/paper: base de Freqtrade en solo lectura + su API
// REST) y la comparacion semanal contra el backtest del laboratorio (/api/paper/compare, publicada desde la PC).
// Honestidad: dinero simulado; con pocas operaciones cerradas se avisa y se muestra el IC; la comparacion separa
// las diferencias de diseno ya documentadas (rebalanceo 5 %, sin slippage, reinversion) del resto.
import {
  el, clear, api, errorBox, when, int, num, fixed, frac, price, dur, card, table, pageHead, loading, chip, kv, stat, isNum,
  signedFrac, field, select, wilson, rateText, ciBar, sym as toSym, parseMs, extLink, DASH,
} from "../core.js";
import { makeChart, disposeChart, css, signedBars, legend, toChartTime, xyChart } from "../charts.js";

const COMP = {
  timing: ["Timing", "Estar en el mercado cuando el otro no: entradas/salidas en otro momento u operaciones que hizo uno solo."],
  tamano: ["Tamaño / rebalanceo", "Distinto tamaño con los dos posicionados: umbral de rebalanceo 5 %, reinversión de Freqtrade, ratio 0,99, redondeo."],
  precio_fill: ["Precio de fill", "Precio real del fill del dry-run contra el open de la vela 1m siguiente (convención del laboratorio)."],
  comisiones: ["Comisiones", "Comisiones del laboratorio (5 bps) menos las cobradas por el dry-run."],
  slippage: ["Slippage", "El laboratorio carga 2 bps por lado; el dry-run no modela slippage."],
  funding_posicion: ["Funding (posición)", "Funding con tasas reales y las posiciones del paper menos el del laboratorio."],
  funding_contable: ["Funding (contable)", "Lo que asentó Freqtrade menos el recálculo con tasas liquidadas (mark price propio, funding fuera de la ventana)."],
};
const SHORT = { timing: "Timing", tamano: "Tamaño", precio_fill: "Fill", comisiones: "Comisiones", slippage: "Slippage", funding_posicion: "Funding pos.", funding_contable: "Funding cont." };
const DESIGN = { tamano: "info", slippage: "info", precio_fill: "", funding_contable: "" };

/** US$ con 2 decimales y signo opcional. */
function money(v, signed = false) {
  if (!isNum(v)) return DASH;
  const r = Math.abs(v) < 0.005 ? 0 : v;
  return (signed ? (r > 0 ? "+" : r < 0 ? "−" : "") : r < 0 ? "−" : "") + "US$ " + num(Math.abs(r), 2, 2);
}
const sMoney = (v) => isNum(v) ? el("span", { class: Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg" }, money(v, true)) : DASH;
const bps = (v) => isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), 1) + " bps" : DASH;
const pairName = (s) => s ? toSym(s) : DASH;

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const f = { symbol: q.get("symbol") || "", result: q.get("result") || "", since: q.get("since") || "" };
  const head = pageHead("Paper trading", "Freqtrade en dry-run de donchian-ensemble-v1: dinero simulado, precios reales de Binance. Solo lectura.");
  const top = el("div", {}, loading());
  const cmpBox = el("div", {}, loading("Cargando la comparación con el backtest…"));
  clear(root, head, top, cmpBox);
  const charts = [];
  let paper = null;

  async function load() {
    try {
      paper = await api("/api/paper", null, ctx.signal);
      charts.splice(0).forEach(disposeChart);
      clear(top, ...renderPaper(paper, f, ctx, charts));
    } catch (e) {
      const b = errorBox(e, "el paper trading");
      if (b) clear(top, b, e.status === 503 ? el("p", { class: "small muted" },
        "El panel lee la base del dry-run en la VM (", el("code", {}, "HEATSIGNAL_FREQTRADE_DB"), ", por defecto ~/heatsignal/data/freqtrade_dryrun.sqlite). ",
        "Si el servicio heatsignal-freqtrade no corrió nunca, la base no existe.") : null, frequiCard());
    }
  }
  async function loadCompare() {
    try {
      const c = await api("/api/paper/compare", null, ctx.signal);
      clear(cmpBox, renderCompare(c, charts));
    } catch (e) {
      if (e.status === 404) clear(cmpBox, card("Comparación con el backtest", null,
        el("div", { class: "note" }, "Todavía no hay una comparación publicada. Se corre en la PC: ",
          el("code", {}, "python -m heatsignal.lab.paper_compare --upload"), " (o el timer semanal heatsignal-paper-compare, domingos 01:00).")));
      else { const b = errorBox(e, "la comparación"); if (b) clear(cmpBox, b); }
    }
  }
  ctx.redraw = () => { if (paper) { charts.splice(0).forEach(disposeChart); clear(top, ...renderPaper(paper, f, ctx, charts)); loadCompare(); } };
  await Promise.all([load(), loadCompare()]);
  ctx.every(30000, load);
  return { refresh: () => { load(); loadCompare(); }, unmount: () => charts.forEach(disposeChart) };
}

// FreqUI: puerto local del tunel hs-ft (la URL viene de /api/paper; sin respuesta se arma con el mismo esquema que el panel)
const FREQUI_FALLBACK = () => location.protocol + "//localhost:8081";

function frequiCard(url) {
  url = url || FREQUI_FALLBACK();
  return card("FreqUI (interfaz de Freqtrade)", "solo por túnel SSH",
    el("p", {}, "FreqUI escucha solo en 127.0.0.1:8081 de la VM. En la PC: abrí el túnel con ", el("code", {}, "hs-ft"),
      " (dejalo corriendo), después ", el("code", {}, "hs-ft-open"), " o ", extLink(url, url.replace(/^https?:\/\//, "")),
      ". Usuario y clave: ", el("code", {}, "hs-ft-pass"), "."),
    el("p", { class: "small muted" }, "Sin el túnel abierto el link no carga. Este panel no guarda ni muestra la clave."));
}

// ------------------------------------------------------------------ estado del dry-run

function renderPaper(p, f, ctx, charts) {
  const b = p.bot || {}, w = p.wallet || {}, t = p.totals || {};
  const apiOk = p.source && p.source.api && p.source.api.ok;
  const ci = t.ci95;
  const stats = el("div", { class: "stats" },
    stat("Bot", el("span", { class: "chips" }, b.dry_run === false ? chip("DINERO REAL", "bad") : chip("dry-run", "info"),
      b.state ? chip(b.state === "running" ? "corriendo" : b.state, b.state === "running" ? "ok" : "warn") : chip(apiOk ? "?" : "API sin respuesta", "warn")),
      (b.strategy || "—") + (b.version ? " · Freqtrade " + b.version : "")),
    stat("Wallet (equity)", isNum(w.equity) ? money(w.equity) : money(w.start + (w.realized || 0)),
      isNum(w.equity) ? "inicial " + money(w.start) + " · " + signedFrac(w.return, 2) : "sin precio actual: solo realizado", isNum(w.return) ? (w.return >= 0 ? "ok-t" : "bad-t") : ""),
    stat("PnL realizado", sMoney(w.realized), "comisiones " + money(w.fees) + " · funding " + money(w.funding, true)),
    stat("PnL no realizado", isNum(w.unrealized) ? sMoney(w.unrealized) : DASH, apiOk ? int(t.open) + " abiertas · " + money(w.stake_open) + " en posición" : "la API de Freqtrade no respondió"),
    stat("Operaciones cerradas", int(t.closed), t.closed ? rateText(t.wins, t.closed, ci) : "todavía ninguna"),
    stat("Arriba hace", isNum(b.uptime_s) ? dur(b.uptime_s) : DASH,
      (b.bot_start_ms ? "paper desde " + new Date(b.bot_start_ms).toLocaleString("es-AR") : "") + (isNum(b.paper_days) ? " · " + num(b.paper_days, 1) + " días" : "")),
  );
  const notes = (p.notes || []).map((n) => el("div", { class: "note warn" }, n));

  // abiertas
  const open = p.open_trades || [];
  const openCard = card("Operaciones abiertas", int(open.length) + (apiOk ? " · precio actual de Freqtrade" : " · sin precio actual"),
    table([{ label: "Par" }, { label: "Entrada" }, { label: "Precio entrada", cls: "n" }, { label: "Precio actual", cls: "n" },
      { label: "Tamaño", cls: "n", title: "stake actual (US$) y cantidad" }, { label: "Ajustes", cls: "n" },
      { label: "PnL no realizado", cls: "n" }, { label: "%", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Hace", cls: "n" }],
      open.map((o) => [el("b", {}, pairName(o.symbol)), when(o.open_ms), price(o.open_rate), price(o.current_rate),
        el("span", {}, money(o.stake_amount), el("span", { class: "muted small" }, " · " + num(o.amount, 4))), int(o.adjustments),
        sMoney(o.unrealized_pnl), isNum(o.unrealized_ratio) ? signedFrac(o.unrealized_ratio, 2) : DASH, sMoney(o.funding), dur(o.duration_s)]),
      { empty: "Ninguna operación abierta (el objetivo de los 7 pares está en 0 o todavía no hubo vela de 4h).", label: "Operaciones abiertas" }),
    el("p", { class: "small muted", style: { marginTop: "8px" } },
      "Tamaño objetivo por par = (wallet × 0,99) / 7 × objetivo de la regla (0–1). Los ajustes se hacen solo si el cambio supera el 5 % del capital del par."));

  // equity por operacion cerrada
  const eq = (p.equity || []).filter((x) => isNum(x.time_ms) && isNum(x.equity));
  const eqNode = el("div", { class: "chart short", role: "img", "aria-label": "Equity del paper por operación cerrada" });
  const eqCard = card("Equity del paper", "por operación cerrada (US$) + valuación actual",
    eq.length >= 2 ? eqNode : el("p", { class: "empty" }, "Sin operaciones cerradas todavía: la curva arranca con la primera."),
    eq.length >= 2 ? legend([[css("--s1"), "equity (realizado; el último punto incluye lo no realizado)"]]) : null,
    el("p", { class: "small muted" }, "La comparación contra la canasta y el buy & hold está en la sección de abajo (la arma la PC con las velas 1m)."));
  if (eq.length >= 2) {
    let series = null;
    const chart = makeChart(eqNode, { localization: { locale: "es-AR", priceFormatter: (v) => "US$ " + num(v, 0) } }, () => series && series.applyOptions({ color: css("--s1") }));
    if (chart) {
      charts.push(chart);
      series = chart.addLineSeries({ lineWidth: 2, lineType: 1, priceLineVisible: false, lastValueVisible: true });
      const pts = new Map();
      for (const x of eq) pts.set(toChartTime(x.time_ms), x.equity);
      series.setData([...pts.entries()].sort((a, c) => a[0] - c[0]).map(([time, value]) => ({ time, value })));
      chart._retheme();
      requestAnimationFrame(() => chart.timeScale().fitContent());
    }
  }

  // cerradas con filtros
  const closed = p.closed_trades || [];
  const symbols = [...new Set([...closed, ...open].map((x) => x.symbol).filter(Boolean))].sort();
  const sSym = select("symbol", [["", "todos"], ...symbols.map((s) => [s, pairName(s)])], f.symbol);
  const sRes = select("result", [["", "todas"], ["win", "ganadoras"], ["loss", "perdedoras"]], f.result);
  const iSince = el("input", { type: "date", name: "since", value: f.since });
  const apply = () => {
    Object.assign(f, { symbol: sSym.value, result: sRes.value, since: iSince.value });
    ctx.replaceQuery({ symbol: f.symbol, result: f.result, since: f.since });
    ctx.redraw();
  };
  const sinceMs = f.since ? parseMs(f.since + "T00:00:00") : null;
  const rows = closed.filter((c) => (!f.symbol || c.symbol === f.symbol) && (!f.result || (f.result === "win" ? c.win : c.win === false)) &&
    (!sinceMs || (c.close_ms || 0) >= sinceMs)).slice().reverse();
  const nW = rows.filter((c) => c.win).length, fci = wilson(nW, rows.length);
  const pnlSum = rows.reduce((a, c) => a + (c.pnl || 0), 0);
  const form = el("form", { class: "filters", role: "search", "aria-label": "Filtros de operaciones cerradas", onsubmit: (ev) => { ev.preventDefault(); apply(); } },
    field("Par", sSym), field("Resultado", sRes), field("Cerradas desde", iSince),
    el("button", { class: "btn primary", type: "submit" }, "Aplicar"),
    el("button", { class: "btn", type: "button", onclick: () => { sSym.value = ""; sRes.value = ""; iSince.value = ""; apply(); } }, "Limpiar"));
  const closedCard = card("Operaciones cerradas", int(closed.length) + " en total",
    form,
    el("div", { class: "note info" }, el("b", {}, "Con estos filtros: "), int(rows.length) + " operaciones · PnL " + money(pnlSum, true) + " · ",
      f.result ? "filtro por resultado: la tasa no aplica" : rateText(nW, rows.length, fci),
      rows.length && rows.length < 30 ? el("span", {}, " · ", el("b", {}, "muestra chica"), ": no alcanza para sacar conclusiones.") : null,
      !f.result && rows.length ? el("div", { style: { maxWidth: "360px", marginTop: "6px" } }, ciBar(nW / rows.length, fci)) : null),
    el("p", { class: "small muted" }, "Donchian gana con pocas operaciones grandes: en el backtest acertó ~21 %. Una tasa baja no es por sí sola una falla."),
    table([{ label: "Par" }, { label: "Entrada" }, { label: "Salida" }, { label: "Duración", cls: "n" }, { label: "Precio entrada", cls: "n" },
      { label: "Precio salida", cls: "n" }, { label: "Tamaño máx.", cls: "n" }, { label: "Ajustes", cls: "n" }, { label: "PnL", cls: "n" },
      { label: "%", cls: "n" }, { label: "Comisiones", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Motivo" }],
      rows.map((c) => [el("b", {}, pairName(c.symbol)), when(c.open_ms), when(c.close_ms), dur(c.duration_s), price(c.open_rate), price(c.close_rate),
        money(c.max_stake_amount), int(c.adjustments), sMoney(c.pnl), isNum(c.pnl_ratio) ? signedFrac(c.pnl_ratio, 2) : DASH, money(c.fees),
        sMoney(c.funding), c.exit_reason || DASH]),
      { empty: closed.length ? "Ninguna operación cerrada con esos filtros." : "Todavía no se cerró ninguna operación.", label: "Operaciones cerradas", scroll: true }));

  // por par
  const pairs = p.by_pair || [];
  const pairCard = card("Resumen por par", "PnL en US$ del dry-run",
    table([{ label: "Par" }, { label: "Abierta" }, { label: "Cerradas", cls: "n" }, { label: "Acierto", cls: "n" }, { label: "Realizado", cls: "n" },
      { label: "No realizado", cls: "n" }, { label: "Comisiones", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Órdenes / ajustes", cls: "n" }],
      pairs.map((x) => [el("b", {}, pairName(x.symbol)), x.open ? chip("sí · " + money(x.stake_open), "info") : el("span", { class: "muted" }, "no"),
        int(x.closed), x.closed ? frac(x.hit_rate, 0) + " de " + int(x.closed) : DASH, sMoney(x.realized), isNum(x.unrealized) ? sMoney(x.unrealized) : DASH,
        money(x.fees), sMoney(x.funding), int(x.orders) + " / " + int(x.adjustments)]),
      { empty: "Sin operaciones todavía.", label: "Resumen por par" }));

  const src = p.source || {};
  const sourceNote = el("p", { class: "small muted" }, "Leído de la base ", el("code", {}, src.db || "—"), " (solo lectura) ", when(src.read_at ? parseMs(src.read_at) : null, true),
    src.api && src.api.ok ? " · API de Freqtrade OK" : " · API de Freqtrade: " + ((src.api && src.api.error) || "sin respuesta"), ". Se refresca cada 30 s.");
  return [stats, ...notes, el("div", { class: "stack" }, openCard, el("div", { class: "grid two" }, eqCard, frequiCard(p.frequi && p.frequi.url)), closedCard, pairCard, sourceNote)];
}

// ------------------------------------------------------------------ comparacion con el backtest

function renderCompare(c, charts) {
  const r = c.latest || {};
  const w = r.window || {};
  const head = el("p", { class: "small muted" }, "Ventana ", when(w.start_ms), " → ", when(w.end_ms), " (", num(w.days, 1), " días; inicio: ", w.start_source || "—",
    ") · generada ", when(parseMs(c.generated_at)), " en la PC · regla del laboratorio con ",
    el("code", {}, r.params ? "target_vol " + r.params.target_vol + ", canales " + (r.params.channels || []).join("/") : "—"),
    r.spec_hash ? el("span", {}, " (spec ", el("code", {}, r.spec_hash), ")") : null, ".");
  if (r.status !== "ok") {
    return card("Comparación con el backtest", chip("sin datos", "warn"), head, ...(r.notes || []).map((n) => el("div", { class: "note warn" }, n)), historyBlock(c.history || []));
  }
  const t = r.totals || {}, d = r.decomposition || {};
  const notes = (r.notes || []).map((n) => el("div", { class: "note warn" }, n));
  const stats = el("div", { class: "stats" },
    stat("Paper (dry-run)", sMoney(t.paper.total), signedFrac(t.paper.retorno, 2) + " · " + int(t.paper.ordenes) + " órdenes"),
    stat("Backtest (laboratorio)", sMoney(t.sim.total), signedFrac(t.sim.retorno, 2) + " · " + int(t.sim.ordenes) + " órdenes"),
    stat("Diferencia paper − backtest", sMoney(t.diff), signedFrac(t.diff_frac, 3) + " del capital"),
    r.curves ? stat("Canasta / B&H medio", signedFrac(r.curves.basket_return, 2) + " / " + signedFrac(r.curves.buy_and_hold_mean_return, 2), "misma ventana, con costos y funding") : null);

  const comps = Object.keys(COMP).filter((k) => k in d);
  const decompTable = table([{ label: "Componente" }, { label: "Efecto (US$)", cls: "n" }, { label: "Qué es" }],
    [...comps.map((k) => [el("span", {}, COMP[k][0], " ", k in DESIGN ? chip("diseño", DESIGN[k] || "", "diferencia de diseño ya documentada") : null),
      sMoney(d[k]), { v: el("span", { class: "small muted" }, COMP[k][1]), cls: "wrap" }]),
    [el("b", {}, "Total"), sMoney(d.total), { v: el("span", { class: "small muted" }, "residuo " + num(d.residual, 6) + " US$ (la suma es exacta)"), cls: "wrap" }]],
    { label: "Descomposición de la diferencia" });
  const design = (r.design_differences || []).map((x) => el("li", {}, chip(COMP[x.componente] ? COMP[x.componente][0] : x.componente, "info"), " ", x.texto));

  const pairs = r.by_pair || [];
  const pairTable = table([{ label: "Par" }, { label: "Paper", cls: "n" }, { label: "Backtest", cls: "n" }, { label: "Dif.", cls: "n" },
    { label: "Órdenes p/b", cls: "n", title: "paper / backtest" }, { label: "Entradas p/b", cls: "n" }, { label: "Ajustes p/b", cls: "n" }, { label: "Salidas p/b", cls: "n" },
    { label: "Fill vs open 1m", cls: "n", title: "media (n) del precio de fill del dry-run contra el open de la vela 1m siguiente" },
    { label: "Comisiones p/b", cls: "n" }, { label: "Funding p/b", cls: "n" }, { label: "Objetivo al inicio", cls: "n", title: "posición objetivo de la regla (0–1 del capital del par) al arrancar la ventana" }],
    pairs.map((x) => [el("b", {}, pairName(x.symbol)), sMoney(x.paper.total), sMoney(x.sim.total), sMoney(x.diff),
      int(x.paper.ordenes) + " / " + int(x.sim.ordenes), int(x.paper.entradas) + " / " + int(x.sim.entradas), int(x.paper.ajustes) + " / " + int(x.sim.ajustes),
      int(x.paper.salidas) + " / " + int(x.sim.salidas), x.fill_bps && x.fill_bps.n ? bps(x.fill_bps.mean) + " (" + x.fill_bps.n + ")" : DASH,
      money(x.paper.comisiones) + " / " + money(x.sim.comisiones), money(x.paper.funding, true) + " / " + money(x.sim.funding, true),
      fixed(x.target_start_sim, 3)]), { label: "Comparación por par" });

  const tradeDetails = pairs.filter((x) => (x.trades || []).length).map((x) => el("details", { class: "box" },
    el("summary", {}, el("b", {}, pairName(x.symbol)), " · " + x.trades.length + " operaciones · realizado " + money(x.paper.realizado, true) + " / " + money(x.sim.realizado, true) +
      " · no realizado " + money(x.paper.no_realizado, true) + " / " + money(x.sim.no_realizado, true) + " (paper / backtest)"),
    table([{ label: "" }, { label: "Entrada paper" }, { label: "Entrada backtest" }, { label: "Δ min", cls: "n" }, { label: "Δ precio", cls: "n" },
      { label: "Salida paper" }, { label: "Salida backtest" }, { label: "Δ min", cls: "n" }, { label: "Δ precio", cls: "n" }, { label: "Ajustes p/b", cls: "n" }],
      x.trades.map((m) => [m.match === "ambos" ? chip("ambos", "ok") : chip(m.match === "solo_paper" ? "solo paper" : "solo backtest", "warn"),
        m.paper_entry ? el("span", {}, when(parseMs(m.paper_entry)), " ", price(m.paper_entry_price)) : DASH,
        m.sim_entry ? el("span", {}, when(parseMs(m.sim_entry)), " ", price(m.sim_entry_price)) : DASH,
        num(m.entry_delta_min, 0), bps(m.entry_delta_bps),
        m.paper_exit ? el("span", {}, when(parseMs(m.paper_exit)), " ", price(m.paper_exit_price)) : el("span", { class: "muted" }, "abierta"),
        m.sim_exit ? el("span", {}, when(parseMs(m.sim_exit)), " ", price(m.sim_exit_price)) : el("span", { class: "muted" }, "abierta"),
        num(m.exit_delta_min, 0), bps(m.exit_delta_bps), int(m.paper_ajustes) + " / " + int(m.sim_ajustes)]),
      { label: "Operaciones de " + x.symbol })));

  // curvas
  const cv = r.curves;
  let curveBox = el("p", { class: "empty" }, "Sin curvas.");
  if (cv && (cv.points || []).length >= 2) {
    const node = el("div", { class: "chart short", role: "img", "aria-label": "Retorno acumulado: paper, backtest, canasta y buy & hold medio" });
    const cols = ["--s1", "--s2", "--s4", "--s5"];
    const series = [];
    const chart = makeChart(node, { localization: { locale: "es-AR", priceFormatter: (v) => signedFrac(v, 2) } }, () => series.forEach((s, i) => s.applyOptions({ color: css(cols[i]) })));
    if (chart) {
      charts.push(chart);
      for (let i = 0; i < 4; i++) {
        const s = chart.addLineSeries({ lineWidth: i < 2 ? 2 : 1, priceLineVisible: false, lastValueVisible: true });
        const pts = new Map();
        for (const pnt of cv.points) { const ms = parseMs(pnt[0]); if (isNum(ms) && isNum(pnt[i + 1])) pts.set(toChartTime(ms), pnt[i + 1]); }
        s.setData([...pts.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
        series.push(s);
      }
      chart._retheme();
      requestAnimationFrame(() => chart.timeScale().fitContent());
    }
    curveBox = el("div", {}, node, legend([[css("--s1"), "paper (reconstruido con sus fills y las velas 1m)"], [css("--s2"), "backtest del laboratorio"],
      [css("--s4"), "canasta igual-ponderada"], [css("--s5"), "buy & hold medio"]]),
      el("p", { class: "small muted" }, "Retorno acumulado sobre 10.000 US$, paso " + (cv.freq === "1h" ? "horario" : "diario") + ". Todo con costos y funding."));
  }

  return card("Comparación con el backtest", chip("semanal · la corre la PC", "info"),
    head, ...notes,
    el("div", { class: "stack" },
      stats,
      el("div", { class: "grid two" },
        el("div", {}, el("h3", {}, "De dónde sale la diferencia"),
          signedBars(comps.map((k) => ({ label: SHORT[k], value: d[k] })).concat([{ label: "Total", value: d.total, bold: true }]), (v) => money(v, true))),
        el("div", {}, el("h3", {}, "Paper vs backtest vs canasta"), curveBox)),
      decompTable,
      el("div", {}, el("h3", {}, "Diferencias de diseño ya documentadas"), el("ul", { class: "small", style: { margin: "6px 0 0", paddingLeft: "18px", display: "grid", gap: "4px" } }, design),
        el("p", { class: "small muted" }, "Estas diferencias explican una parte esperable de la brecha; lo que quede en timing o precio de fill es lo que hay que mirar.")),
      el("div", {}, el("h3", {}, "Por par"), pairTable,
        el("p", { class: "small muted" }, "Capital: backtest 10.000 US$ / 7 = " + money(r.capital && r.capital.sim_por_par) + " fijo por par; dry-run (wallet × 0,99) / 7 = " +
          money(r.capital && r.capital.paper_por_par_base) + " al inicio, con reinversión. PnL de precio valuado al close de la última vela 1m de la ventana.")),
      tradeDetails.length ? el("div", { class: "stack" }, el("h3", {}, "Entradas y salidas: paper vs backtest"), ...tradeDetails) : null,
      historyBlock(c.history || [])));
}

function historyBlock(hist) {
  const rows = hist.slice().reverse();
  const pts = hist.filter((h) => isNum(h.diff)).map((h) => [parseMs(h.generated_at), h.diff]);
  return el("div", {}, el("h3", {}, "Historial (una corrida por fecha)"),
    rows.length ? table([{ label: "Fecha" }, { label: "Ventana" }, { label: "Paper", cls: "n" }, { label: "Backtest", cls: "n" }, { label: "Dif.", cls: "n" },
      ...Object.keys(COMP).map((k) => ({ label: COMP[k][0], cls: "n" })), { label: "Órdenes p/b", cls: "n" }],
      rows.map((h) => [h.date, h.window ? num(h.window.days, 1) + " d" : DASH, sMoney(h.paper_total), sMoney(h.sim_total), sMoney(h.diff),
        ...Object.keys(COMP).map((k) => h.decomposition ? sMoney(h.decomposition[k]) : DASH), int(h.paper_orders) + " / " + int(h.sim_orders)]),
      { label: "Historial de comparaciones" }) : el("p", { class: "empty" }, "Sin corridas todavía."),
    pts.length >= 2 ? el("div", { style: { maxWidth: "640px", marginTop: "10px" } }, xyChart({ series: [{ name: "diferencia", color: css("--s1"), points: pts }],
      xFmt: (v) => new Date(v).toLocaleDateString("es-AR"), yFmt: (v) => money(v, true), yLabel: "paper − backtest (US$)", height: 220, ariaLabel: "Diferencia paper − backtest por corrida" })) : null,
    el("p", { class: "small muted" }, "Cada corrida es acumulada desde el inicio del paper; la diferencia entre filas es lo que pasó esa semana."));
}
