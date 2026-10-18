// Vista de prueba para render_view.mjs: renderiza los componentes de exits-ui.js (ficha, atribución, Freqtrade,
// MAE/MFE de hipótesis) con los datos de la API. Solo para tests (no se sirve en el panel).
import { exitChips, exitsCard, ftExitsBlock, excursionCard, excursionSummaryCard, reasonBars } from "../../src/heatsignal/api/static/app/exits-ui.js";
import { el } from "../../src/heatsignal/api/static/app/core.js";

export function renderOverview(d) {
  return [exitChips(d.ficha), exitsCard(d.ficha, d.salidas), ftExitsBlock(d.ft, d.ft_reasons), reasonBars(d.counts),
    excursionSummaryCard(d.summary)].filter(Boolean);
}

export function renderDetail(h) {
  return el("div", {}, excursionCard(h));
}
