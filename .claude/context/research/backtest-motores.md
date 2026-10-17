# Investigación: motor de backtest (2026-10-17)

Reporte de un agente de investigación. Datos verificados contra GitHub, PyPI y la documentación oficial
el 17-oct-2026.

## Recomendación

- **Principal: Freqtrade**, con un **pre-laboratorio vectorizado propio** por delante.
  - Es la única opción madura, gratis y activa que cubre todo a la vez:
    - perpetuos con funding en el backtest y apalancamiento;
    - optimizador hyperopt (Optuna);
    - detección de look-ahead (`lookahead-analysis`, `recursive-analysis`);
    - la **misma estrategia** en backtest, dry-run (paper) y vivo;
    - futuros de Binance, Bybit, Hyperliquid y OKX.
  - Licencia GPL-3: sin problema para uso personal.
- **Alternativa: NautilusTrader**, cuando salga la v2 estable.
  - Hoy está en v2.0.0rc7, en migración.
  - En la v2, `BacktestNode` todavía no acepta datos propios.
  - Es el que modela el funding de forma más rigurosa (liquidación real por intervalo). Curva de aprendizaje alta.
- **Descartados como motor principal:**
  - Hummingbot: su backtest no modela funding (solo un `trade_cost` plano), y quants-lab está quieto desde nov-2025 y exige MongoDB. Sirve solo como destino de ejecución.
  - jesse: el modo en vivo es pago y no documenta funding.
  - vectorbt OSS: el apalancamiento es solo de la versión PRO, y la licencia incluye Commons Clause.
  - backtesting.py: AGPL, un solo activo, sin funding.
  - Lean: pesado (.NET + Docker) y los datos de funding son pagos.
  - hftbacktest: necesita order book tick a tick.
  - backtrader: abandonado.

## Lo que hay que escribir nosotros

- **(A) Capa de features única:** funciones puras sobre `bars_1m` y `klines`. El mismo código se usa en el pre-lab, en Freqtrade y en vivo.
- **(B) Pre-lab vectorizado** (numpy/pandas):
  - costos: comisión, slippage en bps, funding por liquidación y apalancamiento;
  - walk-forward rodante y splits purged con embargo;
  - registro de **todos** los intentos, para calcular el deflated Sharpe y la PBO.
- **(C) Exportador** de SQLite al formato feather de Freqtrade: OHLCV de futuros, mark, `funding_rate` y open_interest.
  - El funding histórico sale de nuestros datos: Hyperliquid y Bybit no lo dan por API, y desde 2025 hay intervalos dinámicos.
- **(D) Orquestador de walk-forward** sobre Freqtrade: hyperopt en la ventana de entrenamiento, backtest en la de prueba, con ventanas rodantes (`--timerange`).
- **(E) Panel:** resultados de backtest y walk-forward, curva de capital, trades, y estado de dry-run y vivo.

## Riesgos y reglas

- **RAM:** 1 año de 1m son ~84 MB por símbolo con ~20 columnas float64. Procesar por símbolo o en float32. Los hyperopt largos conviene correrlos en la PC (WSL); la VM queda para dry-run y vivo.
- **Contra el autoengaño:**
  - holdout final que no se toca nunca (los últimos 2–3 meses);
  - limitar las épocas de hyperopt;
  - contar todas las configuraciones probadas;
  - exigir que la estrategia funcione en varios símbolos.
- **Paridad backtest/vivo:** el feature del minuto t solo se usa una vez cerrado ese minuto.

## Fuentes

- Freqtrade: https://www.freqtrade.io/en/stable/leverage/ · https://www.freqtrade.io/en/stable/data-download/ · https://www.freqtrade.io/en/stable/lookahead-analysis/ · https://github.com/freqtrade/freqtrade
- Nautilus: https://github.com/nautechsystems/nautilus_trader · https://nautilustrader.io/docs/latest/concepts/data/funding_rate_update/
- Hummingbot: https://github.com/hummingbot/hummingbot (`strategy_v2/backtesting/backtesting_engine_base.py`) · https://github.com/hummingbot/quants-lab
- vectorbt: https://github.com/polakowo/vectorbt · https://vectorbt.pro/
- jesse: https://docs.jesse.trade/docs/livetrade · Lean: https://github.com/QuantConnect/Lean · hftbacktest: https://github.com/nkaz001/hftbacktest
