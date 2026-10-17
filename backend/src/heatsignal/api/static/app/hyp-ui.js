// Tabla de hipotesis compartida (ciclos, estrategias, hipotesis).
import { el, table, when, dirSpan, fixed, price, verdictChip, signed } from "./core.js";

/** items: forma de /api/hypotheses. */
export function hypTable(items, ctx, opts = {}) {
  return table([{ label: "Creada" }, { label: "Símbolo" }, { label: "Dirección" }, { label: "Conf.", cls: "n" }, { label: "Entrada", cls: "n" },
    { label: "¿Acertó?" }, { label: "Precio al vencer", cls: "n" }, { label: "Mov. real", cls: "n", title: "cuánto se movió el precio, sin importar la dirección" },
    { label: "PnL neto", cls: "n" }, { label: "vs mantener", cls: "n", title: "PnL neto − buy & hold en la misma ventana" }],
  items.map((h) => [el("a", { href: ctx.href("hipotesis", [h.hypothesis_id]) }, when(h.created_ms)), h.symbol, dirSpan(h.direction), fixed(h.confidence, 2),
    price(h.entry_price), verdictChip(h), price(h.exit_price), signed(h.real_move_pct), signed(h.pnl_pct), signed(h.alpha_pct)]),
  { empty: opts.empty || "Ninguna.", label: "Hipótesis", onRow: (i) => ctx.navigate(ctx.href("hipotesis", [items[i].hypothesis_id])) });
}
