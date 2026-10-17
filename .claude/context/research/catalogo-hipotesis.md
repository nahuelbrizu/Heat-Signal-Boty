# Catálogo de hipótesis v0 (2026-10-17)

Lo armó un agente de investigación. **Nada de esto está registrado todavía**: es material para el
pre-registro. Las reglas completas, con umbrales, están en el reporte del agente (en la conversación del
2026-10-17); acá queda la versión condensada.

## Qué dice la evidencia

- **Con respaldo académico:**
  - Momentum de serie temporal y tendencia (breakouts) a días: sobrevive a los costos.
  - Carry/funding como fuente de retorno: grande, pero comprimiéndose.
  - Reversión a 15m después de flujo taker extremo: real, pero **~1,3 bp bruto contra ~5 bp de costo**.
  - Desbalance de flujo en la apertura de los cuartos de hora, que predice 4–12 h (paper 2026 sin replicar).
- **Sin respaldo académico** (más bien blogs de exchanges): rebote después de cascadas de liquidaciones,
  ratio long/short, funding extremo como señal contraria (BitMEX: sin poder predictivo), estacionalidad
  por hora o por día.
- **Noticias con LLM:** mejor ajuste dentro de la muestra, sin ganancia fuera de muestra; además el precio
  mueve al sentimiento y no al revés.
- **Ventaja nuestra:** las klines 1m que ya guardamos traen `taker_buy_quote`, así que tenemos **1 año de
  flujo taker/CVD de Binance**.

## Convenciones

- `imb = (buy − sell)/(buy + sell)`.
- `σ_tf`: desvío rolling de 30 días, calculado solo con datos pasados.
- `pXX_train`: percentil medido solo en la ventana de entrenamiento y congelado durante el test.
- **Costos:** 10 bps ida y vuelta, más slippage de 2–5 bps por lado (BTC/ETH) o 5–10 bps (XLM/TRX), más el
  funding pagado.
- **Viabilidad:** a horizontes de minutos, el movimiento bruto esperado tiene que superar unos 25–30 bps.

## Hipótesis por familia (riesgo de ser ruido entre paréntesis)

- **Reversión:**
  - `reversion-flujo-extremo-15m-v1`: vela de 15m con |ret| > 2,5σ y flujo extremo a favor (p97) → contra el movimiento, 1 h (ALTO).
  - `reversion-xs-1h-v1`: residual de la alt contra BTC > 2σ → revierte en 4 h (ALTO; la evidencia en monedas grandes va en contra).
- **Continuación:**
  - `donchian-ensemble-v1`: velas 4h, canales N ∈ {20, 40, 80, 160}, tamaño según volatilidad objetivo, long (BAJO-MEDIO; Zarattini et al. 2025, Sharpe > 1,5 neto).
  - `momentum-intradia-volumen-v1`: la primera media hora UTC predice la última (MEDIO-ALTO).
  - `compresion-breakout-v1`: Bollinger comprimido + ruptura con flujo a favor, 12 h (MEDIO-ALTO).
  - `flujo-apertura-hora-v1`: desbalance taker en los minutos :00–:01 > p95 → a favor, 4 h (MEDIO; Kim & Hansen 2026).
  - `oi-precio-continuacion-v1`: precio, OI y compras subiendo → a favor, 8 h (ALTO).
- **Carry/funding:**
  - `carry-xs-funding-v1`: long a las 2 de menor funding y short a las 2 de mayor, neutral en beta (MEDIO; BIS WP 1087).
  - `funding-crowding-short-v1`: funding > p95, OI subiendo y quiebre de mínimos → short 24 h (ALTO).
  - `funding-prepago-v1`: posicionarse alrededor de la hora de liquidación del funding (ALTO, especulativa).
- **Liquidaciones:**
  - `capitulacion-long-v1`: liquidaciones de longs > p99,5, caída > 3σ y desaceleración → long ≤ 4 h (MEDIO; para backtest se usa un proxy con klines).
  - `squeeze-short-v1`: el espejo (MEDIO-ALTO).
  - `cascada-continuacion-v1`: a favor de la cascada mientras acelera, 15 min (ALTO; slippage enorme).
- **Cross-sectional:**
  - `xs-momentum-semanal-v1`: entre las 7 monedas (ALTO; demasiado pocos activos).
  - `lead-lag-btc-alts-v1`: las alts siguen a BTC con retraso (ALTO; en monedas grandes el retraso dura segundos).
- **Estacionalidad:**
  - `lunes-asia-v1` (ALTO).
  - `hora-funding-vol-v1`: no es estrategia, sino filtro de régimen.
- **Cross-exchange:**
  - `funding-hl-vs-cex-v1`: diferencia de funding entre Hyperliquid y la mediana de las CEX > p95 → delta-neutral hasta 72 h (MEDIO).
  - `premium-divergencia-v1`: el mark de un exchange se separa de la mediana → converge (ALTO).
- **Noticias:**
  - `noticia-sec-negativa-v1`: SEC o tono LLM ≤ −0,7 → short 6 h; la variante contraria se registra aparte (ALTO).

## Primer lote recomendado (8)

| # | Hipótesis | Dirección | Horizonte | Backtest con historia |
|---|---|---|---|---|
| 1 | donchian-ensemble-v1 | Long | días | Sí |
| 2 | flujo-apertura-hora-v1 | Ambas | 4 h | Sí (taker de klines) |
| 3 | reversion-flujo-extremo-15m-v1 | Contra | 1 h | Sí |
| 4 | capitulacion-long-v1 | Long | ≤4 h | Proxy sí; real desde hoy |
| 5 | squeeze-short-v1 | Short | ≤4 h | Proxy sí; real desde hoy |
| 6 | carry-xs-funding-v1 | Neutral | 8 h–días | Funding histórico de Binance |
| 7 | funding-crowding-short-v1 | Short | 24 h | Funding sí; OI vía metrics |
| 8 | funding-hl-vs-cex-v1 | Neutral | ≤72 h | Solo forward |

Quedan en modo observación (sin capital): noticias, estacionalidad, lead-lag y premium.

**A fijar en el pre-registro:**
- la grilla de calibración, lo más chica posible;
- las ventanas walk-forward (90 días de entrenamiento y 30 de prueba, avanzando de a 30);
- las métricas: retorno neto por trade con IC por bootstrap de bloques, y Deflated Sharpe contando todas las variantes;
- el criterio de descarte: IC del retorno neto que incluya 0 con N ≥ 100.

## Efectos débiles o desaparecidos tras costos

- Reversión a 15m.
- Momentum cross-sectional.
- Efectos de calendario.
- Funding extremo como señal contraria a 8 h.
- Desviaciones perp-spot y entre exchanges: decrecientes en el tiempo.
- Sentimiento de noticias con LLM.

## Fuentes principales

- arXiv:2608.21888 (Kitron & Wengrowicz)
- arXiv:2607.09426 (Kim & Hansen)
- arXiv:2212.06888 (He, Manela, Ross, von Wachter)
- BIS WP 1087 (Schmeling, Schrimpf, Todorov)
- https://abarbon.com/papers/catching-crypto-trends (Zarattini, Pagani, Barbon)
- arXiv:2607.27070 (Garcia Seuma)
- https://www.bitmex.com/blog/xbtusd-funding-mean-reversion-strategy
- SSRN 3379131 (Liu, Tsyvinski, Wu)
- Quantpedia (calendario)
- arXiv:2411.05577 (noticias)

**Citadas de memoria, sin verificar:** Makarov & Schoar 2020; López de Prado 2018; Bailey & López de
Prado (Deflated Sharpe); Harvey, Liu & Zhu 2016.
