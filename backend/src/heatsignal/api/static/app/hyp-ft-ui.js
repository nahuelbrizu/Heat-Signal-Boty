// Hipotesis de 1h ejecutadas en el Freqtrade de hipotesis (DRY-RUN, segunda instancia, 127.0.0.1:8082).
// /api/paper/hypotheses: envios del puente (heatsignal.paper.ft_bridge), estado y PnL de Freqtrade, y la comparacion
// con la resolucion de la hipotesis (precio de salida, PnL) para ver la distancia simulacion-"ejecucion".
// Todo simulado: se avisa arriba de todo. Los textos que vienen de datos se insertan como nodos de texto (el()).
import {
  el, clear, api, errorBox, when, int, num, price, dur, card, table, chip, stat, isNum, dirSpan, rateText, ciBar, sym as toSym, DASH,
} from "./core.js";

const STATUS = {
  sent: ["ok", "enviada"], skipped_pair_busy: ["warn", "omitida: par ocupado"], skipped_late: ["warn", "omitida: tarde"],
  skipped_unsupported: ["warn", "omitida: par no soportado"], error_reintenta: ["bad", "error · se reintenta"], error: ["bad", "error · no se reintenta"],
};
const HYP = { pending: ["info", "pendiente"], resolved: ["ok", "resuelta"], expired_no_data: ["warn", "sin dato de precio"] };

function money(v, signed = false) {
  if (!isNum(v)) return DASH;
  const r = Math.abs(v) < 0.005 ? 0 : v;
  return (signed ? (r > 0 ? "+" : r < 0 ? "−" : "") : r < 0 ? "−" : "") + "US$ " + num(Math.abs(r), 2, 2);
}
const tone = (v, eps = 0.005) => (!isNum(v) || Math.abs(v) < eps ? "" : v > 0 ? "pos" : "neg");
const sMoney = (v) => isNum(v) ? el("span", { class: tone(v) }, money(v, true)) : DASH;
const pp = (v, d = 2) => isNum(v) ? el("span", { class: tone(v) }, (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), d, d) + " %") : DASH;
const ppDiff = (v) => isNum(v) ? el("span", { class: tone(v) }, (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), 2, 2) + " pp") : DASH;
const bps = (v) => isNum(v) ? el("span", { class: tone(v, 0.05) }, (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), 1) + " bps") : DASH;
const secs = (v) => isNum(v) ? (v < 0 ? "−" : "") + dur(Math.abs(v)) : DASH;
const stChip = (s) => { const [k, t] = STATUS[s] || ["", s || DASH]; return chip(t, k); };
const pairName = (s) => s ? toSym(s) : DASH;

function disclaimer(d) {
  return el("div", { class: "note warn" }, el("b", {}, "SIMULADO — sin plata real. "), d.disclaimer || "Freqtrade en dry-run.");
}

function verdict(o) {
  const h = o.hyp || {};
  if (h.status !== "resolved") return chip(h.status === "expired_no_data" ? "sin dato" : "pendiente", h.status === "expired_no_data" ? "warn" : "info");
  return h.win ? chip("✓ acertó", "ok") : chip("✗ falló", "bad");
}

function ftState(o) {
  const f = o.ft;
  if (!f) return o.status === "sent" ? el("span", { class: "muted" }, "sin trade en la base") : DASH;
  if (f.is_open) return chip("abierta", "info");
  return el("span", {}, chip("cerrada", ""), f.exit_reason === "stop_loss" ? chip("stop de seguridad", "warn", "Stop de -20 % (no es parte de la hipótesis)") :
    f.exit_reason && f.exit_reason !== "hyp_due" ? chip(f.exit_reason, "warn") : null);
}

/** Tabla de operaciones de una estrategia (detalle). */
export function renderDetail(s) {
  const ops = (s && s.ops) || [];
  return card((s && s.name) || "Estrategia", int(ops.length) + " hipótesis mandadas al puente",
    table([
      { label: "Emitida" }, { label: "Par" }, { label: "Dir." }, { label: "Vence" }, { label: "Envío" }, { label: "Freqtrade" },
      { label: "Entrada hip.", cls: "n", title: "precio de referencia de la hipótesis (mark al emitir)" },
      { label: "Entrada FT", cls: "n", title: "precio de entrada del dry-run" },
      { label: "Efecto entrada", cls: "n", title: "diferencia de precio de entrada con el signo de la dirección: + = a favor de la operación" },
      { label: "Salida hip.", cls: "n", title: "precio de salida de la resolución (cierre de la vela de 1h, mark)" },
      { label: "Salida FT", cls: "n" },
      { label: "Efecto salida", cls: "n", title: "+ = a favor de la operación" },
      { label: "PnL hip.", cls: "n", title: "neto con 10 bps ida y vuelta (resolver)" },
      { label: "PnL FT", cls: "n", title: "close_profit de Freqtrade: neto de comisiones y funding" },
      { label: "Diferencia", cls: "n", title: "PnL FT − PnL hip., en puntos porcentuales" },
      { label: "PnL FT US$", cls: "n" }, { label: "Resultado hip." }, { label: "Demoras", cls: "n", title: "entrada tras la emisión · salida tras el vencimiento" },
      { label: "Detalle" },
    ], ops.map((o) => {
      const f = o.ft || {}, h = o.hyp || {}, c = o.cmp || {};
      return [when(o.created_ms), el("b", {}, pairName(o.symbol)), dirSpan(o.direction), when(o.due_ms), stChip(o.status), ftState(o),
        price(h.entry_ref), price(f.open_rate), bps(c.entry_effect_bps), price(h.exit_price),
        f.is_open ? el("span", { class: "muted", title: "precio actual" }, price(f.current_rate)) : price(f.close_rate), bps(c.exit_effect_bps),
        pp(h.pnl_pct), f.is_open ? el("span", { class: "muted", title: "no realizado" }, pp(f.unrealized_pct)) : pp(f.pnl_pct), ppDiff(c.pnl_diff_pp),
        f.is_open ? el("span", { class: "muted" }, sMoney(f.unrealized_abs)) : sMoney(f.pnl_abs), verdict(o),
        secs(c.entry_delay_s) + " · " + secs(c.exit_delay_s),
        el("span", { class: "small" }, o.trade_mismatch || o.detail || (o.errors ? o.errors + " error(es): " + (o.last_error || "") : "") || DASH)];
    }), { empty: "Todavía no se mandó ninguna hipótesis de esta estrategia.", label: "Operaciones de " + ((s && s.name) || "la estrategia"), scroll: true }));
}

/** Resumen + una tabla por estrategia. Devuelve una lista de nodos. */
export function renderOverview(d, _href) {
  const t = d.totals || {}, b = d.bridge || {}, last = b.last_run;
  const stats = el("div", { class: "stats" },
    stat("Hipótesis mandadas", int(t.hypotheses), int(t.sent) + " enviadas · " + int(t.skipped) + " omitidas · " + int(t.errors) + " con error"),
    stat("En Freqtrade", int(t.open) + " abiertas", int(t.closed) + " cerradas"),
    stat("PnL realizado (simulado)", sMoney(t.ft_pnl_abs), "stake fijo por operación, leverage 1"),
    stat("Último envío del puente", last ? when(last.ts_ms) : DASH,
      last ? el("span", {}, chip(last.status, last.status === "ok" || last.status === "idle" ? "ok" : "bad"), last.detail ? " " + last.detail : "") : "el puente todavía no corrió"),
  );
  const src = d.source || {};
  const notes = (d.notes || []).map((n) => el("div", { class: "note warn" }, n));
  const summary = card("Por estrategia", "Freqtrade vs resolución de la hipótesis",
    table([
      { label: "Estrategia" }, { label: "Hipótesis", cls: "n" }, { label: "Enviadas", cls: "n" }, { label: "Par ocupado", cls: "n" },
      { label: "Tarde / no soportada", cls: "n" }, { label: "Error", cls: "n" }, { label: "Abiertas", cls: "n" }, { label: "Cerradas", cls: "n" },
      { label: "Acierto FT" }, { label: "PnL FT", cls: "n" }, { label: "Mismo resultado", cls: "n", title: "operaciones cerradas donde Freqtrade ganó si y solo si la hipótesis acertó" },
      { label: "Dif. PnL media", cls: "n", title: "PnL FT − PnL hipótesis (pp)" }, { label: "Efecto entrada / salida", cls: "n" },
      { label: "Stops de seguridad", cls: "n" },
    ], (d.strategies || []).map((s) => [el("b", {}, s.name || s.strategy_id), int(s.hypotheses), int(s.sent), int(s.pair_busy),
      int(s.late) + " / " + int(s.unsupported), int(s.errors), int(s.open), int(s.closed),
      s.closed ? el("div", {}, rateText(s.ft_wins, s.closed, s.ft_ci95), el("div", { style: { maxWidth: "200px" } }, ciBar(s.ft_wins / s.closed, s.ft_ci95))) : DASH,
      sMoney(s.ft_pnl_abs), s.compared ? int(s.same_outcome) + " de " + int(s.compared) : DASH, ppDiff(s.mean_pnl_diff_pp),
      el("span", {}, bps(s.mean_entry_effect_bps), " / ", bps(s.mean_exit_effect_bps)), int(s.safety_stops)]),
    { empty: "No hay estrategias de 1h registradas.", label: "Resumen por estrategia" }),
    el("p", { class: "small muted" }, "Efectos en bps con el signo de la dirección (+ = a favor de la operación). La hipótesis entra al mark del momento de emisión y sale al cierre de la vela de 1h (mark), ",
      "con 10 bps de costo; Freqtrade entra cuando corre el puente (cada 5 min), sale ~1 min después del vencimiento a precio de libro, y cobra comisiones y funding reales del dry-run. ",
      "Si dos hipótesis caen en el mismo par a la vez, la segunda queda omitida (Freqtrade no abre dos trades por par)."));
  const per = (d.strategies || []).filter((s) => (s.ops || []).length).map(renderDetail);
  const foot = el("p", { class: "small muted" }, "Bases (solo lectura): ", el("code", {}, src.bridge_db || DASH), " · ", el("code", {}, src.ft_db || DASH),
    src.api ? (src.api.ok ? " · API del Freqtrade de hipótesis OK" : " · API: " + (src.api.error || "sin respuesta")) : "", ". Se refresca cada 30 s.");
  return [disclaimer(d), stats, ...notes, summary, ...per, foot];
}

/** Monta la seccion en un contenedor (lo usa la vista Paper). */
export async function mountHypFt(box, ctx) {
  async function load() {
    try {
      const d = await api("/api/paper/hypotheses", null, ctx.signal);
      clear(box, el("h2", {}, "Hipótesis de 1h en Freqtrade (dry-run)"), ...renderOverview(d, ctx.href));
    } catch (e) {
      const b = errorBox(e, "las hipótesis en Freqtrade");
      if (b) clear(box, el("h2", {}, "Hipótesis de 1h en Freqtrade (dry-run)"), b,
        e.status === 503 ? el("p", { class: "small muted" }, "Necesita heatsignal-freqtrade-hyp (segunda instancia, 127.0.0.1:8082) y heatsignal-ft-bridge.timer en la VM. ",
          "Es simulado: no opera con plata real.") : null);
    }
  }
  await load();
  return load;
}
