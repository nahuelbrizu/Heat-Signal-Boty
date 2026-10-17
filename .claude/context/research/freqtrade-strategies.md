# freqtrade-strategies (revisado el 2026-10-17)

Repo: https://github.com/freqtrade/freqtrade-strategies (GPL-3, último commit f3340ce del 2026-09-08).
Contiene **68 estrategias de ejemplo** para Freqtrade ≥ 2022.4.

## Qué hay

- **`user_data/strategies/`** (raíz y `berlinguyinca/`): ~50 estrategias clásicas de indicadores (RSI, MACD,
  Bollinger, ADX, Supertrend, Ichimoku, patrones de velas). La mayoría son **solo spot/long**, con
  `minimal_roi` y `stoploss` sacados de hyperopt sobre datos viejos.
- **`futures/`**: 7 estrategias con `can_short = True`, en velas de 1h y 5m:
  - FAdxSma, FOtt, FReinforced, FSample, FSupertrend, TrendFollowing (cruce de tendencia + OBV), VolatilitySystem.
  - Probadas solo en Binance futures, margen isolated.
- **`lookahead_bias/`**: 4 estrategias **con look-ahead a propósito** (DevilStra, GodStraNew, Zeus, wtc).
  Normalizan con `.min()`/`.max()` de todo el dataframe, o sea que usan el futuro.
- **Disclaimer del propio repo:** son educativas, un "punto de partida" y no estrategias listas para usar.

## Cómo las aprovechamos

1. **Benchmark de "análisis técnico común"** en el laboratorio: correr las 7 de futuros (y algunas clásicas) **tal
   cual, sin re-optimizar**.
   - Sus parámetros salieron de hyperopt sobre otros períodos, así que nuestro año de datos es fuera de muestra
     para ellas: es una prueba honesta.
   - Responde una pregunta concreta: ¿nuestras hipótesis le ganan al TA estándar?
   - Cada una cuenta como un intento en el Deflated Sharpe.
2. **Casos de prueba del detector de look-ahead:** las 4 de `lookahead_bias/` tienen que dar positivo en nuestros
   tests y en el `lookahead-analysis` de Freqtrade. Si alguna pasa limpia, el detector está roto.
3. **Plantillas de `IStrategy` para futuros:** sirven para la etapa 3. Muestran `can_short`, `leverage()`,
   `populate_entry_trend`/`exit` y la config de futuros (`trading_mode: futures`, `margin_mode: isolated`).

## Cuidado

- No re-optimizarlas sobre nuestros datos y después "elegir la mejor": es exactamente el error del caso de ML4T.
- Necesitan **TA-Lib** (librería en C) y `technical`. Hay que instalarlos en la PC, no hace falta en la VM.
- Las de stoploss −0.265 o −1 tienen riesgo de liquidación con apalancamiento: correrlas con leverage 1.
