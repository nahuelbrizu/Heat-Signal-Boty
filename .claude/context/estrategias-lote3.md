# Lote 3 de estrategias: specs para aprobar (PROPUESTA: pendiente de aprobación antes de correr)

**PROPUESTA: pendiente de aprobación antes de correr.** Ninguna de estas reglas se corrió contra datos reales
(ni `lab.run` ni `evaluate`): las specs se congelan y se aprueban ANTES de ver resultados. Solo se probaron con
datos sintéticos (`backend/tests/test_lab_strategies_lote3.py`: comportamiento, no look-ahead y, para el par
ETH/BTC, PnL de un spread que revierte en `lab/sim.py`).

Generado desde `backend/src/heatsignal/lab/strategies.py` (`LOTE3`). El `spec_hash` identifica cada versión: si
cambia una coma de la regla o de los parámetros, es otra estrategia (`-v2`). La **grilla** es todo lo que el
walk-forward puede elegir; cada combinación cuenta como un intento para el Deflated Sharpe. Los hashes de los
lotes 1 y 2 no cambiaron (lo verifica `test_lote1_and_lote2_hashes_unchanged`).

## Cómo se evalúa (igual que los lotes 1 y 2)

- **Historia:** Binance USDS-M desde el lanzamiento de cada perpetuo. **Holdout:** los últimos 3 meses, sellados.
- **Ejecución:** al open de la vela 1m siguiente a la decisión. **Costos:** comisión 5 bps + slippage 2 bps por
  lado, más el funding real liquidado.
- **Walk-forward:** 180 días de entrenamiento y 30 de prueba, avanzando de a 30.
- **Para pasar a paper trading:** DSR ≥ 0.95, break-even ≥ 2.0× el costo esperado, le gana a la canasta y al
  buy & hold medio, y le gana a las entradas al azar (p ≤ 0.05).
- **Intentos del lote 3:** 8 combinaciones (tsmom 2, low-vol 1, pares 2, rango 1, reversion-xs 1, prepago 1).
  **El programa pasa de K = 36 (29 combinaciones del lab + 7 del benchmark de Freqtrade) a K = 44**: el DSR del programa se calcula con K = 44.

## Convención de pesos (estrategias de 2 patas)

El simulador recibe una señal por símbolo; el valor es la fracción del capital **asignado a ese símbolo**
(`allocation="equal"`: capital / n_símbolos). Como todos los símbolos reciben la misma asignación, la relación
entre patas se conserva con cualquier universo: ETH con peso −w y BTC con +β·w tienen nocionales en relación
1 : β (beta-neutral). Con w = 1/max(1, β) ninguna pata pasa de 1×. Verificado en
`test_pares_spread_reversion_is_profitable_in_simulator`. En `reversion-xs-1h-v1` la pata BTC es la **suma** de
las coberturas abiertas de todas las alts (puede llegar a ~6× la asignación de BTC si las 6 alts disparan juntas).

## Con qué `--symbols` correr cada una

El simulador reparte el capital entre todos los símbolos de la señal: si se corre una estrategia de universo
restringido con los 7, el retorno y el DD quedan diluidos (el Sharpe no cambia), y la canasta / buy & hold de
comparación se arman con esos símbolos.

| Estrategia | `--symbols` |
|---|---|
| `tsmom-diario-v1` | los 7 (default de `lab.run`, sin `--symbols`) |
| `low-vol-xs-v1` | los 7 (necesita >= 5 con dato; con menos queda plana) |
| `pares-eth-btc-v1` | `--symbols ETHUSDT,BTCUSDT` |
| `rango-reversion-v1` | `--symbols BNBUSDT,TRXUSDT,XLMUSDT` |
| `reversion-xs-1h-v1` | los 7 (las 6 alts + BTC, que es la pata de cobertura) |
| `funding-prepago-v1` | los 7 |

## `tsmom-diario-v1` · continuacion

- **Regla:** Momentum de serie de tiempo. Decision diaria a las 00:00 UTC con velas 1h: posicion = signo del retorno de 7 dias (de t-169h a t-1h: se salta la ultima hora) * (vol objetivo / vol realizada de 30 dias con retornos 1h, anualizada), tope 1x. Solo se rebalancea si cambia el signo o si el peso cambia mas de 20% relativo al actual. Long y short, los 7 simbolos.
- **Timeframe / dirección / horizonte:** 1h · both · dias
- **Parámetros congelados:** `{'symbols': ['BTCUSDT', 'SOLUSDT', 'XRPUSDT', 'ETHUSDT', 'XLMUSDT', 'BNBUSDT', 'TRXUSDT'], 'lookback_hours': 168, 'skip_hours': 1, 'vol_window_hours': 720, 'vol_min_frac': 0.9, 'target_vol': 0.15, 'max_leverage': 1.0, 'rebalance_threshold': 0.2}`
- **Grilla de calibración:** `{'target_vol': [0.15, 0.25]}` (2 combinaciones)
- **Datos:** klines
- **Fuente:** ML4T H7 (research/ml4t-metodologia.md: signo del retorno 7d x sigma objetivo / RV 30d, diario; riesgo MEDIO); Moskowitz, Ooi & Pedersen 2012 (JFE, Time series momentum); catalogo-hipotesis.md: momentum de serie temporal con respaldo academico
- **Correr con:** los 7 (default de `lab.run`, sin `--symbols`)
- **spec_hash:** `7515d798e5d66a55`

## `low-vol-xs-v1` · cross-sectional

- **Regla:** Diario a las 00:00 UTC: volatilidad realizada de 7 dias (retornos 1h) de cada simbolo; long las 2 de menor volatilidad y short las 2 de mayor, mismo nocional (dolar-neutral, NO beta-neutral). Tenencia 24 h: se re-arma cada dia y solo se opera lo que cambia. Minimo 5 simbolos con dato; si no, plano.
- **Timeframe / dirección / horizonte:** 1h · neutral · 24h
- **Parámetros congelados:** `{'rv_hours': 168, 'rv_min_frac': 0.9, 'n_side': 2, 'min_symbols': 5}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines
- **Fuente:** ML4T H2 (research/ml4t-metodologia.md: long las 2 de menor RV 7d, short las 2 de mayor, 24 h; riesgo MEDIO-ALTO, puede ser beta disfrazada); anomalia de baja volatilidad: Ang, Hodrick, Xing & Zhang 2006 (JF, acciones)
- **Correr con:** los 7 (necesita >= 5 con dato; con menos queda plana)
- **spec_hash:** `307d3572e576e4c2`

## `pares-eth-btc-v1` · reversion

- **Regla:** Pares ETH/BTC con velas 1h: spread = log(ETH) - beta*log(BTC), beta por OLS de las 720 velas anteriores (30 dias), z-score del spread con la misma ventana. z > +2 -> short ETH / long BTC por beta; z < -2 -> long ETH / short BTC; beta fija al entrar, ninguna pata > 1x. Salida con |z| < 0.5, stop con |z| > 4 o a las 72 h. Tras stop o salida por tiempo, no re-entra hasta |z| < umbral. Solo ETH y BTC.
- **Timeframe / dirección / horizonte:** 1h · neutral · <=72h
- **Parámetros congelados:** `{'leg_y': 'ETHUSDT', 'leg_x': 'BTCUSDT', 'window_hours': 720, 'entry_z': 2.0, 'exit_z': 0.5, 'stop_z': 4.0, 'max_hold_hours': 72}`
- **Grilla de calibración:** `{'entry_z': [2.0, 2.5]}` (2 combinaciones)
- **Datos:** klines
- **Fuente:** propuesta del usuario (lote 3), no esta en catalogo-hipotesis.md; pairs trading: Gatev, Goetzmann & Rouwenhorst 2006 (RFS); version con hedge OLS rolling y z-score del residuo
- **Correr con:** `--symbols ETHUSDT,BTCUSDT`
- **spec_hash:** `c9c2243ebcef9393`

## `rango-reversion-v1` · reversion

- **Regla:** Velas 1h, solo BNB, TRX y XLM. Regimen rango = ADX(14) < 20. En rango: close bajo la banda inferior de Bollinger(20, 2) -> long; sobre la superior -> short. Take profit en la EMA20 del momento de entrada (nivel fijo), stop a 1.5 ATR(14) del close de entrada, salida a las 12 h.
- **Timeframe / dirección / horizonte:** 1h · both · <=12h
- **Parámetros congelados:** `{'symbols': ['BNBUSDT', 'TRXUSDT', 'XLMUSDT'], 'adx_n': 14, 'adx_max': 20.0, 'bb_n': 20, 'bb_k': 2.0, 'ema_n': 20, 'atr_n': 14, 'sl_atr': 1.5, 'hold_hours': 12}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines
- **Fuente:** propuesta del usuario (lote 3), no esta en catalogo-hipotesis.md; Bollinger (bandas 20, 2) y Wilder 1978 (ADX, ATR); sin paper academico
- **Correr con:** `--symbols BNBUSDT,TRXUSDT,XLMUSDT`
- **spec_hash:** `a03dba31be20137f`

## `reversion-xs-1h-v1` · reversion

- **Regla:** Velas 1h, las 6 alts contra BTC. Residual e = ret_1h(alt) - beta*ret_1h(BTC), beta rolling de las 720 velas anteriores (30 dias). Si e < -2 sigma(e) (sigma trailing 30 dias) -> long alt y short BTC por beta; si e > +2 sigma -> al reves. Salida a las 4 h. La pata BTC suma las coberturas abiertas.
- **Timeframe / dirección / horizonte:** 1h · neutral · 4h
- **Parámetros congelados:** `{'symbols': ['SOLUSDT', 'XRPUSDT', 'ETHUSDT', 'XLMUSDT', 'BNBUSDT', 'TRXUSDT'], 'btc': 'BTCUSDT', 'window_hours': 720, 'k_sigma': 2.0, 'hold_hours': 4}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines
- **Fuente:** catalogo-hipotesis.md 1.2 (reversion-xs-1h-v1: residual de la alt contra BTC > 2 sigma -> revierte en 4 h; riesgo ALTO, la evidencia en monedas grandes va en contra)
- **Correr con:** los 7 (las 6 alts + BTC, que es la pata de cobertura)
- **spec_hash:** `e2a331826092f698`

## `funding-prepago-v1` · carry

- **Regla:** Alrededor de cada liquidacion T = 00/08/16 UTC. En T-60 min se mira la ultima tasa YA liquidada (la de T-8 h; la de T todavia no se conoce). Si > p90 de las 270 liquidaciones anteriores (90 dias): short de T-60 a T y long de T a T+60. Si < p10: long de T-60 a T y short de T a T+60. Los 7 simbolos.
- **Timeframe / dirección / horizonte:** 5min · both · 2h
- **Parámetros congelados:** `{'symbols': ['BTCUSDT', 'SOLUSDT', 'XRPUSDT', 'ETHUSDT', 'XLMUSDT', 'BNBUSDT', 'TRXUSDT'], 'settle_hours': [0, 8, 16], 'pre_minutes': 60, 'post_minutes': 60, 'pct_hi': 0.9, 'pct_lo': 0.1, 'pct_window': 270, 'max_age_hours': 9}`
- **Grilla de calibración:** `ninguna (sin calibrar)` (1 combinación)
- **Datos:** klines, funding
- **Fuente:** catalogo-hipotesis.md 3.3 (funding-prepago-v1: posicionarse alrededor de la hora de liquidacion del funding; riesgo ALTO, especulativa); sin paper academico
- **Correr con:** los 7
- **spec_hash:** `3fc248f8641d0402`

## Decisiones de implementación para revisar antes de aprobar

1. **tsmom: umbral de rebalanceo RELATIVO** (|Δw| > 20% de |w_actual|; siempre se opera si cambia el signo o se
   parte de 0). Alternativa: absoluto (|Δw| > 0.20 del capital del símbolo), que opera menos.
   Hueco de datos o vol no definida ese día → se mantiene la posición. Vol: ≥ 90% de las 720 velas.
2. **low-vol: dólar-neutral, no beta-neutral** (como pide H2): probablemente long BTC/TRX y short SOL/XRP → puede
   ser beta (short del mercado) disfrazada. Con < 5 símbolos con dato, todo plano.
3. **pares: beta sobre log-precios (niveles)** de las 720 velas ANTERIORES (no incluye la actual) y z-score del
   residuo con la misma beta. β se fija al entrar. No se entra con |z| > 4. Tras stop o salida por tiempo no se
   re-entra hasta que |z| < umbral de entrada (re-armado; si no, re-entraría en la vela siguiente). La regresión
   en niveles de 30 días puede ser inestable (relación espuria si no hay cointegración).
4. **rango: régimen = ADX(14) de Wilder < 20** (se eligió ADX y no pendiente de EMA). Bandas con SMA(20) (Bollinger
   estándar), salida en la **EMA20 del momento de entrada como nivel fijo** (no la EMA móvil) y la entrada exige
   que esa EMA esté del lado del objetivo. Sin grilla (bb_k = 2.0) para no sumar intentos.
5. **reversion-xs: sin grilla** (k = 2σ). Una posición por alt a la vez; σ(e) necesita 60 días de calentamiento
   (30 para β + 30 para σ). La pata BTC puede apalancarse si disparan varias alts a la vez.
6. **funding-prepago: el funding de T no se usa** (se conoce en T); en T−60 se usa la última tasa liquidada
   (normalmente la de T−8 h), con tolerancia de 9 h. La posición de T−60 a T está abierta en la liquidación T,
   así que el simulador le cobra/paga ese funding: es parte del PnL (es justamente la apuesta). Los horarios
   00/08/16 son fijos: si un símbolo pasó a funding cada 4 h, se sigue operando solo en esos tres horarios.
   "Velas 5m" es nominal: la regla no mira precios, solo el calendario y el funding.
