// Alertas derivadas de /api/summary (las usan el resumen y los badges de la navegacion).
// level: bad (algo esta roto), warn (dato parcial o atrasado), info (contexto que conviene saber).
import { dur, int } from "./core.js";

const WORKER_STALE_S = 30;

export function computeAlerts(s) {
  const out = [];
  if (!s) return out;
  const push = (level, text, link) => out.push({ level, text, link });

  for (const w of s.workers || []) {
    if (!w.connected) push("bad", `${w.worker}: sin datos (desconectado). Reconexiones: ${int(w.reconnects)}.`, "#/sistema");
    else if (typeof w.last_message_age_s === "number" && w.last_message_age_s > WORKER_STALE_S)
      push("warn", `${w.worker}: último mensaje hace ${dur(w.last_message_age_s)}.`, "#/mercado");
  }
  if (!(s.workers || []).length) push("warn", "No hay workers de exchanges registrados todavía.");

  const a = s.analysis || {};
  if (!a.enabled) push("warn", "El ciclo de análisis está apagado: no hay hipótesis ni features en vivo.");
  else if (!a.graph_enabled) push("warn", "El grafo LLM está deshabilitado (falta key de proveedor): los ciclos no razonan.");
  const lc = a.last_cycle;
  if (lc && lc.status === "error") push("bad", `El último ciclo (${lc.run_id}) terminó en error: ${lc.detail || "sin detalle"}`, "#/ciclos/" + encodeURIComponent(lc.run_id));
  if (lc && a.interval_s && typeof lc.age_s === "number" && lc.age_s > a.interval_s * 2.5)
    push("warn", `El último ciclo fue hace ${dur(lc.age_s)} (el intervalo es ${dur(a.interval_s)}).`, "#/ciclos");
  const cyc = s.counts && s.counts.cycles && s.counts.cycles.by_status;
  if (cyc && cyc.error && !(lc && lc.status === "error"))
    push("warn", `${int(cyc.error)} ciclo${cyc.error > 1 ? "s" : ""} con error en el historial.`, "#/ciclos?status=error");

  const rec = s.recording || {};
  if (rec.status === "stale") push("bad", `La grabación histórica no escribe (último flush ${rec.last_flush_age_s === null ? "nunca" : "hace " + dur(rec.last_flush_age_s)}).`, "#/datos");
  else if (rec.status === "off") push("info", "La grabación histórica está apagada (HEATSIGNAL_RECORD=0).");
  if (rec.stats && rec.stats.write_errors) push("bad", `El grabador tuvo ${int(rec.stats.write_errors)} errores de escritura.`, "#/sistema");

  const feeds = (s.freshness && s.freshness.news_feeds) || {};
  for (const [name, f] of Object.entries(feeds)) if (f && f.error) push("warn", `Feed de noticias "${name}" con error: ${f.error}`, "#/sistema");

  const h = s.counts && s.counts.hypotheses;
  if (h && h.due) push("info", `${int(h.due)} hipótesis vencidas esperan al job de resolución (corre cada 5 min).`, "#/hipotesis?status=pending");

  if (s.lab && s.lab.error) push("warn", "Laboratorio: " + s.lab.error, "#/laboratorio");

  const mem = s.memory;
  if (mem && mem.stats && mem.stats.evicted_by_cap) push("warn", `La ventana en memoria expulsó ${int(mem.stats.evicted_by_cap)} eventos por tope: faltan datos.`);
  if (mem && typeof mem.accumulated_s === "number" && mem.accumulated_s < 1800)
    push("info", `La ventana en memoria arrancó hace ${dur(mem.accumulated_s)} (se vacía con cada reinicio): las métricas de 1 h/24 h todavía son parciales.`);
  return out;
}
