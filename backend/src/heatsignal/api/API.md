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
| `/api/lab`, `/api/lab/{id}` | lee archivos (se releen solo si cambian) | al abrir; cambian cuando se publica desde la PC |
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
  "lab": {"generated_at": "2026-10-17T15:00:00+00:00", "strategies": 7, "passing": 1}
}
```

- `counts.*` es `null` si esa parte está apagada. `hypotheses.due` = pendientes ya vencidas que esperan
  al job de resolución (timer de 5 min).
- `recording.status`: `ok` (escribió hace < 120 s), `starting` (recién arrancado), `stale` (no escribe),
  `off` (grabación apagada).
- `lab`: `null` sin resumen publicado; `{"error": "..."}` si el archivo está roto.

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
  grafo en `heatsignal_checkpoints.sqlite`, en otro hilo, y se cachea) | `null` (no hay razonamiento:
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

`side` es el lado de la orden de liquidación (sell = long liquidado). `coverage`: `full` (Bybit) o
`partial` (Binance ≤ 1/s por símbolo, OKX no informa el total): no comparar montos entre exchanges como si
fueran completos. `totals` suma todo el filtro, no solo la página.

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
