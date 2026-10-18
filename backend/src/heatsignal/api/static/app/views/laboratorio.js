// Laboratorio de backtest: lote (/api/lab) con veredicto y checks, y detalle (/api/lab/{id}) con curva de capital OOS vs
// canasta y buy & hold, costo -> Sharpe, descomposicion del PnL, ventanas walk-forward, baselines y p contra azar.
// Diagnosticos anti-sobreajuste (lab.diagnostics): columna PBO en la tabla y, en el detalle, PBO por CSCV + grafico
// de vecinos (Sharpe con cada parametro movido +-20 % / +-40 %). renderOverview / renderDetail: render puro (tests).
import {
  el, clear, api, errorBox, int, fixed, frac, num, card, table, pageHead, loading, chip, kv, stat, isNum, signedFrac, svgEl,
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

/** Listón de todo el programa: con K intentos, el mejor Sharpe que daría el puro azar (SR0). */
function programNote(p) {
  if (!p || !isNum(p.sr0_annual)) return null;
  return el("div", { class: "note warn" }, el("b", {}, "Listón del programa: "),
    "llevamos " + int(p.k) + " intentos (" + int(p.k_lab) + " combinaciones del laboratorio + " + int(p.k_benchmark) + " estrategias del benchmark). " +
    "Con tantos intentos, el mejor Sharpe que daría el azar ronda " + fixed(p.sr0_annual, 2) + " anual. La columna «DSR prog.» mide cada estrategia contra ese listón; " +
    "el DSR de la tabla solo corrige por sus propias combinaciones. Un DSR prog. bajo significa que el resultado todavía puede ser suerte.");
}

/** Benchmark: estrategias de TA públicas de Freqtrade, sin modificar, sobre las mismas monedas y período. */
function benchmarkCard(b) {
  if (!b || !(b.results || []).length) return null;
  const st = b.settings || {};
  return card("Benchmark: estrategias públicas de Freqtrade", el("span", { class: "small muted" }, String(b.start || "").slice(0, 10) + " → " + String(b.end || "").slice(0, 10)),
    el("p", { class: "small muted" }, "Estrategias del repo freqtrade-strategies (" + ((b.source || {}).license || "GPL-3.0") + ") corridas sin cambiar parámetros. " +
      "Sirven de vara: si una estrategia nuestra no le gana a esto, no aporta. Comisión " + fixed((st.fee_per_side || 0) * 1e4, 1) + " bps por lado, funding real, sin reinversión. " +
      "Ojo: el Sharpe acá es sobre PnL realizado por día de cierre, no exactamente igual al del laboratorio."),
    table([{ label: "Estrategia" }, { label: "TF" }, { label: "Trades", cls: "n" }, { label: "Long / short", cls: "n" }, { label: "Retorno", cls: "n" },
      { label: "Sharpe", cls: "n" }, { label: "Máx. DD", cls: "n" }, { label: "Acierto", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Apalanc.", cls: "n" }, { label: "Notas" }],
    b.results.map((r) => [el("b", {}, r.strategy || r.run_as), r.timeframe, int(r.trades), int(r.trades_long) + " / " + int(r.trades_short),
      signedFrac(r.return_total), fixed(r.sharpe_daily, 2), frac(r.max_dd), frac(r.win_rate, 0), signedFrac(r.funding_total),
      isNum(r.leverage_max) ? fixed(r.leverage_max, 1) + "×" : "—",
      { v: el("div", { class: "small muted", style: { minWidth: "220px" } }, r.ok ? (r.compat || "") : "error: " + (r.error || "?")), cls: "wrap" }]),
    { empty: "Sin resultados.", label: "Benchmark de Freqtrade" }),
    (b.notes || []).length ? el("ul", { class: "small muted" }, b.notes.map((n) => el("li", {}, n))) : null);
}

/** PBO (CSCV) de la grilla y meseta de vecinos, para la tabla del lote. */
export function pboCell(dg) {
  if (!dg) return el("span", { class: "muted", title: "todavía no se corrió python -m heatsignal.lab.diagnostics" }, "—");
  const parts = [];
  if (isNum(dg.pbo)) {
    const kind = dg.pbo >= 0.5 ? "bad" : dg.pbo >= 0.25 ? "warn" : "ok";
    parts.push(chip(fixed(dg.pbo, 2), kind, "probabilidad de sobreajuste de la grilla (CSCV, " + int(dg.pbo_partitions) + " particiones, " + int(dg.pbo_combinations) + " combinaciones)"));
  } else parts.push(el("span", { class: "muted small", title: dg.pbo_skipped || "sin PBO" }, dg.pbo_skipped ? "sin grilla" : "—"));
  if (isNum(dg.meseta)) parts.push(el("div", { class: "small muted" }, "meseta " + frac(dg.meseta, 0)));
  else if (isNum(dg.sharpe_dev)) parts.push(el("div", { class: "small muted" }, "meseta n/a"));
  if (dg.stale) parts.push(el("div", { class: "small warn-t", title: "el diagnóstico es de un resultado anterior al publicado" }, "desactualizado"));
  return el("div", {}, parts);
}

/** Tabla del lote (la usan mount y renderOverview). */
function labTable(items, href, onOpen) {
  return card(null, null, table([{ label: "Estrategia" }, { label: "Veredicto" }, { label: "Sharpe OOS", cls: "n" }, { label: "IC 95 %", cls: "n" },
    { label: "PSR / DSR", cls: "n" }, { label: "DSR prog.", cls: "n", title: "DSR contra el listón de todo el programa (todas las combinaciones probadas)" },
    { label: "PBO", cls: "n", title: "probabilidad de sobreajuste de la grilla (CSCV) y meseta de parámetros vecinos; diagnóstico, no suma intentos" },
    { label: "Retorno", cls: "n" }, { label: "Máx. DD", cls: "n" }, { label: "Trades", cls: "n" }, { label: "Acierto", cls: "n" },
    { label: "Break-even", cls: "n", title: "costo por lado que deja el Sharpe en 0" }, { label: "Canasta", cls: "n", title: "Sharpe de la canasta" },
    { label: "p azar", cls: "n" }, { label: "Criterios" }, { label: "Holdout" }],
  items.map((s) => [
    { v: el("div", { style: { minWidth: "300px" } }, s.has_detail ? el("a", { href: href("laboratorio", [s.strategy_id]) }, el("b", {}, s.strategy_id)) : el("b", {}, s.strategy_id),
      el("div", { class: "small muted mono" }, (s.oos_range || []).join(" → ") + " · " + int(s.windows) + " ventanas · " + int(s.n_combinations) + " comb."),
      el("div", { class: "muted", style: { marginTop: "4px" } }, specBlock(s))), cls: "wrap" },
    verdictChip(s.verdict), fixed(s.sharpe, 2), s.sharpe_ci95 ? fixed(s.sharpe_ci95[0], 2) + " / " + fixed(s.sharpe_ci95[1], 2) : "—",
    fixed(s.psr, 3) + " / " + fixed(s.dsr, 3), fixed(s.dsr_program, 3), pboCell(s.diagnostics), signedFrac(s.total_return), frac(s.max_drawdown), int(s.trades), frac(s.hit_rate, 0),
    isNum(s.breakeven_bps) ? num(s.breakeven_bps, 1) + " bps" : "—", fixed(s.basket_sharpe, 2), fixed(s.random_p, 3),
    { v: el("div", { style: { minWidth: "200px" } }, checkChips(s.verdict && s.verdict.checks)), cls: "wrap" }, holdoutCell(s),
  ]), { empty: "Sin estrategias en el lote.", label: "Lote del laboratorio",
    onRow: onOpen ? (i) => items[i].has_detail && onOpen(items[i].strategy_id) : undefined }),
  el("p", { class: "small muted", style: { marginTop: "8px" } }, "Sharpe anualizado; retornos y drawdown en fracción del capital inicial; costos en bps por lado. " +
    "PBO: probabilidad de que la mejor combinación de la grilla sea sobreajuste (≥ 0,5 = la elección no se sostiene). Las que no tienen detalle publicado no son clickeables."));
}

/** Render puro del resumen (tabla + listón del programa). */
export function renderOverview(d, href) {
  return [labTable(d.strategies || [], href, null), programNote(d.program)].filter(Boolean);
}

const STEP_COLOR = { "-0.4": ["--s2", 1], "-0.2": ["--s2", 0.5], "0.2": ["--s1", 0.5], "0.4": ["--s1", 1] };
const stepLabel = (st) => (st > 0 ? "+" : "−") + Math.round(Math.abs(st) * 100) + " %";

/** Grafico de vecinos: una fila por parametro, eje x = Sharpe del desarrollo (7 bps), un punto por variacion. */
export function neighborsChart(r) {
  const nbs = (r.neighbors || []).filter((n) => n.status !== "omitido" && isNum(n.sharpe));
  const params = [...new Set((r.neighbors || []).filter((n) => n.status !== "omitido").map((n) => n.param))];
  const base = r.base || {};
  if (!params.length) return el("p", { class: "empty" }, "Sin vecinos evaluados (ningún parámetro numérico movible).");
  const xs = nbs.map((n) => n.sharpe).concat(isNum(base.sharpe) ? [base.sharpe, base.sharpe * (r.threshold || 0.5)] : [], [0]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  const pad = (x1 - x0) * 0.08 || 0.5; x0 -= pad; x1 += pad;
  const W = 640, rowH = 26, m = { l: 150, r: 16, t: 26, b: 34 }, H = m.t + params.length * rowH + m.b;
  const sx = (v) => m.l + (v - x0) / (x1 - x0) * (W - m.l - m.r);
  const g = svgEl("svg", { class: "viz", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Sharpe del desarrollo con cada parámetro movido ±20 % y ±40 %; línea llena = original, punteada = 50 % del original" });
  const yBottom = H - m.b;
  const vline = (x, color, dash, label) => {
    g.append(svgEl("line", { x1: sx(x), x2: sx(x), y1: m.t - 6, y2: yBottom, stroke: color, "stroke-width": 1.5, "stroke-dasharray": dash || null }));
    if (label) g.append(svgEl("text", { x: sx(x) + 4, y: m.t - 10, style: `fill:${color}` }, label));
  };
  if (x0 < 0 && x1 > 0) g.append(svgEl("line", { class: "axis", x1: sx(0), x2: sx(0), y1: m.t - 6, y2: yBottom }));
  if (isNum(base.sharpe)) {
    vline(base.sharpe, css("--ink"), null, "original " + fixed(base.sharpe, 2));
    if (base.sharpe > 0) vline(base.sharpe * (r.threshold || 0.5), css("--warn"), "4 3", null);
  }
  g.append(svgEl("line", { class: "axis", x1: m.l, x2: W - m.r, y1: yBottom, y2: yBottom }));
  const step = (x1 - x0) / 5;
  for (let i = 0; i <= 5; i++) g.append(svgEl("text", { x: sx(x0 + i * step), y: yBottom + 16, "text-anchor": "middle" }, num(x0 + i * step, 2)));
  g.append(svgEl("text", { x: (m.l + W - m.r) / 2, y: H - 4, "text-anchor": "middle" }, "Sharpe del desarrollo (7 bps por lado)"));
  params.forEach((p, i) => {
    const y = m.t + i * rowH + rowH / 2;
    g.append(svgEl("line", { class: "grid-l", x1: m.l, x2: W - m.r, y1: y, y2: y }),
      svgEl("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, p));
    for (const n of nbs.filter((q) => q.param === p)) {
      const [col, op] = STEP_COLOR[String(n.step)] || ["--s5", 1];
      g.append(svgEl("circle", { cx: sx(n.sharpe), cy: y, r: 5, fill: css(col), "fill-opacity": op, stroke: css(col), "stroke-width": 1.2 },
        svgEl("title", {}, `${p} ${stepLabel(n.step)} (= ${n.value}): Sharpe ${fixed(n.sharpe, 2)} · costos ×${r.cost_mult || 3}: ${fixed(n.sharpe_x3, 2)}`)));
    }
  });
  return el("div", {}, g, legend([[css("--s2"), "−40 %"], [css("--s2"), "−20 % (claro)"], [css("--s1"), "+20 % (claro)"], [css("--s1"), "+40 %"],
    [css("--ink"), "Sharpe original"], [css("--warn"), "50 % del original (punteada)"]]));
}

function neighborsTable(r) {
  const by = {};
  for (const n of r.neighbors || []) (by[n.param] = by[n.param] || {})[String(n.step)] = n;
  const cell = (n) => {
    if (!n) return "—";
    if (n.status === "omitido") return el("span", { class: "small muted", title: n.reason || "" }, "omitido");
    const cls = n.en_meseta === false ? "bad-t" : n.en_meseta ? "ok-t" : "";
    return el("div", { title: n.reason || "" }, el("span", { class: "mono small muted" }, String(n.value) + (n.status === "recortado" ? " (límite)" : "")),
      el("div", { class: cls }, fixed(n.sharpe, 2)), el("div", { class: "small muted" }, "×" + (r.cost_mult || 3) + ": " + fixed(n.sharpe_x3, 2)));
  };
  return table([{ label: "Parámetro" }, { label: "Original", cls: "n" }, { label: "−40 %", cls: "n" }, { label: "−20 %", cls: "n" }, { label: "+20 %", cls: "n" }, { label: "+40 %", cls: "n" }],
    Object.keys(by).map((p) => [el("span", { class: "mono" }, p), el("span", { class: "mono" }, fmtVal((r.base_params || {})[p])),
      cell(by[p]["-0.4"]), cell(by[p]["-0.2"]), cell(by[p]["0.2"]), cell(by[p]["0.4"])]),
    { label: "Vecinos por parámetro", scroll: true, empty: "Sin vecinos." });
}

/** Tarjeta de diagnosticos del detalle: PBO + robustez por vecinos, con como leerlos. */
export function diagnosticsCard(dg) {
  const howTo = el("div", { class: "note info" }, el("b", {}, "Cómo leerlo. "),
    "PBO: se parte el desarrollo en bloques y, en cada forma de dividirlo en dos mitades, se elige la mejor combinación de la grilla en una mitad y se mira en qué puesto queda en la otra. " +
    "PBO es la fracción de veces que quedó en la mitad de abajo: cerca de 0 = la elección se sostiene; 0,5 o más = es tan buena como elegir al azar (sobreajuste). " +
    "Vecinos: cada fila es un parámetro movido −40 %, −20 %, +20 % y +40 % con el resto fijo. Si los puntos quedan cerca de la línea del original y a la derecha de la punteada (50 %), " +
    "hay una meseta: el resultado no depende de un valor exacto. Si caen lejos, el resultado es frágil. Es un diagnóstico, no una selección: no cambia parámetros congelados, " +
    "no suma intentos al listón del programa y no usa el holdout.");
  if (!dg) return card("Diagnósticos anti-sobreajuste", "sin correr", el("p", { class: "muted small" }, "Todavía no hay diagnósticos para esta estrategia. En la PC: python -m heatsignal.lab.diagnostics ambos <id>"), howTo);
  const p = dg.pbo_detail || {}, r = dg.robustez_detail || {}, deg = p.degradation || {};
  const pboStats = dg.pbo_detail ? el("div", { class: "stats" },
    stat("PBO", isNum(p.pbo) ? fixed(p.pbo, 2) : "—", int(p.n_partitions) + " particiones · " + int(p.n_blocks) + " bloques de " + int(p.block_days) + " días · " + int(p.n_combinations) + " comb.",
      isNum(p.pbo) ? (p.pbo >= 0.5 ? "bad-t" : p.pbo >= 0.25 ? "warn-t" : "ok-t") : ""),
    stat("Degradación", isNum(deg.slope) ? fixed(deg.slope, 2) : "—", "pendiente Sharpe fuera vs dentro (≤ 0: lo elegido no se traslada)"),
    stat("Prob. de pérdida fuera", frac(p.prob_oos_loss, 0), "Sharpe < 0 de la elegida en la otra mitad"),
    stat("Sharpe medio elegida", fixed(deg.is_sharpe_mean, 2) + " → " + fixed(deg.oos_sharpe_mean, 2), "dentro → fuera de la mitad"))
    : el("p", { class: "muted small" }, dg.pbo_skipped || "PBO sin correr.");
  const robStats = dg.robustez_detail ? el("div", { class: "stats" },
    stat("Meseta", isNum(r.meseta) ? frac(r.meseta, 0) : "n/a", isNum(r.meseta) ? "de " + int(r.n_neighbors) + " vecinos con Sharpe ≥ 50 % del original" : (r.meseta_note || ""),
      isNum(r.meseta) ? (r.meseta >= 0.8 ? "ok-t" : r.meseta >= 0.5 ? "warn-t" : "bad-t") : ""),
    stat("Caída máxima", fixed(r.max_drop, 2), r.worst ? "peor: " + r.worst.param + " " + stepLabel(r.worst.step) + (isNum(r.max_drop_rel) ? " (" + frac(r.max_drop_rel, 0) + " del original)" : "") : ""),
    stat("Sharpe desarrollo", fixed((r.base || {}).sharpe, 2), num(r.cost_bps_per_side, 1) + " bps por lado"),
    stat("Con costos ×" + (r.cost_mult || 3), fixed((r.base || {}).sharpe_x3, 2), isNum(r.meseta_x3) ? "meseta ×" + (r.cost_mult || 3) + ": " + frac(r.meseta_x3, 0) : ""))
    : el("p", { class: "muted small" }, "Robustez por vecinos sin correr.");
  return card("Diagnósticos anti-sobreajuste", el("span", {}, chip("diagnóstico, no selección", "info", "no suma intentos al K del programa ni cambia parámetros"),
    dg.stale ? chip("de un resultado anterior", "warn") : null),
  el("p", { class: "small muted" }, "Período de desarrollo " + ((dg.development || []).map((x) => String(x).slice(0, 10)).join(" → ") || "—") + " · holdout: " + (dg.holdout || "no usado") + "."),
  el("h3", {}, "Sobreajuste de la grilla (PBO, CSCV)"), pboStats,
  el("h3", { style: { marginTop: "12px" } }, "Robustez por parámetros vecinos"), robStats,
  dg.robustez_detail ? neighborsChart(r) : null,
  dg.robustez_detail ? neighborsTable(r) : null,
  (r.params_not_numeric || []).length ? el("p", { class: "small muted" }, "No se mueven (no son números): " + r.params_not_numeric.join(", ") + ".") : null,
  howTo);
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
    labTable(items, ctx.href, (id) => ctx.navigate(ctx.href("laboratorio", [id]))),
    programNote(d.program),
    benchmarkCard(d.benchmark_ta),
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
  const charts = [];
  clear(root, ...detailBody(d, charts));
  return { unmount: () => { charts.forEach(disposeChart); } };
}

/** Render puro del detalle (sin red): lo usa detail() y los tests. */
export function renderDetail(d) {
  return el("div", {}, detailBody(d, []));
}

function detailBody(d, charts) {
  const o = d.oos || {}, c = d.costs || {}, wf = d.walk_forward || {}, b = d.baselines || {}, hr = o.hit_rate || {};
  const eqBox = el("div");
  let chart = null;

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
      charts.push(chart);
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

  return [
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
    diagnosticsCard(d.diagnostics),
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
  ].filter(Boolean);
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
