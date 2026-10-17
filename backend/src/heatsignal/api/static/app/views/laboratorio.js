// Laboratorio de backtest: lote (/api/lab) con veredicto y checks, y detalle (/api/lab/{id}) con curva de capital OOS vs
// canasta y buy & hold, costo -> Sharpe, descomposicion del PnL, ventanas walk-forward, baselines y p contra azar.
import {
  el, clear, api, errorBox, int, fixed, frac, num, card, table, pageHead, loading, chip, kv, stat, isNum, signedFrac,
} from "../core.js";
import { makeChart, disposeChart, css, xyChart, signedBars, legend } from "../charts.js";
import { labRequestAction, labRequestsBlock } from "../actions.js";
import { seg, base, norm, sym as toSym, dur } from "../core.js";

const CHECK = {
  opera: ["opera", "hizo operaciones en el período fuera de muestra"],
  dsr: ["DSR", "Deflated Sharpe Ratio ≥ mínimo (corrige por cantidad de combinaciones probadas)"],
  breakeven: ["costos", "el costo de equilibrio supera al costo esperado por el margen pedido"],
  le_gana_a_la_canasta: ["canasta", "Sharpe mayor que la canasta igual-ponderada"],
  le_gana_al_buy_and_hold_medio: ["B&H", "Sharpe mayor que el buy & hold medio"],
  le_gana_al_azar: ["azar", "p contra entradas al azar ≤ máximo"],
};
const checkChips = (checks) => el("div", { class: "chips" }, Object.entries(checks || {}).map(([k, ok]) =>
  chip((ok ? "✓ " : "✗ ") + (CHECK[k] || [k])[0], ok ? "ok" : "bad", (CHECK[k] || [0, k])[1])));
const verdictChip = (v) => v && v.pasa ? chip("pasa a paper", "ok") : chip("no pasa", "warn");
const holdoutNote = (start, state = "SELLADO") => state === "ABIERTO"
  ? el("div", { class: "note warn" }, el("b", {}, "Holdout abierto"), " desde " + (start ? String(start).slice(0, 10) : "?") +
    ": ya se usó la única apertura. Su resultado está abajo; a partir de acá esos meses dejan de ser una prueba limpia para esta estrategia.")
  : el("div", { class: "note info" }, el("b", {}, "Holdout sellado"),
    " desde " + (start ? String(start).slice(0, 10) : "?") + ": esos meses no se usaron para nada y se abren una sola vez, al final. Todo lo de esta página es fuera de muestra (walk-forward) pero anterior al holdout.");

const SPEC_LABEL = [["family", "familia"], ["timeframe", "temporalidad"], ["direction", "dirección"], ["horizon", "horizonte"], ["source", "fuente"]];
const fmtVal = (v) => v === null || v === undefined ? "—" : typeof v === "object" ? Object.entries(v).map(([k, x]) => k + "=" + (Array.isArray(x) ? "[" + x.join(", ") + "]" : x)).join(", ") : String(v);
/** Ficha corta de una estrategia del lote (description, family, timeframe, direction, horizon, params, grid, source). */
export function specBlock(s, { full = false } = {}) {
  const sp = s.spec || s;
  const parts = SPEC_LABEL.filter(([k]) => sp[k] !== undefined && sp[k] !== null && sp[k] !== "").map(([k, l]) => el("span", {}, el("b", {}, l), fmtVal(sp[k])));
  if (full && sp.params) parts.push(el("span", {}, el("b", {}, "parámetros"), el("span", { class: "mono" }, fmtVal(sp.params))));
  if (full && sp.grid) parts.push(el("span", {}, el("b", {}, "grilla"), el("span", { class: "mono" }, fmtVal(sp.grid))));
  return el("div", {}, sp.description ? el("div", { class: full ? "" : "small" }, sp.description) : null, parts.length ? el("div", { class: "spec" }, parts) : null);
}
function holdoutCell(s) {
  if (s.holdout !== "ABIERTO") return chip("sellado", "info", "todavía no se abrió");
  const r = s.holdout_result;
  if (!r) return chip("abierto", "warn");
  return el("div", { class: "small" }, chip(r.confirma ? "abierto · confirmó" : "abierto · no confirmó", r.confirma ? "ok" : "bad"),
    el("div", { class: "muted" }, "Sharpe " + fixed(r.sharpe, 2) + " · " + signedFrac(r.total_return) + " · " + int(r.trades) + " trades"));
}

export async function mount(root, ctx) {
  if (ctx.route.parts[0]) return detail(root, ctx, ctx.route.parts[0]);
  clear(root, loading());
  let d;
  try { d = await api("/api/lab", null, ctx.signal); }
  catch (e) {
    clear(root, pageHead("Laboratorio"), e.status === 404
      ? el("div", { class: "note" }, "Todavía no se publicaron resultados. En la PC: python -m heatsignal.lab.publish --upload")
      : errorBox(e, "el laboratorio") || "");
    return;
  }
  const all = d.strategies || [];
  const symbols = [...new Set(all.flatMap((s) => (s.symbols || []).map(toSym)))].sort();
  const coin = ctx.route.query.get("symbol") ? toSym(ctx.route.query.get("symbol")) : "";
  const items = coin ? all.filter((s) => (s.symbols || []).some((x) => norm(x) === norm(coin))) : all;
  const pass = items.filter((s) => s.verdict && s.verdict.pasa).length;
  const reqs = labRequestsBlock(ctx);
  const crit = (items.find((s) => s.verdict && s.verdict.criterios) || {}).verdict;
  clear(root,
    pageHead("Laboratorio (backtest)", "Publicado " + new Date(d.generated_at).toLocaleString("es-AR") + " · " + items.length + " estrategias" + (coin ? " con " + base(coin) : "") + " · " + pass + " pasan",
      symbols.length ? seg("Filtrar por moneda", [["", "todas"], ...symbols.map((x) => [x, base(x)])], coin, (v) => ctx.navigate(ctx.href("laboratorio", [], { symbol: v }))) : null),
    el("div", { class: "note info" }, el("b", {}, "Holdout: "), "los últimos meses (desde " + String((items[0] && items[0].holdout_start) || "?").slice(0, 10) +
      ") no se usan para elegir nada y se abren UNA sola vez por estrategia, al final. " +
      (items.some((s) => s.holdout === "ABIERTO") ? items.filter((s) => s.holdout === "ABIERTO").length + " ya lo abrieron (ver columna Holdout); el resto sigue sellado." : "Todos siguen sellados.")),
    crit ? el("div", { class: "note" }, "Para pasar a paper trading tiene que cumplir todos los criterios: DSR ≥ " + fixed(crit.criterios.dsr_min, 2) +
      ", costo de equilibrio ≥ " + fixed(crit.criterios.breakeven_vs_cost_min, 1) + "× el esperado, p contra azar ≤ " + fixed(crit.criterios.p_random_max, 2) +
      ", y ganarle a la canasta y al buy & hold medio.") : null,
    card(null, null, table([{ label: "Estrategia" }, { label: "Veredicto" }, { label: "Sharpe OOS", cls: "n" }, { label: "IC 95 %", cls: "n" },
      { label: "PSR / DSR", cls: "n" }, { label: "Retorno", cls: "n" }, { label: "Máx. DD", cls: "n" }, { label: "Trades", cls: "n" }, { label: "Acierto", cls: "n" },
      { label: "Break-even", cls: "n", title: "costo por lado que deja el Sharpe en 0" }, { label: "Canasta", cls: "n", title: "Sharpe de la canasta" },
      { label: "p azar", cls: "n" }, { label: "Criterios" }, { label: "Holdout" }],
    items.map((s) => [
      { v: el("div", { style: { minWidth: "300px" } }, s.has_detail ? el("a", { href: ctx.href("laboratorio", [s.strategy_id]) }, el("b", {}, s.strategy_id)) : el("b", {}, s.strategy_id),
        el("div", { class: "small muted mono" }, (s.oos_range || []).join(" → ") + " · " + int(s.windows) + " ventanas · " + int(s.n_combinations) + " comb."),
        el("div", { class: "muted", style: { marginTop: "4px" } }, specBlock(s))), cls: "wrap" },
      verdictChip(s.verdict), fixed(s.sharpe, 2), s.sharpe_ci95 ? fixed(s.sharpe_ci95[0], 2) + " / " + fixed(s.sharpe_ci95[1], 2) : "—",
      fixed(s.psr, 3) + " / " + fixed(s.dsr, 3), signedFrac(s.total_return), frac(s.max_drawdown), int(s.trades), frac(s.hit_rate, 0),
      isNum(s.breakeven_bps) ? num(s.breakeven_bps, 1) + " bps" : "—", fixed(s.basket_sharpe, 2), fixed(s.random_p, 3),
      { v: el("div", { style: { minWidth: "200px" } }, checkChips(s.verdict && s.verdict.checks)), cls: "wrap" }, holdoutCell(s),
    ]), { empty: "Sin estrategias en el lote.", label: "Lote del laboratorio",
      onRow: (i) => items[i].has_detail && ctx.navigate(ctx.href("laboratorio", [items[i].strategy_id])) }),
    el("p", { class: "small muted", style: { marginTop: "8px" } }, "Sharpe anualizado; retornos y drawdown en fracción del capital inicial; costos en bps por lado. Las que no tienen detalle publicado no son clickeables.")),
    card("Pedidos al laboratorio", el("span", { class: "small muted" }, "los corre tu PC (worker); pueden tardar minutos u horas"),
      el("div", { style: { marginBottom: "12px" } }, labRequestAction(ctx, coin ? [coin] : symbols, all, () => reqs.reload())),
      reqs.node),
  );
  return { refresh: () => reqs.reload() };
}

async function detail(root, ctx, id) {
  clear(root, loading());
  let d;
  try { d = await api("/api/lab/" + encodeURIComponent(id), null, ctx.signal); }
  catch (e) { clear(root, pageHead(id, null, null, el("a", { href: "#/laboratorio" }, "← Laboratorio")), errorBox(e, "el detalle") || ""); return; }
  ctx.setTitle("Lab " + d.strategy_id);
  const o = d.oos || {}, c = d.costs || {}, wf = d.walk_forward || {}, b = d.baselines || {}, hr = o.hit_rate || {};
  const eqBox = el("div");
  let chart = null;
  const charts = [];

  if (d.equity_daily) {
    const node = el("div", { class: "chart", role: "img", "aria-label": "Curva de capital fuera de muestra" });
    clear(eqBox, node, legend([[css("--s1"), "estrategia"], [css("--s2"), "canasta"], [css("--s5"), "buy & hold medio"]]),
      el("p", { class: "small muted" }, "Retorno acumulado (suma simple de retornos diarios, misma convención que el retorno total). Fechas UTC."));
    const series = [];
    chart = makeChart(node, { timeScale: { timeVisible: false }, localization: { locale: "es-AR", priceFormatter: (v) => signedFrac(v, 1) } }, () => {
      const cols = [css("--s1"), css("--s2"), css("--s5")];
      series.forEach((s, i) => s.applyOptions({ color: cols[i] }));
    });
    if (chart) {
      const add = (data, w) => { const s = chart.addLineSeries({ lineWidth: w, priceLineVisible: false, lastValueVisible: true }); s.setData((data || []).map(([t, v]) => ({ time: t, value: v }))); series.push(s); };
      add(d.equity_daily.strategy, 2); add(d.equity_daily.basket, 1); add(d.equity_daily.buy_and_hold_mean, 1);
      chart._retheme();
      chart.timeScale().fitContent();
      requestAnimationFrame(() => chart.timeScale().fitContent());  // tras el primer layout (ancho real en celular)
    }
  } else {
    clear(eqBox, el("div", { class: "note warn" }, "Este resultado es anterior a la curva de capital: no hay equity diaria publicada.",
      (d.notes || []).length ? el("ul", { style: { margin: "6px 0 0", paddingLeft: "18px" } }, d.notes.map((n) => el("li", {}, n))) : null));
  }

  const curve = (c.cost_curve || []).map((p) => [p.cost_bps, p.sharpe]);
  const bd = d.breakdown || {};
  const pass = d.verdict && d.verdict.pasa;

  clear(root,
    pageHead(d.strategy_id, el("span", { class: "mono small" }, "spec " + d.spec_hash + " · run " + d.run + " · " + (d.runs || []).length + " corridas · " + (d.symbols || []).join(", ")),
      el("span", { class: "row" }, el("span", { class: "small muted" }, "fuera de muestra:"), verdictChip(d.verdict),
        d.holdout && typeof d.holdout === "object" ? chip((d.holdout.verdict ? d.holdout.verdict.pasa : d.holdout.confirma) ? "holdout confirmó" : "holdout no confirmó",
          (d.holdout.verdict ? d.holdout.verdict.pasa : d.holdout.confirma) ? "ok" : "bad") : null),
      el("a", { href: "#/laboratorio" }, "← Laboratorio")),
    holdoutNote(d.data && d.data.holdout_start, d.holdout && typeof d.holdout === "object" ? "ABIERTO" : (d.data && d.data.holdout) || "SELLADO"),
    d.spec ? card("Ficha de la estrategia", d.spec.family ? chip(d.spec.family) : "", specBlock(d.spec, { full: true })) : null,
    el("div", { class: "stats" },
      stat("Sharpe OOS", fixed(o.sharpe, 2), o.sharpe_ci95 ? "IC 95 % " + fixed(o.sharpe_ci95[0], 2) + " a " + fixed(o.sharpe_ci95[1], 2) : ""),
      stat("PSR / DSR", fixed(o.psr, 3) + " / " + fixed(o.dsr, 3), "SR0 anual " + fixed(o.sr0_annual, 3) + " · " + int(d.n_combinations) + " combinaciones"),
      stat("Retorno total", signedFrac(o.total_return), int(o.days) + " días fuera de muestra"),
      stat("Máx. drawdown", frac(o.max_drawdown), ""),
      stat("Trades", int(o.trades), isNum(hr.hit_rate) ? "acertó " + frac(hr.hit_rate, 0) + " de " + int(hr.n) + " (IC " + frac(hr.lo, 0) + "–" + frac(hr.hi, 0) + ")" : ""),
      stat("Track record mínimo", isNum(o.min_trl_days) ? int(o.min_trl_days) + " días" : "—", "para que el Sharpe sea distinguible de SR0")),
    card("Veredicto", pass ? chip("cumple todos los criterios", "ok") : chip("no cumple", "warn"), checkChips(d.verdict && d.verdict.checks),
      d.verdict && d.verdict.criterios ? el("p", { class: "small muted", style: { marginTop: "8px" } }, "Criterios: " + Object.entries(d.verdict.criterios).map(([k, v]) => k + " = " + v).join(" · ")) : null),
    card("Curva de capital fuera de muestra", "estrategia vs canasta y buy & hold", eqBox),
    d.holdout && typeof d.holdout === "object" ? holdoutCard(d.holdout, charts) : null,
    el("div", { class: "grid two" },
      card("Costo → Sharpe", "bps por lado",
        curve.length ? xyChart({ series: [{ name: "Sharpe", color: css("--s1"), points: curve }], xLabel: "costo (bps por lado)", yLabel: "Sharpe",
          xFmt: (v) => num(v, 0), yFmt: (v) => num(v, 2), ariaLabel: "Sharpe en función del costo por lado",
          marks: [isNum(c.expected_cost_bps_per_side) ? { x: c.expected_cost_bps_per_side, label: "esperado " + num(c.expected_cost_bps_per_side, 1), color: css("--s2") } : null,
            isNum(c.breakeven_bps_per_side) && c.breakeven_bps_per_side <= Math.max(...curve.map((p) => p[0])) ? { x: c.breakeven_bps_per_side, label: "equilibrio", color: css("--bad") } : null].filter(Boolean) })
          : el("p", { class: "empty" }, "Sin curva de costos."),
        kv([["Costo esperado por lado", num(c.expected_cost_bps_per_side, 1) + " bps"], ["Costo de equilibrio por lado", num(c.breakeven_bps_per_side, 1) + " bps"],
          ["Margen (equilibrio / esperado)", isNum(c.breakeven_vs_expected) ? num(c.breakeven_vs_expected, 1) + "×" : "—"]])),
      card("De dónde sale el retorno", "fracción del capital inicial",
        signedBars([["precio", "Precio"], ["funding", "Funding"], ["comisiones", "Comisiones"], ["slippage", "Slippage"], ["total", "Total"]]
          .filter(([k]) => k in bd).map(([k, label]) => ({ label, value: k === "comisiones" || k === "slippage" ? -Math.abs(bd[k]) : bd[k], bold: k === "total" }))),
        el("p", { class: "small muted" }, "Comisiones y slippage se muestran como costo (negativo)."))),
    el("div", { class: "grid two" },
      card("Baselines", "",
        kv([["Sharpe de la canasta", fixed(b.basket_sharpe, 2)], ["Entradas al azar (n)", int(b.random_entries && b.random_entries.n)],
          ["Sharpe mediano al azar", fixed(b.random_entries && b.random_entries.median_sharpe, 2)],
          ["p contra azar", el("b", {}, fixed(b.random_entries && b.random_entries.p_value, 4))]]),
        el("h3", { style: { margin: "12px 0 6px" } }, "Buy & hold por símbolo (Sharpe)"),
        table([{ label: "Símbolo" }, { label: "Sharpe", cls: "n" }], Object.entries(b.buy_and_hold_sharpe || {}).map(([s, v]) => [s, fixed(v, 2)]), { label: "Buy & hold" })),
      card("Datos", "",
        kv([["Primer dato", d.data && d.data.first], ["Último dato", d.data && d.data.last], ["Holdout desde", d.data && String(d.data.holdout_start).slice(0, 10)],
          ["Holdout", d.holdout && typeof d.holdout === "object" ? chip("ABIERTO", "warn") : chip(d.data && d.data.holdout || "—", "info")], ["Entrenamiento / test", int(wf.train_days) + " / " + int(wf.test_days) + " días"],
          ["Ventanas", int(wf.windows)], ["Fuera de muestra", (wf.oos || []).join(" → ")], ["Generado", d.generated_at ? new Date(d.generated_at).toLocaleString("es-AR") : "—"]]),
        (d.notes || []).length ? el("ul", { class: "small muted" }, d.notes.map((n) => el("li", {}, n))) : null)),
    card("Ventanas walk-forward elegidas", int((wf.chosen || []).length) + " ventanas · parámetros elegidos solo con datos de entrenamiento",
      table([{ label: "#", cls: "n" }, { label: "Entrenamiento" }, { label: "Test (fuera de muestra)" }, { label: "Parámetros" }, { label: "Sharpe en entrenamiento", cls: "n" }],
        (wf.chosen || []).map((w, i) => [i + 1, (w.train || []).join(" → "), (w.test || []).join(" → "),
          el("span", { class: "mono small" }, Object.entries(w.params || {}).map(([k, v]) => k + "=" + v).join(", ")), fixed(w.train_sharpe, 2)]),
        { label: "Ventanas walk-forward", scroll: true, caption: "El Sharpe de entrenamiento está inflado por la selección: el que cuenta es el OOS de arriba." })),
  );
  return { unmount: () => { disposeChart(chart); charts.forEach(disposeChart); } };
}

/** Linea de capital (estrategia, canasta, B&H medio) en lightweight-charts. */
function equityChart(eq, charts) {
  const node = el("div", { class: "chart short", role: "img", "aria-label": "Curva de capital" });
  const series = [];
  const chart = makeChart(node, { timeScale: { timeVisible: false }, localization: { locale: "es-AR", priceFormatter: (v) => signedFrac(v, 1) } }, () => {
    const cols = [css("--s1"), css("--s2"), css("--s5")];
    series.forEach((x, i) => x.applyOptions({ color: cols[i] }));
  });
  if (chart) {
    charts.push(chart);
    for (const [data, w] of [[eq.strategy, 2], [eq.basket, 1], [eq.buy_and_hold_mean, 1]]) {
      const x = chart.addLineSeries({ lineWidth: w, priceLineVisible: false, lastValueVisible: true });
      x.setData((data || []).map(([t, v]) => ({ time: t, value: v })));
      series.push(x);
    }
    chart._retheme();
    requestAnimationFrame(() => chart.timeScale().fitContent());
  }
  return el("div", {}, node, legend([[css("--s1"), "estrategia"], [css("--s2"), "canasta"], [css("--s5"), "buy & hold medio"]]));
}

/** Resultado de la UNICA apertura del holdout. */
function holdoutCard(h, charts) {
  const v = h.verdict || {};
  const ok = v.pasa !== undefined ? v.pasa : h.confirma;
  const hr = h.hit_rate && typeof h.hit_rate === "object" ? h.hit_rate : { hit_rate: h.hit_rate };
  const win = Array.isArray(h.window) ? h.window.join(" → ") : h.window;
  const bd = h.breakdown || null;
  return card("Holdout (apertura única)", ok ? chip("confirmó el resultado fuera de muestra", "ok") : chip("no confirmó", "bad"),
    el("p", { class: "small muted" }, "Período " + (win || "—") + (h.days ? " · " + int(h.days) + " días" : "") +
      ". Se corrió una sola vez con los parámetros elegidos antes de abrirlo; no se vuelve a ajustar nada mirando este resultado."),
    el("div", { class: "stats" },
      stat("Sharpe holdout", fixed(h.sharpe, 2), h.sharpe_ci95 ? "IC 95 % " + fixed(h.sharpe_ci95[0], 2) + " a " + fixed(h.sharpe_ci95[1], 2) : ""),
      stat("Retorno", signedFrac(h.total_return), ""), stat("Máx. drawdown", frac(h.max_drawdown), ""),
      stat("Trades", int(h.trades), isNum(hr.hit_rate) ? "acertó " + frac(hr.hit_rate, 0) + (hr.n ? " de " + int(hr.n) : "") + (isNum(hr.lo) ? " (IC " + frac(hr.lo, 0) + "–" + frac(hr.hi, 0) + ")" : "") : ""),
      stat("Parámetros usados", el("span", { class: "mono small" }, fmtVal(h.selected_params)), "elegidos con datos anteriores")),
    v.checks ? el("div", { style: { marginTop: "10px" } }, checkChips(v.checks)) : null,
    h.equity_daily ? el("div", { style: { marginTop: "12px" } }, equityChart(h.equity_daily, charts)) : el("p", { class: "muted small" }, "Sin curva diaria del holdout."),
    bd ? el("div", { style: { marginTop: "12px", maxWidth: "560px" } }, el("h3", {}, "De dónde sale el retorno del holdout"),
      signedBars([["precio", "Precio"], ["funding", "Funding"], ["comisiones", "Comisiones"], ["slippage", "Slippage"], ["total", "Total"]]
        .filter(([k]) => k in bd).map(([k, label]) => ({ label, value: k === "comisiones" || k === "slippage" ? -Math.abs(bd[k]) : bd[k], bold: k === "total" })))) : null,
    h.baselines ? el("p", { class: "small muted", style: { marginTop: "8px" } }, "Baselines en el holdout: canasta " + fixed(h.baselines.basket_sharpe, 2) +
      (h.baselines.random_entries ? " · p contra azar " + fixed(h.baselines.random_entries.p_value, 3) : "")) : null);
}
