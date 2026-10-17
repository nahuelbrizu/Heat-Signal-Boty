// Nucleo de la SPA: DOM seguro, formatos, cliente de la API y componentes chicos.
// Regla de seguridad: todo texto (noticias, razonamiento del LLM, ids) entra como nodo de texto. Nunca se inserta HTML armado con datos.

// ------------------------------------------------------------------ DOM

/** el("div", {class: "x", onclick: fn}, hijos...) — los hijos string se escapan (createTextNode). */
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === "") continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    node.setAttribute(k, String(v));
  }
  append(node, children);
  return node;
}

export function clear(node, ...children) { node.replaceChildren(); append(node, children); return node; }

/** Solo http(s): una URL de un feed nunca puede ser javascript:. */
export function safeUrl(u) {
  try {
    const url = new URL(String(u), location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}

export function extLink(url, text) {
  const href = safeUrl(url);
  return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text) : el("span", {}, text);
}

// ------------------------------------------------------------------ formatos (es-AR)

const NF = new Map();
const nfmt = (min, max) => {
  const key = min + "|" + max;
  if (!NF.has(key)) NF.set(key, new Intl.NumberFormat("es-AR", { minimumFractionDigits: min, maximumFractionDigits: max }));
  return NF.get(key);
};
export const isNum = (v) => typeof v === "number" && Number.isFinite(v);
export const DASH = "—";

export function num(v, d = 2, min = 0) { return isNum(v) ? nfmt(min, d).format(v) : DASH; }
export function int(v) { return isNum(v) ? nfmt(0, 0).format(v) : DASH; }
export function fixed(v, d = 2) { return isNum(v) ? nfmt(d, d).format(v) : DASH; }
export function price(v) {
  if (!isNum(v)) return DASH;
  const a = Math.abs(v);
  return nfmt(0, a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 4 : 6).format(v);
}
export function usd(v) {
  if (!isNum(v)) return DASH;
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e9) return s + "US$ " + nfmt(0, 2).format(a / 1e9) + " B";
  if (a >= 1e6) return s + "US$ " + nfmt(0, 2).format(a / 1e6) + " M";
  if (a >= 1e3) return s + "US$ " + nfmt(0, 1).format(a / 1e3) + " k";
  return s + "US$ " + nfmt(0, 0).format(a);
}
/** Puntos porcentuales (2.5 = +2,5 %). */
export function pct(v, d = 2) { return isNum(v) ? nfmt(0, d).format(v) + " %" : DASH; }
/** Fraccion (0.43 = 43 %). */
export function frac(v, d = 1) { return isNum(v) ? nfmt(0, d).format(v * 100) + " %" : DASH; }
export function signedPct(v, d = 2) {
  if (!isNum(v)) return DASH;
  const r = Number(Math.abs(v).toFixed(d));
  return (r === 0 ? "" : v > 0 ? "+" : "−") + nfmt(0, d).format(r) + " %";
}
export function signedFrac(v, d = 1) { return isNum(v) ? signedPct(v * 100, d) : DASH; }
/** funding de 8 h (0.0001 = 0,0100 %). */
export function funding(v) { return isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + nfmt(4, 4).format(Math.abs(v) * 100) + " %" : DASH; }
export function bytes(n) {
  if (!isNum(n)) return DASH;
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return nfmt(0, i ? 1 : 0).format(n) + " " + u[i];
}
export function dur(s) {
  if (!isNum(s)) return DASH;
  s = Math.max(0, s);
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  if (s < 86400 * 2) return nfmt(0, 1).format(s / 3600) + " h";
  return nfmt(0, 1).format(s / 86400) + " d";
}
export function ago(ms, now = Date.now()) { return isNum(ms) ? "hace " + dur((now - ms) / 1000) : DASH; }
export function dt(ms) { return isNum(ms) ? new Date(ms).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" }) : DASH; }
export function dtSec(ms) { return isNum(ms) ? new Date(ms).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "medium" }) : DASH; }
export function timeOnly(ms) { return isNum(ms) ? new Date(ms).toLocaleTimeString("es-AR") : DASH; }
export function day(ms) { return isNum(ms) ? new Date(ms).toLocaleDateString("es-AR") : DASH; }
export function isoUtc(ms) { return isNum(ms) ? new Date(ms).toISOString().replace(".000Z", "Z") : ""; }
/** Fecha/hora local con el ISO UTC en el tooltip. */
export function when(ms, withSec = false) {
  return el("time", { datetime: isoUtc(ms), title: isoUtc(ms) + " (UTC)", class: "nowrap" }, withSec ? dtSec(ms) : dt(ms));
}
export const parseMs = (iso) => { const t = Date.parse(iso); return Number.isNaN(t) ? null : t; };
export const norm = (s) => String(s || "").replace(/[-/]/g, "").toUpperCase();
/** "SOLUSDT" o "SOL-USDT" -> "SOL-USDT" */
export function sym(s) {
  const n = norm(s);
  for (const q of ["USDT", "USDC", "USD"]) if (n.endsWith(q) && n.length > q.length) return n.slice(0, -q.length) + "-" + q;
  return n;
}
export const base = (s) => sym(s).split("-")[0];
export const shortId = (id) => String(id || "").slice(0, 8);

// ------------------------------------------------------------------ API

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ") : "HTTP " + status);
    this.status = status;
  }
}

/** GET JSON. params: objeto (se omiten null/""/undefined). */
export async function api(path, params, signal) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== null && v !== undefined && v !== "") qs.set(k, String(v));
  const url = path + (qs.toString() ? "?" + qs : "");
  const r = await fetch(url, { cache: "no-store", signal, headers: { Accept: "application/json" } });
  let body = null;
  try { body = await r.json(); } catch { /* cuerpo no JSON */ }
  if (!r.ok) throw new ApiError(r.status, body && body.detail !== undefined ? body.detail : "HTTP " + r.status);
  return body;
}

/** Mensaje para el usuario segun el codigo (contrato de API.md). */
export function errorBox(e, what = "los datos") {
  if (e && e.name === "AbortError") return null;
  const st = e && e.status;
  const msg = e && e.message ? e.message : String(e);
  if (st === 503) return el("div", { class: "note warn" }, el("b", {}, "No disponible: "), msg);
  if (st === 404) return el("div", { class: "note" }, el("b", {}, "No encontrado: "), msg);
  if (st === 400 || st === 422) return el("div", { class: "note bad" }, el("b", {}, "Parámetros inválidos: "), msg);
  return el("div", { class: "note bad" }, "No se pudieron cargar " + what + ": " + msg);
}

// ------------------------------------------------------------------ componentes

export function chip(text, kind = "", title) { return el("span", { class: "chip " + kind, title }, text); }

export const CYCLE_STATUS = {
  ran: ["ok", "corrió"], warmup: ["info", "calentando"], no_strategies: ["warn", "sin estrategias"],
  no_llm: ["warn", "sin LLM"], error: ["bad", "error"],
};
export const HYP_STATUS = { pending: ["info", "pendiente"], resolved: ["ok", "resuelta"], expired_no_data: ["warn", "sin datos de precio"] };
export const STRATEGY_STATUS = { pending: ["info", "registrada"], running: ["ok", "activa"], evaluated: ["", "cerrada"] };
export const OUTCOME = { hypothesis: ["ok", "abrió hipótesis"], no_hypothesis: ["", "no entró"], rejected: ["bad", "rechazada"] };
export const TONE = { positive: ["ok", "positivo"], negative: ["bad", "negativo"], mixed: ["warn", "mixto"], neutral: ["", "neutral"], none: ["", "sin noticias"] };
export const statusChip = (map, s) => { const [k, t] = map[s] || ["", s || DASH]; return chip(t, k); };

export function dirSpan(d) {
  const long = d === "LONG";
  return el("span", { class: long ? "buy" : "sell", style: { fontWeight: 650 } }, long ? "▲ LONG" : "▼ SHORT");
}
export function sideSpan(side) {
  if (!side) return DASH;
  const buy = String(side).toLowerCase() === "buy";
  return el("span", { class: buy ? "buy" : "sell" }, buy ? "compra" : "venta");
}
/** Numero con color por signo. */
export function signed(v, fmt = signedPct) {
  if (!isNum(v)) return DASH;
  const z = Math.abs(v) < 0.005;
  return el("span", { class: z ? "" : v > 0 ? "pos" : "neg" }, fmt(v));
}

export function verdictChip(h) {
  if (h.status === "pending") return h.overdue ? chip("vencida · esperando resolución", "warn") : chip("pendiente", "info");
  if (h.status === "expired_no_data" || h.win === null || h.win === undefined) return chip("sin dato de precio", "warn");
  return h.win ? chip("✓ acertó", "ok") : chip("✗ falló", "bad");
}

/** Tasa de acierto honesta: "acertó X % de N casos (IC 95 %: a–b %)". */
export function rateText(wins, n, ci) {
  if (!n) return "sin casos resueltos todavía";
  return "acertó " + frac(wins / n, 0) + " de " + int(n) + (n === 1 ? " caso" : " casos") +
    (ci ? " (IC 95 %: " + num(ci[0] * 100, 0) + "–" + num(ci[1] * 100, 0) + " %)" : "");
}
export function ciBar(rate, ci, title) {
  if (!isNum(rate) || !ci) return el("span", { class: "muted" }, DASH);
  return el("div", { class: "ci", title: title || "tasa " + frac(rate, 0) + " · IC 95 % " + frac(ci[0], 0) + "–" + frac(ci[1], 0), role: "img",
    "aria-label": "tasa " + frac(rate, 0) + ", intervalo de confianza 95 % de " + frac(ci[0], 0) + " a " + frac(ci[1], 0) },
    el("span", { class: "r", style: { left: ci[0] * 100 + "%", width: Math.max(0.5, (ci[1] - ci[0]) * 100) + "%" } }),
    el("span", { class: "p", style: { left: rate * 100 + "%" } }));
}
/** Brier contra su referencia (decir siempre la tasa observada). */
export function brierText(b, ref) {
  if (!isNum(b)) return el("span", { class: "muted" }, "Brier: sin casos");
  const better = isNum(ref) && b < ref;
  return el("span", {}, "Brier ", el("b", {}, fixed(b, 3)), " vs referencia ", fixed(ref, 3), " ",
    isNum(ref) ? chip(better ? "el confidence aporta" : "el confidence no aporta", better ? "ok" : "warn",
      "Referencia = Brier de decir siempre la tasa observada. Menor es mejor.") : null);
}

export function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const p of pairs) {
    if (!p) continue;
    const [k, v, cls] = p;
    dl.append(el("dt", {}, k), el("dd", { class: cls }, v));
  }
  return dl;
}

export function stat(label, value, foot, cls) {
  return el("div", { class: "stat" }, el("div", { class: "label" }, label), el("div", { class: "value " + (cls || "") }, value),
    foot ? el("div", { class: "foot" }, foot) : null);
}

export function card(title, hint, ...body) {
  return el("section", { class: "card" },
    title || hint ? el("div", { class: "card-head" }, title ? el("h2", {}, title) : el("span"), hint ? el("div", { class: "hint" }, hint) : null) : null,
    ...body);
}

/**
 * Tabla accesible con scroll propio.
 * cols: [{label, cls, title}] ; rows: [[celda | {v, cls}]] ; opts: {caption, empty, onRow(i), rowLabel(i)}
 */
export function table(cols, rows, opts = {}) {
  const t = el("table", {},
    opts.caption ? el("caption", {}, opts.caption) : null,
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { scope: "col", class: c.cls, title: c.title }, c.label)))));
  const tb = el("tbody");
  if (!rows.length) tb.append(el("tr", {}, el("td", { colspan: cols.length, class: "empty" }, opts.empty || "Sin datos.")));
  rows.forEach((r, i) => {
    const tr = el("tr", opts.onRow ? { class: "clickable" } : {});
    r.forEach((cell, j) => {
      const isObj = cell && typeof cell === "object" && !(cell instanceof Node) && "v" in cell;
      tr.append(el("td", { class: (isObj && cell.cls) || cols[j].cls }, isObj ? cell.v : cell));
    });
    if (opts.onRow) tr.addEventListener("click", (ev) => { if (!ev.target.closest("a,button,input,select")) opts.onRow(i); });
    tb.append(tr);
  });
  t.append(tb);
  return el("div", { class: "table-wrap" + (opts.scroll ? " scroll" : ""), tabindex: "0", role: "region", "aria-label": opts.label || opts.caption || "tabla" }, t);
}

export function pager(total, limit, offset, go) {
  const page = Math.floor(offset / limit) + 1, pages = Math.max(1, Math.ceil(total / limit));
  return el("nav", { class: "pager", "aria-label": "Paginación" },
    el("span", {}, total ? "Mostrando " + int(offset + 1) + "–" + int(Math.min(total, offset + limit)) + " de " + int(total) : "0 resultados"),
    el("span", { class: "row" },
      el("button", { class: "btn small", type: "button", disabled: offset <= 0, onclick: () => go(Math.max(0, offset - limit)) }, "← Anterior"),
      el("span", {}, "página " + page + " de " + pages),
      el("button", { class: "btn small", type: "button", disabled: offset + limit >= total, onclick: () => go(offset + limit) }, "Siguiente →")));
}

/** Botonera segmentada accesible (aria-pressed). */
export function seg(label, options, value, onPick) {
  return el("div", { class: "seg", role: "group", "aria-label": label },
    options.map(([v, text]) => el("button", { type: "button", "aria-pressed": String(v === value), onclick: () => onPick(v) }, text ?? v)));
}

export function field(label, control) { return el("label", { class: "field" }, el("span", {}, label), control); }
export function select(name, options, value) {
  return el("select", { name }, options.map(([v, t]) => {
    const o = el("option", { value: v }, t);
    if (String(v) === String(value ?? "")) o.selected = true;
    return o;
  }));
}

export const loading = (t = "Cargando…") => el("div", { class: "loading", role: "status" }, t);

/** Texto de terceros: escapado y con saltos de linea preservados. */
export const prose = (t, cls = "") => el("div", { class: "prose " + cls }, t ?? "");

export function confChip(c) { return isNum(c) ? chip("conf " + fixed(c, 2), "", "confidence del analista (0–1)") : null; }

/** Lee una salida de analista que puede venir como {confidence, text} o como string. */
export function analystParts(a) {
  if (a === null || a === undefined) return null;
  if (typeof a === "string") return { text: a };
  return a;
}

/** IC 95 % de Wilson (mismo calculo que el backend), para agregados que el cliente suma. */
export function wilson(k, n, z = 1.959964) {
  if (!n) return null;
  const p = k / n, den = 1 + (z * z) / n, c = (p + (z * z) / (2 * n)) / den;
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / den;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}

export const PROVIDER = { anthropic: "Anthropic (Claude)", google: "Gemini (Google)", gemini: "Gemini (Google)", openai: "OpenAI" };
export const providersText = (ps) => (ps || []).map((p) => PROVIDER[p] || p).join(" → ");

export function pageHead(title, sub, extra, crumbs) {
  return el("div", { class: "page-head" },
    el("div", {}, crumbs ? el("div", { class: "crumbs" }, crumbs) : null, el("h1", {}, title), sub ? el("div", { class: "sub" }, sub) : null),
    extra || null);
}

/** POST JSON de una accion (analizar, pedir al laboratorio). El header X-HeatSignal-Action es obligatorio (sin el, 403). */
export async function apiPost(path, payload, signal) {
  const r = await fetch(path, {
    method: "POST", cache: "no-store", signal,
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-HeatSignal-Action": "1" },
    body: JSON.stringify(payload || {}),
  });
  let body = null;
  try { body = await r.json(); } catch { /* sin cuerpo */ }
  if (!r.ok) {
    const err = new ApiError(r.status, body && body.detail !== undefined ? body.detail : "HTTP " + r.status);
    err.body = body;
    throw err;
  }
  return body;
}

/** 404/405 de un endpoint de accion que todavia no se desplego. */
export const notDeployed = (e) => e && (e.status === 405 || (e.status === 404 && e.message === "Not Found"));
