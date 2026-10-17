// Almacenamiento por niveles (GET /api/history/storage): disco con el umbral marcado, que hay caliente (VM) y frio
// (Drive) por tabla, ultimo archivado. Lo usan "Datos guardados" y "Sistema". Badge para respuestas que vinieron de Drive.
import { el, int, bytes, day, ago, chip, card, table, isNum, stat } from "./core.js";

const RUN_STATUS = { ok: ["ok", "ok"], nothing: ["", "nada que hacer"], partial: ["warn", "no llegó al objetivo"], error: ["bad", "error"], skipped: ["", "bajo el umbral"] };

export function diskMeter(dk) {
  if (!dk || !isNum(dk.used_pct)) return el("p", { class: "muted" }, "Sin datos de disco.");
  const thr = dk.threshold_pct, tgt = dk.target_pct, u = dk.used_pct;
  const kind = isNum(thr) && u >= thr ? "bad" : isNum(thr) && u >= thr - 5 ? "warn" : "";
  return el("div", {},
    el("div", { class: "disk-meter", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(u),
      "aria-label": "Uso de disco " + u + " %, umbral de archivado " + thr + " %" },
      el("span", { class: "fill " + kind, style: { width: Math.min(100, u) + "%" } }),
      isNum(tgt) ? el("span", { class: "mark target", style: { left: tgt + "%" }, title: "objetivo tras archivar " + tgt + " %" }) : null,
      isNum(thr) ? el("span", { class: "mark", style: { left: thr + "%" }, title: "umbral de archivado " + thr + " %" }) : null),
    el("div", { class: "disk-legend" },
      el("span", {}, u + " % usado · " + bytes(dk.used_bytes) + " de " + bytes((dk.used_bytes || 0) + (dk.free_bytes || 0))),
      el("span", {}, "umbral " + thr + " % (archiva a Drive) · objetivo " + tgt + " %"),
      el("span", {}, bytes(dk.free_bytes) + " libres")));
}

function range(a, b) { return isNum(a) ? day(a) + " → " + (isNum(b) ? day(b) : "hoy") : "—"; }

export function storageCard(s) {
  const dk = s.disk || {};
  const run = s.last_run, chk = s.last_check;
  const [rk, rt] = run ? RUN_STATUS[run.status] || ["", run.status] : ["", "nunca"];
  return card("Almacenamiento: VM y Drive", dk.over_threshold ? chip("sobre el umbral: se archiva en la próxima corrida", "bad") : chip("bajo el umbral", "ok"),
    diskMeter(dk),
    el("div", { class: "stats", style: { margin: "12px 0" } },
      stat("Último archivado", run ? ago(run.finished_ms) : "nunca", run ? el("span", {}, chip(rt, rk), " · liberó " + bytes(run.freed_bytes)) : "corre cada hora si el disco pasa el umbral"),
      stat("Último chequeo", chk && isNum(chk.ts_ms) ? ago(chk.ts_ms) : "—", chk ? (chk.note || chk.status || "") : "heatsignal-archive.timer"),
      stat("Cache de lecturas de Drive", s.cache ? bytes(s.cache.bytes) : "—", s.cache ? int(s.cache.files) + " archivos · tope " + bytes(s.cache.cap_bytes) : "lectura fría apagada"),
      stat("Base histórica", bytes(s.history_db && s.history_db.size_bytes), s.history_db ? "auto_vacuum " + s.history_db.auto_vacuum + " · " + bytes(s.history_db.freelist_bytes) + " libres adentro" : "")),
    table([{ label: "Datos" }, { label: "En la VM (caliente)" }, { label: "Rango en la VM" }, { label: "En Drive (frío)" }, { label: "Rango en Drive" }],
      (s.tables || []).map((t) => {
        const h = t.hot || {}, c = t.cold;
        const hot = t.table === "checkpoints" ? bytes(h.size_bytes) : t.table === "export" ? int(h.days) + " días" : int(h.rows) + " filas";
        const hotRange = t.table === "export" ? (h.last_day ? "último " + h.last_day : "—") : t.table === "checkpoints" ? "últimos " + int(s.checkpoint_keep_days) + " días" : range(h.first_ms, h.last_ms);
        return [t.label, hot, hotRange,
          c ? el("span", {}, int(c.files) + " archivos · " + (t.table === "checkpoints" ? int(c.rows) + " checkpoints" : int(c.rows) + " filas") + " · " + bytes(c.bytes)) : el("span", { class: "muted" }, "nada"),
          c ? range(c.first_ms, c.end_ms - 1) : "—"];
      }), { label: "Caliente y frío por tabla" }),
    el("p", { class: "small muted", style: { marginTop: "8px" } },
      "Cuando el disco de la VM llega al " + dk.threshold_pct + " %, lo más viejo (más de " + int(s.keep_days) + " días) se sube a " + (s.remote || "Drive (sin configurar)") +
      ", se verifica (tamaño + md5) y recién ahí se borra de la VM. Las consultas que piden ese rango lo bajan de Drive solas. Nunca se archivan " + (s.protected || []).join(" ni ") + "."),
    s.remote_error ? el("div", { class: "note bad" }, "Lectura de Drive no disponible: " + s.remote_error) : null,
    run && run.detail && (run.detail.errors || []).length ? el("div", { class: "note bad" }, "Errores del último archivado: " + run.detail.errors.join(" · ")) : null);
}

// Badge + nota para una respuesta de /api/history/bars o /liquidations que leyó de Drive.
export function coldNote(d) {
  if (!d || !d.source || d.source === "local") return d && d.archive_note ? el("p", { class: "small muted" }, d.archive_note) : null;
  const a = d.archive || {};
  return el("div", { class: "note " + (d.partial ? "bad" : "warn") },
    chip(d.source === "drive" ? "desde Drive" : "VM + Drive", "info"), " ",
    (a.note || "parte del rango vino de Drive") + " · " + int(a.files) + " archivos (" + int(a.cache_hits) + " del cache, " + int(a.downloaded) + " bajados, " + (isNum(a.seconds) ? a.seconds.toFixed(1) + " s" : "—") + ")",
    d.partial ? el("div", {}, "Incompleto: " + (d.partial_reasons || []).join(" · ")) : null);
}
