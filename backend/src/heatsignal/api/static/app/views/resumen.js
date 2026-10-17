// Resumen: estado de exchanges y grabacion, frescura, ultimo ciclo y proveedor LLM, conteos, track record global,
// laboratorio y alertas. Todo sale de /api/summary (se redibuja con cada poll de 5 s).
import {
  el, clear, int, dur, pct, frac, price, bytes, ago, when, chip, stat, card, kv, table, pageHead, statusChip, CYCLE_STATUS,
  OUTCOME, wilson, rateText, ciBar, providersText, loading, isNum, num,
} from "../core.js";
import { computeAlerts } from "../alerts.js";

export async function mount(root, ctx) {
  const render = (s) => {
    if (!s) { clear(root, pageHead("Resumen"), loading("Esperando el primer dato del proceso…")); return; }
    clear(root, view(s, ctx));
  };
  render(ctx.summary);
  return { onSummary: (s) => render(s) };
}

function view(s, ctx) {
  const now = s.now_ms;
  const alerts = computeAlerts(s);
  const c = s.counts || {};
  const h = c.hypotheses;
  const workers = s.workers || [];
  const up = workers.filter((w) => w.connected).length;
  const a = s.analysis || {};
  const lc = a.last_cycle || a.last_ran;  // tras un reinicio, el ultimo con razonamiento sale del historial
  const rec = s.recording || {};

  const ci = h ? wilson(h.wins, h.resolved) : null;
  const statsRow = el("div", { class: "stats" },
    stat("Exchanges con datos", up + " / " + workers.length, workers.map((w) => w.worker).join(" · ") || "sin workers", up < workers.length ? "bad-t" : ""),
    stat("Último ciclo del grafo", lc ? statusChip(CYCLE_STATUS, lc.status) : "—", lc ? ago(lc.ts_ms, now) + " · " + int(lc.n_hypotheses) + " hipótesis" + (a.last_cycle ? "" : " · antes del reinicio") : a.enabled ? "todavía no corrió" : "análisis apagado"),
    stat("Hipótesis", h ? int(h.total) : "—", h ? `${int(h.pending)} pendientes · ${int(h.resolved)} resueltas · ${int(h.expired_no_data)} sin datos` : "evaluación apagada"),
    stat("Track record global", h && h.resolved ? frac(h.wins / h.resolved, 0) : "—", h ? rateText(h.wins, h.resolved, ci) : "—"),
    stat("Laboratorio", s.lab && !s.lab.error ? s.lab.passing + " de " + s.lab.strategies + " pasan" : s.lab && s.lab.error ? "ilegible" : "sin publicar",
      s.lab && s.lab.generated_at ? "publicado " + new Date(s.lab.generated_at).toLocaleString("es-AR") : "python -m heatsignal.lab.publish --upload"),
    stat("Paper trading", s.paper && s.paper.available ? int(s.paper.open) + " abiertas" : s.paper && s.paper.error ? "no disponible" : "—",
      s.paper && s.paper.available ? int(s.paper.closed) + " cerradas · realizado " + signedUsd(s.paper.realized) : s.paper ? s.paper.error : "no configurado",
      s.paper && s.paper.available === false ? "bad-t" : ""),
    stat("Grabación histórica", recChip(rec.status), rec.enabled ? `${int(rec.rows && rec.rows.bars_1m)} barras 1m · ${bytes(rec.size_bytes)}` : "apagada"),
  );

  const alertsBox = card("Alertas", alerts.length ? alerts.length + " activas" : "",
    alerts.length
      ? el("div", { class: "alerts" }, alerts.map((x) => el("div", { class: "alert " + (x.level === "info" ? "" : x.level) },
          el("span", { class: "dot " + (x.level === "info" ? "" : x.level), "aria-hidden": "true" }),
          el("div", {}, el("span", { class: "sr-only" }, x.level === "bad" ? "Error: " : x.level === "warn" ? "Aviso: " : "Info: "), x.text,
            x.link ? el("span", {}, " ", el("a", { href: x.link }, "ver")) : null))))
      : el("div", { class: "alert ok" }, el("span", { class: "dot ok", "aria-hidden": "true" }), "Todo en orden: exchanges con datos, grabación al día y sin ciclos con error."));

  const exchanges = card("Exchanges", "WebSocket nativos · uptime desde el arranque",
    table([{ label: "Exchange" }, { label: "Estado" }, { label: "Último mensaje", cls: "n" }, { label: "Reconexiones", cls: "n" }, { label: "Uptime", cls: "n" }],
      workers.map((w) => [
        el("b", {}, w.worker),
        w.connected ? chip("conectado", "ok") : chip("sin datos", "bad"),
        isNum(w.last_message_age_s) ? dur(w.last_message_age_s) : "—",
        int(w.reconnects),
        isNum(w.uptime_ratio) ? pct(w.uptime_ratio * 100, 2) : "—",
      ]), { empty: "Sin workers registrados todavía.", label: "Exchanges" }));

  const graph = card("Grafo de análisis", a.graph_enabled ? "LLM: " + (providersText(a.llm_providers) || "—") : "",
    kv([
      ["Estado", !a.enabled ? chip("apagado", "warn") : a.graph_enabled ? chip("habilitado", "ok") : chip("sin LLM (falta key)", "warn")],
      ["Proveedor LLM", providersText(a.llm_providers) || "—"],
      ["Modelos en pausa", Object.keys(a.llm_cooldowns || {}).length
        ? el("div", {}, Object.entries(a.llm_cooldowns).map(([m, c]) => el("div", { class: "small" }, chip(m, "warn"), " ", c.motivo, " · vuelve en " + dur(c.segundos))))
        : chip("ninguno", "ok")],
      ["Intervalo", dur(a.interval_s)],
      ["Último ciclo", lc ? el("span", {}, statusChip(CYCLE_STATUS, lc.status), " ", el("a", { href: ctx.href("ciclos", [lc.run_id]) }, ago(lc.ts_ms, now))) : "—"],
      ["Próximo ciclo", isNum(a.next_cycle_eta_ms) ? (a.next_cycle_eta_ms > now ? "en " + dur((a.next_cycle_eta_ms - now) / 1000) : "en curso o atrasado") : "—"],
      ["Ciclos guardados", c.cycles ? Object.entries(c.cycles.by_status).map(([k, v]) => (CYCLE_STATUS[k] || [0, k])[1] + " " + v).join(" · ") : "—"],
    ]),
    lc && lc.detail ? el("p", { class: "muted small", style: { marginTop: "8px" } }, lc.detail) : null,
    a.last_ran ? el("div", { style: { marginTop: "10px" } },
      el("h3", {}, "Último ciclo con razonamiento ", el("a", { class: "small", href: ctx.href("ciclos", [a.last_ran.run_id]) }, a.last_ran.run_id)),
      el("ul", { style: { margin: "6px 0 0", paddingLeft: "18px" } }, (a.last_ran.decisions || []).map((d) =>
        el("li", {}, el("b", {}, d.symbol), " ", statusChip(OUTCOME, d.outcome), " ", el("span", { class: "muted clamp" }, d.reason))))) : null);

  const mem = s.memory;
  const market = card("Mercado en memoria", mem ? "ventana desde " + new Date(mem.started_at_ms).toLocaleString("es-AR") + " · se vacía con cada reinicio" : "",
    mem ? table([{ label: "Símbolo" }, { label: "Mark medio", cls: "n" }, { label: "Por exchange" }, { label: "Último trade", cls: "n" },
      { label: "Trades", cls: "n" }, { label: "Tickers", cls: "n" }, { label: "Liqs", cls: "n" }],
      Object.entries(mem.symbols).sort().map(([sym, m]) => [
        el("span", {}, el("a", { href: ctx.href("moneda", [sym]) }, el("b", {}, sym)), " ", el("a", { class: "small", href: ctx.href("mercado", [sym]) }, "mercado")),
        price(m.mark),
        el("span", { class: "small muted" }, Object.entries(m.mark_by_source || {}).map(([ex, v]) => ex + " " + price(v)).join(" · ")),
        isNum(m.last_trade_age_s) ? dur(m.last_trade_age_s) : "—",
        int(m.counts.trade), int(m.counts.ticker), int(m.counts.liquidation),
      ]), { empty: "La ventana todavía está vacía.", label: "Mercado" })
      : el("p", { class: "empty" }, "Modo memory apagado: no hay ventana en RAM."),
    mem ? el("p", { class: "small muted", style: { marginTop: "8px" } },
      `Eventos escritos ${int(mem.stats.written)} · vencidos ${int(mem.stats.expired)} · expulsados por tope ${int(mem.stats.evicted_by_cap)} · acumulado ${dur(mem.accumulated_s)}`) : null);

  const fr = s.freshness || {};
  const fresh = card("Frescura", "antigüedad del último dato de cada fuente",
    kv([
      ...Object.entries(fr.workers_last_message_age_s || {}).map(([w, v]) => [w, isNum(v) ? dur(v) : "—", isNum(v) && v > 30 ? "bad-t" : ""]),
      ["último ciclo", isNum(fr.last_cycle_age_s) ? dur(fr.last_cycle_age_s) : "—"],
      ["grabación (flush)", isNum(fr.recorder_last_flush_age_s) ? dur(fr.recorder_last_flush_age_s) : "—", rec.status === "stale" ? "bad-t" : ""],
      ...Object.entries(fr.news_feeds || {}).map(([n, f]) => ["noticias · " + n, f.error ? el("span", { class: "bad-t" }, "error: " + f.error) : isNum(f.last_ok_age_s) ? dur(f.last_ok_age_s) : "—"]),
    ]));

  const track = card("Hipótesis contra la realidad", "global, todas las estrategias",
    h ? el("div", {},
      el("p", {}, el("span", { class: "verdict" }, rateText(h.wins, h.resolved, ci))),
      h.resolved ? ciBar(h.wins / h.resolved, ci) : null,
      el("p", { class: "small muted", style: { marginTop: "8px" } },
        "Una tasa sola no alcanza: el PnL neto contra mantener la moneda y la calibración (Brier) están por estrategia. ",
        el("a", { href: "#/estrategias" }, "Ver estrategias"), " · ", el("a", { href: "#/hipotesis" }, "ver todas las hipótesis")),
      kv([["Total", int(h.total)], ["Pendientes", int(h.pending)], ["Vencidas esperando resolución", int(h.due)], ["Resueltas", int(h.resolved)],
        ["Aciertos", int(h.wins)], ["Sin dato de precio", int(h.expired_no_data)]]))
      : el("p", { class: "empty" }, "Evaluación apagada (ciclo de análisis desactivado)."));

  const recCard = card("Grabación histórica", rec.enabled ? "base SQLite propia en la VM" : "",
    rec.enabled ? kv([
      ["Estado", recChip(rec.status)],
      ["Último flush", isNum(rec.last_flush_age_s) ? "hace " + dur(rec.last_flush_age_s) : "—"],
      ["Barras de 1 min", int(rec.rows && rec.rows.bars_1m)],
      ["Liquidaciones", int(rec.rows && rec.rows.liquidations)],
      ["Noticias", int(rec.rows && rec.rows.news)],
      ["Tamaño", bytes(rec.size_bytes)],
      ["Eventos tardíos / errores", int(rec.stats && rec.stats.late_events) + " / " + int(rec.stats && rec.stats.write_errors)],
    ]) : el("p", { class: "empty" }, "No se está guardando historia (HEATSIGNAL_RECORD=0)."),
    rec.enabled ? el("p", { class: "small", style: { marginTop: "8px" } }, el("a", { href: "#/datos" }, "Explorar datos guardados →")) : null);

  return el("div", { class: "stack" },
    pageHead("Resumen", el("span", {}, "Actualizado ", when(now, true), " · proceso arriba hace " + dur(s.uptime_s))),
    el("div", { class: "note info" }, "Lenguaje histórico: todo lo que muestra el panel describe lo que pasó (\"acertó en X % de N casos\"), nunca lo que va a pasar."),
    statsRow,
    alertsBox,
    el("div", { class: "grid two" }, graph, track),
    el("div", { class: "grid two" }, exchanges, fresh),
    market,
    el("div", { class: "grid two" }, recCard, labCard(s.lab)),
    el("div", { class: "grid two" }, paperCard(s.paper, now)),
  );
}

function recChip(st) {
  return { ok: chip("grabando", "ok"), starting: chip("arrancando", "info"), stale: chip("no escribe", "bad"), off: chip("apagada", "") }[st] || chip(st || "—");
}

function labCard(lab) {
  return card("Laboratorio (backtest)", "",
    !lab ? el("p", { class: "empty" }, "Todavía no se publicaron resultados (python -m heatsignal.lab.publish --upload en la PC).")
      : lab.error ? el("div", { class: "note bad" }, lab.error)
        : el("div", {},
          el("p", {}, el("span", { class: "verdict" }, lab.passing + " de " + lab.strategies), " estrategias pasan todos los criterios fuera de muestra."),
          el("p", { class: "small muted" }, "Walk-forward con costos y funding reales; el holdout de 3 meses sigue sellado. Publicado ",
            new Date(lab.generated_at).toLocaleString("es-AR"), "."),
          el("a", { href: "#/laboratorio" }, "Ver el lote →")));
}

function signedUsd(v) {
  if (!isNum(v)) return "—";
  const r = Math.abs(v) < 0.005 ? 0 : v;
  return (r > 0 ? "+" : r < 0 ? "−" : "") + "US$ " + num(Math.abs(r), 2, 2);
}

function paperCard(p, now) {
  const c = p && p.compare;
  return card("Paper trading (Freqtrade dry-run)", p && p.available ? chip("dry-run · dinero simulado", "info") : "",
    !p ? el("p", { class: "empty" }, "No configurado en este proceso.")
      : !p.available ? el("div", { class: "note warn" }, p.error)
        : el("div", {},
          kv([
            ["Operaciones abiertas", int(p.open)],
            ["Cerradas", int(p.closed) + (p.closed && p.closed < 30 ? " (muestra chica)" : "")],
            ["PnL realizado", signedUsd(p.realized), isNum(p.realized) && p.realized < 0 ? "bad-t" : ""],
            ["Paper desde", p.bot_start_ms ? new Date(p.bot_start_ms).toLocaleString("es-AR") : "—"],
            ["Última operación", p.last_trade_ms ? ago(p.last_trade_ms, now) : "—"],
            ["Comparación con el backtest", !c ? "sin publicar" : c.error ? el("span", { class: "bad-t" }, c.error)
              : c.status !== "ok" ? "sin datos todavía" : "paper − backtest " + signedUsd(c.diff) + " (" + int(c.runs) + " corridas)"],
          ]),
          el("p", { class: "small muted", style: { marginTop: "8px" } }, "PnL no realizado, curva y detalle por par en la vista de paper trading. ",
            el("a", { href: "#/paper" }, "Ver paper trading →"))));
}
