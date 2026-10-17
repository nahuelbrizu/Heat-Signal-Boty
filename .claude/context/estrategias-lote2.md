# Lote 2 de estrategias: specs para aprobar (PROPUESTA: pendiente de aprobación antes de correr)

**PROPUESTA: pendiente de aprobación antes de correr.** Ninguna de estas reglas se corrió contra datos reales
(ni `lab.run` ni `evaluate`): las specs se congelan y se aprueban ANTES de ver resultados. Solo se probaron con
datos sintéticos (`backend/tests/test_lab_strategies_lote2.py`: comportamiento y no look-ahead).

Generado desde `backend/src/heatsignal/lab/strategies.py` (`LOTE2`). El `spec_hash` identifica cada versión: si
cambia una coma de la regla o de los parámetros, es otra estrategia (`-v2`). La **grilla** es todo lo que el
walk-forward puede elegir; cada combinación cuenta como un intento para el Deflated Sharpe. Los hashes del lote 1
no cambiaron (lo verifica `test_lote1_hashes_unchanged`).

## Cómo se evalúa (igual que el lote 1)

- **Historia:** Binance USDS-M desde el lanzamiento de cada perpetuo, con los 7 símbolos (salvo que la spec
  restrinja el universo: `momentum-intradia-v1` solo BTC y ETH; `lunes-asia-v1` solo BTC). **Estas dos se corren
  con `--symbols BTCUSDT ETHUSDT` y `--symbols BTCUSDT`**: el simulador reparte el capital entre todos los
  símbolos de la señal, así que con los 7 el retorno y el DD quedarían diluidos (el Sharpe no cambia).
- **Holdout:** los **últimos 3 meses** quedan sellados.
- **Ejecución:** al open de la vela 1m siguiente a la decisión.
- **Costos:** comisión 5 bps + slippage 2 bps por lado, más el funding real liquidado (≈7 bps por lado).
- **Walk-forward:** 180 días de entrenamiento y 30 de prueba, avanzando de a 30.
- **Para pasar a paper trading:** DSR ≥ 0.95, break-even ≥ 2.0× el costo esperado,
  le gana a la canasta y al buy & hold medio, y le gana a las entradas al azar (p ≤ 0.05).
- **`lunes-asia-v1` está en modo observación:** aunque pase los criterios, no recibe capital.
- **Intentos del lote 2:** 9 combinaciones en total (se suman a las del lote 1 para el ajuste del programa).

## `compresion-breakout-v1` · continuacion

- **Regla:** Velas 1h. Compresion: ancho de Bollinger(20, 2) bajo su p10 trailing de 30 dias durante >= 6 velas seguidas. Entrada al cierre de la vela cuyo close rompe el rango (max/min) de esa racha, si el desbalance taker de la vela de ruptura va a favor y |imb| supera su p75 trailing de 30 dias. Stop en el lado opuesto del rango; salida a las 12 h. Long y short.
- **Timeframe / dirección / horizonte:** 1h · both · 12h
- **Parámetros congelados:** `{'bb_n': 20, 'bb_k': 2.0, 'width_pct': 0.1, 'width_window': 720, 'min_bars': 6, 'imb_pct': 0.75, 'hold_hours': 12}`
- **Grilla de calibración:** `{'width_pct': [0.1, 0.2], 'min_bars': [6, 12]}` (4 combinaciones)
- **Datos:** klines
- **Fuente:** catalogo-hipotesis.md 2.3 (compresion-breakout-v1: Bollinger comprimido + ruptura con flujo a favor, 12 h; riesgo MEDIO-ALTO); sin paper academico citado
- **spec_hash:** `a25d6babd6964b30`

## `momentum-intradia-v1` · continuacion

- **Regla:** Senal = retorno 00:00 -> 00:30 UTC (open de la vela 00:00 al close de la 00:29, dia completo). Si |senal| > p60 trailing de las ultimas 90 senales diarias, abrir en esa direccion a las 23:30 UTC del mismo dia y cerrar a las 00:00. Solo BTC y ETH (los demas sin senal). Sin filtro de volumen.
- **Timeframe / dirección / horizonte:** 1m · both · 30min
- **Parámetros congelados:** `{'symbols': ['BTCUSDT', 'ETHUSDT'], 'signal_minutes': 30, 'pct': 0.6, 'pct_window_days': 90, 'entry': '23h30min'}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines
- **Fuente:** catalogo-hipotesis.md 2.2 (momentum-intradia-volumen-v1: la primera media hora UTC predice la ultima; riesgo MEDIO-ALTO); esta version no usa el filtro de volumen
- **spec_hash:** `4776269a35a6edde`

## `oi-precio-continuacion-v1` · continuacion

- **Regla:** Velas 1h. ret_4h > 1 sigma_4h (desvio trailing de 30 dias), cambio del OI en CONTRATOS a 4 h > p80 trailing (90 dias) y desbalance taker de las ultimas 4 h > 0 -> long 8 h. Simetrico: ret_4h < -1 sigma, OI subiendo igual y desbalance < 0 -> short 8 h. metrics disponible en ts + 5m.
- **Timeframe / dirección / horizonte:** 1h · both · 8h
- **Parámetros congelados:** `{'k_sigma': 1.0, 'sigma_window': 720, 'oi_pct': 0.8, 'oi_window_hours': 2160, 'oi_max_age': '30min', 'hold_hours': 8}`
- **Grilla de calibración:** `{'k_sigma': [1.0, 1.5]}` (2 combinaciones)
- **Datos:** klines, metrics
- **Fuente:** catalogo-hipotesis.md 2.5 (oi-precio-continuacion-v1: precio, OI y compras subiendo -> a favor 8 h; riesgo ALTO); ML4T H6 (OI en contratos, 4 h -> 8 h; alli en version contraria)
- **spec_hash:** `df0fd44676eb37c7`

## `carry-xs-funding-v1` · carry

- **Regla:** En cada liquidacion de funding: ordenar los simbolos por el promedio de sus ultimas 3 tasas liquidadas; long a las 2 de menor funding y short a las 2 de mayor, mismo nocional, neutral. Se rebalancea solo si cambia la composicion. Minimo 5 simbolos con dato (tasa de hace <= 9 h); si no, no se cambia nada. Distinta de funding-crowding-xs-v1 (aca se cobra el carry, no se apuesta a la reversion).
- **Timeframe / dirección / horizonte:** 8h · neutral · hasta cambio de composicion
- **Parámetros congelados:** `{'lookback': 3, 'n_side': 2, 'min_symbols': 5, 'max_age_hours': 9}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines, funding
- **Fuente:** catalogo-hipotesis.md 3.1 (carry-xs-funding-v1: long las 2 de menor funding, short las 2 de mayor; riesgo MEDIO); BIS WP 1087 (Schmeling, Schrimpf, Todorov)
- **spec_hash:** `118bdbb71f36906a`

## `lunes-asia-v1` · estacionalidad

- **Regla:** MODO OBSERVACION (sin capital aunque pase los criterios): long BTC desde el domingo 23:00 UTC hasta el lunes 23:00 UTC, todas las semanas.
- **Timeframe / dirección / horizonte:** calendario · long · 24h
- **Parámetros congelados:** `{'symbols': ['BTCUSDT'], 'entry': '23h', 'hold_hours': 24, 'modo': 'observacion'}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines
- **Fuente:** catalogo-hipotesis.md 6.1 (lunes-asia-v1, estacionalidad; riesgo ALTO); Quantpedia (calendario). Efectos de calendario: debiles o desaparecidos tras costos
- **spec_hash:** `73cb513730c5c018`

