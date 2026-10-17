// Acciones (las unicas escrituras del panel): "Analizar ahora" (POST /api/analyze) y "Mandar al laboratorio"
// (POST /api/lab/requests). Ambas con header X-HeatSignal-Action. El seguimiento del analisis se pollea cada 5 s solo
// mientras esta en curso; los pedidos al laboratorio los corre la PC del usuario (worker) y pueden tardar horas.
import { el, clear, api, apiPost, notDeployed, chip, dur, when, ago, table, pager, int, norm, errorBox, loading, fixed, signedFrac, shortId } from "./core.js";

const PREP = "Función en preparación: el backend todavía no expone este endpoint.";
const ANALYZE_STATUS = {
  queued: ["info", "en cola"], running: ["info", "corriendo"], ran: ["ok", "terminó"], error: ["bad", "error"],
  warmup: ["warn", "ventana calentando"], no_strategies: ["warn", "sin estrategias"], no_llm: ["warn", "sin LLM"],
};
const LAB_STATUS = { pending: ["info", "pendiente"], running: ["info", "corriendo en la PC"], done: ["ok", "listo"], error: ["bad", "error"] };
export const labStatusChip = (s) => { const [k, t] = LAB_STATUS[s] || ["", s]; return chip(t, k); };
const runIdFromUrl = (u) => (String(u || "").match(/cycles\/([^/?#]+)/) || [])[1];

/** Boton "Analizar ahora" con seguimiento. symbol: "XRP-USDT". */
export function analyzeAction(ctx, symbol) {
  const status = el("div", { class: "small", role: "status", "aria-live": "polite" });
  const btn = el("button", { class: "btn primary", type: "button" }, "Analizar ahora");
  let timer = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  ctx.signal.addEventListener("abort", stop);

  async function follow(runId, t0) {
    try {
      const r = await api("/api/analyze/" + encodeURIComponent(runId), null, ctx.signal);
      const [k, t] = ANALYZE_STATUS[r.status] || ["", r.status];
      const done = !["queued", "running"].includes(r.status);
      const rid = runIdFromUrl(r.cycle_url) || runId;
      clear(status, chip(t, k), " ", el("span", { class: "muted" }, runId + " · " + dur((Date.now() - t0) / 1000)),
        r.detail ? el("div", { class: "muted" }, r.detail) : null,
        done && r.cycle_url ? el("div", {}, el("a", { href: ctx.href("ciclos", [rid]) }, "Ver el razonamiento del ciclo →")) : null,
        done ? null : el("div", { class: "muted" }, "Suele tardar 1–2 min. Se consulta cada 5 s."));
      if (done) { stop(); btn.disabled = false; }
    } catch (e) {
      if (e.name === "AbortError") return;
      stop(); btn.disabled = false;
      clear(status, el("span", { class: "err" }, notDeployed(e) ? PREP : "No se pudo seguir el análisis: " + e.message));
    }
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    clear(status, loading("Pidiendo el análisis…"));
    try {
      const r = await apiPost("/api/analyze", { symbol: norm(symbol) }, ctx.signal);
      const t0 = Date.now();
      await follow(r.run_id, t0);
      stop();
      timer = setInterval(() => { if (!document.hidden) follow(r.run_id, t0); }, 5000);
    } catch (e) {
      btn.disabled = false;
      if (e.name === "AbortError") return;
      if (notDeployed(e)) clear(status, el("span", { class: "warn-t" }, PREP));
      else if (e.status === 429 && e.body && e.body.run_id) {  // ya hay uno en curso: seguir ese
        const t0 = Date.now(), rid = e.body.run_id;
        btn.disabled = true;
        await follow(rid, t0);
        stop();
        timer = setInterval(() => { if (!document.hidden) follow(rid, t0); }, 5000);
        status.prepend(el("div", { class: "warn-t" }, e.message + " · se muestra ese:"));
      } else if (e.status === 429) clear(status, el("span", { class: "warn-t" }, "Muy pronto (" + e.message + "): se puede volver a pedir en " + dur((e.body && e.body.retry_after_s) || 0) + "."));
      else if (e.status === 503) clear(status, el("span", { class: "warn-t" }, "El grafo está deshabilitado (sin key de LLM o ciclo apagado): " + e.message));
      else clear(status, el("span", { class: "err" }, "No se pudo pedir el análisis: " + e.message));
    }
  });
  return el("div", { class: "action" }, btn, status);
}

/**
 * Boton "Mandar al laboratorio" con confirmacion (dialog). symbols: ["XRP-USDT"]; labStrategies: items de /api/lab.
 * onSent(request) se llama tras un 201.
 */
export function labRequestAction(ctx, symbols, labStrategies, onSent) {
  const status = el("div", { class: "small", role: "status", "aria-live": "polite" });
  const btn = el("button", { class: "btn", type: "button" }, "Mandar al laboratorio");
  btn.addEventListener("click", () => openDialog());

  function openDialog() {
    const all = el("input", { type: "checkbox", id: "lr-all", checked: true });
    const boxes = (labStrategies || []).map((s) => {
      const cb = el("input", { type: "checkbox", value: s.strategy_id, disabled: true });
      return el("label", { class: "check" }, cb, el("span", {}, el("b", {}, s.strategy_id), s.description ? el("span", { class: "muted small" }, " — " + s.description) : null));
    });
    all.addEventListener("change", () => boxes.forEach((b) => { const cb = b.querySelector("input"); cb.disabled = all.checked; }));
    const note = el("textarea", { rows: "2", maxlength: "500", style: { width: "100%" }, placeholder: "opcional: por qué lo pedís" });
    const err = el("div", { class: "small" });
    const send = el("button", { class: "btn primary", type: "submit" }, "Confirmar y enviar");
    const cancel = el("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Cancelar");
    const form = el("form", { method: "dialog" },
      el("h2", { id: "lr-title" }, "Mandar " + symbols.join(", ") + " al laboratorio"),
      el("div", { class: "note warn", style: { marginTop: "10px" } },
        el("b", {}, "Antes de enviar: "), "lo corre tu PC (el worker del laboratorio), no la VM: puede tardar minutos u horas y solo avanza con la PC prendida. ",
        "Cada corrida suma intentos al Deflated Sharpe (anti p-hacking): pedir de más vuelve más exigente el criterio para todas. Pedilo solo si tiene sentido."),
      el("fieldset", { style: { border: "1px solid var(--line)", borderRadius: "8px", padding: "8px 12px", margin: "10px 0" } },
        el("legend", {}, "Estrategias"), el("label", { class: "check" }, all, el("span", {}, el("b", {}, "Todas las del lote"), " (\"*\")")),
        el("div", { style: { display: "grid", gap: "2px", maxHeight: "200px", overflowY: "auto" } }, boxes)),
      el("label", { class: "field" }, el("span", {}, "Nota"), note),
      err,
      el("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "12px" } }, cancel, send));
    const dlg = el("dialog", { class: "dlg", "aria-labelledby": "lr-title" }, form);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const ids = all.checked ? ["*"] : boxes.map((b) => b.querySelector("input")).filter((c) => c.checked).map((c) => c.value);
      if (!ids.length) { clear(err, el("span", { class: "err" }, "Elegí al menos una estrategia o \"todas\".")); return; }
      send.disabled = true;
      try {
        const r = await apiPost("/api/lab/requests", { strategy_ids: ids, symbols: symbols.map(norm), note: note.value.trim() }, ctx.signal);
        dlg.close();
        clear(status, chip("pedido " + shortId(r.request_id) + " · " + (r.status || "pending"), "info"), " ",
          el("span", { class: "muted" }, "La PC lo toma cuando esté prendida; el estado se ve en Laboratorio → Pedidos."));
        if (onSent) onSent(r);
      } catch (e) {
        send.disabled = false;
        clear(err, el("span", { class: notDeployed(e) ? "warn-t" : "err" }, notDeployed(e) ? PREP : "No se pudo enviar: " + e.message));
      }
    });
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }
  return el("div", { class: "action" }, btn, status);
}

/** Tabla de pedidos al laboratorio (GET /api/lab/requests), opcionalmente filtrada por simbolo en el cliente. */
export function labRequestsBlock(ctx, { symbol = null, limit = 20 } = {}) {
  const box = el("div", {}, loading());
  let offset = 0;
  async function load() {
    try {
      const d = await api("/api/lab/requests", { limit: symbol ? 200 : limit, offset: symbol ? 0 : offset }, ctx.signal);
      let items = d.items || [];
      if (symbol) items = items.filter((r) => (r.symbols || []).some((s) => norm(s) === norm(symbol)));
      clear(box,
        table([{ label: "Pedido" }, { label: "Estado" }, { label: "Estrategias" }, { label: "Símbolos" }, { label: "Creado" }, { label: "Estado desde" }, { label: "Resultado / error" }],
          items.map((r) => [el("span", { class: "mono small", title: r.request_id }, shortId(r.request_id)),
            el("span", {}, labStatusChip(r.status), r.worker ? el("div", { class: "small muted" }, "worker " + r.worker) : null),
            { v: el("span", { class: "small" }, (r.strategy_ids || []).join(", ").replace("*", "todas")), cls: "wrap" }, (r.symbols || []).join(", "),
            r.created_ms ? when(r.created_ms) : "—", r.status_ms ? ago(r.status_ms) : "—",
            { v: el("div", {}, r.error ? el("div", { class: "err small" }, r.error) : null, r.result ? resultText(r.result) : null,
              r.note ? el("div", { class: "muted small" }, "nota: " + r.note) : null,
              r.result_url ? resultLink(ctx, r.result_url) : null, !r.error && !r.result && !r.note ? "—" : null), cls: "wrap" }]),
          { empty: symbol ? "No hay pedidos para esta moneda." : "Todavía no se pidió nada al laboratorio.", label: "Pedidos al laboratorio" }),
        symbol ? null : pager(d.total || 0, limit, offset, (o) => { offset = o; load(); }));
    } catch (e) {
      if (e.name === "AbortError") return;
      clear(box, notDeployed(e) ? el("p", { class: "muted" }, PREP) : errorBox(e, "los pedidos") || "");
    }
  }
  load();
  return { node: box, reload: load };
}

/** Resumen del resultado que dejo el worker: {file, strategies, ok, errors, passing}. */
function resultText(res) {
  if (typeof res !== "object") return el("span", { class: "small" }, String(res));
  return el("div", { class: "small" }, int(res.strategies) + " estrategias · " + int(res.ok) + " ok · " + int(res.errors) + " con error · ",
    chip(int(res.passing) + " pasan", res.passing ? "ok" : "warn"));
}

/** Resultado completo (/api/lab/requests/{id}/result) a demanda. */
function resultLink(ctx, url) {
  const box = el("div");
  const btn = el("button", { class: "btn small", type: "button" }, "Ver resultado");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    clear(box, loading());
    try {
      const d = await api(url, null, ctx.signal);
      const rows = d.strategies || [];
      clear(box, table([{ label: "Estrategia" }, { label: "Estado" }, { label: "Sharpe", cls: "n" }, { label: "Retorno", cls: "n" }, { label: "Trades", cls: "n" }, { label: "Veredicto" }],
        rows.map((x) => {
          const sm = x.summary || x;
          return [el("b", {}, x.strategy_id || "—"), x.status === "error" ? chip("error", "bad", x.error || "") : chip(x.status || "ok", "ok"),
            fixed(sm.sharpe, 2), signedFrac(sm.total_return), int(sm.trades), sm.pasa === undefined ? "—" : chip(sm.pasa ? "pasa" : "no pasa", sm.pasa ? "ok" : "warn")];
        }), { empty: "El resultado no trae estrategias.", label: "Resultado del pedido" }),
        el("p", { class: "small muted" }, "Cada corrida suma intentos al Deflated Sharpe del lote."));
    } catch (e) { clear(box, errorBox(e, "el resultado") || ""); btn.disabled = false; }
  });
  return el("div", { style: { marginTop: "4px" } }, btn, box);
}
