// Sesiones del dia (#/sesiones?day=YYYY-MM-DD&symbol=XRP-USDT): linea de tiempo Asia -> Europa -> EE.UU. -> cierre
// (horario local con DST), la vela de apertura de cada una sobre el grafico de 1h y el analisis acumulado del dia
// (por codigo y, si hubo, del grafo). Fuente: /api/sessions (diario append-only) + /api/htf/{symbol}.
import { el, clear, api, errorBox, when, card, pageHead, loading, chip, seg, kv, prose, price, fixed, frac, usd, signed, isNum, base, norm, sym as toSym, DASH } from "../core.js";
import { htfPanel, trendChip } from "../htf-ui.js";
import { css } from "../charts.js";

const SHORT = { asia: "Asia", europa: "Londres", eeuu: "NY", cierre: "Cierre" };
const CODE_STATUS = { ok: ["ok", "datos completos"], partial: ["warn", "datos parciales"], missing: ["bad", "sin datos"] };
const LLM_STATUS = { con_llm: ["ok", "con LLM"], sin_llm: ["", "sin LLM"], error: ["bad", "error del grafo"] };

function statusChips(s) {
  if (!s.triggered) return [chip("todavía no cerró la vela de apertura", "info")];
  if (!s.code) return [chip("sin analizar (proceso caído o fuera del catch-up)", "warn")];
  const [k, t] = CODE_STATUS[s.code.status] || ["", s.code.status];
  const out = [chip("código: " + t, k)];
  if (s.llm) { const [k2, t2] = LLM_STATUS[s.llm.status] || ["", s.llm.status]; out.push(chip(t2, k2)); }
  else out.push(chip("LLM pendiente", "info"));
  return out;
}

function symbolDetail(a) {
  if (!a) return el("p", { class: "muted small" }, "Sin análisis de esta moneda en la sesión.");
  const o = a.opening, w = a.own;
  return el("div", {},
    el("p", { class: "small" }, a.summary || ""),
    kv([
      o ? [a.role === "close" ? "Vela de cierre (23:00–00:00 UTC)" : "Vela de apertura (1h)", "O " + price(o.open) + " · H " + price(o.high) + " · L " + price(o.low) + " · C " + price(o.close) +
        " · " + o.minutes + "/60 min"] : ["Vela", "sin datos"],
      o ? ["Cuerpo / mechas", frac(o.body_frac, 0) + " del rango · mecha sup. " + price(o.upper_wick) + " · inf. " + price(o.lower_wick) + (isNum(o.range_atr) ? " · rango " + fixed(o.range_atr, 2) + " ATR1h" : "")] : null,
      o ? ["Volumen vs misma hora (20 d)", isNum(o.vol_ratio) ? fixed(o.vol_ratio, 2) + "× (" + o.vol_days + " días)" : DASH] : null,
      ["Desde la apertura UTC", signed(a.change_from_day_open_pct)],
      ["Tendencia del día", trendChip(a.daily_trend, a.trend_reason)],
      ["Máx / mín del día hasta ahí", price(a.day_high) + " / " + price(a.day_low)],
      a.role === "open" ? ["Rango de la sesión anterior" + (a.prev_session ? " (" + (SHORT[a.prev_session] || a.prev_session) + ")" : ""),
        isNum(a.prev_range_high) ? price(a.prev_range_low) + " – " + price(a.prev_range_high) + " · roto: " + (a.broke_prev_range || DASH) : DASH] : null,
      w ? ["Datos propios del día", "CVD " + usd(w.cvd_usd) + " · liq. longs " + usd(w.liq_long_usd) + " / shorts " + usd(w.liq_short_usd) +
        " · funding " + (isNum(w.funding_8h_mean) ? fixed(w.funding_8h_mean * 100, 4) + " %" : DASH) + " · OI " + (isNum(w.oi_change_pct) ? fixed(w.oi_change_pct, 2) + " %" : DASH)] : null,
      ["Banderas (código)", Object.entries(a.flags || {}).filter(([, v]) => v).map(([k]) => k).join(", ") || "ninguna"],
    ]),
    (a.data_gaps || []).length ? el("div", { class: "note warn small" }, el("b", {}, "Huecos: "), a.data_gaps.join("; ")) : null);
}

export async function mount(root, ctx) {
  const symbols = ctx.symbols();
  const q = ctx.route.query;
  let day = q.get("day") || "";
  let symbol = toSym(q.get("symbol") || symbols[0] || "");
  if (!symbols.some((s) => norm(s) === norm(symbol))) symbol = symbols[0] || "";
  const head = el("div"), timeline = el("div", {}, loading()), journalBox = el("div");
  const htf = symbol ? htfPanel(ctx, symbol, { title: "Velas de 1h de " + base(symbol) + " con las aperturas de sesión" }) : null;
  clear(root,
    pageHead("Sesiones del día", "Al cerrar la primera vela de 1h de cada sesión se analiza por código y se suma al diario del día; el grafo recibe el diario. Lenguaje histórico."),
    head, card("Línea de tiempo", "hora local del navegador · UTC entre paréntesis", timeline), htf ? htf.node : null,
    card("Análisis acumulado del día", "orden: Asia → Europa → EE.UU. → cierre", journalBox));
  let data = null;

  function drawHead() {
    const days = (data && data.days) || [];
    const opts = [["", "hoy"], ...days.slice(0, 7).map((d) => [d.day, d.day.slice(5)])];
    clear(head, el("div", { class: "row", style: { marginBottom: "12px" } },
      seg("Día UTC", opts, day, (v) => { day = v; ctx.replaceQuery({ day, symbol }); load(); }),
      seg("Moneda", symbols.map((s) => [s, base(s)]), symbol, (v) => ctx.navigate(ctx.href("sesiones", [], { day, symbol: v })))));
  }

  function render() {
    drawHead();
    const sess = data.sessions || [];
    clear(timeline, el("p", { class: "small muted" }, "Modo del grafo: ", el("b", {}, data.mode || DASH),
      data.mode === "sessions" ? " (corre al cerrar cada vela de apertura, todas las criptos juntas)" : " (intervalo fijo; el diario por código se guarda igual)",
      " · próxima: ", el("b", {}, data.next.label), " ", when(data.next.trigger_ms)),
    el("ol", { class: "timeline" }, sess.map((s) => el("li", {},
      el("div", { class: "row" }, el("b", {}, s.label), el("span", { class: "small muted" }, s.local_time + " " + s.tz + " → abre " + when(s.open_ms) + " (" + s.open_at.slice(11, 16) + " UTC), analiza " + when(s.trigger_ms)), ...statusChips(s))))),
    !data.journal_available ? el("div", { class: "note warn" }, "Diario no disponible en este proceso.") : null);

    if (htf) {
      const up = css("--s4");
      htf.chart.setExtraMarkers(sess.filter((s) => s.triggered).map((s) => ({ ms: s.open_ms, text: SHORT[s.name] || s.name, color: up, position: "aboveBar", shape: "square" })));
    }
    const blocks = sess.filter((s) => s.code || s.llm).map((s) => {
      const a = s.code && s.code.data && s.code.data.symbols ? s.code.data.symbols[symbol] : null;
      return el("article", { class: "box", style: { marginBottom: "12px", border: "1px solid var(--line)", borderRadius: "8px", padding: "10px 12px" } },
        el("div", { class: "row", style: { marginBottom: "6px" } }, el("h3", { style: { margin: 0 } }, s.label), ...statusChips(s)),
        symbolDetail(a),
        s.llm ? el("div", { style: { marginTop: "8px" } }, el("h4", { style: { margin: "4px 0" } }, s.llm.status === "con_llm" ? "Resumen del grafo (Redactor)" : "Grafo"),
          prose(s.llm.summary || ""), s.llm.run_id ? el("div", { class: "small muted" }, el("a", { href: ctx.href("ciclos", [s.llm.run_id]) }, s.llm.run_id)) : null) : null,
        s.code ? el("details", { class: "box", style: { marginTop: "8px" } }, el("summary", {}, "Resumen por código de todas las criptos"), prose(s.code.summary || "")) : null);
    });
    clear(journalBox, blocks.length ? el("div", {}, blocks) : el("p", { class: "empty" }, "Todavía no hay sesiones analizadas este día."));
  }

  async function load() {
    try {
      data = await api("/api/sessions", { day }, ctx.signal);
      render();
    } catch (e) { const b = errorBox(e, "las sesiones"); if (b) clear(timeline, b); }
  }

  await load();
  if (htf) { await htf.reload(true); htf.reloadHyps(); if (data) render(); }
  ctx.every(60000, () => { load(); if (htf) htf.reload(false); });
  let lastRun = null;
  return {
    refresh: () => { load(); if (htf) htf.reload(false); },
    onSummary: (s) => { const id = s.analysis && s.analysis.last_ran && s.analysis.last_ran.run_id; if (lastRun !== null && id !== lastRun) load(); lastRun = id || ""; },
    unmount: () => { if (htf) htf.dispose(); },
  };
}
