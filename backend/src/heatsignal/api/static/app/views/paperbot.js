// Bot propio de paper trading (/api/paperbot y /api/paperbot/{id}): corre cada hora en la VM, simula las estrategias de
// params.json con las MISMAS reglas del laboratorio, dinero ficticio. Prueba hacia adelante (2026-10-20): tres grupos
// por rol — hipótesis principal (las familias de ruptura long, short y neta: deciden con su criterio pre-registrado),
// seguimiento (20 variantes del lote 6, exploratorio, corrección BH al evaluar) y paper previo (lote 3 + max-ruptura).
// Cada estrategia tiene su propio inicio (la que se sumó después arranca con su capital y su canasta desde ahí). Honestidad: aviso fijo de que es simulado y de que
// el backtest todavia puede descartarlas; huecos y corridas con error a la vista; tasas con n e IC.
import {
  el, clear, api, apiPost, errorBox, when, int, num, frac, price, card, table, pageHead, loading, chip, kv, stat, isNum,
  signedFrac, rateText, sym as toSym, DASH, fixed,
} from "../core.js";
import { css, signedBars, legend, xyChart } from "../charts.js";
import { exitChips, exitRows, reasonBars, motivoChip, MOTIVO } from "../exits-ui.js";

const NAMES = {
  "tsmom-diario-v1": "Momentum diario (tsmom)",
  "low-vol-xs-v1": "Baja volatilidad cross-sectional",
  "pares-eth-btc-v1": "Pares ETH/BTC",
  "rango-reversion-v1": "Reversión en rango (BNB/TRX/XLM)",
  "reversion-xs-1h-v1": "Reversión alts vs BTC 1h",
  "funding-prepago-v1": "Funding prepago",
  "max-ruptura-10d-v1": "Ruptura del máximo de 10 días (lote 5)",
  "l6-familia-ruptura-long-v1": "Familia ruptura LONG (12 rupturas, 1/12 c/u)",
  "l6-familia-ruptura-short-v1": "Familia ruptura SHORT (12 rupturas, 1/12 c/u)",
  "l6-familia-ruptura-neta-v1": "Familia ruptura NETA (balance long − short)",
};
const ROLE_ORDER = ["principal", "seguimiento", "paper_previo", "sltp"];
const ROLE_LABEL = { principal: "Hipótesis principal (decide)", seguimiento: "Seguimiento (exploratorio, no decide)", paper_previo: "Paper previo",
  sltp: "Con SL/TP obligatorio (-v2, en paralelo)" };
const ROLE_SUB = {
  principal: "pre-registradas antes del primer dato (decisions.md, 2026-10-20); solo estas deciden, con su criterio a 3 y 6 meses",
  seguimiento: "20 variantes del lote 6 que pasaron backtest y robustez; NO deciden nada: al evaluarlas se corrige por comparaciones múltiples (Benjamini–Hochberg)",
  paper_previo: "estrategias que ya corrían antes de la prueba hacia adelante (lote 3 y max-ruptura-10d-v1)",
  sltp: "desde el 2026-10-26: cada estrategia con su -v2 que usa las mismas entradas pero sale SOLO por stop (3 ATR, entre 8 % y 20 %) u objetivo (2× el stop), con el stop siempre antes de la liquidación. No decide la prueba hacia adelante",
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
const name = (sid) => NAMES[sid] || (/-v2$/.test(sid) && NAMES[sid.replace(/-v2$/, "-v1")] ? NAMES[sid.replace(/-v2$/, "-v1")] + " · con SL/TP" : sid);
const countsLine = (c) => c && Object.keys(c).length ? el("div", { class: "small muted" },
  Object.entries(c).map(([m, n]) => (MOTIVO[m] || m) + " " + int(n)).join(" · ")) : null;

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
/** Resultado del holdout (si se abrió): se muestra siempre al lado del backtest, confirme o no. */
export function holdoutChip(lab) {
  const h = lab && lab.holdout_result;
  if (!h) return lab && lab.holdout === "ABIERTO" ? chip("holdout abierto", "info") : null;
  return h.confirma ? chip("holdout: confirmó", "ok")
    : chip("holdout: no confirmó", "bad", "el trimestre reservado no confirmó el backtest" + (isNum(h.random_p) ? " (p azar " + num(h.random_p, 2) + ")" : ""));
}
const HOLD_CHECKS = { retorno_neto_positivo: "retorno neto > 0", le_gana_a_la_canasta: "le gana a la canasta", le_gana_al_azar: "le gana al azar" };
function holdoutText(h) {
  if (!h) return null;
  const failed = Object.entries(h.checks || {}).filter(([, ok]) => ok === false).map(([k]) => HOLD_CHECKS[k] || k);
  return el("div", {}, el("b", {}, h.confirma ? "Holdout: confirmó. " : "Holdout: NO confirmó. "),
    (h.window ? "Ventana " + String(h.window[0]).slice(0, 10) + " a " + String(h.window[1]).slice(0, 10) + " · " : "") +
    "Sharpe " + num(h.sharpe, 2) + " · retorno " + signedFrac(h.total_return, 1) + " · " + int(h.trades) + " trades" +
    (isNum(h.basket_sharpe) ? " · canasta Sharpe " + num(h.basket_sharpe, 2) : "") + (isNum(h.random_p) ? " · p azar " + num(h.random_p, 2) : "") + ".",
    failed.length ? " Falla: " + failed.join(", ") + "." : "",
    h.confirma ? "" : " Según la regla fijada no pasa a plata real; el paper junta evidencia en otro régimen.");
}
function labCell(lab) {
  if (!lab) return el("span", { class: "muted" }, "sin backtest");
  return el("span", {}, el("span", { class: "chips" }, labChip(lab), holdoutChip(lab)), el("div", { class: "small" }, "Sharpe OOS ", el("b", { class: isNum(lab.sharpe) ? (lab.sharpe >= 0 ? "pos" : "neg") : "" }, num(lab.sharpe, 2)),
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
    failed.length ? el("div", {}, "Falla: ", failed.join(", "), ".") : null,
    holdoutText(lab.holdout_result));
}

export async function mount(root, ctx) {
  const head = pageHead("Bot paper", "Bot propio que corre cada hora en la PC y simula, con las mismas reglas del laboratorio, la prueba hacia adelante del lote 6 (hipótesis principal y seguimiento) y el paper previo. Desde acá podés enviarle estrategias del laboratorio como seguimiento.",
    el("a", { class: "btn", href: ctx.href("paper") }, "Freqtrade dry-run →"));
  const top = el("div", {}, loading());
  const detailBox = el("div", {});
  const enrollBox = el("div", {});
  clear(root, head, top, enrollBox, detailBox);
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
  clear(enrollBox, enrollBlock(ctx, load));
  ctx.every(60000, load);
  return { refresh: load };
}

// ------------------------------------------------------------------ enviar estrategias del laboratorio al bot

const MAX_ENROLL = 10; // = PaperbotEnrollBody.strategy_ids max_length

/** Estrategias del laboratorio que todavia no corren en el bot, ordenadas por Sharpe fuera de muestra, con casilla
 * para elegir y "Enviar al bot" (POST /api/paperbot/enroll): entran como seguimiento, desde su primera corrida. */
export function enrollBlock(ctx, onDone) {
  const box = el("div", {}, loading());
  const status = el("div", { role: "status" });
  let showAll = false;
  async function load() {
    let d;
    try { d = await api("/api/paperbot/candidates", showAll ? { all: 1 } : null, ctx.signal); } catch (e) {
      const b = errorBox(e, "las estrategias del laboratorio"); if (b) clear(box, b); return;
    }
    const items = d.items || [];
    const visible = showAll ? items : items.filter((x) => x.enviable).slice(0, 15);
    const boxes = new Map();
    const send = el("button", { class: "btn primary", type: "button", disabled: true }, "Enviar al bot");
    const count = el("span", { class: "muted small" }, "");
    const sync = () => {
      const n = [...boxes.values()].filter((c) => c.checked).length;
      send.disabled = n === 0 || n > MAX_ENROLL;
      count.textContent = n ? n + " elegida(s)" + (n > MAX_ENROLL ? " · máximo " + MAX_ENROLL + " por envío" : "") : "";
    };
    const rows = visible.map((x) => {
      const cb = el("input", { type: "checkbox", value: x.strategy_id, disabled: !x.enviable,
        "aria-label": "elegir " + x.strategy_id });
      cb.addEventListener("change", sync);
      if (x.enviable) boxes.set(x.strategy_id, cb);
      return [cb,
        el("div", {}, el("a", { href: ctx.href("laboratorio", [x.strategy_id]) }, el("b", { class: "mono small" }, x.strategy_id)),
          x.description ? el("div", { class: "small muted" }, String(x.description).slice(0, 110)) : null,
          x.motivo ? el("div", { class: "small warn-t" }, "no se puede enviar: " + x.motivo) : null),
        el("span", { class: "num" }, fixed(x.sharpe)),
        el("span", { class: "num" }, fixed(x.dsr, 3)),
        el("span", { class: "num" }, int(x.trades)),
        el("span", { class: "num" }, isNum(x.max_drawdown) ? frac(x.max_drawdown) : DASH),
        el("span", { class: "chips" }, x.pasa_lab ? chip("pasa lab", "ok") : chip("no pasa lab", "bad"),
          x.pasa_programa ? chip("pasa programa", "ok") : chip("no pasa corrección", "warn",
            "veredicto del programa: corregido por la cantidad de estrategias probadas"))];
    });
    send.addEventListener("click", async () => {
      const ids = [...boxes.entries()].filter(([, c]) => c.checked).map(([k]) => k);
      if (!ids.length) return;
      send.disabled = true;
      clear(status, el("span", { class: "muted" }, "enviando…"));
      try {
        const r = await apiPost("/api/paperbot/enroll", { strategy_ids: ids }, ctx.signal);
        const errs = Object.entries(r.errors || {});
        clear(status, chip(int((r.added || []).length) + " enviada(s) al bot", "ok"), " ",
          el("span", { class: "muted small" }, r.nota || ""),
          errs.length ? el("div", { class: "err small" }, errs.map(([k, v]) => k + ": " + v).join(" · ")) : null);
        await load();
        if (onDone) await onDone();
      } catch (e) {
        send.disabled = false;
        clear(status, el("span", { class: "err" }, "No se pudo enviar: " + e.message));
      }
    });
    const toggle = el("button", { class: "btn", type: "button" }, showAll ? "Ver solo las mejores enviables" : "Ver todas (" + int(items.length) + ")");
    toggle.addEventListener("click", () => { showAll = !showAll; load(); });
    clear(box,
      el("p", { class: "small muted" }, "Estrategias del laboratorio que todavía no corren en el bot, ordenadas por Sharpe fuera de muestra. ",
        "Al enviarlas entran como ", el("b", {}, "seguimiento"), " (exploratorio: no deciden nada y al evaluarlas se corrige por comparaciones múltiples), ",
        "con los parámetros que eligió el laboratorio, desde su primera corrida (:02 UTC) y con 10.000 US$ ficticios. Simulado, sin plata real. ",
        "Un Sharpe alto en el backtest no garantiza nada: justamente para eso está el paper."),
      items.length ? table([{ label: "" }, { label: "Estrategia" }, { label: "Sharpe OOS", cls: "num" }, { label: "DSR", cls: "num" },
        { label: "Trades", cls: "num" }, { label: "Max DD", cls: "num" }, { label: "Veredicto" }], rows)
        : el("p", { class: "muted" }, "Todas las estrategias del laboratorio ya corren en el bot."),
      el("div", { class: "row", style: { justifyContent: "space-between", marginTop: "8px", gap: "8px", flexWrap: "wrap" } },
        toggle, el("span", { class: "row", style: { gap: "8px" } }, count, send)),
      status);
  }
  load();
  return card("Enviar estrategias del laboratorio al bot", "elegí y mandá a correr en el bot (seguimiento)", box);
}

function disclaimer(ov) {
  return el("div", { class: "note warn", role: "note" }, el("b", {}, "Simulado, sin plata real. "),
    ov && ov.disclaimer ? ov.disclaimer : "Paper trading con dinero ficticio; el backtest o el holdout del laboratorio pueden descartar estas estrategias.");
}

// ------------------------------------------------------------------ resumen

export function renderOverview(ov, href, sel) {
  const last = ov.last_run || {}, c = ov.counts || {};
  const stats = el("div", { class: "stats" },
    stat("Última corrida", el("span", { class: "chips" }, statusChip(last.status), ov.stale ? chip("atrasado", "warn", "más de 2 h sin corrida ok") : null),
      last.hour_ms ? el("span", {}, "hora ", when(last.hour_ms), last.reason ? " · " + last.reason : "") : "ninguna"),
    stat("Corridas", int(c.ok) + " ok", int(c.error) + " con error · " + int(c.skipped) + " omitidas", c.error ? "bad-t" : ""),
    stat("Huecos", int((ov.gaps || []).length), (ov.gaps || []).length ? "horas sin simular (no se rellenan)" : "ninguno"),
    stat("Paper desde", ov.started_ms ? when(ov.started_ms) : DASH, "10.000 US$ ficticios por estrategia, cada una desde su primera corrida"));
  const notes = (ov.notes || []).map((n) => el("div", { class: "note bad" }, n));
  const withLab = (ov.strategies || []).filter((s) => s.lab);
  const failing = withLab.filter((s) => !s.lab.pasa);
  const negative = withLab.filter((s) => isNum(s.lab.sharpe) && s.lab.sharpe < 0);
  if (failing.length) notes.push(el("div", { class: "note bad" }, el("b", {}, "Backtest del laboratorio: "),
    int(failing.length) + " de " + int(withLab.length) + " estrategias no pasan los criterios del laboratorio" +
    (negative.length ? " (" + int(negative.length) + " con Sharpe fuera de muestra negativo)" : "") +
    ". El paper sigue corriendo para compararlo con la realidad, no porque estén aprobadas."));

  const strategies = ov.strategies || [];
  const byRole = {};
  for (const s of strategies) (byRole[s.rol || "paper_previo"] ||= []).push(s);
  const sinDatos = ov.sin_datos || [];
  const sections = ROLE_ORDER.map((r) => {
    const list = byRole[r] || [];
    const missing = sinDatos.filter((x) => x.rol === r && x.nunca_corrio);
    const body = r === "principal" ? principalBlock(list, href, sel, ov) : r === "sltp" ? sltpTable(list, strategies, href, sel)
      : stratTable(list, href, sel, r === "seguimiento");
    return el("section", { class: "stack", "aria-label": ROLE_LABEL[r], "data-rol": r },
      card(ROLE_LABEL[r], ROLE_SUB[r], body,
        missing.length ? el("div", { class: "note warn" }, el("b", {}, "Sin datos todavía: "),
          missing.map((x) => x.strategy_id + " (" + (x.motivo || "sin motivo") + ")").join("; ")) : null,
        r === "paper_previo" ? el("p", { class: "small muted" }, "Capital ficticio 10.000 US$ por estrategia repartido en partes iguales entre sus símbolos, sin reinversión. ",
          "Costos 5 bps de comisión + 2 bps de slippage por lado (igual que el laboratorio); funding real liquidado de Binance.") : null));
  });

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
      ...sections,
      el("div", { class: "grid two" },
        card("Corridas recientes", "una por hora, a los :02 UTC", runTable),
        card("Huecos", "horas que el bot no simuló", gapTable,
          el("p", { class: "small muted" }, "Si la VM estuvo apagada o la corrida falló, esas horas NO se simulan después: al volver, las posiciones se llevan al objetivo de la regla y el hueco queda registrado.")))),
  ];
}

// ------------------------------------------------------------------ grupos por rol

const sharpeText = (v) => isNum(v) ? num(v, 2) : DASH;

/** Tabla de estrategias (seguimiento: con Sharpe diario y la marca de exploratorio). */
function stratTable(list, href, sel, seguimiento) {
  const cols = [{ label: "Estrategia" }, { label: "Equity", cls: "n" }, { label: "Retorno", cls: "n" },
    { label: "Canasta", cls: "n", title: "buy & hold igual-ponderado de los mismos símbolos desde el inicio del paper" },
    { label: "Sharpe", cls: "n", title: "Sharpe anualizado de los retornos diarios (00:00 UTC) desde su inicio en paper" },
    { label: "Realizado", cls: "n" }, { label: "No realizado", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Costos", cls: "n", title: "comisiones + slippage" },
    { label: "Exposición", cls: "n", title: "nocional bruto / capital" }, { label: "Órdenes", cls: "n" }, { label: "Trades cerrados", cls: "n" },
    { label: "Salidas", title: "stop / objetivo / tiempo de la regla y cierres por motivo (desde que se registra el motivo)" }, { label: "Posición actual" },
    { label: "Backtest (laboratorio)", title: "veredicto del walk-forward OOS publicado (lab_summary.json)" }];
  return table(cols, list.map((s) => {
    const sh = s.sharpe || {};
    return [
      el("a", { href: href("paperbot", [s.strategy_id]), "aria-current": s.strategy_id === sel ? "true" : null }, el("b", {}, name(s.strategy_id)),
        el("div", { class: "small muted mono" }, s.strategy_id),
        s.started_ms ? el("div", { class: "small muted" }, "desde ", when(s.started_ms)) : null),
      money(s.equity), sFrac(s.return), sFrac(s.basket_return),
      el("span", {}, el("b", { class: isNum(sh.valor) ? (sh.valor >= 0 ? "pos" : "neg") : "" }, sharpeText(sh.valor)),
        el("div", { class: "small muted" }, int(sh.dias || 0) + " d · canasta " + sharpeText(sh.canasta)),
        seguimiento && sh.marca ? el("div", { class: "small" }, chip(sh.marca, "warn", "seguimiento exploratorio: no decide; al evaluar se corrige por comparaciones múltiples (Benjamini–Hochberg)")) : null),
      sMoney(s.realized), sMoney(s.unrealized), sMoney(s.funding),
      money((s.fees || 0) + (s.slippage || 0)), num(s.exposure, 2) + "×", int(s.n_orders),
      s.n_trades ? int(s.wins) + "/" + int(s.n_trades) + " ganadores" : "0",
      { v: el("div", { style: { minWidth: "160px" } }, exitChips(s.exits, { small: true }), countsLine(s.exit_counts)), cls: "wrap" },
      el("span", { class: "chips" }, ...(s.positions || []).map((p) => weightChip(p.symbol, p.weight)).filter(Boolean),
        (s.positions || []).some((p) => Math.abs(p.weight) > 1e-9) ? null : el("span", { class: "muted" }, "plana")), labCell(s.lab)];
  }), { empty: "Ninguna estrategia con corrida ok en este grupo.", label: "Estrategias", scroll: true });
}

const pct = (v, d = 1) => isNum(v) ? num(v * 100, d) + " %" : DASH;

/** -v2 con SL/TP: su retorno al lado del de la v1 (mismas entradas) y sus operaciones abiertas / cerradas. */
function sltpTable(list, all, href, sel) {
  const byId = Object.fromEntries((all || []).map((s) => [s.strategy_id, s]));
  const cols = [{ label: "Estrategia" }, { label: "Retorno v2", cls: "n" },
    { label: "Retorno v1", cls: "n", title: "la misma estrategia sin SL/TP obligatorio, desde su propio inicio (no es el mismo período)" },
    { label: "Canasta", cls: "n" }, { label: "Equity", cls: "n" },
    { label: "Abiertas", cls: "n", title: "operaciones abiertas, cada una con su stop y objetivo" },
    { label: "Cerradas", title: "por stop / por objetivo" }, { label: "Más cerca del stop", title: "operación abierta con menos margen hasta su stop (desde la marca)" },
    { label: "Posición actual" }];
  return el("div", { class: "stack" }, table(cols, list.map((s) => {
    const x = s.sltp || {}, m = x.por_motivo || {}, v1 = byId[(s.exits || {}).base_v1];
    const near = (x.abiertas || []).filter((t) => isNum(t.a_stop)).sort((a, b) => a.a_stop - b.a_stop)[0];
    return [
      el("a", { href: href("paperbot", [s.strategy_id]), "aria-current": s.strategy_id === sel ? "true" : null }, el("b", {}, name(s.strategy_id)),
        el("div", { class: "small muted mono" }, s.strategy_id), s.started_ms ? el("div", { class: "small muted" }, "desde ", when(s.started_ms)) : null),
      sFrac(s.return), v1 ? el("span", {}, sFrac(v1.return), el("div", { class: "small muted" }, "desde ", when(v1.started_ms))) : DASH,
      sFrac(s.basket_return), money(s.equity), int(x.n_abiertas || 0),
      el("span", {}, chip("stop " + int(m.stop || 0), m.stop ? "bad" : ""), " ", chip("objetivo " + int(m.objetivo || 0), m.objetivo ? "ok" : "")),
      near ? el("span", {}, el("b", {}, toSym(near.symbol).split("-")[0]), " a " + pct(near.a_stop) + " del stop") : DASH,
      el("span", { class: "chips" }, ...(s.positions || []).map((p) => weightChip(p.symbol, p.weight)).filter(Boolean),
        (s.positions || []).some((p) => Math.abs(p.weight) > 1e-9) ? null : el("span", { class: "muted" }, "plana"))];
  }), { empty: "Ninguna -v2 con corrida ok todavía.", label: "Estrategias con SL/TP", scroll: true }),
  el("p", { class: "small muted" }, "Arrancan sin posiciones el 2026-10-26 y solo toman entradas nuevas de la v1 (no copian las posiciones que la v1 ya tenía). ",
    "Los stops y objetivos se chequean en velas 1m también durante un hueco del bot (como una orden puesta en el exchange); las entradas no."));
}

/** Operaciones abiertas y cerradas de una -v2 (detalle). */
export function sltpBlock(x) {
  if (!x) return null;
  const sideEl = (t) => el("span", { class: t.side > 0 ? "buy" : "sell" }, t.side > 0 ? "▲ long" : "▼ short");
    const open = table([{ label: "Símbolo" }, { label: "Componente" }, { label: "Lado" }, { label: "Entrada" }, { label: "Precio entrada", cls: "n" },
    { label: "Stop", cls: "n" }, { label: "Objetivo", cls: "n" }, { label: "Liquidación", cls: "n", title: "margen aislado, mmr supuesto 2 %; el stop queda siempre a ≤ 50 % de esta distancia" },
    { label: "Tamaño", cls: "n", title: "fracción del capital del símbolo (= apalancamiento)" }, { label: "Marca", cls: "n" }, { label: "Va", cls: "n" },
    { label: "Falta al stop", cls: "n" }, { label: "Falta al objetivo", cls: "n" }],
  (x.abiertas || []).map((t) => [el("b", {}, toSym(t.symbol)), el("span", { class: "small mono" }, t.slot), sideEl(t), when(t.entry_ms), price(t.ref_price),
    el("span", {}, price(t.sl), el("div", { class: "small muted" }, "−" + pct(t.sl_frac) + " · ATR " + t.atr_tf)),
    el("span", {}, price(t.tp), el("div", { class: "small muted" }, "+" + pct(t.tp_frac))),
    t.liq_price ? el("span", {}, price(t.liq_price), el("div", { class: "small muted" }, pct(t.liq_frac, 0) + " de la entrada")) : el("span", { class: "muted", title: "long a ≤ 1x: no se liquida" }, "no aplica"),
    num(t.leverage, 2) + "×" + (t.weight < 1 ? " · " + num(t.weight, 3) : ""), price(t.mark), sFrac(t.ret_marca), pct(t.a_stop), pct(t.a_objetivo)]),
  { empty: "Ninguna operación abierta.", label: "Operaciones abiertas con SL/TP", scroll: true });
  const closed = table([{ label: "Símbolo" }, { label: "Componente" }, { label: "Lado" }, { label: "Entrada" }, { label: "Salida" }, { label: "Precio entrada", cls: "n" },
    { label: "Nivel tocado", cls: "n" }, { label: "Motivo" }, { label: "En el nivel", cls: "n", title: "retorno de precio hasta el nivel, antes de costos; el fill real es el open de la vela siguiente" }],
  (x.cerradas || []).map((t) => [el("b", {}, toSym(t.symbol)), el("span", { class: "small mono" }, t.slot), sideEl(t), when(t.entry_ms), when(t.exit_ms), price(t.ref_price),
    price(t.nivel_tocado), chip(t.motivo === "stop" ? "stop" : "objetivo", t.motivo === "stop" ? "bad" : "ok"), sFrac(t.ret_nivel)]),
  { empty: "Ninguna cerrada todavía.", label: "Operaciones cerradas con SL/TP", scroll: true });
  const m = x.por_motivo || {}, n = (m.stop || 0) + (m.objetivo || 0);
  return el("div", { class: "stack" },
    el("div", { class: "note info" }, el("b", {}, "SL/TP obligatorio. "), x.nota || ""),
    el("div", { class: "chips" }, chip(int(x.n_abiertas) + " abiertas", "info"), chip("stop " + int(m.stop || 0), m.stop ? "bad" : ""), chip("objetivo " + int(m.objetivo || 0), m.objetivo ? "ok" : ""),
      n ? el("span", { class: "small" }, rateText(m.objetivo || 0, n, x.objetivo_ci95) + " llegaron al objetivo (el mínimo para no perder con 1:2 es 33 %)") : null),
    n && n < 30 ? el("div", { class: "note" }, el("b", {}, "Muestra chica: "), int(n) + " operaciones cerradas no alcanzan para sacar conclusiones.") : null,
    el("div", {}, el("h3", {}, "Abiertas"), open), el("div", {}, el("h3", {}, "Cerradas"), closed));
}

const VERDICT_CHECKS = { sharpe_positivo: "Sharpe > 0", le_gana_a_la_canasta: "Sharpe > referencia", p_menor_igual_005: "p ≤ 0,05" };
function verdictLine(label, v) {
  if (!v) return null;
  const failed = Object.entries(v.checks || {}).filter(([, ok]) => !ok).map(([k]) => VERDICT_CHECKS[k] || k);
  return el("div", {}, chip(label + ": " + (v.pasa ? "pasa" : "no pasa"), v.pasa ? "ok" : "bad"),
    " Sharpe " + sharpeText(v.sharpe) + " vs referencia " + sharpeText(v.sharpe_referencia) + " · " + int(v.dias) + " días" +
    (v.p !== undefined ? " · p " + (isNum(v.p) ? num(v.p, 3) : DASH) : "") + (failed.length ? " · falla: " + failed.join(", ") : "") + ".");
}

/** Criterio pre-registrado: días desde el inicio, hitos de 3 y 6 meses y el veredicto desde los 3 meses. */
export function criterioBlock(c) {
  if (!c) return null;
  const cur = c.en_curso || {};
  return el("div", { class: "stack" },
    el("div", { class: "small" }, el("b", {}, "Criterio pre-registrado: "), c.texto),
    el("div", { class: "chips" }, chip(int(c.dias) + " días desde el inicio", "info", "inicio " + String(c.inicio_at || "").slice(0, 10)),
      chip(c.estado || "en curso", /pasa$/.test(c.estado || "") && !/no pasa/.test(c.estado || "") ? "ok" : /no pasa/.test(c.estado || "") ? "bad" : ""),
      el("span", { class: "small muted" }, "3 meses: " + String(c.hito_3m_at || "").slice(0, 10) + " · 6 meses: " + String(c.hito_6m_at || "").slice(0, 10))),
    c.veredicto_3m ? null : el("div", { class: "small muted" }, "En curso (informativo, todavía no cuenta): Sharpe " + sharpeText(cur.sharpe) +
      " vs referencia " + sharpeText(cur.sharpe_referencia) + " con " + int(cur.dias || 0) + " días. El veredicto aparece a los 3 meses."),
    verdictLine("3 meses", c.veredicto_3m), verdictLine("6 meses", c.veredicto_6m));
}

/** Criterios de parada pre-registrados (bootstrap del drawdown del backtest): drawdown y duración bajo el agua
 * desde el inicio contra P95 (alerta: tamaño a la mitad) y P99 (parar) del horizonte transcurrido. */
export function paradaBlock(p) {
  if (!p) return null;
  if (!p.estado) return el("div", { class: "small note warn" }, p.error || "criterios de parada no disponibles");
  const KIND = { ok: "ok", alerta: "warn", parar: "bad" };
  const pc = (x) => isNum(x) ? num(x * 100, 1) + " %" : DASH;
  const dd = p.drawdown || {}, du = p.duracion || {};
  return el("div", { class: "stack" },
    el("div", { class: "chips" }, el("b", { class: "small" }, "Parada pre-registrada: "),
      chip(p.label || p.estado, KIND[p.estado] || "", "semáforo ok / alerta / parar (hash " + (p.hash || "?") + ", " + (p.fecha || "") + ")")),
    el("div", { class: "small" }, chip("DD " + (dd.nivel || ""), KIND[dd.nivel] || ""), " drawdown " + pc(dd.valor) + " · P95 " + pc(dd.p95) + " · P99 " + pc(dd.p99)),
    el("div", { class: "small" }, chip("bajo el agua " + (du.nivel || ""), KIND[du.nivel] || ""),
      " " + int(du.valor || 0) + " días (ahora " + int(du.actual || 0) + ") · P95 " + num(du.p95, 0) + " · P99 " + num(du.p99, 0)),
    el("div", { class: "small muted" }, "Umbrales del horizonte transcurrido (" + int(p.dias || 0) + " días, interpolados). " + (p.nota || "")));
}

/** Balance de fuerzas por símbolo: votos long vs short de las componentes y la posición neta resultante. */
export function votesBlock(votos) {
  if (!votos) return null;
  const rows = Object.entries(votos).map(([s, v]) => {
    const n = v.componentes || 12;
    const w = isNum(v.neta) ? v.neta : 0;
    const bar = el("span", { class: "vbar", role: "img", "aria-label": "posición neta " + num(w, 2),
      style: { display: "inline-block", position: "relative", width: "90px", height: "8px", background: "var(--line, #8883)", borderRadius: "4px", verticalAlign: "middle" } },
      el("span", { style: { position: "absolute", top: "0", height: "8px", borderRadius: "4px", left: (w >= 0 ? 50 : 50 + w * 50) + "%",
        width: Math.abs(w) * 50 + "%", background: w >= 0 ? "var(--pos, #2a9d5c)" : "var(--neg, #d64545)" } }));
    return el("div", { class: "small" }, el("b", {}, toSym(s).split("-")[0] + " "), int(v.long) + " long · " + int(v.short) + " short",
      el("span", { class: "muted" }, " de " + int(n) + " "), bar, " neta " + (w > 0 ? "+" : "") + num(w, 2));
  });
  return el("div", {}, el("div", { class: "small" }, el("b", {}, "Balance de fuerzas"), el("span", { class: "muted" }, " (última corrida)")), ...rows);
}

function principalBlock(list, href, sel, ov) {
  if (!list.length) return el("p", { class: "empty" }, "Las hipótesis principales todavía no tienen una corrida ok.");
  const order = ["l6-familia-ruptura-long-v1", "l6-familia-ruptura-short-v1", "l6-familia-ruptura-neta-v1"];
  const sorted = [...list].sort((a, b) => (order.indexOf(a.strategy_id) + 99) % 99 - (order.indexOf(b.strategy_id) + 99) % 99);
  const cards = sorted.map((s) => el("div", { class: "card", style: { padding: "12px" } },
    el("a", { href: href("paperbot", [s.strategy_id]), "aria-current": s.strategy_id === sel ? "true" : null }, el("b", {}, name(s.strategy_id))),
    el("div", { class: "small muted mono" }, s.strategy_id + (s.spec_hash ? " · spec " + s.spec_hash : "")),
    el("div", { class: "small" }, "Equity " + money(s.equity) + " · retorno ", sFrac(s.return), " · canasta ", sFrac(s.basket_return)),
    criterioBlock(s.criterio), paradaBlock(s.parada), votesBlock(s.votos)));
  return el("div", { class: "stack" },
    el("div", { class: "grid three", style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "12px" } }, ...cards),
    el("div", { class: "note info" }, el("b", {}, "Lectura conjunta: "), ov.lectura_conjunta || DASH));
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
    { label: "Precio salida", cls: "n" }, { label: "Precio", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Costos", cls: "n" }, { label: "Neto", cls: "n" },
    { label: "Motivo", title: "por qué cerró: stop, objetivo, tiempo, señal o rebalanceo (— = anterior al registro de motivos)" }],
  trades.map((t) => [el("b", {}, toSym(t.symbol)), el("span", { class: t.direction === "long" ? "buy" : "sell" }, t.direction === "long" ? "▲ long" : "▼ short"),
    when(t.entry_ms), when(t.exit_ms), price(t.entry_price), price(t.exit_price), sMoney(t.pnl_price), sMoney(t.funding),
    money((t.fees || 0) + (t.slippage || 0)), sMoney(t.pnl_net), motivoChip(t.motivo)]),
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

  return card(name(d.strategy_id), el("span", { class: "chips" }, chip("simulado", "warn"), labChip(d.lab), holdoutChip(d.lab), d.spec_hash ? chip("spec " + d.spec_hash, "", "hash de la regla congelada") : null),
    el("div", { class: "stack" },
      d.rol ? el("div", { class: "chips" }, chip(ROLE_LABEL[d.rol] || d.rol, d.rol === "principal" ? "info" : ""),
        d.rol === "seguimiento" && d.sharpe && d.sharpe.marca ? chip(d.sharpe.marca, "warn") : null) : null,
      d.criterio ? el("div", { class: "note info" }, criterioBlock(d.criterio)) : null,
      d.parada ? el("div", { class: "note" }, paradaBlock(d.parada)) : null,
      d.votos ? votesBlock(d.votos) : null,
      labBlock(d.lab),
      stats,
      trades.length && trades.length < 30 ? el("div", { class: "note" }, el("b", {}, "Muestra chica: "), int(trades.length) + " trades cerrados no alcanzan para sacar conclusiones.") : null,
      kv([["Estrategia", el("code", {}, d.strategy_id)], ["Parámetros de la grilla", el("code", {}, JSON.stringify(d.params || {}))],
        ["Origen de los parámetros", d.origen || DASH], ["Universo", (d.universe || []).map(toSym).join(", ")],
        ["En paper desde", d.started_ms ? el("span", {}, when(d.started_ms), " · " + int(d.hours) + " h con corrida ok") : DASH], ["Estado a la hora", when(d.hour_ms)]]),
      el("div", { class: "grid two" },
        el("div", {}, el("h3", {}, "Equity horaria vs canasta"), chart),
        el("div", {}, el("h3", {}, "De dónde sale el PnL"), bars,
          el("p", { class: "small muted" }, "Precio = realizado + no realizado (precios crudos); el slippage va aparte, así la suma es exacta."))),
      el("div", {}, el("h3", {}, "Posiciones actuales"), posTable),
      el("div", {}, el("h3", {}, "Descomposición por símbolo"), bySym),
      el("div", { class: "grid two" },
        el("div", {}, el("h3", {}, "Cómo sale"), exitChips(d.exits), exitRows(d.exits)),
        el("div", {}, el("h3", {}, "Cierres por motivo"), reasonBars(d.exit_counts),
          el("p", { class: "small muted" }, "El motivo se registra desde el 2026-10-19 (tabla aditiva exit_reasons); los trades anteriores figuran como «sin registro». ",
            "Re-sincronización / inicio / hueco = la orden la pidió el bot para volver al objetivo de la regla, no una salida de la regla."))),
      d.sltp ? el("div", {}, el("h3", {}, "Operaciones con stop y objetivo"), sltpBlock(d.sltp)) : null,
      el("div", {}, el("h3", {}, "Trades cerrados"), trTable),
      el("div", {}, el("h3", {}, "Órdenes simuladas"), orTable,
        el("p", { class: "small muted" }, "Fill al open de la vela 1m siguiente a la decisión (convención del laboratorio). ",
          "“Re-sincronización”, “inicio” y “después de hueco” son órdenes que llevan la posición al objetivo de la regla fuera de una señal: se marcan para separarlas en la comparación con el backtest.")),
      el("div", {}, el("h3", {}, "Funding"), fTable)));
}
