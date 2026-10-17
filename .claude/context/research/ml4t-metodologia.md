# ML4T (Stefan Jansen, 3ª ed.) aplicado a HeatSignal (2026-10-17)

Reporte de un agente de investigación. Leyó el código del repo (MIT, commit `16dbb8c`) y las salidas de los
notebooks. El texto del libro tiene copyright: esto está resumido con palabras propias.

## Caso de estudio "Crypto Perps / Funding": la lección principal

- **Diseño del estudio:**
  - Datos: 19 perpetuos de Binance, 2020–2025, barras de 8 h alineadas a la liquidación del funding.
  - Hipótesis: un premium alto indica un mercado sobrecargado de longs, y ese premium revierte. Predice el **retorno de precio cross-sectional**; no intenta cosechar el funding.
  - El funding se registra aparte, como flujo de caja de la posición.
  - Holdout sellado: 2024–2025.
  - Se probaron unos 2.800 backtests en un embudo: señal, después asignación de capital, después overlays de riesgo.
- **Resultado:**
  - La mejor configuración tuvo Sharpe de validación **1,67** (p = 0,005).
  - Su **Deflated Sharpe fue ≈ −0,1**.
  - En el holdout: **Sharpe −0,83, CAGR −45%, MaxDD −82%**.
  - Todas las estadísticas sin corregir decían "funciona". Solo el DSR anticipó el fracaso.
- **Trampas que señala el estudio:**
  - Binance fecha cada barra en su apertura. Hay que re-fecharla a su cierre **antes** de cualquier shift o filtro, o aparece look-ahead.
  - Hay que hacer el shift antes de filtrar.
  - Contar filas no es contar observaciones independientes. El premium tiene una persistencia de ~37 períodos: equivale a unas 30 observaciones independientes por año.
  - Momentum y reversión sobre la misma serie se cancelan.
  - El funding tiene un tope del exchange, y se satura justo en los regímenes que importan.
  - Un P&L que viene del funding es carry, aunque no se haya diseñado así. Hay que descomponer el P&L en precio + funding − costos.

## Aplicable a nosotros

- **Sirve tal cual:**
  - el reloj de disponibilidad;
  - el ledger de funding;
  - la descomposición del P&L;
  - el DSR calculado con el conteo de intentos;
  - el holdout sellado.
- **Juega en contra:**
  - 7 símbolos contra 19: lo cross-sectional queda muy débil.
  - 1 año de velas.
  - Los datos multi-exchange arrancan recién ahora.
- **Juega a favor:** tenemos OI, liquidaciones, flujo taker y 4 exchanges. Abre hipótesis que el estudio no probó, sobre todo el spread de funding entre exchanges y las cascadas, aunque requieren de 3 a 6 meses de datos acumulados.
- **Ojo:** el mark price no es el precio índice. Con nuestras columnas no se puede calcular el premium real; hacen falta los `premiumIndexKlines` de Binance.

## Receta para el laboratorio

1. **Pre-registro.** Cada estrategia tiene un spec versionado con:
   - universo;
   - reloj de decisión;
   - **ejecución en el open de la vela 1m siguiente**;
   - regla de señal → posición;
   - horizonte y salida;
   - costos;
   - splits.

   El hash del spec identifica la corrida. Se registra **cada** intento, y ese conteo K alimenta el DSR.
2. **Reloj de disponibilidad:**
   - Las agregaciones desde 1m se arman con `label="right", closed="left"`.
   - El funding se toma según lo que se sabía en t: el que muestra el WS es una predicción, el que vale es el liquidado.
   - Si hay huecos, la etiqueta queda en null.
3. **Etiqueta:**
   - Principal: retorno a horizonte fijo desde la ejecución real, `open[t+1+h]/open[t+1]−1`.
   - Etiqueta neta: se restan los costos y el funding liquidado mientras la posición está abierta.
   - Alternativa: triple barrier con k·σ, calibrando k por MFE/MAE solo en train. Si una misma vela toca TP y SL, se asume SL.
4. **Validación:**
   - Holdout: los últimos ~3 meses más todo el período forward.
   - Walk-forward: 6 meses de train, 1 de test, avance mensual.
   - Purga de h barras y embargo igual al lookback de los features.
   - CPCV para estimar la PBO.
   - Umbrales siempre con ventanas trailing; nunca percentiles calculados sobre toda la muestra.
5. **Costos:**
   - Comisión taker de 5 bps por lado.
   - Slippage por símbolo, estimado con los proxies de Roll o Corwin-Schultz, ×2–3 en barras con liquidaciones extremas.
   - Funding con el signo de la posición.
   - **Siempre** mostrar la curva de Sharpe contra el costo (0–30 bps) y el costo de break-even. Se descarta la estrategia si el break-even es menor a 2× el costo esperado.
6. **Métricas:**
   - IC de rango con t-HAC.
   - Sharpe con IC por bootstrap estacionario por bloques.
   - **PSR, DSR, MinTRL**, MaxDD.
   - Turnover, hit rate con IC de Wilson, MFE/MAE.
   - Descomposición del P&L: precio / funding / costos.
7. **Baselines obligatorios:**
   - buy & hold del símbolo;
   - canasta equal-weight de los 7;
   - **entradas aleatorias con la misma frecuencia, tenencia y dirección** (≈1.000 permutaciones como distribución nula).
8. **Decisión:**
   - Abrir el holdout **una sola vez**.
   - **Si el DSR ≤ 0, la estrategia no avanza**, aunque el PSR dé un p chico.

## Features calculables con nuestras columnas

- **Momentum:**
  - retorno con skip (1 h, 4 h, 24 h, 7 d);
  - retorno escalado por volatilidad;
  - distancia a la media medida en ATR;
  - pendiente de la regresión de log(close).
- **Reversión:**
  - z-score del retorno corto;
  - distancia al VWAP;
  - RSI.
- **Volatilidad:**
  - volatilidad realizada;
  - estimadores Parkinson, Garman-Klass y Yang-Zhang;
  - vol-of-vol, ratio de volatilidades y percentil de volatilidad.
- **Liquidez:**
  - volumen relativo contra la misma hora UTC;
  - Amihud;
  - spreads de Roll y Corwin-Schultz;
  - λ de Kyle.
- **Microestructura:**
  - desbalance de flujo (OFI) taker;
  - CVD y su divergencia con el precio;
  - desacuerdo de flujo entre exchanges;
  - desbalance de liquidaciones y z-score de las liquidaciones totales.
- **Carry:**
  - nivel y z-score del funding (14 d);
  - half-life AR(1);
  - dispersión de funding entre exchanges;
  - ranking de funding entre los 7.
- **Posicionamiento:**
  - ΔOI **en contratos** (`oi_usd/mark`), no en USD, porque en USD se mueve solo con el precio;
  - ratio OI / volumen.
- **Estado:**
  - minutos hasta la próxima liquidación de funding;
  - hora UTC en seno/coseno;
  - día de la semana;
  - cantidad de noticias en la última hora.

**Regla:** los features de flujo se usan como señal con al menos 1 barra de lag.

## Hipótesis que propone

| Id | Regla resumida | Datos | Riesgo |
|---|---|---|---|
| H1 | Reversión por crowding de funding: z-score a 42 liquidaciones; long al de menor z, short al de mayor z; 24 h | Funding de Binance (se puede bajar la historia) | Alto |
| H2 | Baja volatilidad cross-sectional: long a las 2 de menor RV 7d, short a las 2 de mayor; 24 h | Velas | Medio-alto (puede ser beta disfrazada) |
| H3 | Funding extremo ponderado por OI > p95 de 90 d → short (< p5 → long); 24 h | Funding | Alto |
| H4 | Liquidaciones de longs > p99 con caída > 2σ en 15m → long, triple barrier hasta 4 h | Liquidaciones (desde hoy) | Alto |
| H5 | Absorción: OFI 1h > +0,25 con retorno negativo → short 1 h | Flujo | Muy alto; sirve como **control que se espera que falle por costos** |
| H6 | ΔOI en contratos > +5% con caída > 1,5% en 4 h → long 8 h (y la regla simétrica) | OI | Medio-alto |
| H7 | Momentum de serie de tiempo: signo del retorno 7d × σ objetivo / RV 30d, diario | Velas | Medio |
| H8 | z del retorno 1h < −3 sin noticias → long 4 h (y la regla simétrica) | Velas + RSS | Medio-alto |
| H9 | Spread de funding entre exchanges > 0,05% durante 3 liquidaciones → delta-neutral, máximo 7 días | Funding de 4 exchanges | Medio |
| H10 | Baselines: B&H, canasta, entradas aleatorias | — | Control |

## Librerías del autor (MIT, Python ≥ 3.12, versiones 0.1.x: fijar versión exacta)

- **ml4t-diagnostic** (la más útil):
  - splitters walk-forward, CPCV, purga y embargo;
  - DSR, PSR, MinTRL, PBO, FDR, bootstrap;
  - es pesada: statsmodels, sklearn y arch.
- **ml4t-engineer:**
  - más de 120 features (volatilidad, liquidez, OFI);
  - etiquetas triple barrier y horizonte fijo;
  - barras alternativas.
- **ml4t-backtest:**
  - event-driven, **con módulo de funding para perpetuos**;
  - sirve como chequeo de paridad de un simulador propio.
- **No aplican:**
  - ml4t-live: está orientada a IBKR.
  - ml4t-data: ya tenemos ingesta propia.
