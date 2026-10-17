// Datos guardados: que hay en la base historica (/api/history/summary), explorador de barras propias (/api/history/bars:
// precio + delta de flujo + liquidaciones long/short) y tabla de liquidaciones (/api/history/liquidations) con totales.
// /api/history/bars es pesado para rangos largos: se pide solo al tocar "Ver", nunca se pollea.
import {
  el, clear, api, errorBox, when, int, usd, bytes, day, dur, price, funding, card, table, pageHead, loading, chip, kv, field, select, pager, seg, stat, isNum, ago,
} from "../core.js";
import { makeChart, disposeChart, css, toChartTime, pricePrecision, legend } from "../charts.js";
import { storageCard, coldNote } from "../storage-ui.js";

const TF = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "1h": 3600e3 };
const RANGES = [["6h", 6 * 3600e3, "6 h"], ["24h", 24 * 3600e3, "24 h"], ["3d", 3 * 86400e3, "3 días"], ["7d", 7 * 86400e3, "7 días"], ["30d", 30 * 86400e3, "30 días"], ["90d", 90 * 86400e3, "90 días"], ["180d", 180 * 86400e3, "180 días"]];
const EXCHANGES = [["", "combinado"], ["binance", "binance"], ["bybit", "bybit"], ["okx", "okx"], ["hyperliquid", "hyperliquid"]];
const MAX_BARS = 5000;

export async function mount(root, ctx) {
  const q = ctx.route.query;
  const symbols = ctx.symbols();
  const f = {
    symbol: symbols.includes(q.get("symbol")) ? q.get("symbol") : symbols[0] || "",
    tf: TF[q.get("tf")] ? q.get("tf") : "5m", exchange: q.get("exchange") || "", range: RANGES.some((r) => r[0] === q.get("range")) ? q.get("range") : "24h",
  };
  const lq = { side: "", min_usd: "", offset: 0 };
  const sumBox = el("div", {}, loading()), storeBox = el("div"), ctrl = el("div"), chartNode = el("div", { class: "chart tall", role: "img", "aria-label": "Barras guardadas" });
  const barsMsg = el("div"), barsStats = el("div", { class: "stats", style: { marginBottom: "12px" } }), liqCtrl = el("div"), liqBox = el("div");
  const st = { chart: null, series: {} };

  clear(root,
    pageHead("Datos guardados", "Lo que la VM viene grabando minuto a minuto (base SQLite propia), con respaldo diario."),
    sumBox,
    storeBox,
    card("Explorador de barras", "agregados por minuto de la ingesta (no son velas de un exchange)", ctrl, barsMsg, barsStats, chartNode,
      legend([[css("--buy"), "vela alcista", true], [css("--sell"), "vela bajista", true], [css("--s1"), "delta de flujo taker (compra − venta)", true],
        [css("--s4"), "shorts liquidados (arriba)", true], [css("--s2"), "longs liquidados (abajo)", true]]),
      el("p", { class: "small muted", style: { marginTop: "6px" } }, "Hora local. Liquidaciones de Binance y OKX son muestreadas (parciales); \"combinado\" suma volúmenes y liquidaciones, y toma OHLC del exchange de referencia (binance > bybit > okx > hyperliquid).")),
    card("Liquidaciones guardadas", "mismo símbolo, exchange y rango que el explorador", liqCtrl, liqBox));

  function drawCtrl() {
    clear(ctrl, el("div", { class: "filters" },
      field("Símbolo", (() => { const s = select("symbol", symbols.map((x) => [x, x]), f.symbol); s.onchange = () => { f.symbol = s.value; go(); }; return s; })()),
      el("div", { class: "field" }, el("span", {}, "Temporalidad"), seg("Temporalidad", Object.keys(TF).map((t) => [t, t]), f.tf, (v) => { f.tf = v; go(); })),
      el("div", { class: "field" }, el("span", {}, "Rango"), seg("Rango", RANGES.map(([k, , t]) => [k, t]), f.range, (v) => { f.range = v; go(); })),
      field("Exchange", (() => { const s = select("exchange", EXCHANGES, f.exchange); s.onchange = () => { f.exchange = s.value; go(); }; return s; })()),
      el("button", { class: "btn", type: "button", onclick: () => go() }, "Actualizar")));
  }
  const go = () => { ctx.replaceQuery(f); drawCtrl(); lq.offset = 0; loadBars(); loadLiqs(); };

  function rangeMs() {
    const until = Date.now(), r = RANGES.find((x) => x[0] === f.range)[1];
    return { since: until - r, until };
  }

  // ---------------------------------------------------------------- resumen
  async function loadSummary() {
    try {
      const d = await api("/api/history/summary", null, ctx.signal);
      const s = d.store, r = d.recorder;
      const stale = isNum(s.bars_1m.last_ms) && Date.now() - s.bars_1m.last_ms > 180e3;
      clear(sumBox,
        el("div", { class: "stats" },
          stat("Barras de 1 min", int(s.bars_1m.rows), "desde " + day(s.bars_1m.first_ms)),
          stat("Último minuto guardado", isNum(s.bars_1m.last_ms) ? ago(s.bars_1m.last_ms) : "—", stale ? "la grabación está atrasada" : "al día", stale ? "bad-t" : ""),
          stat("Liquidaciones", int(s.liquidations.rows), "última " + (isNum(s.liquidations.last_ms) ? ago(s.liquidations.last_ms) : "—")),
          stat("Noticias", int(s.news.rows), "última " + (isNum(s.news.last_ms) ? ago(s.news.last_ms) : "—")),
          stat("Tamaño de la base", bytes(s.size_bytes), el("span", { title: s.path }, String(s.path || "").split("/").pop())),
          stat("Respaldo", d.last_upload ? ago(d.last_upload.ts_ms) : "nunca", d.last_upload ? "a " + d.last_upload.remote + " · " + int(s.export_days) + " días exportados" : int(s.export_days) + " días exportados · último " + (s.last_export_day || "—"))),
        el("div", { class: "grid two", style: { marginTop: "16px" } },
          card("Velas históricas descargadas", "Binance 1m; 5m–1h se derivan exactas",
            table([{ label: "Exchange" }, { label: "Símbolo" }, { label: "Intervalo" }, { label: "Filas", cls: "n" }, { label: "Desde" }, { label: "Hasta" }],
              (s.klines || []).map((k) => [k.exchange, k.symbol, k.interval, int(k.rows), day(k.first_ms), day(k.last_ms)]), { empty: "Todavía no se descargaron.", label: "Velas históricas" })),
          card("Grabador", "resumen con " + dur(d.summary_age_s) + " de antigüedad (cache 30 s)",
            kv([["Eventos procesados", int(r.events)], ["Barras escritas", int(r.bars_written)], ["Liquidaciones escritas", int(r.liquidations_written)],
              ["Eventos tardíos (fusionados)", int(r.late_events)], ["Errores de escritura", int(r.write_errors), r.write_errors ? "bad-t" : ""],
              ["Último flush", isNum(r.last_flush_ms) ? when(r.last_flush_ms, true) : "—"], ["Último día exportado", s.last_export_day || "—"]]))));
    } catch (e) { const b = errorBox(e, "el resumen histórico"); if (b) clear(sumBox, b); }
  }

  // ---------------------------------------------------------------- caliente (VM) vs frio (Drive)
  async function loadStorage() {
    try { clear(storeBox, storageCard(await api("/api/history/storage", null, ctx.signal))); }
    catch (e) { const b = errorBox(e, "el estado del almacenamiento"); if (b) clear(storeBox, b); }
  }

  // ---------------------------------------------------------------- barras
  function ensureChart() {
    if (st.chart) return true;
    st.chart = makeChart(chartNode, { rightPriceScale: { scaleMargins: { top: 0.04, bottom: 0.42 } } }, () => {
      const s = st.series; if (!s.price) return;
      const up = css("--buy"), dn = css("--sell");
      s.price.applyOptions({ upColor: up, downColor: dn, borderUpColor: up, borderDownColor: dn, wickUpColor: up, wickDownColor: dn });
      s.delta.applyOptions({ color: css("--s1") });
      s.liqS.applyOptions({ color: css("--s4") }); s.liqL.applyOptions({ color: css("--s2") });
    });
    if (!st.chart) return false;
    st.series.price = st.chart.addCandlestickSeries({ priceLineVisible: false });
    st.series.delta = st.chart.addHistogramSeries({ priceScaleId: "delta", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    st.chart.priceScale("delta").applyOptions({ scaleMargins: { top: 0.62, bottom: 0.21 } });
    st.series.liqS = st.chart.addHistogramSeries({ priceScaleId: "liq", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    st.series.liqL = st.chart.addHistogramSeries({ priceScaleId: "liq", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
    st.chart.priceScale("liq").applyOptions({ scaleMargins: { top: 0.81, bottom: 0.0 } });
    st.chart._retheme();
    return true;
  }

  let seq = 0;
  async function loadBars() {
    const { since, until } = rangeMs();
    const n = Math.ceil((until - since) / TF[f.tf]);
    if (n > MAX_BARS) {
      clear(barsMsg, el("div", { class: "note warn" }, `Ese rango pide ${int(n)} barras de ${f.tf} (tope ${int(MAX_BARS)} por exchange). Subí la temporalidad o acortá el rango.`));
      clear(barsStats);
      return;
    }
    if (!ensureChart()) return;
    const my = ++seq;
    clear(barsMsg, loading("Consultando la base (puede tardar en rangos largos; si el rango está archivado, se baja de Drive)…"));
    try {
      const params = { symbol: f.symbol, tf: f.tf, since, until };
      if (f.exchange) params.exchange = f.exchange; else params.combine = "true";
      const t0 = performance.now();
      const d = await api("/api/history/bars", params, ctx.signal);
      if (my !== seq) return;
      const bars = d.bars.filter((b) => isNum(b.open) && isNum(b.close));
      const prec = pricePrecision(bars.length ? bars[bars.length - 1].close : 1);
      st.series.price.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: Math.pow(10, -prec) } });
      st.series.price.setData(bars.map((b) => ({ time: toChartTime(b.t_ms), open: b.open, high: b.high, low: b.low, close: b.close })));
      st.series.delta.setData(d.bars.map((b) => ({ time: toChartTime(b.t_ms), value: b.delta_usd || 0, color: (b.delta_usd || 0) >= 0 ? css("--s1") : css("--faint") })));
      st.series.liqS.setData(d.bars.map((b) => ({ time: toChartTime(b.t_ms), value: b.liq_short_usd || 0 })));
      st.series.liqL.setData(d.bars.map((b) => ({ time: toChartTime(b.t_ms), value: -(b.liq_long_usd || 0) })));
      st.chart.timeScale().fitContent();
      const sum = (k) => d.bars.reduce((a, b) => a + (b[k] || 0), 0);
      // combinado: `minutes` suma los minutos con fila de cada exchange -> se compara contra minutos x exchanges
      const nEx = d.combined ? Math.max(1, (d.exchanges || []).length) : 1;
      const minutes = sum("minutes"), expected = d.bars.length ? Math.round((d.until_ms - d.since_ms) / 60e3) * nEx : 0;
      const last = d.bars[d.bars.length - 1] || {};
      const cover = expected ? minutes / expected : null;
      clear(barsStats,
        stat("Barras", int(d.count), (d.combined ? "combinado (ref. " + (last.ref_exchange || "—") + ")" : d.exchange) + " · " + d.tf),
        stat("Cobertura", isNum(cover) ? Math.round(cover * 100) + " %" : "—", int(minutes) + " de " + int(expected) + " minutos" + (nEx > 1 ? "·exchange" : "") + " con fila" + (cover !== null && cover < 0.98 ? " · hay huecos" : ""), cover !== null && cover < 0.98 ? "warn-t" : ""),
        stat("Compra / venta taker", usd(sum("buy_usd")) + " / " + usd(sum("sell_usd")), "delta " + usd(sum("delta_usd"))),
        stat("Liquidaciones", usd(sum("liq_long_usd") + sum("liq_short_usd")), "longs " + usd(sum("liq_long_usd")) + " · shorts " + usd(sum("liq_short_usd")) + " · n=" + int(sum("n_liqs"))),
        stat("Último cierre", price(last.close), "mark " + price(last.mark) + " · funding " + funding(last.funding_8h)),
        stat("Open interest", usd(last.oi_usd), d.combined ? int(last.oi_exchanges) + " exchanges lo informan" : "último de la barra"));
      clear(barsMsg, coldNote(d), el("p", { class: "small muted" }, `${new Date(d.since_ms).toLocaleString("es-AR")} → ${new Date(d.until_ms).toLocaleString("es-AR")} · consulta ${dur((performance.now() - t0) / 1000)}`,
        d.count ? "" : " · sin barras en ese rango"));
    } catch (e) { const b = errorBox(e, "las barras"); if (b && my === seq) clear(barsMsg, b); }
  }

  // ---------------------------------------------------------------- liquidaciones
  function drawLiqCtrl() {
    const sSide = select("side", [["", "ambas"], ["long", "longs liquidados"], ["short", "shorts liquidados"]], lq.side);
    const iMin = el("input", { type: "number", name: "min_usd", min: "0", step: "1000", value: lq.min_usd, placeholder: "0", style: { width: "120px" } });
    const form = el("form", { class: "filters" }, field("Posición", sSide), field("Mínimo USD", iMin), el("button", { class: "btn", type: "submit" }, "Filtrar"));
    form.onsubmit = (ev) => { ev.preventDefault(); lq.side = sSide.value; lq.min_usd = iMin.value; lq.offset = 0; loadLiqs(); };
    clear(liqCtrl, form);
  }
  async function loadLiqs() {
    clear(liqBox, loading());
    const { since, until } = rangeMs();
    try {
      const d = await api("/api/history/liquidations", { symbol: f.symbol, exchange: f.exchange, side: lq.side, min_usd: lq.min_usd, since, until, limit: 100, offset: lq.offset }, ctx.signal);
      clear(liqBox,
        coldNote(d),
        el("div", { class: "note warn" }, "Cobertura: Bybit es completa; Binance (≤ 1 por segundo por símbolo) y OKX son parciales. Los totales subestiman y no se comparan entre exchanges."),
        el("div", { class: "stats", style: { marginBottom: "12px" } },
          stat("Liquidaciones (filtro)", int(d.totals.n), "en todo el filtro, no solo la página"),
          stat("Longs liquidados", el("span", { class: "sell" }, usd(d.totals.long_usd))),
          stat("Shorts liquidados", el("span", { class: "buy" }, usd(d.totals.short_usd)))),
        table([{ label: "Hora" }, { label: "Exchange" }, { label: "Símbolo" }, { label: "Liquidado" }, { label: "Precio", cls: "n" }, { label: "Cantidad", cls: "n" }, { label: "USD", cls: "n" }, { label: "Cobertura" }],
          d.items.map((l) => [when(l.ts_ms, true), l.exchange, l.symbol, el("span", { class: l.position === "long" ? "sell" : "buy" }, l.position), price(l.price), price(l.qty), usd(l.notional_usd),
            l.coverage === "partial" ? chip("parcial", "warn") : chip("completa", "ok")]), { empty: "Sin liquidaciones guardadas con ese filtro.", label: "Liquidaciones guardadas", scroll: true }),
        pager(d.total, 100, lq.offset, (o) => { lq.offset = o; loadLiqs(); }));
    } catch (e) { const b = errorBox(e, "las liquidaciones"); if (b) clear(liqBox, b); }
  }

  drawCtrl();
  drawLiqCtrl();
  if (!f.symbol) { clear(barsMsg, el("p", { class: "empty" }, "No hay símbolos configurados.")); }
  await Promise.all([loadSummary(), loadStorage(), f.symbol ? loadBars() : null, f.symbol ? loadLiqs() : null]);
  return { unmount: () => disposeChart(st.chart), refresh: () => Promise.all([loadSummary(), loadStorage()]) };
}
