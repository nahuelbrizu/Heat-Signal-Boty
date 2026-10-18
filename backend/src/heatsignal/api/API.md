# API del panel interno (solo lectura)

Contrato para la single page del panel. Servida por `api/panel.py` + `api/panel_api.py` dentro del proceso
de ingesta, en `127.0.0.1:8000` de la VM (se abre con túnel SSH). Todo es `GET` y nada escribe.

## Convenciones

- **JSON limpio**: sin NaN/inf (se mandan como `null`).
- **Tiempos**: cada instante va dos veces: `*_ms` (epoch ms UTC) y su ISO 8601 UTC (`*_at` o `t`, con
  `+00:00`).
- **Parámetros de tiempo** (`since`, `until`): epoch ms (solo dígitos) o ISO 8601
  (`2026-10-17T00:00:00Z`; sin zona se toma UTC). Intervalos semiabiertos `[since, until)`.
- **Paginación**: `{"total", "limit", "offset", "items": [...]}`. `total` = cuántos cumplen el filtro
  (no solo la página). Para la página siguiente: `offset += limit` mientras `offset < total`.
- **Símbolos**: se aceptan `SOLUSDT`, `sol-usdt` o `SOL/USDT`; las respuestas usan el formato interno
  `SOL-USDT`. Noticias: activo base (`XRP`, también acepta `XRPUSDT`).
- **Errores**: cuerpo `{"detail": "mensaje"}`.
  - `422`: parámetro con tipo o rango inválido (lo valida FastAPI; `detail` es una lista de errores).
  - `400`: valor bien tipado pero sin sentido (fecha ilegible, `since >= until`, rango demasiado grande,
    símbolo o `run_id` con caracteres inválidos).
  - `404`: no existe. `503`: la parte del sistema que lo provee está apagada (sin análisis, sin grabación,
    sin ventana en memoria).

## Qué pollear y cada cuánto

| Endpoint | Peso | Sugerencia |
|---|---|---|
| `/api/summary` | liviano (memoria + 2-3 consultas chicas; conteos de la base cacheados 30 s) | cada 5 s, siempre |
| `/api/market/{symbol}` | liviano a medio (recorre la ventana en RAM del símbolo) | cada 5–10 s, solo la vista abierta |
| `/api/hypotheses`, `/api/strategies`, `/api/cycles` | liviano (base de evaluación chica, con índices) | al abrir la vista y cuando cambien los `counts` del summary |
| `/api/analysis/latest` | medio (parsea los últimos 200 reportes; cache 30 s) | cuando cambie `summary.analysis.last_ran.run_id` |
| `/api/cycles/{run_id}` | liviano si está en `cycle_log`; si cae al checkpoint, 0,1–2 s en otro hilo (se cachea) | a demanda |
| `/api/news` | liviano (índice por fecha; corre en otro hilo) | al abrir; cada 5 min como mucho (el poll RSS es cada 5 min) |
| `/api/history/summary` | escanea tablas grandes, pero cacheado 30 s y en otro hilo | al abrir la vista |
| `/api/history/bars` | **pesado** para rangos largos (ver abajo); en otro hilo | a demanda; no pollear |
| `/api/history/liquidations` | liviano a medio (en otro hilo) | a demanda |
| `/api/history/storage` | liviano (catálogo + resumen cacheado 30 s) | al abrir Datos / Sistema |
| `/api/lab`, `/api/lab/{id}` | lee archivos (se releen solo si cambian) | al abrir; cambian cuando se publica desde la PC |
| `/api/paper` | lee la base de Freqtrade (read-only) + 4 llamadas a su API REST, en otro hilo; cache 10 s | al abrir la vista; cada 30 s si está abierta |
| `/api/paper/compare` | lee un archivo (se relee solo si cambia) | al abrir; cambia una vez por semana |
| `/api/paperbot`, `/api/paperbot/{id}` | lee `paperbot.sqlite` (read-only, otro hilo); cache 10 s | al abrir la vista; cada 60 s si está abierta (el bot escribe una vez por hora) |
| `/api/system` | liviano | al abrir la vista de sistema; cada 30–60 s si está abierta |
| `/api/status` | **pesado** (todo junto, incluye features de todos los símbolos) | solo para el panel viejo; la SPA no lo necesita |

Para saber si hay algo nuevo sin pedir listas: comparar entre polls de `/api/summary`
`counts.hypotheses.total`, `counts.hypotheses.resolved`, `counts.cycles.total` y
`analysis.last_ran.run_id`. Si cambia `build`, el proceso se reinició (deploy): recargar la página.

---

## `GET /api/summary`

Estado liviano para el polling frecuente.

```json
{
  "build": "49bfcd0f46a0",
  "now_ms": 1792253686223, "now_at": "2026-10-17T16:14:46.223000+00:00",
  "uptime_s": 3600.1,
  "workers": [
    {"worker": "binance", "connected": true, "last_message_age_s": 0.3, "reconnects": 0, "uptime_ratio": 0.9998}
  ],
  "memory": {
    "started_at_ms": 1792250085913, "started_at": "2026-10-17T15:14:45.913000+00:00", "accumulated_s": 3600.3,
    "stats": {"written": 912345, "expired": 800000, "evicted_by_cap": 0,
              "in_memory": {"liquidation": 2, "trade": 51000, "ticker": 21000}},
    "symbols": {
      "SOL-USDT": {"counts": {"liquidation": 2, "trade": 2, "ticker": 3},
                   "mark_by_source": {"okx": 150.2, "binance": 150.1}, "mark": 150.15, "last_trade_age_s": 0.4}
    }
  },
  "analysis": {
    "enabled": true, "graph_enabled": true, "llm_providers": ["anthropic", "google"], "interval_s": 900,
    "last_cycle": {"run_id": "cycle-1792253100000", "ts_ms": 1792253100000, "ts_at": "...", "status": "ran",
                   "n_hypotheses": 0, "detail": "XRP-USDT=no_hypothesis", "age_s": 586.2},
    "last_ran":   {"run_id": "cycle-1792253100000", "...": "igual que last_cycle", "decisions": [
                   {"symbol": "XRP-USDT", "outcome": "no_hypothesis", "reason": "...", "strategy_id": null}]},
    "next_cycle_eta_ms": 1792254000000
  },
  "counts": {
    "strategies": {"total": 2, "by_status": {"running": 1, "pending": 1}},
    "hypotheses": {"total": 4, "pending": 1, "due": 0, "resolved": 2, "expired_no_data": 1, "wins": 1},
    "cycles": {"total": 3, "by_status": {"error": 1, "ran": 1, "warmup": 1}},
    "news_window": 3
  },
  "freshness": {
    "workers_last_message_age_s": {"binance": 0.3},
    "last_cycle_age_s": 586.2,
    "news_feeds": {"coindesk": {"last_ok_age_s": 120.5, "error": null}},
    "recorder_last_flush_age_s": 4.1
  },
  "recording": {
    "enabled": true, "status": "ok", "last_flush_age_s": 4.1,
    "stats": {"events": 912345, "bars_written": 4000, "liquidations_written": 50, "late_events": 3,
              "write_errors": 0, "last_flush_ms": 1792253682000},
    "rows": {"bars_1m": 20, "liquidations": 3, "news": 3}, "size_bytes": 152256, "summary_age_s": 12.0
  },
  "lab": {"generated_at": "2026-10-17T15:00:00+00:00", "strategies": 7, "passing": 1},
  "paper": {"available": true, "open": 1, "closed": 0, "realized": 0.0, "bot_start_ms": 1792255535139,
            "last_trade_ms": 1792255537487, "read_at_ms": 1792260000000,
            "compare": {"generated_at": "...", "status": "ok", "window": {"...": "..."}, "paper_total": -1.2,
                        "sim_total": -0.9, "diff": -0.3, "runs": 1}}
}
```

- `counts.*` es `null` si esa parte está apagada. `hypotheses.due` = pendientes ya vencidas que esperan
  al job de resolución (timer de 5 min).
- `recording.status`: `ok` (escribió hace < 120 s), `starting` (recién arrancado), `stale` (no escribe),
  `off` (grabación apagada).
- `lab`: `null` sin resumen publicado; `{"error": "..."}` si el archivo está roto.
- `paper`: `null` si el paper trading no está configurado; `{"available": false, "error": "Freqtrade no esta: ..."}`
  si no existe la base del dry-run. Sale solo de la base (cache 15 s, otro hilo; no llama a la API REST).
  `open` es el contador del menú. `compare`: `null` sin comparación publicada.

## `GET /api/hypotheses`

Todas las hipótesis, filtradas y paginadas (más nuevas primero por defecto).

| Parámetro | Tipo | Default | Notas |
|---|---|---|---|
| `strategy_id` | UUID | — | 422 si no es UUID |
| `symbol` | str | — | `SOLUSDT` / `SOL-USDT` |
| `status` | `pending` \| `resolved` \| `expired_no_data` | — | |
| `win` | bool | — | implica resueltas |
| `since`, `until` | tiempo | — | sobre `created_at` |
| `order` | `desc` \| `asc` | `desc` | por `created_at` |
| `limit` | 1–500 | 50 | |
| `offset` | 0–100000 | 0 | |

```json
{
  "total": 2, "limit": 1, "offset": 0,
  "items": [{
    "hypothesis_id": "52549a3b-5a1a-41b6-92af-2312cc4dd198",
    "strategy_id": "4bfb3f90-108e-495e-a49e-a19597160915",
    "run_id": "cycle-1792206109022",
    "symbol": "SOL-USDT", "direction": "SHORT", "confidence": 0.8,
    "entry_price": 100.0, "entry": "100", "horizon_s": 3600,
    "atr_normalized_trigger": null, "heatmap_model_id": null,
    "rationale": "clusters similares reaccionaron ...",
    "created_at": "2026-10-01T02:00:00+00:00", "created_ms": 1790820000000,
    "due_at": "2026-10-01T03:00:00+00:00", "due_ms": 1790823600000,
    "status": "resolved", "overdue": false,
    "resolved_at": "2026-10-01T03:00:00+00:00", "resolved_ms": 1790823600000,
    "exit_price": 103.0, "exit": "103", "exit_price_at": "2026-10-01T03:00:00+00:00",
    "win": false,
    "pnl_pct": -3.1, "gross_pnl_pct": -3.0, "cost_bps": 10.0,
    "real_move_pct": 3.0, "benchmark_pnl_pct": 3.0, "alpha_pct": -6.1
  }],
  "filters": {"strategy_id": null, "symbol": null, "status": "resolved", "win": null,
              "since_ms": null, "until_ms": null, "order": "desc"}
}
```

- Porcentajes en puntos (`2.5` = +2,5 %). `pnl_pct` es **neto** de costos; `gross_pnl_pct` bruto;
  `real_move_pct` = cuánto se movió el precio (sin importar la dirección); `benchmark_pnl_pct` = buy &
  hold en la misma ventana; `alpha_pct` = neto − benchmark.
- `entry`/`exit` son strings decimales exactos; `entry_price`/`exit_price` los mismos como número.
- `overdue`: pendiente con la ventana ya vencida (la resuelve el job cada 5 min).

## `GET /api/hypotheses/by-trend`

Tasa de acierto cortada por la tendencia del día UTC al emitir (`daily_trend`) y por la dirección respecto de ella
(`trend_alignment`). Filtros: `strategy_id`, `symbol`, `timeframe` (`1h` | `none`). Cada segmento trae `total`,
`pending`, `expired_no_data`, `n` (resueltas), `wins`, `rate`, `ci95` (Wilson 95 %, `null` con n = 0),
`mean_pnl_pct`, `mean_benchmark_pnl_pct` (buy & hold en la misma ventana) y `mean_alpha_pct`. Las hipótesis
anteriores al contexto 1h caen en `sin_registro`.

```json
{"filters": {"strategy_id": null, "symbol": "XRP-USDT", "timeframe": "1h"}, "rule": "htf-trend-v1 (features/htf.py)",
 "overall": {"total": 12, "pending": 2, "expired_no_data": 0, "n": 10, "wins": 6, "rate": 0.6, "ci95": [0.31, 0.83], "...": "..."},
 "by_daily_trend": [{"daily_trend": "alcista", "n": 6, "wins": 4, "rate": 0.667, "ci95": [0.3, 0.9], "...": "..."}],
 "by_alignment": [{"trend_alignment": "a_favor", "...": "..."}],
 "by_trend_and_alignment": [{"daily_trend": "alcista", "trend_alignment": "a_favor", "...": "..."}]}
```

`GET /api/hypotheses` acepta además `timeframe`, `daily_trend` y `trend_alignment` (`none` = hipótesis sin el
dato) y cada hipótesis trae `timeframe`, `horizon_bars`, `daily_trend`, `trend_alignment`, `htf_model_id` y
`htf_snapshot` (números del contexto 1h al emitir; `null` en las viejas).

## `GET /api/htf/{symbol}?candles=48`

Contexto multi-temporalidad de una moneda (`HtfContext`, `schema/features.py`): últimas ≤ 48 velas de 1h CERRADAS
(`candles_1h`: `open_ms`, `open_at`, OHLC, `source` = `klines` | `bars_1m` | `rest`, `minutes`), día UTC
(`day_open`, `day_high`, `day_low`, `change_from_open_pct`/`_atr`, `pos_in_day_range`), EMA20/SMA50 de 1h y su
pendiente, EMA10/SMA20 diarias, ATR14 de 1h, `daily_trend` + `trend_reason`, `setups` y `data_gaps`. 503 si el
contexto está apagado (`HEATSIGNAL_HTF=0`), 404 si el símbolo no está configurado.

## `GET /api/sessions?day=YYYY-MM-DD`

Diario del día UTC (por defecto hoy): las sesiones configuradas (`HEATSIGNAL_SESSIONS`; por defecto Asia/Tokio 09:00,
Europa/Londres 08:00, EE.UU./Nueva York 09:30 y el cierre UTC 23:00) con su hora local, `open_ms`/`open_at` (UTC,
DST resuelto por zoneinfo), `trigger_ms` (cierre de la primera vela de 1h), `triggered`, y las filas del diario:
`code` (análisis por código: `summary` + `data` = `SessionAnalysis` con la vela de apertura, tendencia del día,
rango de la sesión anterior, datos propios del día y banderas por símbolo) y `llm` (`status` `con_llm` | `sin_llm` |
`error`, `summary` del Redactor, `run_id`). Además `mode` (`sessions` | `interval`), `next` (próximo disparo) y
`days` (días con diario). 503 si no hay sesiones configuradas; 400/422 si el día es inválido.

`GET /api/hypotheses` acepta `session` (`asia` | `europa` | `eeuu` | `cierre` | `none`) y cada hipótesis trae
`session` y `session_day`; `GET /api/hypotheses/by-trend` trae además `by_session`.

## `GET /api/hypotheses/{hypothesis_id}`

La misma forma que un item de la lista, más:

```json
{
  "...": "campos de la hipotesis",
  "strategy": {"strategy_id": "...", "description": "rebote", "status": "running",
               "entry_conditions": {"symbol": "SOL-USDT"}, "exit_conditions": {"horizon_s": 3600}},
  "cycle": {"run_id": "cycle-1792206109022", "ts_ms": 1792206109022, "ts_at": "...", "status": "ran",
            "detail": "SOL-USDT=hypothesis",
            "decision": {"symbol": "SOL-USDT", "outcome": "hypothesis", "reason": "...", "strategy_id": "..."},
            "has_report": true,
            "analysis": {"liquidations": {"confidence": 0.5, "text": "..."}, "order_flow": null, "context": null,
                         "news": null, "debate": [], "report": "..."}},
  "cycle_url": "/api/cycles/cycle-1792206109022"
}
```

`cycle` es `null` si la hipótesis no tiene `run_id` o el ciclo no está en el historial (usar `cycle_url`
igual: puede estar en los checkpoints). 404 si no existe; 422 si el id no es UUID.

## `GET /api/strategies`

```json
{
  "total": 2,
  "items": [{
    "strategy_id": "4bfb3f90-108e-495e-a49e-a19597160915",
    "description": "rebote", "status": "running", "author": "nahue",
    "registered_at": "2026-10-17T16:14:45.950737+00:00", "registered_ms": 1792253685950,
    "symbol": "SOL-USDT", "horizon_s": 3600,
    "hypotheses": {"total": 4, "pending": 1, "due": 0, "resolved": 2, "expired_no_data": 1, "wins": 1},
    "first_hypothesis_ms": 1790812800000, "last_hypothesis_ms": 1792253686021,
    "track_record": {
      "resolved": 2, "wins": 1, "rate": 0.5, "ci95": [0.0945, 0.9055],
      "mean_pnl_pct": -0.6, "mean_gross_pnl_pct": -0.5, "mean_benchmark_pnl_pct": 2.5, "mean_alpha_pct": -3.1,
      "brier": 0.4, "brier_reference": 0.25, "mean_confidence": 0.7
    }
  }],
  "stats_age_s": 0.0
}
```

- `ci95`: IC 95 % de Wilson de la tasa de acierto (`null` con n = 0). Mostrar siempre n e IC junto a la
  tasa (CLAUDE.md, honestidad estadística).
- `brier`: media de (confidence − acierto)² sobre las resueltas. `brier_reference` = Brier de decir
  siempre la tasa observada: si `brier` no queda por debajo, el confidence no aporta información.
- Agregados cacheados 10 s (`stats_age_s`).

## `GET /api/strategies/{strategy_id}`

Parámetro: `limit` (1–500, default 50) = hipótesis incluidas. Forma de un item de la lista, más:

```json
{
  "entry_conditions": {"symbol": "SOL-USDT", "regla": "..."},
  "exit_conditions": {"horizon_s": 3600},
  "status_history": [
    {"status": "pending", "changed_at": "2026-10-17T16:14:45.950737+00:00", "changed_ms": 1792253685950},
    {"status": "running", "changed_at": "2026-10-17T16:14:45.960848+00:00", "changed_ms": 1792253685960}
  ],
  "calibration": [
    {"lower": 0.0, "upper": 0.1, "n": 0, "mean_confidence": null, "hit_rate": null, "ci95": null},
    {"lower": 0.6, "upper": 0.7, "n": 1, "mean_confidence": 0.6, "hit_rate": 1.0, "ci95": [0.2065, 1.0]}
  ],
  "hypotheses_page": {"total": 4, "limit": 1, "offset": 0, "items": ["..."],
                      "next": "/api/hypotheses?strategy_id=...&offset=1"}
}
```

`calibration` siempre trae 10 buckets de igual ancho (diagrama de confiabilidad). 404 si no está registrada.

## `GET /api/cycles`

Historial de ciclos del grafo, más nuevos primero, **sin** el reporte (pesa).

| Parámetro | Tipo | Default |
|---|---|---|
| `status` | `warmup` \| `no_strategies` \| `no_llm` \| `ran` \| `error` | — |
| `limit` | 1–500 | 50 |
| `offset` | 0–100000 | 0 |

```json
{
  "total": 3, "limit": 2, "offset": 0,
  "items": [
    {"run_id": "cycle-1792253100000", "ts_ms": 1792253100000, "ts_at": "...", "status": "error", "n_hypotheses": 0,
     "detail": "grafo: RateLimitError(...)", "decisions": [], "has_report": false}
  ],
  "counts": {"error": 1, "ran": 1, "warmup": 1}
}
```

## `GET /api/cycles/{run_id}`

Un ciclo con el razonamiento completo del grafo.

```json
{
  "run_id": "cycle-1792206109022", "ts_ms": 1792206109022, "ts_at": "...", "status": "ran",
  "n_hypotheses": 1, "detail": "SOL-USDT=hypothesis",
  "decisions": [{"symbol": "SOL-USDT", "outcome": "hypothesis", "reason": "...", "strategy_id": "..."}],
  "report": {
    "status": "ok", "title": "...", "summary": "...",
    "symbols": {
      "SOL-USDT": {
        "liquidations": {"confidence": 0.6, "text": "..."},
        "order_flow": {"confidence": 0.5, "text": "..."},
        "context": {"confidence": 0.5, "text": "..."},
        "news": {"confidence": 0.4, "tone": "neutral", "text": "...", "material_primary_negative": false},
        "debate": [{"side": "bull", "round": 1, "strength": 0.6, "text": "..."}],
        "report": "texto del Redactor"
      }
    }
  },
  "report_source": "cycle_log",
  "hypotheses": ["hipotesis emitidas en este ciclo (forma de /api/hypotheses)"],
  "notes": []
}
```

- `report_source`: `cycle_log` | `checkpoint` (no estaba en el historial; se leyó del estado final del
  grafo en `heatsignal_checkpoints.sqlite`, en otro hilo, y se cachea) | `checkpoint_drive` (el checkpoint ya
  se archivó: se bajó de `gdrive:archive/checkpoints/...` al cache; `notes` lo dice) | `null` (no hay razonamiento:
  `notes` dice por qué, p.ej. un `warmup`).
- Un run que solo existe en los checkpoints responde con `detail: "(solo en checkpoints)"`.
- 404 si no está en ningún lado; 400 si el `run_id` tiene caracteres raros.

## `GET /api/analysis/latest`

Último análisis del grafo de cada símbolo (con rotación, cada ciclo cubre solo algunos).

```json
{
  "symbols": {
    "SOL-USDT": {"run_id": "cycle-...", "ts_ms": 1792206109022, "ts_at": "...",
                 "decision": {"symbol": "SOL-USDT", "outcome": "no_hypothesis", "reason": "...", "strategy_id": null},
                 "analysis": {"liquidations": "...", "order_flow": "...", "context": "...", "news": "...",
                              "debate": [], "report": "..."}}
  },
  "last_ran": {"run_id": "cycle-...", "status": "ran", "n_hypotheses": 0, "detail": "...", "decisions": []}
}
```

## `GET /api/news`

Noticias guardadas (`heatsignal_history.sqlite`, tabla `news`) más las de la ventana en memoria (48 h) que
no llegaron a la base (`stored: false`).

| Parámetro | Tipo | Default | Notas |
|---|---|---|---|
| `symbol` | str | — | activo base: `XRP`, `XRPUSDT`, `XRP-USDT` |
| `source` | str | — | nombre del feed (`coindesk`, `sec`, ...) |
| `primary` | bool | — | fuente primaria (emisor/regulador) |
| `q` | str 2–100 | — | texto en título o resumen (sin distinguir mayúsculas) |
| `since`, `until` | tiempo | — | sobre `published_ms` |
| `limit` | 1–200 | 50 | |
| `offset` | 0–10000 | 0 | |

```json
{
  "total": 4, "limit": 2, "offset": 0,
  "items": [{
    "item_id": "a1b2...", "source": "coindesk", "primary": false,
    "title": "...", "summary": "...", "url": "https://...",
    "published_ms": 1792253675913, "published_at": "2026-10-17T16:14:35.913000+00:00",
    "fetched_ms": 1792253676913, "fetched_at": "2026-10-17T16:14:36.913000+00:00",
    "symbols": ["XRP"], "market_wide": false, "stored": true
  }],
  "sources": {"db": 3, "window_only": 1}
}
```

Las noticias son texto de terceros: mostrarlas escapadas, nunca como HTML.

## `GET /api/history/summary`

Qué hay guardado en la base histórica (cache 30 s, calculado en otro hilo).

```json
{
  "recorder": {"events": 912345, "bars_written": 4000, "liquidations_written": 50, "late_events": 3,
               "write_errors": 0, "last_flush_ms": 1792253682000},
  "store": {
    "path": "data/heatsignal_history.sqlite", "size_bytes": 152256,
    "bars_1m": {"rows": 20, "first_ms": 1791997200000, "last_ms": 1791997740000},
    "liquidations": {"rows": 3, "last_ms": 1791997203000},
    "news": {"rows": 3, "last_ms": 1792253656913},
    "klines": [{"exchange": "binance", "symbol": "XRP-USDT", "interval": "1m", "rows": 3000000,
                "first_ms": 1600000000000, "last_ms": 1792200000000}],
    "last_export_day": "2026-10-16", "export_days": 3
  },
  "summary_age_s": 12.0,
  "last_upload": {"ts_ms": 1792200000000, "ts_at": "...", "remote": "gdrive:", "dir": "data/export"}
}
```

`last_upload` lo escribe `history/export.py` tras un `rclone copy` exitoso (`null` hasta el primero).

## `GET /api/history/bars`

Barras propias (`bars_1m`, agregados por minuto de la ingesta) agregadas a la temporalidad pedida.

| Parámetro | Tipo | Default | Notas |
|---|---|---|---|
| `symbol` | str | **obligatorio** | |
| `exchange` | `binance` \| `bybit` \| `okx` \| `hyperliquid` | todos | |
| `tf` | `1m` \| `5m` \| `15m` \| `1h` | `1m` | |
| `since` | tiempo | `until − 500 barras` | se alinea hacia atrás al borde de `tf` |
| `until` | tiempo | ahora | |
| `combine` | bool | `false` | una sola serie sumando exchanges |

Tope: **5000 barras** por exchange (`400` si el rango pide más: acortar o subir `tf`).

```json
{
  "symbol": "SOL-USDT", "tf": "5m", "tf_ms": 300000,
  "since_ms": 1791997200000, "since_at": "2026-10-14T17:00:00+00:00",
  "until_ms": 1791997800000, "until_at": "2026-10-14T17:10:00+00:00",
  "exchange": "bybit", "combined": false, "exchanges": ["bybit"], "count": 2,
  "bars": [{
    "exchange": "bybit", "t_ms": 1791997200000, "t": "2026-10-14T17:00:00+00:00", "minutes": 5,
    "open": 100.5, "high": 106.5, "low": 99.5, "close": 105.5,
    "buy_usd": 50.0, "sell_usd": 20.0, "delta_usd": 30.0, "n_trades": 15, "block_usd": 0.0,
    "liq_long_usd": 100.0, "liq_short_usd": 0.0, "n_liqs": 1,
    "mark": 105.0, "funding_8h": 0.0001, "oi_usd": 1000000.0
  }]
}
```

- Sin `exchange` y sin `combine`: una fila por (exchange, barra), ordenadas por `t_ms` y exchange.
- `combine=true`: `exchange: "all"`; volúmenes, liquidaciones y conteos sumados; OHLC y `mark` del
  `ref_exchange` (prioridad binance > bybit > okx > hyperliquid, no se promedian velas de mercados
  distintos); `funding_8h` = media; `oi_usd` = suma de los `oi_exchanges` que lo informan (Binance no trae
  OI por WS).
- `open`/`close`/`mark`/`funding_8h`/`oi_usd` = primer/último minuto con dato dentro de la barra; pueden
  ser `null` (minuto sin trades o sin ticker). `minutes` = minutos con fila (huecos si < tf).
- Liquidaciones: `liq_long_usd` = longs liquidados (orden SELL), `liq_short_usd` = shorts.
- **Costo**: la agregación es en SQL (índice symbol+minute_ms) y corre en otro hilo, pero escanea cada
  minuto del rango: ~1 s cada ~300 mil minutos-exchange en la PC (más lento en la e2-small). Ej.: `1h`
  sobre 60 días × 4 exchanges ≈ 1,5 s. Para vistas largas usar `1h` con rangos de semanas; no pollear.

### Lectura fría (rangos archivados en Drive)

Cuando el disco de la VM pasa el umbral (80 %), `history/archive.py` sube a Drive lo más viejo (más de 60 días) y
lo borra de la base (ver "Almacenamiento por niveles" abajo). Si `since`/`until` tocan un rango archivado, el
endpoint baja esos archivos de Drive en otro hilo (cache local LRU `data/cold_cache/`, 300 MB) y los combina
con lo local. Los archivos son de un día UTC y las barras de hasta 1 h: cada barra sale entera de un solo lado.

| Parámetro extra | Tipo | Default | Notas |
|---|---|---|---|
| `allow_partial` | bool | false | si un archivo de Drive falla: devolver el resto con `partial: true` en vez de 503 |

Campos extra en la respuesta (también en `/api/history/liquidations`):

```json
{
  "source": "mixed",
  "archive": {"files": 3, "cache_hits": 1, "downloaded": 2, "seconds": 4.2, "errors": [],
              "first_ms": 1786060800000, "end_ms": 1786320000000,
              "note": "parte del rango vino de Drive (archivado): la primera consulta puede tardar; despues sale del cache"},
  "partial": false, "partial_reasons": []
}
```

- `source`: `local` (no tocó Drive; `archive` = null), `drive` (todo lo devuelto vino de Drive) o `mixed`.
- Errores: Drive caído, archivo alterado (md5 distinto al del catálogo) o remoto sin configurar → **503** con el
  motivo en `detail`; tardar más de 120 s → 503 (lo ya bajado queda en el cache para el reintento). Nunca datos
  parciales en silencio: con `allow_partial=true` se devuelve lo legible y `partial: true` + `partial_reasons`.
- Más de 120 archivos fríos en una consulta → 400 (acortar el rango).

## `GET /api/history/liquidations`

| Parámetro | Tipo | Default | Notas |
|---|---|---|---|
| `symbol` | str | todos | |
| `exchange` | exchange | todos | |
| `side` | `long` \| `short` | — | posición liquidada |
| `since`, `until` | tiempo | — | |
| `min_usd` | ≥ 0 | 0 | |
| `limit` | 1–1000 | 200 | |
| `offset` | 0–100000 | 0 | |

```json
{
  "total": 2, "limit": 1, "offset": 0,
  "items": [{"exchange": "binance", "symbol": "SOL-USDT", "ts_ms": 1791997202000, "ts_at": "...",
             "side": "buy", "position": "short", "price": 101.0, "qty": 100.0, "notional_usd": 10100.0,
             "coverage": "partial"}],
  "totals": {"n": 2, "long_usd": 200.0, "short_usd": 10100.0}
}
```

Lectura fría: igual que `/api/history/bars` (`allow_partial`, `source`, `archive`, `partial`), pero **solo si se
pasa `since`**: sin `since` el listado es de la base local y, si hay liquidaciones archivadas, viene
`"archive_note": "hay N archivos de liquidaciones en Drive (...): pedir since para incluirlos"`. Con datos fríos,
`total`/`totals` suman local + Drive y la página se arma sobre la unión ordenada por `ts_ms` descendente.

`side` es el lado de la orden de liquidación (sell = long liquidado). `coverage`: `full` (Bybit) o
`partial` (Binance ≤ 1/s por símbolo, OKX no informa el total): no comparar montos entre exchanges como si
fueran completos. `totals` suma todo el filtro, no solo la página.

## `GET /api/history/storage`

Almacenamiento por niveles: disco con el umbral, qué hay en caliente (VM) y en frío (Drive) por tabla y el
último archivado. 503 si la grabación está apagada.

```json
{
  "disk": {"path": "/home/nahue/heatsignal/data", "total_bytes": 10330861568, "used_bytes": 6932578304,
           "free_bytes": 2851401728, "used_pct": 70.86, "threshold_pct": 80.0, "target_pct": 70.0,
           "over_threshold": false},
  "tables": [
    {"table": "bars_1m", "label": "Barras de 1 min (propias)",
     "hot": {"rows": 2400000, "first_ms": 1786060800000, "last_ms": 1792262460000},
     "cold": {"kind": "table", "files": 30, "rows": 1200000, "bytes": 41000000, "first_ms": 1781000000000,
              "end_ms": 1786060800000, "last_archived_ms": 1792262460000, "first_at": "...", "end_at": "...",
              "last_archived_at": "..."}},
    {"table": "liquidations", "...": "..."}, {"table": "news", "...": "..."},
    {"table": "klines_1m", "hot": {"rows": 600000, "symbols": 7, "first_ms": 0, "last_ms": 0}, "cold": null},
    {"table": "checkpoints", "hot": {"path": "...", "exists": true, "size_bytes": 8192000}, "cold": null},
    {"table": "export", "hot": {"days": 3, "last_day": "2026-10-17"}, "cold": {"files": 120, "...": "..."}}
  ],
  "last_run": {"run_id": "arch-1792262460000", "started_ms": 0, "finished_ms": 0, "started_at": "...",
               "finished_at": "...", "status": "ok", "threshold_pct": 80.0, "target_pct": 70.0,
               "used_pct_before": 80.4, "used_pct_after": 69.9, "freed_bytes": 1100000000,
               "detail": {"archived": ["klines 2025-10 (...)"], "errors": [], "vacuum": []}},
  "runs": ["... ultimas 5 ..."],
  "last_check": {"mode": "run", "ts_ms": 0, "status": "skipped", "note": "uso 70.9 % < umbral 80.0 %: ..."},
  "history_db": {"auto_vacuum": "none", "freelist_bytes": 0, "size_bytes": 523976704},
  "cache": {"dir": ".../data/cold_cache", "files": 2, "bytes": 2400000, "cap_bytes": 314572800},
  "remote": "gdrive:archive", "remote_error": null, "cold_reads": true,
  "keep_days": 60, "checkpoint_keep_days": 30,
  "protected": ["heatsignal_eval.sqlite", "freqtrade_dryrun.sqlite"], "summary_age_s": 3.1
}
```

- `used_pct` es el de `df`: usado / (usado + disponible). `threshold_pct`/`target_pct` salen de
  `HEATSIGNAL_ARCHIVE_THRESHOLD` / `HEATSIGNAL_ARCHIVE_TARGET` (default 80 / 70).
- `cold` es `null` si nunca se archivó esa tabla. `cold.end_ms` es exclusivo. `export` en frío = días cuya carpeta
  local `data/export/YYYY/MM/DD` se verificó contra Drive y se borró de la VM.
- `last_run` = última corrida que hizo algo (o falló); `last_check` = último `archive run` (aunque no hiciera
  nada), leído de `data/archive_status.json`. `status`: `ok`, `nothing`, `partial` (no llegó al objetivo con lo
  elegible), `error` (no se borró nada de la unidad que falló), `skipped` (bajo el umbral).
- `history_db.auto_vacuum`: `none` = lo borrado queda como espacio libre dentro de la base (se reusa) hasta un
  VACUUM completo; `incremental` = se devuelve al disco en cada corrida.

### Almacenamiento por niveles (resumen)

`python -m heatsignal.history.archive [check|run|status|vacuum]` en la VM (`heatsignal-archive.timer`, cada hora).
Orden: exports locales ya verificados en Drive → velas 1m (re-descargables) → bars_1m/liquidations/news → checkpoints
del grafo. Cada unidad: CSV.gz (mismo formato que el export) → `rclone copyto --immutable` a `gdrive:archive/...`
→ verificación de tamaño + md5 → DELETE + alta en `archive_catalog` en una sola transacción. Nunca
`heatsignal_eval.sqlite` ni `freqtrade_dryrun.sqlite`. Detalle en el docstring de `history/archive.py`.

## `GET /api/lab`

El `lab_summary.json` publicado desde la PC (`python -m heatsignal.lab.publish --upload`). 404 si no hay.

```json
{
  "generated_at": "2026-10-17T15:00:00+00:00",
  "strategies": [{
    "strategy_id": "donchian-ensemble-v1", "spec_hash": "86061813c7d1691c", "run": "20261017T153637",
    "runs": 1, "symbols": ["BTCUSDT", "..."], "oos_range": ["2021-03-13", "2026-07-15"], "windows": 65,
    "n_combinations": 2, "sharpe": 1.08, "sharpe_ci95": [0.10, 2.01], "psr": 0.995, "dsr": 0.994,
    "total_return": 0.43, "max_drawdown": 0.098, "trades": 635, "hit_rate": 0.21, "breakeven_bps": 76.7,
    "breakdown": {"precio": 0.53, "funding": -0.054, "comisiones": 0.031, "slippage": 0.012, "total": 0.43},
    "basket_sharpe": 0.63, "random_p": 0.0099,
    "verdict": {"pasa": true, "checks": {"opera": true, "dsr": true, "breakeven": true, "...": true},
                "criterios": {"dsr_min": 0.95, "breakeven_vs_cost_min": 2.0, "p_random_max": 0.05}},
    "holdout": "SELLADO", "holdout_start": "2026-07-16 00:00:00+00:00", "has_detail": true
  }]
}
```

## `GET /api/lab/{strategy_id}`

`strategy_id` del laboratorio (`[a-z0-9][a-z0-9._-]*`, p.ej. `donchian-ensemble-v1`; 400 si no). Lee
`data/lab_detail_<id>.json`. 404 si no está publicado.

```json
{
  "strategy_id": "donchian-ensemble-v1", "spec_hash": "86061813c7d1691c",
  "run": "20261017T153637", "runs": ["20261017T153637"], "symbols": ["BTCUSDT", "..."],
  "data": {"first": "...", "last": "...", "holdout_start": "...", "holdout": "SELLADO"},
  "n_combinations": 2,
  "walk_forward": {"train_days": 180, "test_days": 30, "windows": 65, "oos": ["2021-03-13", "2026-07-15"],
                   "chosen": [{"train": ["2020-09-14", "2021-03-13"], "test": ["2021-03-13", "2021-04-12"],
                               "params": {"target_vol": 0.15}, "train_sharpe": 3.6}]},
  "oos": {"sharpe": 1.08, "sharpe_ci95": [0.10, 2.01], "psr": 0.995, "dsr": 0.994, "sr0_annual": 0.013,
          "min_trl_days": 808, "days": 1950, "total_return": 0.43, "max_drawdown": 0.098, "trades": 635,
          "hit_rate": {"hit_rate": 0.21, "lo": 0.18, "hi": 0.25, "n": 635, "wins": 135}},
  "costs": {"breakeven_bps_per_side": 76.7, "expected_cost_bps_per_side": 7.0, "breakeven_vs_expected": 10.95,
            "cost_curve": [{"cost_bps": 0.0, "sharpe": 1.19, "ann_return": 0.089},
                           {"cost_bps": 7.0, "sharpe": 1.08, "ann_return": 0.081}]},
  "breakdown": {"precio": 0.53, "funding": -0.054, "comisiones": 0.031, "slippage": 0.012, "total": 0.43},
  "baselines": {"buy_and_hold_sharpe": {"BTCUSDT": -0.12, "...": 0.0}, "basket_sharpe": 0.63,
                "random_entries": {"n": 100, "median_sharpe": 0.43, "p_value": 0.0099}},
  "verdict": {"pasa": true, "checks": {"...": true}, "criterios": {"...": 0}},
  "equity_daily": {
    "strategy": [["2021-03-13", 0.0012], ["2021-03-14", 0.0031]],
    "basket": [["2021-03-13", -0.004]],
    "buy_and_hold_mean": [["2021-03-13", -0.006]],
    "buy_and_hold": {"BTCUSDT": [["2021-03-13", -0.01]]}
  },
  "notes": [],
  "generated_at": "2026-10-17T15:00:00+00:00"
}
```

- `equity_daily`: `[fecha UTC, retorno acumulado al cierre de esa fecha]`, suma simple de retornos
  diarios (la misma convención que `total_return`; 0.43 = +43 % sobre el capital inicial). `null` en
  resultados anteriores a la curva (lo explica `notes`).
- Sharpe anualizado; `max_drawdown` y retornos en fracción (0.098 = 9,8 %); costos en bps por lado.

## `GET /api/paper`

Paper trading: Freqtrade 2026.9 en **dry-run** de `donchian-ensemble-v1` (dinero simulado). Lee la base SQLite de
Freqtrade en **solo lectura** (`file:...?mode=ro` + `PRAGMA query_only`, en otro hilo) y, si responde, su API REST
(`/api/v1/show_config`, `health`, `balance`, `status`) con las credenciales de `config.secrets.json` (nunca se
devuelven). Cache 10 s. Configuración por entorno del proceso de ingesta:

| Variable | Default |
|---|---|
| `HEATSIGNAL_FREQTRADE_DB` | `~/heatsignal/data/freqtrade_dryrun.sqlite` |
| `HEATSIGNAL_FREQTRADE_SECRETS` | `~/heatsignal/freqtrade/config.secrets.json` (`api_server.username/password`) |
| `HEATSIGNAL_FREQTRADE_API` | `http://127.0.0.1:8081` (vacío = no consultar la API) |
| `HEATSIGNAL_PAPER_WALLET` | `10000` (wallet inicial si la API no responde) |

**503** si el paper trading no está configurado o si no existe/no se puede leer la base ("Freqtrade no esta: no
existe la base del dry-run (...)"). Si la base existe pero la API no responde: 200 con `source.api.ok = false`,
`unrealized_pnl`/`wallet.unrealized`/`wallet.equity` en `null` y una nota.

```json
{
  "source": {"db": "/home/nahue/heatsignal/data/freqtrade_dryrun.sqlite", "read_at_ms": 1792260000000,
             "read_at": "...", "schema": "freqtrade-2026.9", "api": {"ok": true, "url": "http://127.0.0.1:8081", "error": null}},
  "bot": {"dry_run": true, "state": "running", "strategy": "DonchianEnsembleV1", "bot_name": "heatsignal-donchian",
          "version": "2026.9", "timeframe": "4h", "trading_mode": "futures", "max_open_trades": 7,
          "bot_start_ms": 1792255535139, "bot_start_at": "...", "startup_ms": 1792257552663, "startup_at": "...",
          "uptime_s": 2447.3, "paper_days": 0.05, "last_process_ms": 1792259990000, "last_process_at": "..."},
  "wallet": {"start": 10000.0, "total_api": 9999.7, "realized": 0.0, "unrealized": 1.85, "equity": 10001.85,
             "return": 0.000185, "realized_return": 0.0, "fees": 0.29, "funding": 0.0, "stake_open": 589.83},
  "totals": {"open": 1, "closed": 0, "wins": 0, "hit_rate": null, "ci95": null, "orders": 1, "adjustments": 0},
  "open_trades": [{"trade_id": 1, "pair": "TRX/USDT:USDT", "symbol": "TRXUSDT", "direction": "long",
                   "open_ms": 1792255537487, "open_at": "...", "open_rate": 0.33475, "amount": 1762.0,
                   "stake_amount": 589.83, "max_stake_amount": 589.83, "leverage": 1.0, "fees": 0.29, "funding": 0.0,
                   "enter_tag": "donchian", "orders": 1, "adjustments": 0,
                   "fills": [{"time_ms": 1792255537486, "t": "...", "kind": "entrada", "price": 0.33475, "units": 1762.0,
                              "notional": 589.83, "fee": 0.29, "tag": "donchian"}],
                   "current_rate": 0.3358, "unrealized_pnl": 1.85, "unrealized_ratio": 0.0031,
                   "realized_partial": 0.0, "duration_s": 2462.5, "live": true}],
  "closed_trades": [{"...": "igual que open_trades", "close_ms": 0, "close_at": "...", "close_rate": 0.0, "pnl": -3.1,
                     "pnl_ratio": -0.005, "exit_reason": "exit_signal", "duration_s": 86400, "win": false}],
  "equity": [{"time_ms": 1792255535139, "t": "...", "equity": 10000.0, "pnl": 0.0, "trade_id": null},
             {"time_ms": 1792260000000, "t": "...", "equity": 10001.85, "pnl": null, "trade_id": null, "mark_to_market": true}],
  "by_pair": [{"symbol": "TRXUSDT", "pair": "TRX/USDT:USDT", "closed": 0, "wins": 0, "realized": 0.0, "fees": 0.29,
               "funding": 0.0, "open": true, "stake_open": 589.83, "unrealized": 1.85, "orders": 1, "adjustments": 0,
               "last_ms": 1792255537487, "hit_rate": null, "ci95": null}],
  "notes": ["Muestra chica: 0 operaciones cerradas. ..."],
  "compare": null,
  "frequi": {"url": "http://localhost:8081", "tunnel": "hs-ft", "help": "..."}
}
```

- Montos en US$ (USDT). `funding` positivo = cobrado, negativo = pagado (convención de Freqtrade y del laboratorio).
- `equity`: wallet inicial + PnL realizado acumulado en cada cierre; el último punto (`mark_to_market`) suma lo no
  realizado (solo si la API respondió). `realized` incluye salidas parciales de operaciones abiertas.
- `fills[].kind`: `entrada`, `ajuste+`, `ajuste-` (rebalanceo, solo si el cambio supera 5 % del capital del par) o `salida`.
- `ci95`: Wilson 95 % de la tasa de acierto. Con menos de 30 cerradas `notes` avisa que la muestra es chica.
- `compare`: resumen de la última comparación publicada (ver abajo) o `null`.

## `GET /api/paper/compare`

`paper_summary.json` que sube la PC (`python -m heatsignal.lab.paper_compare --upload`, timer semanal
`deploy/pc/heatsignal-paper-compare.timer`). 404 si no hay; 503 si el paper trading no está configurado; 500 si
el archivo está roto. Se relee solo si cambia.

```json
{
  "generated_at": "2026-10-18T01:00:00+00:00",
  "latest": {
    "kind": "paper_compare", "version": 1, "status": "ok", "strategy_id": "donchian-ensemble-v1",
    "spec_hash": "86061813c7d1691c", "params": {"timeframe": "4h", "channels": [20, 40, 80, 160], "vol_window": 180,
                                                "target_vol": 0.15, "max_leverage": 1.0},
    "window": {"start": "...", "end": "...", "start_ms": 0, "end_ms": 0, "days": 7.3, "start_source": "bot_start_time (...)"},
    "capital": {"sim": 10000, "sim_por_par": 1428.57, "paper_wallet": 10000, "paper_por_par_base": 1414.29,
                "tradable_balance_ratio": 0.99, "rebalance_min": 0.05},
    "costs_lab": {"fee_bps": 5.0, "slippage_bps": 2.0},
    "totals": {"paper": {"precio": 0, "comisiones": 0, "slippage": 0, "funding": 0, "total": 0, "realizado": 0,
                         "no_realizado": 0, "entradas": 0, "ajustes": 0, "salidas": 0, "ordenes": 0, "retorno": 0},
               "sim": {"...": "igual"}, "diff": -0.3, "diff_frac": -0.00003},
    "decomposition": {"timing": 0, "tamano": 0, "precio_fill": 0, "comisiones": 0, "slippage": 0,
                      "funding_posicion": 0, "funding_contable": 0, "total": -0.3, "residual": 1e-13},
    "by_pair": [{"symbol": "TRXUSDT", "paper": {"...": "como totals + unidades_inicio/fin, nocional_fin, funding_recalculado"},
                 "sim": {"...": "..."}, "diff": 0, "decomposition": {"...": "por par"},
                 "fill_bps": {"n": 1, "mean": 0.4, "median": 0.4, "max_abs": 0.4},
                 "trades": [{"match": "ambos", "paper_entry": "...", "paper_entry_price": 0.33475, "sim_entry": "...",
                             "sim_entry_price": 0.3347, "entry_delta_min": -0.4, "entry_delta_bps": 1.5,
                             "paper_exit": null, "sim_exit": null, "exit_delta_min": null, "exit_delta_bps": null,
                             "paper_ajustes": 0, "sim_ajustes": 1}],
                 "target_start_sim": 0.42, "paper_frac_first_entry": 0.417, "funding_events": 1}],
    "curves": {"freq": "1h", "columns": ["paper", "sim", "canasta", "buy_and_hold_medio"],
               "points": [["2026-10-17T17:00:00+00:00", 0.0001, 0.0001, 0.002, 0.003]],
               "basket_return": 0.01, "buy_and_hold_mean_return": 0.012},
    "design_differences": [{"id": "rebalanceo_5", "componente": "tamano", "texto": "..."}],
    "data": {"last_kline": "...", "gaps": [], "fills_after_window": 0, "warmup_days": 400},
    "data_update": {"jobs": 14, "rows": 10080, "errors": []},
    "notes": ["Ventana corta (...)"]
  },
  "history": [{"date": "2026-10-18", "generated_at": "...", "file": "compare_2026-10-18.json", "status": "ok",
               "window": {"...": "..."}, "paper_total": -1.2, "sim_total": -0.9, "diff": -0.3,
               "decomposition": {"...": "..."}, "paper_orders": 1, "sim_orders": 2}]
}
```

- `status`: `ok` o `sin_datos` (todavía no hay velas 1m en la PC después del inicio del paper; solo trae `notes`).
- **Descomposición exacta** de `paper − backtest` (`residual` ≈ 0): `timing` (estar en el mercado cuando el otro no),
  `tamano` (distinto tamaño con los dos posicionados: rebalanceo 5 %, reinversión, ratio 0,99, redondeo),
  `precio_fill` (fill real vs open de la vela 1m siguiente), `comisiones` (lab − paper), `slippage` (lo que carga el
  lab, el dry-run no), `funding_posicion` (tasas reales × posiciones del paper − funding del lab), `funding_contable`
  (lo que asentó Freqtrade − ese recálculo). El PnL de precio se valúa al close de la última vela 1m de la ventana.
- `design_differences`: diferencias de diseño ya documentadas (decisions.md), con el componente donde caen.
- `curves`: retorno acumulado sobre el capital (paso horario si la ventana es ≤ 31 días, si no diario).
- `history`: una fila por fecha de corrida (cada corrida es acumulada desde el inicio del paper).

## `GET /api/paperbot`

Bot propio de **paper trading del lote 3** (`heatsignal.paperbot`, timer horario a los :02 UTC). Todo SIMULADO: 10.000 USDT
ficticios por estrategia, sin reinversión, precios reales de Binance, costos 5 + 2 bps por lado y funding liquidado real.
Lee `data/paperbot.sqlite` en solo lectura (sin pandas). 503 si el bot nunca corrió o no está configurado.

```json
{"disclaimer": "SIMULADO: ...", "simulated": true, "read_at": "...", "started_ms": 1792...,
 "last_run": {"run_id": "pb-...", "hour_ms": 1792..., "status": "ok|error|skipped", "reason": "...", "mode": "consecutiva|inicio|hueco",
              "duration_s": 5.1, "peak_rss_kb": 142944, "detail": {"chequeo_velas": {...}, "estrategias": {...}}},
 "last_ok": {...}, "stale": false, "counts": {"ok": 120, "error": 2, "skipped": 1},
 "runs": [ ...últimas 72 corridas... ], "gaps": [{"from_ms": ..., "to_ms": ..., "hours": 3}],
 "lab_generated_at": "2026-10-18T...",
 "strategies": [{"strategy_id": "tsmom-diario-v1", "spec_hash": "7515d798e5d66a55", "params": {"target_vol": 0.15},
                 "origen": "walk-forward del lab 2026-10-18 (...)", "universe": ["BTCUSDT", ...], "hour_ms": ...,
                 "equity": 10012.3, "return": 0.0012, "basket_equity": 9990.1, "basket_return": -0.001,
                 "realized": 3.1, "unrealized": 15.2, "funding": -1.2, "fees": 3.5, "slippage": 1.4, "exposure": 0.39,
                 "positions": [{"symbol": "BTCUSDT", "weight": -0.437, "target": -0.437, "units": -0.0..., "entry_price": ...,
                                "mark_price": ..., "notional": ..., "unrealized": ...}],
                 "n_orders": 9, "n_trades": 2, "wins": 1, "win_ci95": [0.09, 0.91], "spark": [[hour_ms, equity, basket], ...],
                 "lab": {"pasa": false, "sharpe": 0.67, "sharpe_ci95": [-0.18, 1.51], "dsr": 0.94, "checks": {...},
                         "same_spec": true, "...": "veredicto del backtest (lab_summary.json); null si no hay"}}],
 "notes": ["La ultima corrida termino en error: ..."]}
```

- Una corrida por hora; `error` = hueco de datos / fallo de descarga / regla rota: esa hora **no se opera**.
- `gaps`: horas sin corrida ok (VM apagada o errores). No se simulan después: la corrida siguiente lleva las
  posiciones al objetivo de la regla (órdenes con `reason` = `hueco`).

## `GET /api/paperbot/{strategy_id}`

Lo mismo que un elemento de `strategies` más: `equity_hourly` (`[[hour_ms, equity, basket_equity]]`), `breakdown`
(`precio = realizado + no_realizado`, `funding`, `comisiones`, `slippage`, `total`; suma exacta), `breakdown_by_symbol`,
`orders` (últimas 500: `decision_ms`, `fill_ms`, `price`, `price_source` = `open_1m_siguiente|ultimo_precio`,
`weight_from/to`, `units_from/to`, `notional`, `fee`, `slippage`, `reason` = `senal|resincronizacion|inicio|hueco`),
`trades` cerrados, `funding_events` (`rate`, `units` vigentes justo antes, `price`, `cash`), `basket`, `max_drawdown` y
`lab` (veredicto del backtest). 400 si el id no es válido, 404 si el bot no tiene esa estrategia.

## `GET /api/market/{symbol}`

Snapshot de mercado desde la ventana en memoria (solo símbolos configurados; 404 si no).

| Parámetro | Rango | Default |
|---|---|---|
| `trades` | 0–500 | 50 |
| `liquidations` | 0–500 | 50 |

```json
{
  "symbol": "SOL-USDT", "now_ms": 1792253686273, "now_at": "...",
  "exchanges": {
    "binance": {"mark": 150.1, "last": null, "funding_8h": 0.0001, "open_interest": null, "oi_usd": null,
                "ts_ms": 1792253681913, "ts_at": "...", "age_s": 4.4, "funding_age_s": 6.4},
    "okx": {"mark": 150.2, "last": null, "funding_8h": 0.0003, "open_interest": 1000.0, "oi_usd": 150200.0,
            "ts_ms": 1792253682913, "ts_at": "...", "age_s": 3.4, "funding_age_s": 3.4}
  },
  "mark_mean": 150.15, "funding_8h_mean": 0.0002, "oi_usd_total": 150200.0, "oi_exchanges": 1,
  "flow_5m": {"binance": {"buy_usd": 0.0, "sell_usd": 300.0, "n": 1, "delta_usd": -300.0}},
  "liquidations_window": {
    "1h":  {"long_usd": 25000.0, "short_usd": 0.0, "n": 1,
            "by_exchange": {"bybit": {"long_usd": 25000.0, "short_usd": 0.0, "n": 1}}},
    "24h": {"long_usd": 25000.0, "short_usd": 5000.0, "n": 2, "by_exchange": {"...": {}}}
  },
  "last_trades": [{"exchange": "bybit", "ts_ms": 1792253681913, "ts_at": "...", "side": "buy",
                   "price": 151.0, "qty": 1.0, "usd": 151.0, "block": false}],
  "last_liquidations": [{"exchange": "bybit", "ts_ms": 1792253683913, "ts_at": "...", "side": "sell",
                         "position": "long", "price": 149.0, "qty": 167.8, "notional_usd": 25000.0,
                         "coverage": "full"}],
  "window_counts": {"liquidation": 2, "trade": 2, "ticker": 3},
  "open_hypotheses": 1,
  "last_analysis": {"run_id": "cycle-...", "ts_ms": 1792206109022, "ts_at": "...", "decision": {}, "analysis": {}}
}
```

- Por exchange se toma el último valor **no nulo** de cada campo del ticker (los exchanges mandan mark,
  funding y OI en mensajes distintos); `funding_age_s` dice qué tan viejo es el funding.
- `oi_usd`: el que da el exchange o `open_interest × mark`. Binance no trae OI por WS.
- `flow_5m`: volumen taker de los últimos 5 min. `liquidations_window`: 1 h y 24 h (retención de la
  ventana de liquidaciones = 24 h). Ojo: la ventana se vacía en cada reinicio (ver `summary.memory`).
- `last_trades` en orden de llegada (más nuevo primero); `last_liquidations` por hora del evento.

## `GET /api/system`

`disk` trae además `used_bytes`, `used_pct` (como `df`), `threshold_pct` y `target_pct` del archivado a Drive.

```json
{
  "build": "49bfcd0f46a0",
  "version": {"commit": "5054134bb2bf...", "source": "git", "branch": "ingestion-binance-bybit",
              "source_mtime_ms": 1792253585855, "source_mtime_at": "..."},
  "python": "3.13.1", "pid": 5679, "rss_bytes": 63754240,
  "started_ms": 1792253686112, "started_at": "...", "uptime_s": 3600.2, "now_ms": 1792257286277, "now_at": "...",
  "config": {"symbols": ["BTCUSDT", "..."], "exchanges": ["binance", "bybit", "okx", "hyperliquid"],
             "storage": "memory", "analysis_interval_s": 900, "analysis_window_s": 1800},
  "settings": {"news_interval_s": 300, "graph_rotation": 2, "record": true,
               "retention_s": {"trade": 1800, "ticker": 7200, "liquidation": 86400},
               "ticker_interval_s": 1.0, "stale_after_s": 30, "health_log_s": 60, "panel": "127.0.0.1:8000",
               "log_level": "INFO"},
  "databases": {
    "eval": {"path": "data/heatsignal_eval.sqlite", "exists": true, "size_bytes": 69632,
             "modified_ms": 1792253686095, "modified_at": "..."},
    "history": {"...": "igual"}, "checkpoints": {"...": "igual"},
    "lab_summary": {"...": "igual"}, "lab_details": 7
  },
  "disk": {"path": "data", "total_bytes": 10000000000, "free_bytes": 4000000000},
  "export": {"last_export_day": "2026-10-16", "export_days": 3,
             "last_upload": {"ts_ms": 1792200000000, "ts_at": "...", "remote": "gdrive:", "dir": "data/export"}},
  "errors": {
    "cycles": [{"run_id": "cycle-...", "ts_ms": 1792250000000, "ts_at": "...", "detail": "grafo: ..."}],
    "news_feeds": {"ripple": "HTTPError: 404"},
    "workers_down": [],
    "recorder_write_errors": 0,
    "memory_evicted_by_cap": 0
  }
}
```

- `version.commit`: del `.git` del repo, o de un archivo `REVISION` en la raíz si existe (en la VM el código
  se copia con `git archive`: escribir `git rev-parse HEAD > REVISION` al desplegar). `source_mtime_*` =
  archivo `.py` más nuevo (`git archive` conserva la fecha del commit).
- `size_bytes` incluye los `-wal`/`-shm` de SQLite.

## Endpoints previos (sin cambios)

- `GET /api/status`: todo junto, como lo usa `panel.html`. Mismo contenido de siempre (el resumen de la
  base histórica ahora se calcula en otro hilo; NaN del laboratorio salen como `null`).
- `GET /api/features`: FeatureSet en vivo de todos los símbolos (pesado: calcula features).
- `GET /api/klines?symbol=&interval=&limit=`: velas de Binance pedidas en el momento (no guardadas).
- `GET /static/lightweight-charts.js`, `GET /health`, `GET /`.
- `GET /static/app/{ruta}.js|.css`: módulos ES y estilos de la SPA (`static/app/`, sin build ni CDN; `Cache-Control:
  no-cache`; lista blanca de nombres, 404 para cualquier otra cosa). El `build` incluye su contenido: cambiar un archivo
  de la SPA y reiniciar recarga las páginas abiertas.

---

# Acciones (POST)

Son lo único que no es de solo lectura. Todo POST exige:

- header `X-HeatSignal-Action: 1` → si falta, **403** (protección contra requests cruzados: otra página no
  puede mandar un header propio sin un preflight CORS que el panel no acepta);
- `Content-Type: application/json` → si no, **415**;
- cuerpo ≤ 4096 bytes → si no, **413**; JSON ilegible → **400**; campos de más o faltantes → **422**.

Ejemplo desde la SPA:

```js
fetch("/api/analyze", {method: "POST", headers: {"Content-Type": "application/json", "X-HeatSignal-Action": "1"},
                       body: JSON.stringify({symbol: "XRPUSDT"})})
```

Los **429** traen `{"detail", "retry_after_s"}` y el header `Retry-After`.

## `POST /api/analyze` — "Analizar ahora"

Body: `{"symbol": "XRPUSDT"}` (cualquier formato de símbolo; tiene que estar configurado).

Corre un ciclo del grafo LLM **solo para ese símbolo**, fuera de turno, en una tarea asyncio
(`AnalysisLoop.run_symbols`). Mismas reglas que el ciclo agendado: si la ventana está en calentamiento
termina `warmup`, sin estrategias activas `no_strategies`. Queda en el historial de ciclos (`/api/cycles`)
con `run_id` `manual-<ms>` y `detail` que empieza con `"manual: "`. Todas las estrategias activas pasan por
el Sintetizador; si ninguna aplica al símbolo, la decisión es `no_hypothesis`. No avanza la rotación del
ciclo agendado. Si justo está corriendo el ciclo agendado, espera a que termine (un grafo a la vez).

**202:**
```json
{"run_id": "manual-1792253686223", "symbol": "XRP-USDT", "queued_at": "2026-10-17T16:14:46.223000+00:00",
 "queued_ms": 1792253686223, "status": "queued"}
```

Errores:
- **400**: símbolo no configurado.
- **429**: ya hay un análisis manual en curso. Responde `{"detail", "retry_after_s": 60, "run_id"}` con el run en curso.
- **429**: ese símbolo se pidió hace menos de 5 min. `retry_after_s` = lo que falta.
- **503**: grafo deshabilitado (sin API key) o análisis apagado.

## `GET /api/analyze/{run_id}`

```json
{"run_id": "manual-1792253686223", "symbol": "XRP-USDT", "status": "ran",
 "detail": "manual: XRP-USDT=no_hypothesis", "n_hypotheses": 0,
 "queued_at": "...", "queued_ms": 1792253686223, "started_ms": 1792253686230, "finished_ms": 1792253776000,
 "cycle_url": "/api/cycles/manual-1792253686223"}
```

- `status`: `queued`, `running`, o el estado final del ciclo: `ran`, `error`, `warmup`, `no_strategies` o `no_llm`.
- `cycle_url` es `null` mientras no terminó. Pollear cada 3–5 s mientras esté `queued`/`running`; un ciclo con LLM tarda ~1–2 min.
- Tras un reinicio del proceso, el estado sale del historial de ciclos (sin `queued_*`).
- **404** si no existe; **400** si el `run_id` es inválido.

## `POST /api/lab/requests` — "Mandar al laboratorio"

Body:
```json
{"strategy_ids": ["donchian-ensemble-v1"], "symbols": ["XRPUSDT"], "note": "ver si aplica a XRP sola"}
```

- `strategy_ids`: 1–20 ids publicados en `lab_summary.json`, o `["*"]` (todas las publicadas; se expande
  al crear y la solicitud guarda la lista).
- `symbols`: 1–10 entre los configurados; se guardan en formato Binance (`XRPUSDT`).
- `note`: hasta 500 caracteres.

**201:**
```json
{"request_id": "3f0c9b2e6a7d4c1e9b8a7f6e5d4c3b2a", "created_ms": 1792253686223, "created_at": "...",
 "strategy_ids": ["donchian-ensemble-v1"], "symbols": ["XRPUSDT"], "note": "ver si aplica a XRP sola",
 "origin": "panel", "status": "pending", "status_ms": 1792253686223, "status_at": "...", "worker": null,
 "error": null, "result": null, "result_url": null}
```

Errores:
- **400**: estrategia desconocida (el detalle lista las válidas), `"*"` mezclado con otros ids, o símbolo no configurado.
- **429**: ya hay 10 pendientes.
- **503**: no hay `lab_summary.json` publicado, así que no hay contra qué validar las estrategias.

La procesa el worker de la PC (`python -m heatsignal.lab.worker`), así que puede tardar hasta el próximo
pase del worker más lo que dure el backtest (minutos por estrategia). Cada corrida **cuenta como intentos**
en el registro anti p-hacking. El holdout nunca se abre desde la cola.

## `GET /api/lab/requests?status=&limit=&offset=`

- `status`: `pending`, `running`, `done` o `error`.
- `limit`: 1–200 (default 50).

Devuelve una página (más nuevas primero) con la forma del 201, más `pending` (cuántas hay en cola) y `max_pending`.

## `GET /api/lab/requests/{request_id}`

La forma del 201 más `events`, el historial de estados (append-only):

```json
{"...": "...", "status": "done", "worker": "pc",
 "result": {"file": "3f0c....json", "strategies": 1, "ok": 1, "errors": 0, "passing": 0},
 "result_url": "/api/lab/requests/3f0c9b2e6a7d4c1e9b8a7f6e5d4c3b2a/result",
 "events": [
   {"status": "pending", "ts_ms": 1792253686223, "ts_at": "...", "worker": null},
   {"status": "running", "ts_ms": 1792253700000, "ts_at": "...", "worker": "pc"},
   {"status": "done", "ts_ms": 1792254000000, "ts_at": "...", "worker": "pc", "result": {"...": 0}}
 ]}
```

- Con `status: "error"`, `error` trae el motivo. Si fallan todas las estrategias se informa cada una, p.ej.
  `"donchian-ensemble-v1: faltan datos: sin datos de klines para DOGEUSDT ... (correr lab.vision)"`.
- **400** si el id no tiene 32 hex; **404** si no existe.

## `GET /api/lab/requests/{request_id}/result`

El archivo que subió el worker (`data/lab_requests/<id>.json`). **404** mientras no esté `done`.

```json
{"request_id": "3f0c...", "symbols": ["XRPUSDT"], "strategy_ids": ["donchian-ensemble-v1"], "note": "...",
 "generated_at": "...", "worker": "pc", "n_perm": 100, "holdout": "SELLADO (la cola nunca lo abre)",
 "strategies": [
   {"strategy_id": "donchian-ensemble-v1", "status": "ok",
    "file": "results/donchian-ensemble-v1/symbols-XRPUSDT/20261017T170000.json",
    "summary": {"sharpe": 0.4, "sharpe_ci95": [-0.5, 1.3], "dsr": 0.6, "psr": 0.7, "total_return": 0.12,
                "max_drawdown": 0.2, "trades": 90, "hit_rate": 0.3, "breakeven_bps": 20.0,
                "basket_sharpe": 0.1, "random_p": 0.2, "pasa": false},
    "detail": {"...": "misma forma que /api/lab/{strategy_id} (walk-forward, costos, equity_daily, veredicto)"}},
   {"strategy_id": "otra-v1", "status": "error", "error": "faltan datos: ..."}
 ]}
```

Con un solo símbolo, `basket` y `buy_and_hold_mean` de `equity_daily` son el buy & hold de esa moneda.
