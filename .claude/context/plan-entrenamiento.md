# Plan: entrenar a los bots (meta-labeling)

> Estado: **PROPUESTA (2026-10-19)**, para aprobar por etapas. Base: `research/base-conocimiento-ta.md`
> (López de Prado, *Advances in Financial ML*, caps. 3, 5, 7 y 12) y los resultados del laboratorio.

## Idea en una línea
No entrenamos un modelo que "adivine el precio". Las estrategias que ya mostraron algo (las **primarias**)
proponen cada operación. Un segundo modelo (el **meta-modelo**) aprende, con nuestros datos, **cuándo conviene
tomarla y con qué tamaño**. Si el meta-modelo no mejora a la estrategia sola fuera de muestra, se descarta.

## Por qué así
- Lo que tiene señal en nuestro programa es poco y lento: Donchian, tsmom-diario, ema-cruce y max-ruptura (las
  más robustas en los diagnósticos). Todo lo rápido murió por costos.
- Un modelo que decide la dirección desde cero tiene muchísimos grados de libertad, y con 5 años de datos y 7
  monedas es la receta del sobreajuste. Filtrar señales que ya existen es un problema más chico y medible.
- El grafo LLM ya cumple un rol de filtro: el meta-modelo le da un número calibrado para usar y explicar.

## Etapa 0: preparación (sin entrenar nada)
- Instalar en el venv del laboratorio (PC, no la VM): `scikit-learn`, `lightgbm`. Fijar versiones.
- **Nuevo período de prueba limpio.** Los holdouts de julio–octubre 2026 ya se usaron (Donchian, max-ruptura,
  cartera). El examen final del meta-modelo será **el período hacia adelante desde 2026-10-20**: los datos que se
  van juntando solos (mínimo 3 meses) más el paper trading. Se fija acá, antes de empezar.
- Pre-registro del **presupuesto de intentos** del meta-modelo: como máximo 24 configuraciones (familias de
  modelo × conjuntos de features × hiperparámetros), todas contadas. Sin esto no se puede deflactar.

## Etapa 1: dataset de eventos y etiquetas
- **Eventos:** cada entrada de cada primaria en el desarrollo (2021-03 → 2026-07-15), con las reglas congeladas.
  Orden de magnitud: Donchian ~635, ema-cruce ~1.560, max-ruptura ~1.210, tsmom (rebalanceos) ~2.400.
- **Etiqueta = triple barrier** alineada con cómo opera cada primaria:
  - ema-cruce: ya tiene barreras (stop 1,5 ATR, objetivo 3 ATR, tiempo); la etiqueta es cuál tocó primero, neta
    de costos.
  - Donchian / tsmom / max-ruptura: barreras ±k·σ (σ = volatilidad diaria trailing) y tope de tiempo igual al
    horizonte típico de la regla; k fijado de antemano (k = 2, sin grilla).
  - Etiqueta binaria: 1 si la operación terminó con PnL neto > 0.
- **Pesos por unicidad:** las operaciones se solapan en el tiempo (7 monedas, varias a la vez); cada evento pesa
  según cuántas otras comparte su ventana, para no sobrecontar.
- Entregable: `lab/meta/dataset.py` + tests de que ninguna feature ni etiqueta usa datos posteriores al evento.

## Etapa 2: features (todas al cierre, normalizadas en ATR o z-score)
| Grupo | Features |
|---|---|
| Tendencia | (close − EMA20/50/200)/ATR, pendiente EMA200, ADX, +DI−−DI, `daily_trend` (htf-trend-v1) |
| Volatilidad | ATR/precio y su percentil de 1 año, ancho de Bollinger y su percentil, vol realizada 1d/7d/30d |
| Momentum | retornos log 1/4/24/72 barras, RSI, ROC |
| Velas | body_pct, close_pos, sombras/ATR de la vela de señal |
| Derivados | funding z-score 30d, ΔOI en contratos 1h/4h/24h, cuadrante OI × funding, ratio taker, long/short |
| Mercado | tendencia y vol de BTC, correlación 30d de la moneda con BTC, cuántas de las 7 están en tendencia |
| Contexto | hora UTC / sesión, día de la semana |
- Versionado: `features-meta-v1`. Un cambio en las features es una versión nueva y cuenta como intento.

## Etapa 3: entrenamiento y validación
- **Purged K-fold con embargo** (K = 5) sobre el desarrollo: se sacan del train los eventos cuya ventana se
  solapa con el test, y se deja un hueco (embargo) después de cada bloque de test.
- Modelos, en este orden y sin saltear: (1) regresión logística con pocas features (línea base), (2) LightGBM
  chico (profundidad ≤ 4, pocas hojas, fuerte regularización).
- **Calibración** isotónica (o Platt si hay pocos datos) dentro de cada fold.
- Métricas:
  - Estadísticas: Brier y log loss contra la tasa base, AUC, curva de calibración.
  - Económicas: Sharpe, retorno, DD y número de operaciones de la primaria **filtrada** vs **sin filtrar**,
    con costos; DSR con el K del meta-modelo; PBO de las configuraciones.
- Tamaño de posición desde la probabilidad (p. ej. escalar entre 0 y 1 según p − 0,5), comparado con tamaño fijo.
- Importancia de features (permutación / SHAP) solo para entender, **nunca** para elegir features mirando el test.

## Etapa 4: prueba hacia adelante (sin tocar nada)
- El modelo elegido se congela (hash del modelo + versión de features).
- Corre en el **bot de paper** en modo sombra: registra para cada señal de la primaria la probabilidad y la
  decisión (tomar o no, tamaño), y el bot ejecuta **las dos versiones** (filtrada y sin filtrar) para compararlas
  con los mismos precios.
- El grafo recibe la probabilidad como contexto ("en casos similares, X % terminó positivo, n = …").
- Revisión semanal (vista `#/semanal`).

## Etapa 5: criterio para plata real (fijado ahora)
Mínimo 3 meses hacia adelante y:
1. La filtrada le gana a la sin filtrar en Sharpe neto y en drawdown.
2. Brier mejor que la tasa base y calibración razonable.
3. El paper replica al backtest (diferencias explicadas por costos y ejecución).
4. Límites duros de `decisions.md` implementados: riesgo 0,5–1 % por trade, pérdida diaria máxima, kill switch.
5. **Stop del lado del exchange en TODA orden real (2026-10-19, obligatorio).** Cada entrada lleva, en el mismo
   envío, una orden `STOP_MARKET` con `reduceOnly` (o `closePosition`) en el exchange, aunque la regla salga por
   tiempo, canal o señal. Es un seguro ante caídas del bot, de la VM o de la red, no parte de la regla:
   - si la regla tiene stop (ema-cruce 1,5 ATR, rango y vwap 1,5 ATR, pares |z| > 4, ...), el stop del exchange es
     ese mismo nivel (el de `lab/exits.py`, calculado al entrar);
   - si la regla no tiene stop (Donchian, tsmom, max-ruptura, carteras, hipótesis por tiempo), stop de catástrofe
     fijado de antemano y documentado por estrategia antes de operar. **PROPUESTA (a confirmar por el usuario en `decisions.md`): 3 ATR(14) de la temporalidad de la
     regla desde la entrada, con piso de 8 % y techo de 20 % del precio** (el −20 % es el mismo stop de seguridad que
     ya usa el Freqtrade de hipótesis). Cambiar ese valor es una decisión nueva en `decisions.md`, nunca un ajuste
     mirando resultados;
   - el valor de cada estrategia queda escrito en su ficha de salidas (`lab/exits.py`) y en `decisions.md`; la
     orden se verifica después de enviarla (que exista en el exchange); si no se puede colocar el stop, la entrada se
     cancela y la posición se cierra (sin stop no hay posición);
   - el stop de catástrofe NO se simula como parte de la regla en el laboratorio (no cambia los resultados ni los
     spec_hash). El MAE/MFE de paper y de las hipótesis (`evaluation/excursion.py`) sirve para ver cuántas veces lo
     habría tocado (solo descriptivo).
   - Antes de codear la ejecución real: verificar contra la doc oficial vigente de Binance USDS-M el tipo de orden
     (`STOP_MARKET`, `reduceOnly`/`closePosition`, `workingType` MARK_PRICE vs CONTRACT_PRICE) — principio de grounding.

Recién ahí, dinero chico y escalado gradual.

## Alternativas consideradas
- **FreqAI (Freqtrade):** entrena modelos dentro de Freqtrade. Útil para operar después, pero esconde la
  validación purgada y el conteo de intentos. Se usa, si acaso, para ejecutar un modelo ya validado acá.
- **Selector de régimen** (elegir familia por ADX/volatilidad): más simple; queda como línea base contra la cual
  medir el meta-modelo.
- **Redes (LSTM/Transformer):** no antes de agotar lo simple; con este volumen de eventos no se justifican.

## Tiempos estimados
Etapa 0–1: 1–2 días de trabajo. Etapa 2: 1–2 días. Etapa 3: 2–3 días de cómputo y análisis en la PC.
Etapa 4: mínimo 3 meses de calendario. Cada etapa se presenta para aprobar antes de pasar a la siguiente.
