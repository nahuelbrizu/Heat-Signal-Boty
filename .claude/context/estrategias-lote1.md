# Lote 1 de estrategias: specs para aprobar (PROPUESTA, no registradas)

Generado desde `backend/src/heatsignal/lab/strategies.py`. El `spec_hash` identifica cada versión: si cambia una
coma de la regla o de los parámetros, es otra estrategia (`-v2`). La **grilla** es todo lo que el walk-forward puede
elegir; cada combinación cuenta como un intento para el Deflated Sharpe.

## Cómo se evalúa (igual para todas)

- **Historia:** Binance USDS-M desde el lanzamiento de cada perpetuo, con los 7 símbolos.
- **Holdout:** los **últimos 3 meses** quedan sellados.
- **Ejecución:** al open de la vela 1m siguiente a la decisión.
- **Costos:** comisión 5 bps + slippage 2 bps por lado, más el funding real liquidado (≈7 bps por lado).
- **Walk-forward:** 180 días de entrenamiento y 30 de prueba, avanzando de a 30.
- **Para pasar a paper trading:** DSR ≥ 0.95, break-even ≥ 2.0× el costo esperado,
  le gana a la canasta y al buy & hold medio, y le gana a las entradas al azar (p ≤ 0.05).

## `donchian-ensemble-v1` · continuacion

- **Regla:** Ensemble de canales Donchian en 4h con tamano por volatilidad; long.
- **Timeframe / dirección / horizonte:** 4h · long · dias
- **Parámetros congelados:** `{'timeframe': '4h', 'channels': [20, 40, 80, 160], 'vol_window': 180, 'target_vol': 0.2, 'max_leverage': 1.0}`
- **Grilla de calibración:** `{'target_vol': [0.15, 0.2, 0.3]}`
- **Datos:** klines
- **Fuente:** Zarattini, Pagani & Barbon 2025 (SFI RP 25-80)
- **spec_hash:** `86061813c7d1691c`

## `flujo-apertura-hora-v1` · continuacion

- **Regla:** Desbalance taker de los minutos :00-:01 > p95 trailing -> a favor 4 h.
- **Timeframe / dirección / horizonte:** 1m · both · 4h
- **Parámetros congelados:** `{'open_minutes': 2, 'pct': 0.95, 'pct_window_hours': 720, 'hold_hours': 4}`
- **Grilla de calibración:** `{'pct': [0.95, 0.99]}`
- **Datos:** klines
- **Fuente:** Kim & Hansen 2026 (arXiv:2607.09426)
- **spec_hash:** `676f9a08f9af3828`

## `reversion-flujo-extremo-15m-v1` · reversion

- **Regla:** Vela 15m > k sigma con flujo extremo a favor -> en contra 1 h.
- **Timeframe / dirección / horizonte:** 15min · both · 1h
- **Parámetros congelados:** `{'timeframe': '15min', 'vol_window': 2880, 'k_sigma': 2.5, 'imb_pct': 0.97, 'hold': '1h'}`
- **Grilla de calibración:** `{'k_sigma': [2.0, 2.5, 3.0], 'imb_pct': [0.95, 0.97, 0.99]}`
- **Datos:** klines
- **Fuente:** Kitron & Wengrowicz 2026 (arXiv:2608.21888): bruto ~1.3 bp, se espera que muera por costos
- **spec_hash:** `cd15e9623abe8bfa`

## `capitulacion-long-v1` · liquidaciones

- **Regla:** Proxy de cascada de longs (caida > 3 sigma, volumen y venta taker extremos) -> long.
- **Timeframe / dirección / horizonte:** 15min · long · <=4h
- **Parámetros congelados:** `{'vol_window': 2880, 'k_sigma': 3.0, 'volume_pct': 0.99, 'imb_pct': 0.98, 'tp_retrace': 0.5, 'sl_sigma': 0.5, 'max_hold': '4h'}`
- **Grilla de calibración:** `ninguna (sin calibrar)`
- **Datos:** klines
- **Fuente:** He, Wang & Zhang (SSRN 6185958); evidencia debil (arXiv:2607.27070)
- **spec_hash:** `3717fbb833f28a96`

## `squeeze-short-v1` · liquidaciones

- **Regla:** Espejo: suba > 3 sigma con compra taker y volumen extremos -> short.
- **Timeframe / dirección / horizonte:** 15min · short · <=4h
- **Parámetros congelados:** `{'vol_window': 2880, 'k_sigma': 3.0, 'volume_pct': 0.99, 'imb_pct': 0.98, 'tp_retrace': 0.5, 'sl_sigma': 0.5, 'max_hold': '4h'}`
- **Grilla de calibración:** `ninguna (sin calibrar)`
- **Datos:** klines
- **Fuente:** espejo de capitulacion-long-v1
- **spec_hash:** `f146c3d219bc056b`

## `funding-crowding-xs-v1` · carry

- **Regla:** En cada liquidacion: long al menor z-score de funding (14 d), short al mayor.
- **Timeframe / dirección / horizonte:** 8h · neutral · 24h
- **Parámetros congelados:** `{'z_window': 42, 'hold_hours': 24, 'min_symbols': 5}`
- **Grilla de calibración:** `{'hold_hours': [8, 24]}`
- **Datos:** klines, funding
- **Fuente:** ML4T caso crypto perps (IC -0.035); BIS WP 1087
- **spec_hash:** `4aa52e735fc55460`

## `funding-crowding-short-v1` · carry

- **Regla:** Funding > p95 (90 d) + OI en contratos 24 h > p80 + quiebre de minimos -> short 24 h.
- **Timeframe / dirección / horizonte:** 1h · short · 24h
- **Parámetros congelados:** `{'funding_window': 270, 'funding_pct': 0.95, 'oi_window_hours': 2160, 'oi_pct': 0.8, 'hold_hours': 24, 'sl_sigma': 2.0}`
- **Grilla de calibración:** `ninguna (sin calibrar)`
- **Datos:** klines, funding, metrics
- **Fuente:** BIS WP 1087 (carry y riesgo de caida); BitMEX: funding extremo solo no predice
- **spec_hash:** `38869de0fc4bc760`

## `funding-spread-hl-cex-v1` · cross-exchange

- **Regla:** Funding de Hyperliquid vs mediana CEX > p95 -> delta-neutral hasta 72 h.
- **Timeframe / dirección / horizonte:** 1h · neutral · <=72h
- **Parámetros congelados:** `{'pct': 0.95, 'exit_pct': 0.5, 'max_hold_hours': 72}`
- **Grilla de calibración:** `ninguna (sin calibrar)`
- **Datos:** bars_1m multi-exchange (grabacion propia) → **solo forward**: se evalúa con lo que graba la VM
- **Fuente:** BitMEX blog (Hyperliquid funding); sin paper academico
- **spec_hash:** `09cb4efe470c9fef`

## `absorcion-flujo-v1` · control

- **Regla:** CONTROL: OFI 1h > 0.25 con precio cayendo -> short 1 h (y simetrico). Se espera que FALLE neto de costos.
- **Timeframe / dirección / horizonte:** 5min · both · 1h
- **Parámetros congelados:** `{'ofi': 0.25}`
- **Grilla de calibración:** `ninguna (sin calibrar)`
- **Datos:** klines
- **Fuente:** ML4T cap. 18 (cost cliff)
- **spec_hash:** `a3768ae5a4adaadd`

