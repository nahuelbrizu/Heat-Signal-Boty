"""
Contexto macro y de mercados de prediccion, point-in-time (2026-10-19).

    fred.py        series de FRED con fecha de publicacion (ALFRED / realtime_start): un valor existe solo desde
                   que se publico. Necesita FRED_API_KEY; sin key la fuente queda APAGADA y lo dice.
    calendar.py    proximos FOMC, CPI y empleo (NFP) desde un archivo versionado (config/calendar.json) con la
                   fuente oficial de cada fecha.
    polymarket.py  probabilidades implicitas de mercados de Polymarket (Gamma API publica, sin key), lista
                   configurable en config/polymarket_markets.json.
    store.py       base propia append-only (data/heatsignal_macro.sqlite): observaciones, snapshots y corridas.
    context.py     bloque corto "Macro y eventos" para el Analista de Contexto (< ~400 tokens, lenguaje historico).
    job.py         `python -m heatsignal.macro run` (timer horario): Polymarket cada hora, FRED una vez por dia,
                   export diario a data/export/macro/ (lo sube a Drive el timer de historia).

Corre en la VM: sin pandas. La carga para el laboratorio (pandas, PC) vive en `lab/macro_features.py`.
"""
