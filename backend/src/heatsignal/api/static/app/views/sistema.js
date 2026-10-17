// Sistema (/api/system): version, uptime, RAM, tamaños de bases, disco, backups, errores recientes y configuracion.
// Se refresca cada 45 s mientras la vista esta abierta.
import { el, clear, api, errorBox, when, ago, int, dur, bytes, card, table, pageHead, loading, chip, kv, stat, isNum, prose } from "../core.js";
import { storageCard } from "../storage-ui.js";

const DB_LABEL = { eval: "Evaluación (estrategias, hipótesis, ciclos)", history: "Historia (barras, liquidaciones, noticias)", checkpoints: "Checkpoints del grafo", lab_summary: "Resumen del laboratorio", freqtrade: "Freqtrade dry-run (paper trading, solo lectura)", paper_summary: "Comparación paper vs backtest (publicada desde la PC)" };

export async function mount(root, ctx) {
  const body = el("div", {}, loading()), storeBox = el("div");
  clear(root, pageHead("Sistema", "Operación del proceso de ingesta en la VM (solo lectura)."), body, storeBox);

  async function load() {
    try { render(await api("/api/system", null, ctx.signal)); }
    catch (e) { const b = errorBox(e, "el estado del sistema"); if (b) clear(body, b); }
    try { clear(storeBox, storageCard(await api("/api/history/storage", null, ctx.signal))); }
    catch (e) { if (e && e.status === 503) clear(storeBox); else { const b = errorBox(e, "el almacenamiento"); if (b) clear(storeBox, b); } }
  }

  function render(s) {
    const v = s.version || {}, dk = s.disk, ex = s.export, er = s.errors || {};
    const used = dk ? 1 - dk.free_bytes / dk.total_bytes : null;
    const dbs = Object.entries(s.databases || {}).filter(([, x]) => x && typeof x === "object");
    const nErr = (er.cycles || []).length + Object.keys(er.news_feeds || {}).length + (er.workers_down || []).length + (er.recorder_write_errors ? 1 : 0) + (er.memory_evicted_by_cap ? 1 : 0);
    clear(body,
      el("div", { class: "stats" },
        stat("Uptime", dur(s.uptime_s), "desde " + new Date(s.started_ms).toLocaleString("es-AR")),
        stat("RAM (RSS)", bytes(s.rss_bytes), "pid " + s.pid + " · Python " + s.python),
        stat("Versión", el("span", { class: "mono" }, (v.commit || "—").slice(0, 10)), (v.branch || v.source || "") + (v.source_mtime_ms ? " · código del " + new Date(v.source_mtime_ms).toLocaleDateString("es-AR") : "")),
        stat("Disco libre", dk ? bytes(dk.free_bytes) : "—", dk ? "de " + bytes(dk.total_bytes) + " · " + (isNum(dk.used_pct) ? dk.used_pct : Math.round(used * 100)) + " % usado" + (isNum(dk.threshold_pct) ? " · archiva a Drive al " + dk.threshold_pct + " %" : "") : "",
          dk && isNum(dk.used_pct) && isNum(dk.threshold_pct) ? (dk.used_pct >= dk.threshold_pct ? "bad-t" : dk.used_pct >= dk.threshold_pct - 5 ? "warn-t" : "") : used !== null && used > 0.85 ? "bad-t" : ""),
        stat("Último backup", ex && ex.last_upload && ex.last_upload.ts_ms ? ago(ex.last_upload.ts_ms) : "nunca", ex && ex.last_upload && ex.last_upload.remote ? "a " + ex.last_upload.remote : ""),
        stat("Build del panel", el("span", { class: "mono" }, s.build), "cambia con cada reinicio: la página se recarga sola")),
      card("Errores recientes", nErr ? chip(nErr + " fuentes con problemas", "bad") : chip("sin errores", "ok"),
        nErr ? el("div", { class: "stack" },
          (er.workers_down || []).length ? el("div", { class: "note bad" }, "Exchanges sin datos: " + er.workers_down.join(", ")) : null,
          Object.keys(er.news_feeds || {}).length ? table([{ label: "Feed de noticias" }, { label: "Error" }], Object.entries(er.news_feeds).map(([k, x]) => [k, prose(x)]), { label: "Feeds con error" }) : null,
          er.recorder_write_errors ? el("div", { class: "note bad" }, "El grabador tuvo " + int(er.recorder_write_errors) + " errores de escritura.") : null,
          er.memory_evicted_by_cap ? el("div", { class: "note warn" }, "La ventana en memoria expulsó " + int(er.memory_evicted_by_cap) + " eventos por tope.") : null,
          (er.cycles || []).length ? el("div", {}, el("h3", { style: { marginBottom: "6px" } }, "Ciclos con error (últimos 10)"),
            table([{ label: "Hora" }, { label: "Ciclo" }, { label: "Detalle" }], er.cycles.map((c) => [when(c.ts_ms), el("a", { href: ctx.href("ciclos", [c.run_id]) }, c.run_id), { v: prose(c.detail || ""), cls: "wrap" }]), { label: "Ciclos con error" })) : null)
          : el("p", { class: "muted" }, "Ningún error registrado.")),
      el("div", { class: "grid two" },
        card("Bases de datos y archivos", "incluye -wal/-shm de SQLite",
          table([{ label: "Base" }, { label: "Ruta" }, { label: "Tamaño", cls: "n" }, { label: "Modificada" }],
            dbs.map(([k, x]) => [DB_LABEL[k] || k, el("span", { class: "mono small" }, x.path || "—"),
              x.exists === false ? chip("no existe", "warn") : bytes(x.size_bytes), x.modified_ms ? ago(x.modified_ms) : "—"]), { label: "Bases" }),
          isNum(s.databases && s.databases.lab_details) ? el("p", { class: "small muted", style: { marginTop: "6px" } }, int(s.databases.lab_details) + " detalles del laboratorio publicados.") : null),
        card("Exportación y respaldo", "",
          ex ? kv([["Último día exportado", ex.last_export_day || "—"], ["Días exportados", int(ex.export_days)],
            ["Última subida", ex.last_upload && ex.last_upload.ts_ms ? when(ex.last_upload.ts_ms) : "nunca"], ["Destino", ex.last_upload ? (ex.last_upload.remote || "—") + " " + (ex.last_upload.dir || "") : "—"],
            ex.last_upload && ex.last_upload.error ? ["Error", el("span", { class: "bad-t" }, ex.last_upload.error)] : null])
            : el("p", { class: "empty" }, "Grabación apagada: no hay exportación."),
          el("p", { class: "small muted", style: { marginTop: "8px" } }, "Todos los días 00:20 UTC: .csv.gz por día en la VM y copia con rclone."))),
      el("div", { class: "grid two" },
        card("Configuración", "", kv(Object.entries(s.config || {}).map(([k, x]) => [k, el("span", { class: "mono small" }, Array.isArray(x) ? x.join(", ") : String(x))]))),
        card("Ajustes", "", kv(Object.entries(s.settings || {}).map(([k, x]) => [k, el("span", { class: "mono small" },
          x && typeof x === "object" ? Object.entries(x).map(([a, b]) => a + "=" + (isNum(b) && k === "retention_s" ? dur(b) : b)).join(" · ") : String(x))])))),
      card("Versión", "", kv([["Commit", el("span", { class: "mono small" }, v.commit || "—")], ["Origen", v.source || "—"], ["Rama", v.branch || "—"],
        ["Código más nuevo", v.source_mtime_ms ? when(v.source_mtime_ms) : "—"], ["Ahora (servidor)", when(s.now_ms, true)]])),
    );
  }
  await load();
  ctx.every(45000, load);
  return { refresh: load };
}
