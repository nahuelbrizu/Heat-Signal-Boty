# Base de conocimiento: análisis técnico para los bots (revisada)

> Documento del usuario (2026-10-18), resumido y con la revisión contra lo que el laboratorio ya probó.
> Sirve de contexto para los agentes LLM y de catálogo de candidatas. **Ninguna cifra externa de abajo está
> verificada por nosotros**: son hipótesis a validar con nuestros datos, no umbrales.

## Regla central
Todo concepto de análisis técnico entra a los bots como una **función numérica determinista** sobre OHLCV o sobre
datos de derivados, nunca como lectura visual. Así se puede pre-registrar con `strategy_id`, backtestear y medir
con n, IC y Brier. El modelo numérico decide; el LLM explica, cruza contexto y puede vetar. Lenguaje histórico:
"este setup reaccionó en X % de n casos".

Hallazgo transversal: casi ningún patrón o indicador tiene ventaja aislado. El valor aparece al combinarlos con
contexto (tendencia, nivel, régimen de volatilidad, derivados) y al filtrar señales con un segundo modelo
(meta-labeling).

## Velas japonesas
- Primitivas por vela (TA-Lib): `body = |c−o|`, `range = h−l`, sombras superior e inferior, `body_pct = body/range`,
  `close_pos = (c−l)/range`. "Largo/corto" relativo al promedio de las últimas N velas.
- Evidencia: solas no tienen ventaja (Quanta 72: 24 patrones, ~5.900 tickers; controlando por tendencia, igual
  que días sin patrón). Única excepción reportada: inverted hammer en tendencia bajista definida (54,3 % vs
  52,2 %).
- **Uso acordado:** como features de un modelo (binarios + `close_pos`, `body_pct`, sombras/ATR), medidos contra
  un control del mismo régimen. No como lote de estrategias.

## Indicadores (uno o dos por familia; no apilar redundantes)
| Familia | Indicadores | Uso |
|---|---|---|
| Tendencia | EMA/SMA 20-50-200, MACD 12/26/9, ADX/DMI 14, Supertrend | régimen, pendiente, fuerza (ADX > 25 tendencia, < 20 rango) |
| Momentum | RSI 14, Stochastic, ROC | en rango; RSI/Stoch/Williams son casi redundantes |
| Volatilidad | ATR 14, Bollinger 20/2 (%B, ancho), Keltner, Donchian | stops, sizing, normalizar; squeeze |
| Volumen | VWAP, OBV/CVD, volumen / media | precio "justo" intradía, confirmar rupturas |

Reglas: distancias en múltiplos de ATR o z-score; solo velas cerradas; warm-up ~3×n velas; sesgo del timeframe
mayor y entrada del menor, alineados por cierre.

## Estructura de mercado / SMC
Swings (N velas antes y después), HH/HL, BOS, CHoCH, FVG, order blocks, liquidez (equal highs/lows), niveles
previos, sesiones, retrocesos. Librerías: `smartmoneyconcepts`, `pyvsmc` (versiones sin verificar).
**Trampa principal:** un swing de N velas recién existe N velas después; en el backtest solo se usa desde la
vela de confirmación. Fijar y versionar si la mitigación es por mecha o por cierre. Sin evidencia académica:
features de ubicación, medir su aporte incremental. En cripto, la liquidez inferida se puede reemplazar por las
liquidaciones reales que ya ingestamos.

## Familias de estrategias y lo que ya probamos
| Familia (doc) | Nuestra versión | Resultado OOS 2021-03 → 2026-07 |
|---|---|---|
| Time-series momentum | `tsmom-diario-v1` | Sharpe 0,67, falla solo DSR |
| Donchian / canal | `donchian-ensemble-v1` | 1,08; holdout no confirmó |
| Cruce de medias + ATR | `ema-cruce-v1` / `-filtrado-v1` | 0,77 / 0,64, fallan solo DSR |
| Mean reversion Bollinger 1h | `rango-reversion-v1` | −3,15 (no repite Ekström 2025) |
| Squeeze → expansión | `compresion-breakout-v1` | −0,12 |
| Derivados contrarian (OI/funding) | `capitulacion-long-v1`, `oi-precio-continuacion-v1` | sin ventaja neta |
| Breakout máximo de N días | `max-ruptura-10d-v1` (lote 5) | pendiente |
| Mean reversion a VWAP | `vwap-reversion-v1` (lote 5) | pendiente |
| Combinar estrategias poco correlacionadas | `cartera-tendencia-v1` (lote 5) | pendiente |

Selección por régimen (ADX, percentil de ATR/precio, pendiente EMA200, cuadrante OI/funding) y medir cada
estrategia por régimen: candidata para después del meta-labeling.

## Derivados
Cuadrantes OI × funding (Axel Adler Jr, BTC 2019–2026, cifras sin verificar): Q1 OI↑ funding+ (43 %), Q2 OI↑
funding− (9 %), Q3 OI↓ funding+, Q4 OI↓ funding− (9 %, capitulación). Features: z-score de funding, ΔOI 1h/4h/24h,
OI vs media de 30 días, liquidaciones por lado, CVD, desequilibrio del libro.
**Desacuerdo:** el doc pide ΔOI en USD; nosotros lo medimos **en contratos** a propósito (en USD el OI sube solo
porque sube el precio y mezcla precio con apalancamiento). Se mantiene contratos.

## Feature engineering y ML (etapa "entrenar bots", pendiente)
López de Prado, *Advances in Financial ML*: barras por volumen/eventos; features estacionarios (diferenciación
fraccional d ≈ 0,3–0,5); **etiqueta triple barrier** (TP +kσ, stop −kσ, tiempo); **meta-labeling** (una
estrategia primaria propone el lado y un segundo modelo decide si tomarla y con qué tamaño); pesos por unicidad;
gradient boosting o random forest; probabilidad calibrada (isotónica/Platt) y Brier; **purged K-fold con
embargo**. Orden acordado: primero robustez y PBO en el lab, después meta-labeling sobre Donchian / ema-cruce.

## Validación
Contar intentos; PSR; DSR (nuestra implementación usa la fórmula completa de E[max]); **PBO por CSCV**;
walk-forward (eficiencia OOS/IS); purged K-fold. Checklist: costos y funding reales, SL primero en empate, solo
datos al cierre, ≥ 30 trades (mejor 100+), Wilson, Sortino, DD, profit factor, Brier, **robustez con costos ×3 y
parámetros vecinos**, paper antes de plata real. Dato citado (Rulyfi, sin verificar): de 99,9 M backtests sobre
BTC 1h ninguna estrategia tuvo DSR > 0,95; la mejor curva (+684 %) tenía DSR ~0,10.

## Riesgo y sizing (para la etapa de ejecución)
Riesgo fijo por trade (0,5–1 % hasta el stop), stop por ATR (1,5–3), volatility targeting, Kelly solo fraccional
y con 100+ trades. Límites duros: pérdida diaria máxima, máximo de operaciones por día, margen aislado y
apalancamiento fijo, exposición total por correlación, evitar market en baja liquidez, **kill switch** si el
drawdown supera 1,5 × el máximo histórico.

## Fuentes citadas por el documento (sin verificar)
Quanta 72 (velas), TA-Lib, BabyPips (informal), smartmoneyconcepts / pyvsmc, Gbadebo 2026 (momentum cripto),
Quantpedia (trend vs mean reversion en BTC), Ekström 2025, Axel Adler Jr (OI × funding), CryptoSlate, arXiv (OI
en perpetuos), finmlkit, wraquant, Rulyfi (100 M backtests), Bailey & López de Prado 2014 (DSR),
tradingstrategy.ai.
