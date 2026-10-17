# Binance: historia gratis disponible (verificado el 2026-10-17)

Reporte de un agente de investigación. Lo verificó con pedidos reales sin API key y contra
developers.binance.com.

## Lo que hay

| Dato | Fuente | Historia | Notas |
|---|---|---|---|
| Funding | REST `/fapi/v1/fundingRate` (máx 1000 por pedido; 500 pedidos/5 min) o vision `monthly/fundingRate` | **Toda** (XRP desde 2020-01-06) | < 2 MB en total. Algunos `fundingTime` traen ms de offset: redondear |
| OI, long/short (top cuentas, top posiciones, global) y ratio taker, cada 5m | vision `daily/metrics` | BTC desde 2020-09, XRP desde 2021-12 | **El REST `/futures/data/*` solo cubre 30 días.** ~25 MB zip por símbolo. Llega desordenado y con ~1 día de retraso |
| Flujo taker buy/sell a 1m | **Ya lo tenemos**: columnas `taker_buy_*` de las klines 1m | Toda | Coincide exacto con `takerlongshortRatio` |
| Premium index / mark / index, klines 1m | REST `premiumIndexKlines`, `markPriceKlines`, `indexPriceKlines` (este usa `pair`), o vision | Toda | ~65 MB zip por símbolo. Guardando solo ts y close entran en ~20–30 MB |
| Liquidaciones USDS-M | **No existen** (`allForceOrders` da 404; `liquidationSnapshot` solo para COIN-M, discontinuado en 2024-10) | — | Solo lo que grabamos nosotros. El stream `forceOrder` manda **una por símbolo cada 1000 ms**, así que subestima el total |
| aggTrades / trades | vision | Toda | 11,8 GB XRP y 45 GB BTC: solo bajar de a días, agregar a minuto y borrar. El REST solo cubre 48 h |
| bookDepth (±1–5%) | vision daily | Desde 2023 | ~0,5 MB por día y por símbolo, opcional |

- **Formato de URL de vision:** `https://data.binance.vision/data/futures/um/{daily|monthly}/{dataset}/{SYM}/[{interval}/]{SYM}-...-{fecha}.zip`, más un `.CHECKSUM` (sha256).
- **Listado:** `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/daily/`.
- **Mapeo de columnas de `metrics`**, inferido comparando con el REST:
  - `count_toptrader_long_short_ratio` = top por cuentas;
  - `sum_toptrader_long_short_ratio` = top por posiciones;
  - `count_long_short_ratio` = global;
  - `sum_taker_long_short_vol_ratio` = taker.
- **No empalmar sin chequear:** metrics y el REST difieren levemente (son snapshots de instantes distintos).
- **Inicio de los perpetuos:** BTC 2019-09, ETH 2019-11, XRP 2020-01, TRX 2020-01, XLM 2020-01, BNB 2020-02, SOL 2020-09. Todos con funding cada 8 h.

## Trampas

- Todo está en UTC y en ms.
- En las klines y en el ratio taker, el timestamp es el **inicio** del período: tomar el dato como disponible recién en ts + período.
- `openInterestHist` omite los períodos sin trades.
- El filtro de tiempo de `takerlongshortRatio` se comporta raro: usar ventanas y deduplicar.
- Respetar los HTTP 429 y 418. El 418 es un ban de 2 minutos a 3 días.

## Agent OS / MCP

- Servidor MCP en `https://agent.binance.com/mcp/agentic`.
- Requiere **OAuth con cuenta de binance.com incluso para los datos públicos**.
- Opera desde una subcuenta "Agentic", pide confirmación en cada orden y no tiene permiso de retiro.
- No da historia masiva.
- **Veredicto:** no sirve para ingesta ni para backtest. Para operar es más controlable la API con key propia.

## Demo / testnet de futuros

- REST: `https://demo-fapi.binance.com`. WS: `wss://demo-fstream.binance.com`.
- La key se crea en "Demo Trading" de binance.com. Los pasos exactos no se verificaron.
- El saldo es virtual. **El libro no es el real**: sirve para probar la mecánica de las órdenes, no el slippage.

## Prioridad de descarga

1. Funding completo.
2. metrics 5m.
3. Premium index 1m (solo el close).
4. Verificar que las klines guardan `taker_buy_*`: sí, la columna `taker_buy_quote`.

Total estimado: ~0,5–1 GB ya convertido, bajando de a un mes y borrando los zip.
