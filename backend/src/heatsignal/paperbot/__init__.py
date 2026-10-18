"""
Bot propio de PAPER TRADING (simulado: sin plata real, sin API keys de exchange) para las estrategias de `params.json`: las 6 del lote 3
(desde 2026-10-18) y max-ruptura-10d-v1 del lote 5 (desde 2026-10-19, entra sin tocar a las otras).

    python -m heatsignal.paperbot run          # una corrida (la dispara heatsignal-paperbot.timer a los :02 UTC)
    python -m heatsignal.paperbot status       # ultimas corridas y equity por estrategia
    python -m heatsignal.paperbot params-from-lab tsmom-diario-v1 [--write]

Modulos:
  - `store.py`   base append-only `data/paperbot.sqlite` (sin pandas: la lee tambien el panel).
  - `ledger.py`  contabilidad de posiciones (precio realizado/no realizado, funding, comisiones, slippage, trades).
  - `funding.py` funding liquidado de Binance (`GET /fapi/v1/fundingRate`) con cache propia.
  - `params.py`  parametros congelados (`params.json`, versionado) y lectura del `selected_params` del laboratorio.
  - `engine.py`  la corrida horaria (pandas/numpy; llama a las MISMAS reglas de `lab/strategies.py`).
  - `view.py`    JSON para `GET /api/paperbot` (sin pandas).

Este paquete NO importa pandas en `__init__`: el panel importa `store`/`view` y corre sin pandas.
"""
