// Grafico de velas de Binance (/api/klines, pedidas en el momento y no guardadas) con marcas de hipotesis
// (/api/hypotheses del simbolo) y lineas de clusters de liquidacion (features en vivo). Lo usan Mercado y Moneda.
import { el, clear, api, card, seg, num, usd, signedPct, isNum, norm } from "./core.js";
import { makeChart, disposeChart, css, toChartTime, pricePrecision, legend } from "./charts.js";

export const INTERVALS = [["15m", 500], ["1h", 500], ["4h", 500], ["1d", 500], ["1w", 300]];

/**
 * Devuelve {node, reload(fit), reloadHyps(), setFeatures(fs), setLevels(levels), dispose()}.
 * Opcionales: `source(interval)` -> Promise<[{t (s UTC), o, h, l, c}]> reemplaza /api/klines (p.ej. nuestras velas de 1h);
 * `intervals` acota el selector (con uno solo no se muestra); `note` reemplaza el pie; `legendItems` la leyenda.
 */
export function candlePanel(ctx, symbol, { interval = "1h", onInterval, tall = true, title = "Velas", source = null,
  intervals = INTERVALS, note = null, legendItems = null, small = false } = {}) {
  const st = { candles: [], hyps: [], features: null, levels: [], extra: [], chart: null, series: null, lines: [], seq: 0, interval };
  const chartBox = el("div", { class: "chart" + (tall ? " tall" : "") + (small ? " short" : ""), role: "img", "aria-label": "Gráfico de velas de " + symbol });
  const chartMsg = el("span");
  const intSeg = el("span");
  const drawInt = () => clear(intSeg, intervals.length < 2 ? null : seg("Intervalo de las velas", intervals.map(([i]) => [i, i]), st.interval, (v) => {
    st.interval = v; if (onInterval) onInterval(v); drawInt(); reload(true);
  }));
  drawInt();

  const node = card(title, el("span", { class: "row" }, intSeg, el("button", { class: "btn small", type: "button", onclick: () => reload(false) }, "Actualizar velas")),
    chartBox,
    legend(legendItems || [[css("--buy"), "▲ hipótesis LONG"], [css("--sell"), "▼ hipótesis SHORT"], [css("--faint"), "● resultado al vencer (PnL neto)"],
      [css("--s2"), "línea punteada: entrada de las pendientes"], [css("--s3"), "líneas finas: clusters de liquidaciones de la ventana (top 5)"]]),
    el("p", { class: "small muted", style: { marginTop: "6px" } }, note || "Velas de Binance Futures (último precio), hora local; se piden al abrir y no se guardan. ", chartMsg));

  function ensureChart() {
    if (st.chart) return true;
    st.chart = makeChart(chartBox, {}, () => {
      if (!st.series) return;
      const up = css("--buy"), dn = css("--sell");
      st.series.applyOptions({ upColor: up, downColor: dn, borderUpColor: up, borderDownColor: dn, wickUpColor: up, wickDownColor: dn });
      drawOverlays();
    });
    if (!st.chart) return false;
    st.series = st.chart.addCandlestickSeries({});
    st.chart._retheme();
    return true;
  }

  async function reload(fit) {
    if (!ensureChart()) return;
    const seq = ++st.seq;
    const limit = (intervals.find(([i]) => i === st.interval) || [0, 500])[1];
    chartMsg.textContent = "Cargando…";
    try {
      const raw = source ? await source(st.interval) : (await api("/api/klines", { symbol: norm(symbol), interval: st.interval, limit }, ctx.signal)).candles;
      if (seq !== st.seq) return;
      st.candles = raw.map((k) => ({ ...k, ct: toChartTime(k.t * 1000) }));
      const last = st.candles[st.candles.length - 1];
      const prec = pricePrecision(last && last.c);
      st.series.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: Math.pow(10, -prec) } });
      st.series.setData(st.candles.map((k) => ({ time: k.ct, open: k.o, high: k.h, low: k.l, close: k.c })));
      if (fit) st.chart.timeScale().fitContent();
      chartMsg.textContent = "Actualizado " + new Date().toLocaleTimeString("es-AR") + ".";
      drawOverlays();
    } catch (e) {
      if (e.name !== "AbortError" && seq === st.seq) clear(chartMsg, el("span", { class: "err" }, "No se pudieron traer las velas: " + e.message));
    }
  }

  function snap(ms) {
    const cs = st.candles, t = toChartTime(ms);
    if (!cs.length || t < cs[0].ct) return null;
    let lo = 0, hi = cs.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (cs[mid].ct <= t) lo = mid; else hi = mid - 1; }
    return cs[lo].ct;
  }

  function drawOverlays() {
    if (!st.series || !st.candles.length) return;
    for (const l of st.lines) st.series.removePriceLine(l);
    st.lines = [];
    const up = css("--buy"), dn = css("--sell"), faint = css("--faint");
    const markers = [];
    for (const h of st.hyps) {
      const isLong = h.direction === "LONG";
      const t = snap(h.created_ms);
      if (t !== null) markers.push({ time: t, position: isLong ? "belowBar" : "aboveBar", color: isLong ? up : dn,
        shape: isLong ? "arrowUp" : "arrowDown", text: (isLong ? "L " : "S ") + num(h.confidence, 2) });
      if (h.status === "pending") {
        st.lines.push(st.series.createPriceLine({ price: h.entry_price, color: css("--s2"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "entrada " + h.direction }));
      } else if (h.status === "resolved" && isNum(h.resolved_ms)) {
        const te = snap(h.due_ms);
        if (te !== null) markers.push({ time: te, position: "inBar", color: h.win ? up : h.win === false ? dn : faint, shape: "circle", text: signedPct(h.pnl_pct, 1) });
      }
    }
    for (const x of st.extra) {
      const t = snap(x.ms);
      if (t !== null) markers.push({ time: t, position: x.position || "aboveBar", color: x.color || css("--s4"), shape: x.shape || "square", text: x.text || "" });
    }
    if (markers.length > 30) for (const mk of markers) mk.text = "";  // con muchas marcas el texto tapa las velas
    markers.sort((a, b) => a.time - b.time);
    st.series.setMarkers(markers);
    for (const lv of st.levels) {
      if (isNum(lv.price)) st.lines.push(st.series.createPriceLine({ price: lv.price, color: lv.color || css("--s1"), lineWidth: 1, lineStyle: lv.style ?? 2, axisLabelVisible: true, title: lv.title || "" }));
    }
    const fs = st.features;
    if (fs && fs.heatmap) {
      for (const cl of [...fs.heatmap.clusters].sort((a, b) => b.notional_usd - a.notional_usd).slice(0, 5)) {
        st.lines.push(st.series.createPriceLine({ price: cl.price_mid, color: css("--s3"), lineWidth: 1, lineStyle: 0, axisLabelVisible: false,
          title: "liq " + (String(cl.dominant_side).toLowerCase() === "sell" ? "longs " : "shorts ") + usd(cl.notional_usd) }));
      }
    }
  }

  async function reloadHyps() {
    try { st.hyps = (await api("/api/hypotheses", { symbol, limit: 500 }, ctx.signal)).items; } catch { st.hyps = []; }
    drawOverlays();
  }

  return {
    node, reload, reloadHyps,
    setFeatures(fs) { st.features = fs; drawOverlays(); },
    setLevels(levels) { st.levels = levels || []; drawOverlays(); },
    setExtraMarkers(list) { st.extra = list || []; drawOverlays(); },
    dispose() { disposeChart(st.chart); },
  };
}
