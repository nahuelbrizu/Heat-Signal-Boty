// Revisión semanal (#/semanal y #/semanal/2026-W42): cierre por código (sin LLM) de la semana UTC lunes→domingo,
// guardado append-only en weekly_reviews (paper/weekly.py, heatsignal-weekly.timer, lunes 00:30 UTC). Una tarjeta por
// fuente (bot paper, Freqtrade Donchian, Freqtrade de hipótesis, hipótesis del grafo, salud), flechas semana contra
// semana y avisos de muestra chica. Honestidad: lenguaje histórico, toda tasa con n e IC; una semana no prueba nada.
import {
  el, clear, api, errorBox, when, int, num, frac, card, table, pageHead, loading, chip, kv, stat, isNum, signedFrac,
  signedPct, rateText, ciBar, prose, sym as toSym, DASH,
} from "../core.js";
import { reasonBars, FT_REASON, MOTIVO } from "../exits-ui.js";

const SMALL = 30;
const SOURCES = { paperbot: "Bot paper propio", freqtrade: "Freqtrade Donchian", hyp_bridge: "Freqtrade de hipótesis",
  hypotheses: "Hipótesis del grafo", health: "Salud" };
const CYCLE = { ran: ["ok", "corrió"], warmup: ["info", "calentando"], error: ["bad", "error"], no_llm: ["warn", "sin LLM"],
  no_strategies: ["warn", "sin estrategias"] };
const LLM = { con_llm: ["ok", "con LLM"], sin_llm: ["", "sin LLM"], error: ["bad", "error"] };
const SKIP = { skipped_pair_busy: "par ocupado", skipped_late: "tarde", skipped_unsupported: "par no soportado" };

function money(v, signed = false) {
  if (!isNum(v)) return DASH;
  const r = Math.abs(v) < 0.005 ? 0 : v;
  return (signed ? (r > 0 ? "+" : r < 0 ? "−" : "") : r < 0 ? "−" : "") + "US$ " + num(Math.abs(r), 2, 2);
}
const sMoney = (v) => isNum(v) ? el("span", { class: Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg" }, money(v, true)) : DASH;
const sFrac = (v, d = 2) => isNum(v) ? el("span", { class: Math.abs(v) < 5e-5 ? "" : v > 0 ? "pos" : "neg" }, signedFrac(v, d)) : DASH;
const small = (b) => b && (b.small_sample || (isNum(b.n) && b.n < SMALL))
  ? chip("muestra chica", "warn", "menos de " + SMALL + " casos: no permite sacar conclusiones") : null;
const rateCell = (b) => !b || !b.n ? el("span", { class: "muted" }, "sin casos")
  : el("span", {}, rateText(b.wins, b.n, b.ci95), " ", small(b));
const day = (iso) => String(iso || "").slice(0, 10);
const range = (r) => day(r.week_start_at) + " a " + day(new Date(r.week_end_ms - 1).toISOString());

function fmtKind(v, kind) {
  if (!isNum(v)) return DASH;
  if (kind === "money") return money(v, true);
  if (kind === "int") return int(v);
  if (kind === "frac") return frac(v, 0);
  if (kind === "pct") return signedPct(v, 2);
  if (kind === "pp") return num(v * 100, 0) + " pp";
  return num(v, 2);
}

/** Flecha semana contra semana: ▲/▼/= con color si hay "mejor" definido (verde mejor, rojo peor). */
export function arrow(c) {
  if (!c || !c.dir) return el("span", { class: "muted", title: "sin semana anterior para comparar" }, DASH);
  const s = { up: "▲", down: "▼", flat: "=" }[c.dir];
  const cls = c.good === true ? "pos" : c.good === false ? "neg" : "";
  const word = { up: "subió", down: "bajó", flat: "igual" }[c.dir];
  return el("span", { class: cls + " nowrap", title: "semana anterior: " + fmtKind(c.prev, c.kind), "aria-label": word + " contra la semana anterior" },
    s + " " + (c.dir === "flat" ? "" : fmtKind(c.kind === "money" || c.kind === "pct" ? c.delta : Math.abs(c.delta), c.kind === "frac" ? "pp" : c.kind)));
}

// ------------------------------------------------------------------ historial

export function renderOverview(data, href, sel) {
  const weeks = (data && data.weeks) || [];
  const notes = [el("div", { class: "note warn", role: "note" }, el("b", {}, "Una semana no prueba nada. "),
    "Son resultados históricos de 7 días con muestras chicas: sirven para ver que todo corre y para comparar semana contra semana, ",
    "no para aprobar ni descartar una estrategia. Todo es simulado (paper / dry-run), sin plata real.")];
  if (data && data.note) notes.push(el("div", { class: "note" }, data.note));
  const pick = (w, k) => (w.metrics || {})[k];
  const cell = (w, k) => { const m = pick(w, k); return m ? el("span", {}, fmtKind(m.value, m.kind), " ", arrow(m)) : DASH; };
  const t = table([{ label: "Semana" }, { label: "Estado" }, { label: "Freqtrade Donchian", cls: "n" }, { label: "Freqtrade hipótesis", cls: "n" },
    { label: "Hipótesis resueltas", cls: "n" }, { label: "Acierto", cls: "n" }, { label: "Ciclos con error", cls: "n" }, { label: "Generada" }],
  weeks.map((w) => [
    el("a", { href: href("semanal", [w.week]), "aria-current": w.week === sel ? "true" : null }, el("b", {}, w.week), el("div", { class: "small muted" }, range(w))),
    w.status === "ok" ? chip("completa", "ok") : chip("parcial", "warn", "fuentes sin datos: " + (w.failed_sources || []).map((s) => SOURCES[s] || s).join(", ")),
    cell(w, "freqtrade.pnl"), cell(w, "hyp_bridge.pnl"), cell(w, "hypotheses.resolved"), cell(w, "hypotheses.rate"),
    cell(w, "health.cycle_errors"), when(w.generated_ms)]),
  { empty: "Todavía no se cerró ninguna semana.", label: "Historial de semanas", scroll: true });
  return [...notes, card("Historial", "una fila por semana UTC (lunes a domingo); append-only", t)];
}

// ------------------------------------------------------------------ una semana

const exitsText = (c, labels) => c && Object.keys(c).length ? el("span", { class: "small" },
  Object.entries(c).map(([k, n]) => (labels[k] || k) + " " + int(n)).join(" · ")) : el("span", { class: "muted" }, DASH);

function unavailable(src) { return el("div", { class: "note warn" }, el("b", {}, "Sin datos: "), String(src && src.error || "fuente no disponible")); }

function paperbotCard(pb) {
  if (!pb || !pb.available) return card(SOURCES.paperbot, null, unavailable(pb));
  const sts = pb.strategies || [];
  const t = table([{ label: "Estrategia" }, { label: "PnL semana", cls: "n" }, { label: "Canasta", cls: "n", title: "buy & hold igual-ponderado de los mismos símbolos" },
    { label: "vs canasta", cls: "n" }, { label: "Precio", cls: "n" }, { label: "Funding", cls: "n" }, { label: "Costos", cls: "n", title: "comisiones + slippage" },
    { label: "Máx. DD", cls: "n", title: "drawdown máximo dentro de la semana (equity horaria)" }, { label: "Órdenes", cls: "n" },
    { label: "Trades cerrados" }, { label: "Salidas", title: "trades cerrados en la semana por motivo (stop, objetivo, tiempo, señal, rebalanceo)" },
    { label: "Acumulado", cls: "n" }, { label: "Acum. vs canasta", cls: "n" }],
  sts.map((s) => [el("span", {}, el("b", { class: "mono" }, s.strategy_id),
    s.started_in_week ? el("div", {}, chip("entró en la semana", "info", "semana parcial: arrancó con 10.000 US$ el " + day(s.started_at))) : null),
    sMoney(s.pnl_week), sMoney(s.basket_pnl_week), sMoney(s.vs_basket_week), sMoney(s.price_pnl_week), sMoney(s.funding_week), money(s.costs_week),
    isNum(s.max_dd_week) ? signedFrac(s.max_dd_week, 2) : DASH, int(s.orders_week), rateCell(s.trades_week), exitsText(s.exits_week, MOTIVO),
    sMoney(s.pnl_cum), sMoney(s.vs_basket_cum)]),
  { empty: "El bot no tenía estrategias con corridas en esta semana.", label: "Bot paper por estrategia", scroll: true });
  const r = pb.runs || {};
  const errs = pb.errors || [];
  return card(SOURCES.paperbot, el("span", { class: "chips" }, chip("simulado", "warn"), el("a", { href: "#/paperbot" }, "ver el bot →")),
    el("div", { class: "stack" },
      el("div", { class: "stats" },
        stat("Corridas ok", int(r.ok), int(r.ok_hours) + " de " + int(r.expected_hours) + " horas esperadas"),
        stat("Con error", int(r.error), r.error ? "no se operó en esas horas" : "ninguna", r.error ? "bad-t" : ""),
        stat("Horas sin corrida ok", int(r.missing_hours), (pb.gaps || []).length ? int(pb.gaps.length) + " hueco(s) registrados (no se rellenan)" : "sin huecos registrados", r.missing_hours ? "bad-t" : "")),
      t,
      el("p", { class: "small muted" }, "PnL en US$ sobre 10.000 ficticios por estrategia (sin reinversión). Precio + funding − costos = PnL. ",
        "Una estrategia que entró en la semana arranca ese día: su semana es parcial y su canasta también."),
      errs.length ? el("details", { class: "box" }, el("summary", {}, int(pb.n_errors) + " corridas con error"),
        table([{ label: "Hora" }, { label: "Motivo" }], errs.map((e) => [when(e.hour_ms), { v: e.reason, cls: "wrap" }]), { label: "Corridas con error" })) : null));
}

function freqtradeCard(ft) {
  if (!ft || !ft.available) return card(SOURCES.freqtrade, null, unavailable(ft));
  const w = ft.week || {}, c = ft.cum || {}, cmp = ft.compare;
  return card(SOURCES.freqtrade, el("span", { class: "chips" }, chip("dry-run", "warn"), el("a", { href: "#/paper" }, "ver el dry-run →")),
    el("div", { class: "stack" },
      el("div", { class: "stats" },
        stat("Cerradas en la semana", int(w.n), w.opened !== undefined ? int(w.opened) + " abiertas en la semana" : null),
        stat("PnL cerrado", sMoney(w.pnl), "retorno " + signedFrac(w.return, 2) + " sobre " + money(ft.wallet)),
        stat("Ganadoras", w.n ? frac(w.rate, 0) : DASH, w.n ? rateText(w.wins, w.n, w.ci95) : "sin cerradas"),
        stat("Acumulado cerrado", sMoney(c.pnl), int(c.n) + " cerradas · " + (c.n ? frac(c.rate, 0) + " ganadoras" : DASH)),
        stat("Abiertas al cierre", int(ft.open_at_end), "sin PnL no realizado")),
      small(w) ? el("div", { class: "note" }, el("b", {}, "Muestra chica: "), int(w.n) + " operaciones cerradas en la semana.") : null,
      kv([["Comisiones / funding (cerradas)", money(w.fees) + " / " + money(w.funding, true)],
        ["Paper vs backtest", !cmp ? el("span", { class: "muted" }, "sin comparación publicada (lab/paper_compare.py en la PC)")
          : cmp.error ? el("span", { class: "neg" }, cmp.error)
          : el("span", {}, "publicada " + (cmp.generated_at || DASH) + " · estado " + (cmp.status || DASH) +
            (cmp.window ? " · ventana " + [].concat(cmp.window).join(" a ") : "") + " · paper " + money(cmp.paper_total, true) + " vs simulado " + money(cmp.sim_total, true),
            cmp.published_in_week ? null : el("span", { class: "muted" }, " (no se publicó en esta semana)"))]]),
      ft.exits_week && Object.keys(ft.exits_week).length ? el("div", {}, el("h3", {}, "Cierres de la semana por motivo (exit_reason)"), reasonBars(ft.exits_week, FT_REASON)) : null,
      el("p", { class: "small muted" }, ft.unrealized_note || "")));
}

function hypBridgeCard(hb) {
  if (!hb || !hb.available) return card(SOURCES.hyp_bridge, null, unavailable(hb));
  const t = hb.totals || {};
  const rows = (hb.strategies || []).map((s) => [el("b", { class: "mono" }, s.name || s.strategy_id), int(s.emitted), int(s.sent),
    el("span", { title: Object.entries(s.skipped_by_reason || {}).filter(([, v]) => v).map(([k, v]) => (SKIP[k] || k) + ": " + v).join(" · ") || null }, int(s.skipped)),
    int(s.errors), int(s.closed), sMoney(s.ft_pnl), rateCell(s.ft), rateCell(s.hyp),
    s.compared ? int(s.same_outcome) + " de " + int(s.compared) : DASH, isNum(s.mean_pnl_diff_pp) ? signedPct(s.mean_pnl_diff_pp, 2).replace("%", "pp") : DASH,
    s.safety_stops ? chip(int(s.safety_stops) + " stop −20 %", "warn", "stop de seguridad del dry-run, no es parte de la hipótesis") : DASH]);
  return card(SOURCES.hyp_bridge, el("span", { class: "chips" }, chip("dry-run", "warn"), el("a", { href: "#/hipotesis" }, "ver hipótesis →")),
    el("div", { class: "stack" },
      el("div", { class: "stats" },
        stat("Hipótesis de 1h", int(t.emitted), int(t.sent) + " enviadas · " + int(t.skipped) + " omitidas · " + int(t.errors) + " con error"),
        stat("Cerradas en la semana", int(t.closed), "PnL " + money(t.ft_pnl, true)),
        stat("Ganadoras (Freqtrade)", t.ft && t.ft.n ? frac(t.ft.rate, 0) : DASH, t.ft && t.ft.n ? rateText(t.ft.wins, t.ft.n, t.ft.ci95) : "sin cerradas"),
        stat("Diferencia con la resolución", isNum(t.mean_pnl_diff_pp) ? signedPct(t.mean_pnl_diff_pp, 2).replace("%", "pp") : DASH,
          "PnL de Freqtrade − PnL de la hipótesis, en promedio")),
      table([{ label: "Estrategia" }, { label: "Emitidas", cls: "n" }, { label: "Enviadas", cls: "n" }, { label: "Omitidas", cls: "n" }, { label: "Error", cls: "n" },
        { label: "Cerradas", cls: "n" }, { label: "PnL", cls: "n" }, { label: "Ganadoras (Freqtrade)" }, { label: "Aciertos (hipótesis)" },
        { label: "Mismo resultado", cls: "n" }, { label: "Dif. media", cls: "n" }, { label: "Stops" }], rows,
      { empty: "Ninguna hipótesis de 1h en la semana.", label: "Freqtrade de hipótesis por estrategia", scroll: true }),
      hb.exits_week && Object.keys(hb.exits_week).length ? el("div", {}, el("h3", {}, "Cierres de la semana por motivo (exit_reason)"), reasonBars(hb.exits_week, FT_REASON),
        el("p", { class: "small muted" }, "hyp_due = venció la hipótesis (salida por tiempo, la regla); stop_loss = stop de seguridad −20 % (no es parte de la hipótesis).")) : null,
      (hb.notes || []).length ? el("div", {}, ...(hb.notes || []).map((n) => el("div", { class: "note" }, n))) : null));
}

function segTable(title, rows, key, labels) {
  return el("div", {}, el("h3", {}, title), table([{ label: labels.head }, { label: "Resueltas", cls: "n" }, { label: "Acierto" }, { label: "", title: "IC 95 %" },
    { label: "Alfa medio", cls: "n", title: "PnL − mantener (buy & hold) en la misma ventana" }],
  (rows || []).map((s) => [labels[s[key]] || s[key], int(s.n), rateCell(s), ciBar(s.rate, s.ci95), isNum(s.mean_alpha_pct) ? signedPct(s.mean_alpha_pct, 2) : DASH]),
  { empty: "Sin resueltas en la semana.", label: title }));
}

function hypothesesCard(hy) {
  if (!hy || !hy.available) return card(SOURCES.hypotheses, null, unavailable(hy));
  const rw = hy.resolved_week || {};
  const st = table([{ label: "Estrategia" }, { label: "Emitidas", cls: "n" }, { label: "Resueltas", cls: "n" }, { label: "Acierto en la semana" },
    { label: "Alfa medio", cls: "n" }, { label: "Sin dato de precio", cls: "n" }, { label: "Acumulado" }, { label: "Reporte" }],
  (hy.by_strategy || []).map((s) => [el("b", { class: "mono" }, s.name), int(s.emitted), int(s.n), rateCell(s),
    isNum(s.mean_alpha_pct) ? signedPct(s.mean_alpha_pct, 2) : DASH, int(s.expired_no_data), rateCell(s.cum),
    s.report ? (s.report.publishable ? chip("publicable", "ok") : chip("shadow", "info", (s.report.blockers || []).join(" · "))) : DASH]),
  { empty: "Ninguna hipótesis emitida ni resuelta en la semana.", label: "Hipótesis por estrategia", scroll: true });
  return card(SOURCES.hypotheses, el("a", { href: "#/hipotesis" }, "ver todas →"),
    el("div", { class: "stack" },
      el("div", { class: "stats" },
        stat("Emitidas", int(hy.emitted), int(hy.pending_at_end) + " seguían pendientes al cierre"),
        stat("Resueltas en la semana", int(rw.n), rw.n ? rateText(rw.wins, rw.n, rw.ci95) : "sin casos"),
        stat("Alfa medio vs mantener", isNum(rw.mean_alpha_pct) ? signedPct(rw.mean_alpha_pct, 2) : DASH,
          "PnL medio " + (isNum(rw.mean_pnl_pct) ? signedPct(rw.mean_pnl_pct, 2) : DASH) + " · mantener " + (isNum(rw.mean_benchmark_pnl_pct) ? signedPct(rw.mean_benchmark_pnl_pct, 2) : DASH)),
        stat("Acumulado", hy.cum && hy.cum.n ? frac(hy.cum.rate, 0) : DASH, hy.cum && hy.cum.n ? rateText(hy.cum.wins, hy.cum.n, hy.cum.ci95) : "sin casos")),
      rw.n ? el("div", {}, ciBar(rw.rate, rw.ci95), " ", small(rw)) : null,
      el("p", { class: "small" }, el("b", {}, "Salidas: "), "todas por tiempo (" + int((hy.exits_week || {}).tiempo || 0) + " en la semana; pre-registradas sin stop ni objetivo).",
        hy.excursion_week && hy.excursion_week.n ? el("span", { class: "muted" }, " MAE medio " + num(hy.excursion_week.mean_mae_atr, 2) + " ATR · MFE medio " +
          num(hy.excursion_week.mean_mfe_atr, 2) + " ATR (n " + int(hy.excursion_week.n) + "; descriptivo, no pre-registrado).") : null),
      st,
      el("div", { class: "grid wide" },
        segTable("Por tendencia del día", hy.by_daily_trend, "daily_trend", { head: "Tendencia", sin_registro: "sin registro", sin_datos: "sin datos" }),
        segTable("A favor / en contra", hy.by_alignment, "trend_alignment", { head: "Alineación", a_favor: "a favor", en_contra: "en contra", sin_registro: "sin registro", sin_datos: "sin datos" }),
        segTable("Por sesión", hy.by_session, "session", { head: "Sesión", eeuu: "EE.UU.", sin_registro: "sin registro" })),
      el("p", { class: "small muted" }, hy.reports_note || "")));
}

function healthCard(he) {
  if (!he || !he.available) return card(SOURCES.health, null, unavailable(he));
  const cyc = he.cycles || {};
  const b = he.bars_1m || {}, k = he.klines_1m || {};
  const cov = (r) => [el("b", {}, toSym(r.symbol)), r.exchange || el("span", { class: "muted" }, "ninguno"),
    el("span", {}, int(r.present) + " / " + int(r.expected), r.since_ms ? el("div", { class: "small muted" }, "grabando desde ", when(r.since_ms)) : null),
    el("span", { class: r.missing ? "neg" : "" }, int(r.missing)), int(r.longest_gap_min) + " min"];
  return card(SOURCES.health, null,
    el("div", { class: "stack" },
      el("div", { class: "stats" },
        stat("Ciclos del grafo", el("span", { class: "chips" }, ...Object.entries(cyc).map(([s, n]) => { const [kk, t] = CYCLE[s] || ["", s]; return chip(t + " " + n, kk); })),
          Object.keys(cyc).length ? null : "ninguno en la semana"),
        stat("Sesiones del grafo", el("span", { class: "chips" }, ...Object.entries(he.sessions_llm || {}).map(([s, n]) => { const [kk, t] = LLM[s] || ["", s]; return chip(t + " " + n, kk); })),
          he.sessions_llm && Object.keys(he.sessions_llm).length ? "diario de sesiones" : "sin sesiones con grafo"),
        stat("Llamadas al LLM", "no se cuentan", int((he.llm_calls || {}).cycles_reaching_llm) + " ciclos llegaron al grafo con LLM"),
        stat("bars_1m faltantes", isNum(b.missing) ? int(b.missing) : DASH, isNum(b.expected) ? "de " + int(b.expected) + " minutos esperados" : (b.error || DASH), b.missing ? "bad-t" : "")),
      el("div", { class: "note" }, el("b", {}, "Falta: "), (he.llm_calls || {}).note || ""),
      he.archive_note ? el("div", { class: "note" }, he.archive_note) : null,
      (he.cycle_errors || []).length ? el("details", { class: "box" }, el("summary", {}, "Errores de ciclos (agrupados)"),
        table([{ label: "Detalle" }, { label: "Veces", cls: "n" }], he.cycle_errors.map((e) => [{ v: e.detail, cls: "wrap" }, int(e.count)]), { label: "Errores de ciclos" })) : null,
      el("div", { class: "grid two" },
        el("div", {}, el("h3", {}, "Grabación propia (bars_1m)"), b.rows ? table([{ label: "Símbolo" }, { label: "Exchange" }, { label: "Minutos", cls: "n" }, { label: "Faltan", cls: "n" }, { label: "Hueco más largo", cls: "n" }],
          b.rows.map(cov), { label: "Cobertura bars_1m", scroll: true }) : unavailable(b), el("p", { class: "small muted" }, b.note || "")),
        el("div", {}, el("h3", {}, "Velas 1m de Binance (klines)"), k.rows ? table([{ label: "Símbolo" }, { label: "Minutos", cls: "n" }, { label: "Faltan", cls: "n" }, { label: "Hueco más largo", cls: "n" }],
          k.rows.map((r) => { const c = cov(r); c.splice(1, 1); return c; }), { label: "Cobertura klines 1m", scroll: true }) : unavailable(k), el("p", { class: "small muted" }, k.note || "")))));
}

function compareCard(rv) {
  const rows = Object.entries(rv.compare || {}).map(([, c]) => [c.label, fmtKind(c.value, c.kind), fmtKind(c.prev, c.kind), arrow(c)]);
  return card("Semana contra semana", rv.prev_week ? "contra " + rv.prev_week : "no hay semana anterior cerrada",
    table([{ label: "Métrica" }, { label: "Esta semana", cls: "n" }, { label: "Anterior", cls: "n" }, { label: "Cambio", cls: "n" }], rows,
      { empty: "Sin métricas.", label: "Comparación semana contra semana", scroll: true }),
    el("p", { class: "small muted" }, "Verde = mejor, rojo = peor (cuando la métrica tiene un sentido); sin color = solo cambia el volumen. Un cambio de una semana a otra no es una tendencia."));
}

export function renderDetail(rv) {
  const s = rv.summary || {};
  const failed = s.failed_sources || [];
  return el("div", { class: "stack" },
    card("Semana " + rv.week, el("span", { class: "chips" }, rv.status === "ok" ? chip("completa", "ok") : chip("parcial", "warn"),
      chip("por código, sin LLM", "", "el resumen lo arma el código; no depende del LLM")),
    kv([["Período (UTC)", range(rv)], ["Generada", when(rv.generated_ms)], ["Versión del cálculo", el("code", {}, rv.model_id)]]),
    failed.length ? el("div", { class: "note warn" }, el("b", {}, "Fuentes sin datos: "), failed.map((f) => SOURCES[f] || f).join(", "), ". El motivo está en cada tarjeta.") : null,
    el("details", { class: "box" }, el("summary", {}, "Resumen en texto (por código)"), prose(rv.text))),
    compareCard(rv),
    paperbotCard(s.paperbot), freqtradeCard(s.freqtrade), hypBridgeCard(s.hyp_bridge), hypothesesCard(s.hypotheses), healthCard(s.health));
}

// ------------------------------------------------------------------ montaje

export async function mount(root, ctx) {
  const head = pageHead("Revisión semanal", "Cierre por código de la semana UTC (lunes a domingo), los lunes 00:30 UTC. Guardada append-only; solo lectura.");
  const top = el("div", {}, loading());
  const detail = el("div", {});
  clear(root, head, top, detail);
  async function load() {
    try {
      const data = await api("/api/weekly", null, ctx.signal);
      const want = ctx.route.parts[0] || (data.latest && data.latest.week) || null;
      clear(top, ...renderOverview(data, ctx.href, want));
      if (!want) { clear(detail); return; }
      const rv = data.latest && data.latest.week === want ? data.latest : await api("/api/weekly/" + encodeURIComponent(want), null, ctx.signal);
      clear(detail, renderDetail(rv));
    } catch (e) {
      const b = errorBox(e, "la revisión semanal");
      if (b) clear(top, b);
    }
  }
  await load();
  ctx.every(300000, load);
  return { refresh: load };
}
