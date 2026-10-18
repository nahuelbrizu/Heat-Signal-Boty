# Contexto: Estado actual y pendientes

> Foto del proyecto **hoy**. Se reescribe (no se acumula) cada vez que cambia el estado: lo histórico va a
> `historial.md`, las decisiones a `decisions.md`. Última actualización: **2026-10-17**.

## Novedades 2026-10-17 (tarde)

- **Panel = SPA** (`api/static/app/`, vanilla, sin build ni CDN; `panel.html` es solo el cascarón). Vistas:
  resumen, moneda (todo filtrado por una cripto + "Analizar ahora" y "Mandar al laboratorio"), mercado,
  análisis, ciclos, estrategias, hipótesis (acertó/falló contra la realidad), laboratorio (ficha, holdout,
  pedidos), noticias, datos guardados, sistema. API documentada en `api/API.md`.
- **Cola del laboratorio:** los pedidos se guardan en la VM (`lab_requests`, append-only); el worker de la PC
  (`systemctl --user heatsignal-lab-worker.timer`, cada 5 min) los toma por SSH, corre el backtest (cada corrida
  cuenta como intentos; el holdout nunca se abre desde la cola) y sube el resultado. Con la PC apagada,
  los pedidos esperan. Si un pedido queda colgado en `running`, liberarlo con `python -m heatsignal.evaluation.lab_requests fail ID`.
- **Paper trading:** `heatsignal-freqtrade.service` en la VM (dry-run de donchian-ensemble-v1). FreqUI con `hs-ft`,
  `hs-ft-open` y `hs-ft-pass`.
- **Paper vs backtest (2026-10-18, sin desplegar):** `lab/paper_compare.py` (PC) lee la base del dry-run por SSH en
  solo lectura, re-simula la regla congelada y descompone la diferencia (timing, tamano, fill, comisiones, slippage,
  funding); sube `paper_summary.json`. Timer semanal en `deploy/pc/` (domingos 01:00, falta instalarlo). Panel: vista
  `#/paper`, `GET /api/paper` y `GET /api/paper/compare` (requiere deploy del codigo y reinicio de la ingesta).
- **Bot propio de paper del lote 3 (2026-10-18, sin desplegar):** `heatsignal.paperbot` (corrida horaria a los :02 UTC,
  `deploy/heatsignal-paperbot.{service,timer}`, `MemoryMax=400M`; pico medido 143 MB). Llama a las mismas reglas de
  `lab/strategies.py`, simula fills/costos/funding en `data/paperbot.sqlite` (append-only) y registra huecos sin rellenarlos.
  Panel: vista `#/paperbot` (`GET /api/paperbot`, con el veredicto del backtest al lado). Falta: instalar pandas en el venv
  de la VM (`requirements-paperbot.txt`), deploy del código, reiniciar la ingesta y activar el timer; el archivado tiene que
  conservar >= 64 días de velas 1m (`--klines-keep-days 90` en `heatsignal-archive.service`).
- **LLM:** Anthropic (sin crédito) → gemini-3.5 → gemini-3.8-flash / 3.1-flash-lite. Rotar ambas keys (quedaron en el chat).
- **Laboratorio:** resultados del lote 1 y holdout en `decisions.md`.

- **Almacenamiento por niveles (2026-10-18, sin desplegar):** `history/archive.py` + `heatsignal-archive.timer` (cada
  hora: al 80 % de disco sube lo viejo a `gdrive:archive/`, verifica md5 y borra) y lectura fría en el panel
  (`/api/history/storage`, badge "desde Drive"). Respaldo semanal `heatsignal-backup.timer`. Falta: deploy del código,
  instalar los timers y reiniciar la ingesta (nuevo `ReadWritePaths` para rclone). Ver `decisions.md`.

- **Velas de 1h + tendencia del día + sesiones (2026-10-18, sin desplegar):** contexto multi-temporalidad por código
  (`features/htf.py`, regla fija `htf-trend-v1`; velas de 1h CERRADAS desde `klines`/`bars_1m` de la base, REST de Binance
  solo para huecos, declarado; cache incremental en `history/hourly.py`). Sesiones Asia/Europa/EE.UU./cierre con DST
  (`features/sessions.py`): al cerrar la 1ª vela de 1h de cada una se guarda un análisis por código en el diario
  append-only `session_journal` (base de evaluación) y el grafo recibe el diario del día. Modo `HEATSIGNAL_ANALYSIS_MODE=sessions`:
  4 corridas del grafo por día con TODAS las criptos batcheadas (sin llamadas extra). Hipótesis con `timeframe`,
  `daily_trend`, `trend_alignment`, `session` (tabla nueva `hypothesis_context`, migración aditiva); las de 1h vencen en
  un cierre de vela y se resuelven con la vela de 1h (mark). Panel: vista `#/sesiones`, tarjeta de 1h en Moneda, columnas
  y corte por tendencia/sesión (n e IC) en Hipótesis. 5 estrategias nuevas en `backend/strategies/tendencia-diaria-1h/`
  **sin registrar** (las registra el usuario). Falta: deploy + `HEATSIGNAL_ANALYSIS_MODE=sessions` + reinicio.

## Novedades 2026-10-19 (sin desplegar)

- **max-ruptura-10d-v1 en el bot propio de paper.** `paperbot/params.json` ahora define qué corre (6 del lote 3 + max-ruptura,
  spec 39cb9dee6d3e469a, sin grilla). El modo (`inicio`/`consecutiva`/`hueco`) se decide por estrategia: la nueva entra
  "desde hoy" (orden `inicio` al objetivo de la regla, 10.000 US$ y canasta propios desde su primera corrida) sin tocar la
  historia de las otras. Sin cambios de esquema. Panel `#/paperbot`: veredicto del backtest + del holdout (no confirmó)
  y "en paper desde". `lab/publish.py` publica `holdout_result` con ventana, canasta y p azar (republicar con
  `python -m heatsignal.lab.publish --upload` desde la PC para verlos). Corrida en seco en la VM: 147 MB, 3 s.
- **Revisión semanal** `python -m heatsignal.paper.weekly` + `deploy/heatsignal-weekly.{service,timer}` (lunes 00:30 UTC):
  cierra la semana UTC anterior por código (sin LLM) y guarda una fila append-only por semana en `weekly_reviews`
  (base de evaluación). Fuentes: bot propio, Freqtrade Donchian (+ paper_compare), Freqtrade de hipótesis, hipótesis del
  grafo (n, IC de Wilson, alfa, cortes) y salud (bars_1m/klines, ciclos, sesiones con LLM; las llamadas al LLM no se
  registran: falta). Panel `#/semanal`, `GET /api/weekly` y `/api/weekly/{week}`. Corrida en seco en la VM: 28 MB, 6–10 s.
  Falta: deploy del código, reiniciar la ingesta (endpoint nuevo) e instalar el timer.

## Estado por fase

| Fase | Estado | Dónde |
|---|---|---|
| 0 — Bloqueantes | A mitigado (falta leer ToS de exchanges), B implementado, C cerrado | `phase-0-blockers.md` |
| 1 — Ingesta | **En producción de prueba** (VM, modo memory, 24/7) | `phase-1-ingestion.md` |
| 2 — Agentes | Implementado y cableado; **inactivo** hasta tener API key + estrategias | `phase-2-agents.md`, `agents.md` |
| 3 — Evaluación | Implementado; timer cada 5 min en la VM, sin hipótesis todavía | `phase-3-evaluation.md` |
| 4 — Producto | Panel **interno** listo (no público); dashboard/API pública/reporte no iniciados | `phase-4-product.md` |

Código: rama `ingestion-binance-bybit`, pusheada a GitHub (sin PR).
Tests: 847 pasando, 1 skipped (2026-10-19) (`backend/`, `.venv/bin/pytest -q`).

## Qué corre hoy (VM de prueba)

- GCP e2-small `instance-20261016-191100`, `asia-northeast1-a`, proyecto `vocal-operand-271906`.
  Debian 13, 2 vCPU, 1.9 GB RAM + 2 GB swap, disco 10 GB. Código en `~/heatsignal` (copiado con
  `git archive` + `gcloud compute scp`, no es un clon de git). venv Python 3.13 en `~/heatsignal/backend/.venv`.
- `heatsignal-ingestion.service`: ingesta Binance + Bybit + OKX + Hyperliquid (BTC, SOL, XRP, ETH, XLM, BNB, TRX) en **modo memory**
  (datos de mercado solo en RAM) + ciclo de análisis cada 15 min. `MemoryMax=700M`, `Restart=always`.
- `heatsignal-eval.timer` → `heatsignal-eval.service` cada 5 min (resolución + evaluación).
- Persistido: `data/heatsignal_eval.sqlite` (estrategias + hipótesis) y `data/heatsignal_history.sqlite`
  (agregados por minuto, liquidaciones, noticias, velas 1m). `heatsignal-history.timer` (00:20 UTC):
  velas nuevas + export diario + respaldo a Drive. Con API
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

## Pendientes — los define el usuario (actualizado 2026-10-19)

- [ ] **Listón del programa (SR0):** elegir (a) como está, (b) K efectivo por correlación, (c) por familia
      (`decisions.md`, "Abiertas"). Decidir antes de mirar qué opción hace pasar a alguna estrategia.
- [ ] **Seguridad de la VM (a hacer):**
  - [ ] Activar *deletion protection* (hoy apagada).
  - [ ] Borrar la regla de firewall `default-allow-rdp` (3389 abierto a internet, inútil en Linux).
  - [ ] Más adelante: SSH solo por IAP (regla para 35.235.240.0/20, `--tunnel-through-iap` en nuestros
        comandos y en `lab/publish.py`), y Secure Boot.
- [ ] **Rotar keys pegadas en el chat:** `ANTHROPIC_API_KEY` (ya rotada una vez), la key vieja de Gemini (AI
      Studio) y la de Vertex (`AQ.`, en uso). Cargar la nueva en `~/heatsignal/.env` de la VM.
- [ ] **Crédito de Anthropic:** cuando se cargue, Claude vuelve a ser el primario (Haiku/Sonnet): medir costo.
- [ ] **Métricas del grafo** (tokens y costo por ciclo y por modelo, en el panel): pedido pendiente.
- [ ] **Plan de entrenamiento de bots:** `plan-entrenamiento.md` (para revisar y aprobar por etapas).
- [ ] **METAL**: no existe como perpetuo; ¿es MTL (Metal DAO, solo Binance/Bybit) u otro token?
- [ ] **ToS de Binance/Bybit/OKX**: lectura rápida (`phase-0-blockers.md`, Bloqueante A).
- [x] Estrategias del grafo pre-registradas: las 5 de 1h (2026-10-18). `xrp-acumulacion-v1/v2` retiradas (XRP
      pasa a la wallet).
- [x] VM pasada a e2-medium (4 GB) con disco de 20 GB (2026-10-19).

## Pendientes — código / técnicos

Cuando el grafo empiece a correr:
- [x] Grafo validado contra la API real (2026-10-17 03:01 UTC): 8 llamadas, ~90 s, `estado=ran`,
      los 3 símbolos `no_hypothesis`. Falta medir el costo real por ciclo en la consola de Anthropic.
- [ ] Manejar refusals del modelo y modelo de fallback (hoy un fallo del LLM = ciclo `error`, sin reintento
      de otro modelo).
- [ ] Medir la RAM real de la ventana de 30 min de trades en la VM (estimado 160–250 MB).

Calibración / estadística (preguntas abiertas de los agentes de implementación):
- [ ] Baseline momentum del Evaluador mezcla símbolos cuando una estrategia opera varios.
- [ ] Supuesto de costos de 10 bps round-trip (sin slippage ni funding): decidir si se publica así.
- [ ] Calibrar el umbral de `regime-v1` (2 ATR en 20 barras) con datos del shadow.
- [ ] Definición de `price_mid` del cluster (hoy punto medio del rango del cluster).

Noticias:
- [ ] Sin fuente primaria de Ripple (no publica RSS). Opción: monitorear su sala de prensa de otra forma.
- [ ] El etiquetado por palabras clave es simple (`newsrss-v1`): medir falsos positivos con lo que se acumule.

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

Ejecución (sin plan todavía, ver `decisions.md`):
- [ ] Si se decide operar: plan por etapas shadow → paper → dinero chico → escalar. XRPL DEX entra acá
      (Hummingbot `xrpl`).

Operación:
- [ ] Reloj de la WSL de desarrollo adelantado ~1.9 s (`sudo hwclock -s`); la VM está bien (NTP).
- [ ] Deploy repetible: hoy es `git archive` + `scp` manual; documentar o automatizar.
