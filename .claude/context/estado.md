# Contexto: Estado actual y pendientes

> Foto del proyecto **hoy**. Se reescribe (no se acumula) cada vez que cambia el estado: lo histórico va a
> `historial.md`, las decisiones a `decisions.md`. Última actualización: **2026-10-17**.

## Estado por fase

| Fase | Estado | Dónde |
|---|---|---|
| 0 — Bloqueantes | A mitigado (falta leer ToS de exchanges), B implementado, C cerrado | `phase-0-blockers.md` |
| 1 — Ingesta | **En producción de prueba** (VM, modo memory, 24/7) | `phase-1-ingestion.md` |
| 2 — Agentes | Implementado y cableado; **inactivo** hasta tener API key + estrategias | `phase-2-agents.md`, `agents.md` |
| 3 — Evaluación | Implementado; timer cada 5 min en la VM, sin hipótesis todavía | `phase-3-evaluation.md` |
| 4 — Producto | Panel **interno** listo (no público); dashboard/API pública/reporte no iniciados | `phase-4-product.md` |

Código: rama `ingestion-binance-bybit`, pusheada a GitHub (sin PR).
Tests: 389 pasando, 1 skipped (`backend/`, `.venv/bin/pytest -q`).

## Qué corre hoy (VM de prueba)

- GCP e2-small `instance-20261016-191100`, `asia-northeast1-a`, proyecto `vocal-operand-271906`.
  Debian 13, 2 vCPU, 1.9 GB RAM + 2 GB swap, disco 10 GB. Código en `~/heatsignal` (copiado con
  `git archive` + `gcloud compute scp`, no es un clon de git). venv Python 3.13 en `~/heatsignal/backend/.venv`.
- `heatsignal-ingestion.service`: ingesta Binance + Bybit + OKX (BTCUSDT, SOLUSDT) en **modo memory**
  (datos de mercado solo en RAM) + ciclo de análisis cada 15 min. `MemoryMax=700M`, `Restart=always`.
- `heatsignal-eval.timer` → `heatsignal-eval.service` cada 5 min (resolución + evaluación).
- Único archivo persistido: `~/heatsignal/data/heatsignal_eval.sqlite` (estrategias + hipótesis). Con API
  key se suma `heatsignal_checkpoints.sqlite` (checkpoints del grafo).
- `ANTHROPIC_API_KEY` cargada en `~/heatsignal/.env` (2026-10-17): grafo habilitado. Con la primera
  estrategia registrada (`xrp-acumulacion-v1`) el grafo corre cada 15 min y gasta LLM.
- Logs: `sudo journalctl -u heatsignal-ingestion -f`, `sudo journalctl -u heatsignal-eval -n 50`.
- **Panel interno** (`api/panel.py` + `panel.html`): dentro del proceso de ingesta, escucha solo en
  `127.0.0.1:8000` de la VM (sin puerto abierto en el firewall). Incluye gráfico de velas (Binance REST
  pedido al abrir, no se guarda) con hipótesis y clusters encima. Abrir con túnel SSH desde la PC:
  `gcloud compute ssh --zone asia-northeast1-a instance-20261016-191100 --project vocal-operand-271906 -- -N -L 8000:localhost:8000`
  y después `http://localhost:8000`. Muestra exchanges, ventana en memoria, último ciclo, features en
  vivo, estrategias e hipótesis. Solo lectura.

## Pendientes — los define el usuario (bloquean el modo shadow)

- [ ] **Rotar la `ANTHROPIC_API_KEY`**: la actual se pegó en el chat. Generar otra y cargarla a mano en el
      `.env` de la VM. Definir presupuesto: ~670 llamadas/día con ciclo de 15 min.
- [ ] **Pre-registrar las primeras estrategias** con
      `python -m heatsignal.evaluation.register_strategy archivo.json` — antes de ver resultados.
- [ ] **XRP**: sumar `XRPUSDT` a `HEATSIGNAL_SYMBOLS` en el `.env` de la VM y revisar la RAM en el panel.
- [ ] **`xrp-acumulacion-v1`** (borrador en `strategies/`, LONG a 90 días): revisar y registrar.
- [ ] **Símbolos**: confirmar si se suma ETH (`HEATSIGNAL_SYMBOLS`).
- [ ] **ToS de Binance/Bybit/OKX**: lectura rápida (`phase-0-blockers.md`, Bloqueante A).
- [ ] **Push / PR** de la rama `ingestion-binance-bybit`.

## Pendientes — código / técnicos

Cuando el grafo empiece a correr:
- [ ] Validar el grafo contra la API real (todo está testeado solo con modelos falsos).
- [ ] Manejar refusals del modelo y modelo de fallback (hoy un fallo del LLM = ciclo `error`, sin reintento
      de otro modelo).
- [ ] Medir la RAM real de la ventana de 30 min de trades en la VM (estimado 160–250 MB).

Calibración / estadística (preguntas abiertas de los agentes de implementación):
- [ ] Baseline momentum del Evaluador mezcla símbolos cuando una estrategia opera varios.
- [ ] Supuesto de costos de 10 bps round-trip (sin slippage ni funding): decidir si se publica así.
- [ ] Calibrar el umbral de `regime-v1` (2 ATR en 20 barras) con datos del shadow.
- [ ] Definición de `price_mid` del cluster (hoy punto medio del rango del cluster).

Ingesta:
- [ ] Open interest de Binance: el stream `markPrice` no lo trae; la suma de OI sale solo de Bybit + OKX
      (declarado en `data_gaps`). Opción: poll REST `openInterest` de Binance.
- [ ] `reconnects` de health se infiere parseando logs; reemplazar por un hook explícito en los workers.
- [ ] Modo `persistent`: `TimescaleSink` nunca se probó contra Postgres real; `prune_plain()` no está
      agendado. Irrelevante mientras se use modo memory.

Producto (Fase 4):
- [x] Panel interno de operación (túnel SSH, solo lectura).
- [ ] Endpoints públicos FastAPI (reportes `public_view()` por estrategia).
- [ ] Dashboard Next.js.
- [ ] Reporte diario anclado en UTC.

Operación:
- [ ] Reloj de la WSL de desarrollo adelantado ~1.9 s (`sudo hwclock -s`); la VM está bien (NTP).
- [ ] Deploy repetible: hoy es `git archive` + `scp` manual; documentar o automatizar.
