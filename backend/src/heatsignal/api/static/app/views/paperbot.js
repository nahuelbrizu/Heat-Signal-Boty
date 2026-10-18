// Bot propio de paper trading del lote 3 (/api/paperbot y /api/paperbot/{id}): corre cada hora en la VM, simula las 6
// estrategias con las MISMAS reglas del laboratorio, dinero ficticio. Honestidad: aviso fijo de que es simulado y de que
// el backtest todavia puede descartarlas; huecos y corridas con error a la vista; tasas con n e IC.
import {
  el, clear, api, errorBox, when, int, num, frac, price, card, table, pageHead, loading, chip, kv, stat, isNum,
  signedFrac, rateText, sym as toSym, DASH,
} from "../core.js";
import { css, signedBars, legend, xyChart } from "../charts.js";

const NAMES = {
  "tsmom-diario-v1": "Momentum diario (tsmom)",
  "low-vol-xs-v1": "Baja volatilidad cross-sectional",
  "pares-eth-btc-v1": "Pares ETH/BTC",
  "rango-reversion-v1": "Reversión en rango (BNB/TRX/XLM)",
  "reversion-xs-1h-v1": "Reversión alts vs BTC 1h",
  "funding-prepago-v1": "Funding prepago",
};
const STATUS = { ok: ["ok", "ok"], error: ["bad", "error"], skipped: ["", "omitida"] };
const REASON = { senal: "señal", resincronizacion: "re-sincronización", inicio: "inicio", hueco: "después de hueco" };

function money(v, signed = false) {
  if (!isNum(v)) return DASH;
  const r = Math.abs(v) < 0.005 ? 0 : v;
  return (signed ? (r > 0 ? "+" : r < 0 ? "−" : "") : r < 0 ? "−" : "") + "US$ " + num(Math.abs(r), 2, 2);
}
const sMoney = (v) => isNum(v) ? el("span", { class: Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg" }, money(v, true)) : DASH;
const sFrac = (v, d = 2) => isNum(v) ? el("span", { class: Math.abs(v) < 5e-5 ? "" : v > 0 ? "pos" : "neg" }, signedFrac(v, d)) : DASH;
const statusChip = (s) => { const [k, t] = STATUS[s] || ["", s || DASH]; return chip(t, k); };
const name = (sid) => NAMES[sid] || sid;

/** Peso objetivo como chip: L/S y fraccion del capital del simbolo. */
function weightChip(symbol, w) {
  if (!isNum(w) || Math.abs(w) < 1e-9) return null;
  return chip(toSym(symbol).split("-")[0] + " " + (w > 0 ? "L " : "S ") + num(Math.abs(w), 2), w > 0 ? "ok" : "bad",
    (w > 0 ? "long " : "short ") + frac(Math.abs(w), 0) + " del capital asignado a " + toSym(symbol));
}

const CHECKS = { opera: "opera", dsr: "DSR ≥ 0,95", breakeven: "break-even ≥ 2× costo", le_gana_a_la_canasta: "le gana a la canasta",
  le_gana_al_buy_and_hold_medio: "le gana al B&H medio", le_gana_al_azar: "le gana al azar" };

/** Veredicto del backtest del laboratorio (lab_summary.json) para mostrar al lado del paper. */
function labChip(lab) {
  if (!lab) return chip("sin backtest publicado", "", "no hay resultado del laboratorio para esta estrategia");
  if (lab.same_spec === false) return chip("backtest de otra versión", "warn", "el spec_hash del backtest no coincide con el del bot");
  return lab.pasa ? chip("backtest: pasa", "ok") : chip("backtest: no pasa", "bad", "no cumple los criterios para pasar a paper");
}
function labCell(lab) {
  if (!lab) return el("span", { class: "muted" }, "sin backtest");
  return el("span", {}, labChip(lab), el("div", { class: "small" }, "Sharpe OOS ", el("b", { class: isNum(lab.sharpe) ? (lab.sharpe >= 0 ? "pos" : "neg") : "" }, num(lab.sharpe, 2)),
    lab.sharpe_ci95 ? el("span", { class: "muted" }, " (IC " + num(lab.sharpe_ci95[0], 2) + " a " + num(lab.sharpe_ci95[1], 2) + ")") : null));
}
export function labBlock(lab) {
  if (!lab) return el("div", { class: "note" }, "No hay resultado del laboratorio publicado para esta estrategia.");
  const failed = Object.entries(lab.checks || {}).filter(([, ok]) => ok === false).map(([k]) => CHECKS[k] || k);
  return el("div", { class: "note " + (lab.pasa ? "info" : "bad") },
    el("b", {}, lab.pasa ? "El backtest pasó los criterios. " : "El backtest NO pasó los criterios: este paper corre igual, pero la estrategia está descartada salvo que el paper muestre otra cosa con muestra suficiente. "),
    lab.same_spec === false ? el("b", {}, "Ojo: el backtest es de otra versión de la regla (spec " + lab.spec_hash + "). ") : null,
    "Sharpe OOS " + num(lab.sharpe, 2) + (lab.sharpe_ci95 ? " (IC 95 % " + num(lab.sharpe_ci95[0], 2) + " a " + num(lab.sharpe_ci95[1], 2) + ")" : "") +
    " · DSR " + num(lab.dsr, 2) + " · retorno " + signedFrac(lab.total_return, 1) + " · máx. DD " + frac(lab.max_drawdown, 1) +
    " · canasta Sharpe " + num(lab.basket_sharpe, 2) + " · p azar " + num(lab.random_p, 3) +
    (lab.oos_range ? " · OOS " + lab.oos_range[0] + " a " + lab.oos_range[1] : "") + (lab.holdout ? " · holdout " + lab.holdout : "") + ".",
    failed.length ? el("div", {}, "Falla: ", failed.join(", "), ".") : null);
}

export async function mount(root, ctx) {
  const head = pageHead("Bot paper · lote 3", "Bot propio que corre cada hora en la VM y simula las 6 estrategias del lote 3 con las mismas reglas del laboratorio. Solo lectura.",
    el("a", { class: "btn", href: ctx.href("paper") }, "Freqtrade dry-run →"));
  const top = el("div", {}, loading());
  const detailBox = el("div", {});
  clear(root, head, top, detailBox);
  let ov = null;
  const selected = () => ctx.route.parts[0] || (ov && ov.strategies.length ? ov.strategies[0].strategy_id : null);

  async function loadDetail() {
    const sid = selected();
    if (!sid) { clear(detailBox); return; }
    try {
      const d = await api("/api/paperbot/" + encodeURIComponent(sid), null, ctx.signal);
      clear(detailBox, renderDetail(d));
    } catch (e) { const b = errorBox(e, "el detalle de la estrategia"); if (b) clear(detailBox, b); }
  }
  async function load() {
    try {
      ov = await api("/api/paperbot", null, ctx.signal);
      clear(top, ...renderOverview(ov, ctx.href, selected()));
      await loadDetail();
    } catch (e) {
      const b = errorBox(e, "el bot de paper trading");
      if (b) clear(top, disclaimer(null), b, e.status === 503 ? el("p", { class: "small muted" },
        "El bot escribe ", el("code", {}, "data/paperbot.sqlite"), " desde heatsignal-paperbot.timer (a los :02 UTC). ",
        "Hasta la primera corrida no hay nada que mostrar.") : null);
    }
  }
  await load();
  ctx.every(60000, load);
  return { refresh: load };
}

function disclaimer(ov) {
  return el("div", { class: "note warn", role: "note" }, el("b", {}, "Simulado, sin plata real. "),
    ov && ov.disclaimer ? ov.disclaimer : "Paper trading con dinero ficticio; el backtest del lote 3 todavía puede descartar estas estrategias.");
}

// ------------------------------------------------------------------ resumen

export function renderOverview(ov, href, sel) {
  const last = ov.last_run || {}, c = ov.counts || {};
  const stats = el("div", { class: "stats" },
    stat("Última corrida", el("span", { class: "chips" }, statusChip(last.status), ov.stale ? chip("atrasado", "warn", "más de 2 h sin corrida ok") : null),
      last.hour_ms ? el("span", {}, "hora ", when(last.hour_ms), last.reason ? " · " + last.reason : "") : "ninguna"),
    stat("Corridas", int(c.ok) + " ok", int(c.error) + " con error · " + int(c.skipped) + " omitidas", c.error ? "bad-t" : ""),
    stat("Huecos", int((ov.gaps || []).length), (ov.gaps || []).length ? "horas sin simular (no se rellenan)" : "ninguno"),
    stat("Paper desde", ov.started_ms ? when(ov.started_ms) : DASH, "10.000 US$ ficticios por estrategia"));
  const notes = (ov.notes || []).map((n) => el("div", { class: "note bad" }, n));
  const withLab = (ov.strategies || []).filter((s) => s.lab);
  const failing = withLab.filter((s) => !s.lab.pasa);
  const negative = withLab.filter((s) => isNum(s.lab.sharpe) && s.lab.sharpe < 0);
  if (failing.length) notes.push(el("div", { class: "note bad" }, el("b", {}, "Backtest del lote 3: "),
    int(failing.length) + " de " + int(withLab.length) + " estrategias no pasan los criterios del laboratorio" +
    (negative.length ? " (" + int(negative.length) + " con Sharpe fuera de muestra negativo)" : "") +
    ". El paper sigue corriendo para compararlo con la realidad, no porque estén aprobadas."));

  const strategies = ov.strategies || [];
  const stTable = table([{ label: "Estrategia" }, { label: "Equity", cls: "n" }, { label: "Retorno", cls: "n" },
    { label: "Canasta", cls: "n", title: "buy & hold igual-ponderado de los mismos símbolos desde el inicio del paper" },
    { label: "Realizado", cls: "n" }, { label: "No realizado", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Costos", cls: "n", title: "comisiones + slippage" },
    { label: "Exposición", cls: "n", title: "nocional bruto / capital" }, { label: "Órdenes", cls: "n" }, { label: "Trades cerrados", cls: "n" }, { label: "Posición actual" },
    { label: "Backtest (laboratorio)", title: "veredicto del walk-forward OOS publicado (lab_summary.json)" }],
  strategies.map((s) => [
    el("a", { href: href("paperbot", [s.strategy_id]), "aria-current": s.strategy_id === sel ? "true" : null }, el("b", {}, name(s.strategy_id)),
      el("div", { class: "small muted mono" }, s.strategy_id)),
    money(s.equity), sFrac(s.return), sFrac(s.basket_return), sMoney(s.realized), sMoney(s.unrealized), sMoney(s.funding),
    money((s.fees || 0) + (s.slippage || 0)), num(s.exposure, 2) + "×", int(s.n_orders),
    s.n_trades ? int(s.wins) + "/" + int(s.n_trades) + " ganadores" : "0",
    el("span", { class: "chips" }, ...(s.positions || []).map((p) => weightChip(p.symbol, p.weight)).filter(Boolean),
      (s.positions || []).some((p) => Math.abs(p.weight) > 1e-9) ? null : el("span", { class: "muted" }, "plana")), labCell(s.lab)]),
  { empty: "Todavía no hay corridas ok.", label: "Estrategias del bot", scroll: true });

  const runs = ov.runs || [];
  const runTable = table([{ label: "Hora" }, { label: "Estado" }, { label: "Modo" }, { label: "Motivo / nota" }, { label: "Duración", cls: "n" }, { label: "Memoria pico", cls: "n" }],
    runs.slice(0, 48).map((r) => [when(r.hour_ms), statusChip(r.status), r.mode || DASH, { v: r.reason || DASH, cls: "wrap" },
      isNum(r.duration_s) ? num(r.duration_s, 1) + " s" : DASH, isNum(r.peak_rss_kb) ? num(r.peak_rss_kb / 1024, 0) + " MB" : DASH]),
    { empty: "Sin corridas.", label: "Corridas recientes", scroll: true });
  const gaps = ov.gaps || [];
  const gapTable = table([{ label: "Desde" }, { label: "Hasta" }, { label: "Horas", cls: "n" }],
    gaps.map((g) => [when(g.from_ms), when(g.to_ms), int(g.hours)]), { empty: "Sin huecos.", label: "Huecos" });

  return [disclaimer(ov), ...notes, stats,
    el("div", { class: "stack" },
      card("Estrategias", "estado a la última corrida ok (hora cerrada)", stTable,
        el("p", { class: "small muted" }, "Capital ficticio 10.000 US$ por estrategia repartido en partes iguales entre sus símbolos, sin reinversión. ",
          "Costos 5 bps de comisión + 2 bps de slippage por lado (igual que el laboratorio); funding real liquidado de Binance.")),
      el("div", { class: "grid two" },
        card("Corridas recientes", "una por hora, a los :02 UTC", runTable),
        card("Huecos", "horas que el bot no simuló", gapTable,
          el("p", { class: "small muted" }, "Si la VM estuvo apagada o la corrida falló, esas horas NO se simulan después: al volver, las posiciones se llevan al objetivo de la regla y el hueco queda registrado.")))),
  ];
}

// ------------------------------------------------------------------ detalle

export function renderDetail(d) {
  const eq = d.equity_hourly || [];
  const ptsS = eq.filter((r) => isNum(r[1])).map((r) => [r[0], r[1] / d.capital - 1]);
  const ptsB = eq.filter((r) => isNum(r[2])).map((r) => [r[0], r[2] / d.capital - 1]);
  const chart = ptsS.length >= 2 ? el("div", {},
    xyChart({ series: [{ name: "estrategia", color: css("--s1"), points: ptsS }, { name: "canasta", color: css("--s4"), points: ptsB }],
      xFmt: (v) => new Date(v).toLocaleDateString("es-AR"), yFmt: (v) => signedFrac(v, 2), yLabel: "retorno acumulado", height: 260,
      ariaLabel: "Equity horaria de " + d.strategy_id + " contra la canasta" }),
    legend([[css("--s1"), "estrategia (paper)"], [css("--s4"), "canasta igual-ponderada (buy & hold)"]]))
    : el("p", { class: "empty" }, "La curva aparece con la segunda corrida ok.");

  const b = d.breakdown || {};
  const bars = signedBars([["Precio", b.precio], ["Funding", b.funding], ["Comisiones", -(b.comisiones || 0)], ["Slippage", -(b.slippage || 0)]]
    .map(([label, value]) => ({ label, value: value || 0 })).concat([{ label: "Total", value: b.total || 0, bold: true }]), (v) => money(v, true));

  const positions = (d.positions || []);
  const posTable = table([{ label: "Símbolo" }, { label: "Peso", cls: "n", title: "fracción del capital asignado al símbolo" }, { label: "Objetivo regla", cls: "n" },
    { label: "Unidades", cls: "n" }, { label: "Entrada media", cls: "n" }, { label: "Marca", cls: "n" }, { label: "Nocional", cls: "n" }, { label: "No realizado", cls: "n" }],
  positions.map((p) => [el("b", {}, toSym(p.symbol)), Math.abs(p.weight) > 1e-9 ? weightChip(p.symbol, p.weight) : el("span", { class: "muted" }, "plana"),
    num(p.target, 3), num(p.units, 4), price(p.entry_price), price(p.mark_price), money(p.notional), sMoney(p.unrealized)]),
  { label: "Posiciones actuales" });

  const bySym = table([{ label: "Símbolo" }, { label: "Precio", cls: "n" }, { label: "Realizado", cls: "n" }, { label: "No realizado", cls: "n" },
    { label: "Funding", cls: "n" }, { label: "Comisiones", cls: "n" }, { label: "Slippage", cls: "n" }, { label: "Total", cls: "n" }],
  (d.breakdown_by_symbol || []).map((x) => [el("b", {}, toSym(x.symbol)), sMoney(x.precio), sMoney(x.realizado), sMoney(x.no_realizado),
    sMoney(x.funding), money(x.comisiones), money(x.slippage), sMoney(x.total)]), { label: "Descomposición por símbolo" });

  const trades = d.trades || [];
  const nW = trades.filter((t) => t.pnl_net > 0).length;
  const trTable = table([{ label: "Símbolo" }, { label: "Lado" }, { label: "Entrada" }, { label: "Salida" }, { label: "Precio entrada", cls: "n" },
    { label: "Precio salida", cls: "n" }, { label: "Precio", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Costos", cls: "n" }, { label: "Neto", cls: "n" }],
  trades.map((t) => [el("b", {}, toSym(t.symbol)), el("span", { class: t.direction === "long" ? "buy" : "sell" }, t.direction === "long" ? "▲ long" : "▼ short"),
    when(t.entry_ms), when(t.exit_ms), price(t.entry_price), price(t.exit_price), sMoney(t.pnl_price), sMoney(t.funding),
    money((t.fees || 0) + (t.slippage || 0)), sMoney(t.pnl_net)]),
  { empty: "Ningún trade cerrado todavía.", label: "Trades cerrados", scroll: true });

  const orders = d.orders || [];
  const orTable = table([{ label: "Fill" }, { label: "Decisión" }, { label: "Símbolo" }, { label: "Peso", cls: "n" }, { label: "Precio", cls: "n" },
    { label: "Fuente del precio" }, { label: "Nocional", cls: "n" }, { label: "Costos", cls: "n" }, { label: "Motivo" }],
  orders.map((o) => [when(o.fill_ms), when(o.decision_ms), el("b", {}, toSym(o.symbol)), num(o.weight_from, 3) + " → " + num(o.weight_to, 3),
    price(o.price), o.price_source === "ultimo_precio" ? chip("último precio", "warn", "la vela siguiente todavía no existía") : "open 1m siguiente",
    money(o.notional), money((o.fee || 0) + (o.slippage || 0)),
    o.reason === "senal" ? REASON.senal : chip(REASON[o.reason] || o.reason, "warn", "orden fuera de la señal de la regla")]),
  { empty: "Sin órdenes simuladas.", label: "Órdenes simuladas", scroll: true });

  const fund = d.funding_events || [];
  const fTable = table([{ label: "Liquidación" }, { label: "Símbolo" }, { label: "Tasa", cls: "n" }, { label: "Unidades", cls: "n" }, { label: "Precio", cls: "n" }, { label: "Cobrado / pagado", cls: "n" }],
    fund.map((f) => [when(f.funding_ms), toSym(f.symbol), num(f.rate * 100, 4, 4) + " %", num(f.units, 4), price(f.price), sMoney(f.cash)]),
    { empty: "Ninguna liquidación con posición abierta todavía.", label: "Funding", scroll: true });

  const stats = el("div", { class: "stats" },
    stat("Equity", money(d.equity), "retorno " + signedFrac(d.return, 2), isNum(d.return) ? (d.return >= 0 ? "ok-t" : "bad-t") : ""),
    stat("Canasta", isNum(d.basket_equity) ? money(d.basket_equity) : DASH, "retorno " + signedFrac(d.basket_return, 2)),
    stat("Realizado / no realizado", el("span", {}, sMoney(d.realized), " / ", sMoney(d.unrealized)), "funding " + money(d.funding, true)),
    stat("Máx. drawdown", isNum(d.max_drawdown) ? signedFrac(d.max_drawdown, 2) : DASH, "sobre la equity horaria"),
    stat("Trades cerrados", int(trades.length), trades.length ? rateText(nW, trades.length, d.win_ci95) : "todavía ninguno"));

  return card(name(d.strategy_id), el("span", { class: "chips" }, chip("simulado", "warn"), labChip(d.lab), d.spec_hash ? chip("spec " + d.spec_hash, "", "hash de la regla congelada") : null),
    el("div", { class: "stack" },
      labBlock(d.lab),
      stats,
      trades.length && trades.length < 30 ? el("div", { class: "note" }, el("b", {}, "Muestra chica: "), int(trades.length) + " trades cerrados no alcanzan para sacar conclusiones.") : null,
      kv([["Estrategia", el("code", {}, d.strategy_id)], ["Parámetros de la grilla", el("code", {}, JSON.stringify(d.params || {}))],
        ["Origen de los parámetros", d.origen || DASH], ["Universo", (d.universe || []).map(toSym).join(", ")], ["Estado a la hora", when(d.hour_ms)]]),
      el("div", { class: "grid two" },
        el("div", {}, el("h3", {}, "Equity horaria vs canasta"), chart),
        el("div", {}, el("h3", {}, "De dónde sale el PnL"), bars,
          el("p", { class: "small muted" }, "Precio = realizado + no realizado (precios crudos); el slippage va aparte, así la suma es exacta."))),
      el("div", {}, el("h3", {}, "Posiciones actuales"), posTable),
      el("div", {}, el("h3", {}, "Descomposición por símbolo"), bySym),
      el("div", {}, el("h3", {}, "Trades cerrados"), trTable),
      el("div", {}, el("h3", {}, "Órdenes simuladas"), orTable,
        el("p", { class: "small muted" }, "Fill al open de la vela 1m siguiente a la decisión (convención del laboratorio). ",
          "“Re-sincronización”, “inicio” y “después de hueco” son órdenes que llevan la posición al objetivo de la regla fuera de una señal: se marcan para separarlas en la comparación con el backtest.")),
      el("div", {}, el("h3", {}, "Funding"), fTable)));
}
