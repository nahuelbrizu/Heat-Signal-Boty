// Oportunidades (#/oportunidades): alertas de entrada / salida / giro de TODAS las estrategias con su estado en el
// laboratorio (pasa / no pasa / sin probar) y en el bot paper. Fuente: /api/alerts (motor heatsignal-alerts, cada 5 min,
// velas cerradas). Acciones: "Mandar a la demo" y "Descartar" por alerta; interruptor de automatico por estrategia
// (apagado por defecto). La ejecucion en la demo de Bybit es la fase 4: hasta entonces las mandadas quedan en cola.
import { el, clear, api, apiPost, errorBox, card, table, pageHead, loading, chip, stat, when, ago, int, fixed, price, seg, sym as toSym, DASH } from "../core.js";

const KIND = { entrada: ["entrada", "ok"], salida: ["salida", ""], giro: ["giro", "warn"] };
const LAB = { pasa: ["pasa", "ok"], "no pasa": ["no pasa", "bad"], "sin probar": ["sin probar", "warn"] };
const STATE = { nueva: ["nueva", "info"], enviada: ["enviada a la demo", "ok"], auto: ["automática", "ok"], descartada: ["descartada", ""],
  ejecutada: ["ejecutada", "ok"], rechazada: ["rechazada", "bad"] };
const chipOf = (map, k, title) => { const [t, c] = map[k] || [k || DASH, ""]; return chip(t, c, title); };

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const f = { hours: Number(q.get("hours") || 48), lab: q.get("lab") || "", kind: q.get("kind") || "", hide: q.get("hide") !== "0" };
  const head = pageHead("Oportunidades", "Cada vez que una estrategia entra, sale o da vuelta en una cripto (velas cerradas, mismas reglas que el laboratorio), aparece acá con su estado en el laboratorio. "
    + "Podés mandarla a la demo de Bybit o prender el automático por estrategia. No abre hipótesis ni opera con plata real.");
  const box = el("div", {}, loading());
  clear(root, head, box);
  const go = (patch) => ctx.navigate(ctx.href("oportunidades", [], { hours: f.hours, lab: f.lab, kind: f.kind, hide: f.hide ? "1" : "0", ...patch }));

  async function act(a, action, btn) {
    btn.disabled = true;
    try { await apiPost("/api/alerts/" + a.alert_id + "/action", { action }, ctx.signal); await load(); }
    catch (e) { btn.disabled = false; alert((e && e.message) || "no se pudo"); }
  }
  async function setAuto(sid, enabled, input) {
    input.disabled = true;
    try { await apiPost("/api/alerts/auto", { strategy_id: sid, enabled }, ctx.signal); await load(); }
    catch (e) { input.checked = !enabled; input.disabled = false; alert((e && e.message) || "no se pudo"); }
  }

  async function load() {
    let d;
    try { d = await api("/api/alerts", { hours: f.hours, ...(f.kind ? { kind: f.kind } : {}) }, ctx.signal); }
    catch (e) { const b = errorBox(e, "las alertas"); if (b) clear(box, b); return; }
    if (!d.configured) { clear(box, el("div", { class: "note" }, d.nota)); return; }
    const all = d.alerts || [];
    const xs = all.filter((a) => (!f.lab || a.lab_status === f.lab) && (!f.hide || a.state !== "descartada"));
    const run = (d.runs || [])[0];
    const det = (run && run.detail) || {};
    const autoOn = Object.entries(d.auto || {}).filter(([, v]) => v).map(([k]) => k);
    const stats = el("div", { class: "stats" },
      stat("Alertas", int(xs.length), "últimas " + f.hours + " h" + (xs.length !== all.length ? " (de " + int(all.length) + ")" : "")),
      stat("En cola para la demo", int(d.queued), "mandadas a mano o automáticas, sin ejecutar"),
      stat("Última pasada", run ? when(run.ts_ms) : DASH, run ? (run.status === "ok" ? int(run.n_eval) + " estrategias evaluadas" : "estado " + run.status) + " · " + ago(run.ts_ms) : "todavía no corrió",
        run && run.status !== "ok" ? "warn-t" : ""),
      stat("Automático", int(autoOn.length), autoOn.length ? autoOn.slice(0, 2).join(", ") + (autoOn.length > 2 ? "…" : "") : "ninguna estrategia"));
    const notes = [];
    if (det.hueco) notes.push(el("div", { class: "note warn small" }, det.hueco));
    if (run && run.status === "error" && det.error) notes.push(el("div", { class: "note warn small" }, "La última pasada falló: " + det.error));
    if (det.errores && Object.keys(det.errores).length) notes.push(el("div", { class: "note warn small" }, "Estrategias que fallaron en la última pasada: " + Object.keys(det.errores).join(", ")));
    if (det.salteadas) notes.push(el("p", { class: "small muted" }, int(det.salteadas) + " estrategias no se evalúan todavía: " +
      Object.entries(det.salteadas_por_motivo || {}).map(([k, v]) => v + " por " + k).join("; ") + "."));
    const filters = el("div", { class: "chips" },
      seg("Período", [["6", "6 h"], ["24", "24 h"], ["48", "48 h"], ["168", "7 días"]], String(f.hours), (v) => go({ hours: v })),
      seg("Laboratorio", [["", "todas"], ["pasa", "pasa"], ["sin probar", "sin probar"], ["no pasa", "no pasa"]], f.lab, (v) => go({ lab: v })),
      seg("Tipo", [["", "todas"], ["entrada", "entradas"], ["salida", "salidas"], ["giro", "giros"]], f.kind, (v) => go({ kind: v })),
      seg("Descartadas", [["1", "ocultar"], ["0", "mostrar"]], f.hide ? "1" : "0", (v) => go({ hide: v })));
    const rows = xs.map((a) => {
      const canAct = a.state === "nueva";
      const send = el("button", { class: "btn small", onclick: (ev) => act(a, "enviada", ev.currentTarget) }, "Mandar a la demo");
      const drop = el("button", { class: "btn small ghost", onclick: (ev) => act(a, "descartada", ev.currentTarget) }, "Descartar");
      return [
        el("span", { class: "small" }, when(a.decision_ms)),
        el("b", {}, toSym(a.symbol)),
        el("span", { class: "chips" }, chipOf(KIND, a.kind), a.direction !== "flat" ? chip(a.direction, a.direction === "long" ? "ok" : "bad") : null),
        el("span", { class: "num" }, price(a.price)),
        el("div", {}, el("a", { href: ctx.href("laboratorio", [a.strategy_id]) }, el("span", { class: "small" }, a.strategy_id)),
          el("div", { class: "muted small" }, (a.timeframe || "") + (a.source === "generada" ? " · generada" : "") + (a.exit_plan ? " · " + a.exit_plan : ""))),
        el("span", { class: "chips" }, chipOf(LAB, a.lab_status, a.lab_sharpe != null ? "Sharpe OOS " + fixed(a.lab_sharpe, 2) : ""),
          a.paper_role ? chip("bot paper: " + a.paper_role, "info") : null),
        chipOf(STATE, a.state),
        canAct ? el("span", { class: "chips" }, send, drop) : el("span", { class: "muted small" }, DASH)];
    });
    const sids = [...new Set(all.map((a) => a.strategy_id))].sort();
    const labOf = Object.fromEntries(all.map((a) => [a.strategy_id, a.lab_status]));
    const autoRows = sids.map((sid) => {
      const input = el("input", { type: "checkbox", checked: !!(d.auto || {})[sid], onchange: (ev) => setAuto(sid, ev.currentTarget.checked, ev.currentTarget) });
      return [el("label", { class: "chips" }, input, el("span", { class: "small" }, sid)), chipOf(LAB, labOf[sid]),
        int(all.filter((a) => a.strategy_id === sid).length)];
    });
    clear(box, stats, executorCard(d.ejecutor), ...notes, filters,
      rows.length ? table([{ label: "Decisión" }, { label: "Cripto" }, { label: "Tipo" }, { label: "Precio", cls: "num" }, { label: "Estrategia" },
        { label: "Laboratorio / bot" }, { label: "Estado" }, { label: "" }], rows)
        : el("p", { class: "muted" }, "No hay alertas con estos filtros."),
      card("Automático por estrategia", el("span", { class: "small muted" }, "apagado por defecto"),
        el("p", { class: "small" }, "Con el automático prendido, cada alerta nueva de esa estrategia queda en cola para la demo de Bybit sin pasar por el botón. ",
          el("b", {}, "Recomendación: prenderlo solo en estrategias que pasan el laboratorio."), " Solo demo (fondos ficticios): nunca plata real."),
        autoRows.length ? table([{ label: "Estrategia" }, { label: "Laboratorio" }, { label: "Alertas en el período", cls: "num" }], autoRows)
          : el("p", { class: "muted small" }, "Todavía no hay estrategias con alertas en el período.")),
      el("p", { class: "small muted" }, d.nota || ""));
  }
  await load();
  ctx.every(60000, load);
  return { refresh: load };
}

// ------------------------------------------------------------------ ejecutor en la demo de Bybit (alerts/executor.py)

const money = (v) => (typeof v === "number" && Number.isFinite(v) ? (v < 0 ? "−" : "") + "US$ " + fixed(Math.abs(v), 2) : DASH);
const EV = { fill: ["fill", "ok"], rechazo: ["rechazo", "warn"], candado: ["bloqueado", "bad"], conciliacion: ["conciliación", "warn"], error: ["error", "bad"] };

function executorCard(x) {
  if (!x) return null;
  const head = x.keys_ok ? (x.bloqueado ? chip("bloqueado", "bad") : chip("listo", "ok")) : chip("sin clave", "warn");
  const lim = x.limites || {};
  const parts = [
    el("p", { class: "small" }, "Opera lo mandado (botón o automático) en una cuenta ", el("b", {}, "demo"), " de Bybit aparte: " +
      money(lim.notional_usdt) + " por posición, 1x, hasta " + int(lim.max_open) + " abiertas y " + money(lim.max_gross_usdt) +
      " en total; una entrada de más de " + int(lim.max_entry_age_min) + " min se rechaza. Cada posición abierta se cierra sola cuando la regla sale. " +
      "Candado: si la cuenta real acepta la clave, no opera."),
  ];
  if (!x.keys_ok) parts.push(el("div", { class: "note warn small" }, "Falta la clave de la cuenta demo: ", el("code", {}, x.keys_path),
    ". Crear una subcuenta en Bybit → entrar → Demo Trading → avatar → API (contratos: órdenes y posiciones, sin retiros) y pegarla en ese archivo."));
  if (x.bloqueado) parts.push(el("div", { class: "note warn small" }, "Último bloqueo: " + ((x.bloqueado.detail || {}).error || "")));
  if (x.conciliacion && Object.keys(x.conciliacion.diferencias || {}).length) parts.push(el("div", { class: "note warn small" },
    "La posición del exchange no coincide con la suma de las estrategias en: " + Object.keys(x.conciliacion.diferencias).join(", ")));
  parts.push(el("div", { class: "stats" },
    stat("Posiciones abiertas", int((x.abiertas || []).length), "por estrategia y cripto"),
    stat("Resultado realizado", money(x.realizado), "comisiones " + money(x.comisiones), x.realizado >= 0 ? "ok-t" : "bad-t")));
  if ((x.abiertas || []).length) parts.push(table([{ label: "Estrategia" }, { label: "Cripto" }, { label: "Cantidad", cls: "num" }, { label: "Precio medio", cls: "num" }],
    x.abiertas.map((p) => [el("span", { class: "small" }, p.strategy_id), toSym(p.symbol), el("span", { class: "num" }, fixed(p.qty, 4)), el("span", { class: "num" }, price(p.avg))])));
  const evs = (x.eventos || []).slice(0, 12);
  if (evs.length) parts.push(table([{ label: "Cuándo" }, { label: "Evento" }, { label: "Estrategia / cripto" }, { label: "Detalle" }],
    evs.map((e) => [el("span", { class: "small" }, when(e.ts_ms)), chipOf(EV, e.kind),
      el("span", { class: "small" }, (e.strategy_id || DASH) + (e.symbol ? " · " + toSym(e.symbol) : "")),
      el("span", { class: "small" }, e.kind === "fill" ? (e.qty_delta > 0 ? "compra " : "venta ") + fixed(Math.abs(e.qty_delta), 4) + " a " + price(e.price)
        : ((e.detail || {}).motivo || (e.detail || {}).error || ""))])));
  return card("Ejecutor en la demo de Bybit", head, ...parts);
}
