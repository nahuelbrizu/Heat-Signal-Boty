// HeatSignal panel: single page app sin build ni CDN. Router por hash (deep-links compartibles), polling de
// /api/summary cada 5 s (lo unico que se pollea siempre), recarga automatica si cambia `build` (deploy/reinicio)
// y refresco al volver a la pestaña. Cada vista vive en views/*.js y se carga a demanda (import dinamico).
import { el, clear, api, dur, timeOnly, int, errorBox, loading } from "./core.js";
import { computeAlerts } from "./alerts.js";

const POLL_MS = 5000;
const STALE_MS = 15000;

const VIEWS = {
  "": { title: "Resumen", mod: "resumen" },
  moneda: { title: "Moneda", mod: "moneda" },
  mercado: { title: "Mercado", mod: "mercado" },
  analisis: { title: "Análisis del grafo", mod: "analisis" },
  sesiones: { title: "Sesiones del día", mod: "sesiones" },
  ciclos: { title: "Ciclos", mod: "ciclos" },
  estrategias: { title: "Estrategias", mod: "estrategias" },
  hipotesis: { title: "Hipótesis", mod: "hipotesis" },
  lecciones: { title: "Lecciones", mod: "lecciones" },
  laboratorio: { title: "Laboratorio", mod: "laboratorio" },
  paper: { title: "Paper trading", mod: "paper" },
  paperbot: { title: "Bot paper", mod: "paperbot" },
  semanal: { title: "Revisión semanal", mod: "semanal" },
  macro: { title: "Macro y eventos", mod: "macro" },
  noticias: { title: "Noticias", mod: "noticias" },
  datos: { title: "Datos guardados", mod: "datos" },
  sistema: { title: "Sistema", mod: "sistema" },
};

const state = {
  build: null, summary: null, prevSummary: null, lastOk: 0, lastErr: null, system: null,
  view: null, viewKey: null, mountSeq: 0,
};

// ------------------------------------------------------------------ tema (auto / claro / oscuro)

const THEMES = [["auto", "Tema: automático"], ["light", "Tema: claro"], ["dark", "Tema: oscuro"]];
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const b = document.getElementById("theme-btn");
  if (b) { b.textContent = { auto: "◐", light: "☀", dark: "☾" }[t] || "◐"; b.title = (THEMES.find((x) => x[0] === t) || THEMES[0])[1]; b.setAttribute("aria-label", b.title); }
  window.dispatchEvent(new Event("hs-theme"));
}
function getTheme() {
  const q = new URLSearchParams(location.search).get("tema");  // ?tema=dark (capturas)
  if (q) return q;
  try { return localStorage.getItem("hs-theme") || "auto"; } catch { return "auto"; }
}

// ------------------------------------------------------------------ router

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || "", parts: parts.slice(1), query: new URLSearchParams(qs || "") };
}

export function href(name, parts = [], query) {
  let h = "#/" + [name, ...parts].filter((p) => p !== undefined && p !== null && p !== "").map(encodeURIComponent).join("/");
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== null && v !== undefined && v !== "") q.set(k, String(v));
    if (q.toString()) h += "?" + q;
  }
  return h;
}

export function navigate(h, { replace = false } = {}) {
  if (replace) { history.replaceState(null, "", h); route(); }
  else if (location.hash === h) { state.viewKey = null; route(); }
  else location.hash = h;
}

async function route() {
  const r = parseHash();
  const def = VIEWS[r.name];
  const main = document.getElementById("main");
  const key = location.hash;
  if (state.view && state.viewKey === key) return;
  unmountView();
  const seq = ++state.mountSeq;
  state.viewKey = key;
  markNav(r.name);
  document.getElementById("sidenav").classList.remove("open");
  document.getElementById("menu-btn").setAttribute("aria-expanded", "false");
  if (!def) {
    document.title = "No existe · HeatSignal";
    clear(main, el("div", { class: "card" }, el("h1", {}, "Esa vista no existe"), el("p", {}, el("a", { href: "#/" }, "Volver al resumen"))));
    return;
  }
  clear(main, loading());
  const ctrl = new AbortController();
  const timers = [];
  const ctx = {
    route: r, signal: ctrl.signal, href, navigate,
    get summary() { return state.summary; },
    get system() { return state.system; },
    symbols: () => symbols(),
    setTitle: (t) => { document.title = t + " · HeatSignal"; },
    /** Intervalo que se corta al salir de la vista y no corre con la pestaña oculta. */
    every(ms, fn) { timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); },
    /** Cambia la URL sin re-montar la vista (filtros, deep-link). parts opcional para fijar el sub-recurso. */
    replaceQuery(query, parts = r.parts) {
      const h = href(r.name, parts, query);
      history.replaceState(null, "", h);
      state.viewKey = location.hash;
    },
  };
  let mod;
  try {
    mod = await import("./views/" + def.mod + ".js");
  } catch (e) {
    if (seq === state.mountSeq) clear(main, errorBox(e, "la vista"));
    return;
  }
  if (seq !== state.mountSeq) return;
  ctx.setTitle(def.title);
  const root = el("div", { class: "view" });
  clear(main, root);
  state.view = { ctrl, timers, hooks: null };
  try {
    const hooks = await mod.mount(root, ctx);
    if (seq === state.mountSeq && state.view) state.view.hooks = hooks || null;
  } catch (e) {
    if (seq === state.mountSeq) root.append(errorBox(e));
  }
  if (seq === state.mountSeq) {
    const h1 = root.querySelector("h1");
    if (h1 && document.activeElement && document.activeElement.closest && document.activeElement.closest(".sidenav")) {
      h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true });
    }
  }
}

function unmountView() {
  const v = state.view;
  state.view = null;
  if (!v) return;
  v.ctrl.abort();
  v.timers.forEach(clearInterval);
  try { v.hooks && v.hooks.unmount && v.hooks.unmount(); } catch { /* nada */ }
}

function markNav(name) {
  for (const a of document.querySelectorAll(".sidenav a[data-view]")) {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

// ------------------------------------------------------------------ datos compartidos

export function symbols() {
  const cfg = state.system && state.system.config && state.system.config.symbols;
  const fromCfg = (cfg || []).map((s) => {
    const n = String(s).replace(/[-/]/g, "").toUpperCase();
    return n.endsWith("USDT") ? n.slice(0, -4) + "-USDT" : n;
  });
  const fromMem = Object.keys((state.summary && state.summary.memory && state.summary.memory.symbols) || {});
  return [...new Set([...fromCfg, ...fromMem])].sort();
}

async function loadSystem() {
  try { state.system = await api("/api/system"); } catch { /* el resumen igual funciona */ }
}

async function poll() {
  try {
    const s = await api("/api/summary");
    if (state.build && s.build !== state.build) { location.reload(); return; }  // deploy o reinicio: recargar
    const first = !state.build;
    state.build = s.build;
    state.prevSummary = state.summary;
    state.summary = s;
    state.lastOk = Date.now();
    state.lastErr = null;
    if (first) await loadSystem();
    renderChrome();
    const v = state.view;
    if (v && v.hooks && v.hooks.onSummary) {
      try { v.hooks.onSummary(s, state.prevSummary); } catch (e) { console.error(e); }
    }
  } catch (e) {
    state.lastErr = e.message || String(e);
    renderLive();
  }
}

// ------------------------------------------------------------------ cromo (encabezado, nav)

function renderLive() {
  const box = document.getElementById("live");
  const age = state.lastOk ? Date.now() - state.lastOk : null;
  let cls = "", text = "conectando…";
  if (age !== null && age <= STALE_MS) { cls = "ok"; text = "en vivo"; }
  else if (age !== null) { cls = "stale"; text = "datos atrasados " + dur(age / 1000); }
  else if (state.lastErr) { cls = "stale"; text = "sin conexión"; }
  const label = text + (state.lastErr ? " · " + state.lastErr : "");
  if (box.dataset.text !== text) {  // aria-live: solo anuncia cambios de estado, no cada segundo
    box.dataset.text = text;
    box.className = "live " + cls;
    clear(box, el("span", { class: "dot", "aria-hidden": "true" }), el("span", {}, text));
  }
  box.title = label;
}

function renderChrome() {
  renderLive();
  const s = state.summary;
  if (!s) return;
  document.getElementById("topmeta").textContent =
    "proceso arriba hace " + dur(s.uptime_s) + " · actualizado " + timeOnly(s.now_ms) + " · build " + s.build;
  const alerts = computeAlerts(s);
  const bad = alerts.filter((a) => a.level === "bad").length, warn = alerts.filter((a) => a.level === "warn").length;
  const c = s.counts || {};
  setBadge("", bad ? [String(bad), "bad", bad + " alertas"] : warn ? [String(warn), "warn", warn + " avisos"] : null);
  const cyc = c.cycles && c.cycles.by_status;
  setBadge("ciclos", cyc && cyc.error ? [int(cyc.error), "bad", cyc.error + " ciclos con error"] : c.cycles ? [int(c.cycles.total), "", "ciclos guardados"] : null);
  setBadge("hipotesis", c.hypotheses ? [int(c.hypotheses.total), c.hypotheses.due ? "warn" : "", c.hypotheses.pending + " pendientes"] : null);
  setBadge("estrategias", c.strategies ? [int(c.strategies.total), "", "estrategias registradas"] : null);
  setBadge("noticias", c.news_window !== null && c.news_window !== undefined ? [int(c.news_window), "", "noticias en la ventana de 48 h"] : null);
  const lab = s.lab;
  setBadge("laboratorio", lab && !lab.error ? [lab.passing + "/" + lab.strategies, lab.passing ? "" : "", "pasan / total"] : lab && lab.error ? ["!", "bad", lab.error] : null);
  const pp = s.paper;
  setBadge("paper", pp && pp.available ? [int(pp.open), "", pp.open + " operaciones abiertas en el dry-run"] : pp && pp.error ? ["!", "bad", pp.error] : null);
  const rec = s.recording || {};
  setBadge("datos", rec.status === "stale" ? ["!", "bad", "la grabación no escribe"] : rec.status === "off" ? ["off", "", "grabación apagada"] : null);
  const down = (s.workers || []).filter((w) => !w.connected).length;
  setBadge("mercado", down ? [down + " caído" + (down > 1 ? "s" : ""), "bad", "exchanges sin datos"] : null);
}

function setBadge(view, b) {
  const a = document.querySelector(`.sidenav a[data-view="${view}"] .badge`);
  if (!a) return;
  if (!b) { a.hidden = true; return; }
  a.hidden = false;
  a.textContent = b[0];
  a.className = "badge " + (b[1] || "");
  a.title = b[2] || "";
}

function buildChrome() {
  const nav = document.getElementById("sidenav");
  const ul = el("ul");
  for (const [name, def] of Object.entries(VIEWS)) {
    ul.append(el("li", {}, el("a", { href: "#/" + name, "data-view": name }, el("span", {}, def.title), el("span", { class: "badge", hidden: true }))));
  }
  clear(nav, ul, el("p", { class: "navnote" },
    "Herramienta personal de solo lectura. Todo lo que se ve es histórico: tasas con n e intervalo de confianza, nunca pronósticos."));
  const btn = document.getElementById("menu-btn");
  btn.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(open));
  });
  const tb = document.getElementById("theme-btn");
  tb.addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    const cur = (() => { try { return localStorage.getItem("hs-theme") || "auto"; } catch { return "auto"; } })();
    const next = order[(order.indexOf(cur) + 1) % order.length];
    try { localStorage.setItem("hs-theme", next); } catch { /* sin storage */ }
    applyTheme(next);
  });
  applyTheme(getTheme());
}

// ------------------------------------------------------------------ errores de JS: visibles, nunca silenciosos

function showJsError(msg) {
  let box = document.getElementById("js-errors");
  if (!box) {
    box = el("div", { id: "js-errors", class: "note bad", role: "alert", style: { position: "fixed", right: "16px", bottom: "16px", maxWidth: "min(520px, calc(100vw - 32px))", zIndex: 30 } },
      el("b", {}, "Error en el panel (recargá la página; si sigue, revisá la consola): "));
    document.body.append(box);
  }
  if (box.childNodes.length < 6) box.append(el("div", { class: "small mono js-error" }, String(msg).slice(0, 300)));
}
window.addEventListener("error", (e) => showJsError(e.message || e));
window.addEventListener("unhandledrejection", (e) => { if (!(e.reason && e.reason.name === "AbortError")) showJsError((e.reason && e.reason.message) || e.reason); });

// ------------------------------------------------------------------ arranque

buildChrome();
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  poll();  // el navegador frena los timers en segundo plano: al volver, refrescar ya
  const v = state.view;
  if (v && v.hooks && v.hooks.refresh) v.hooks.refresh();
});
setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
setInterval(renderLive, 1000);
poll().then(route);
