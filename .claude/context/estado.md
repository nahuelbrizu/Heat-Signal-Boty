# Contexto: Estado actual y pendientes

> Foto del proyecto **hoy**, corta a propósito (se lee al inicio de cada sesión). Se **reescribe**, no se acumula:
> lo histórico va a `historial.md` (las "Novedades" viejas están al final, en "Archivo de estado.md"), las
> decisiones a `decisions.md` (leer solo la sección que haga falta, ver su índice). Contenido al día hasta
> **2026-10-27**; achicado el 2026-11-18.

## Cómo trabajar barato

Tareas chicas directo, sin subagentes; agentes solo para trabajo grande y, si es rutina, con modelo liviano (Sonnet);
nada de polling; revisar una vez por semana (lunes, con la revisión semanal). No leer `decisions.md` ni `historial.md`
enteros: buscar con grep y leer el tramo. El grafo de la VM usa Gemini por Vertex (se paga en Google Cloud, no en el
plan de Claude); Anthropic API sin crédito (se saltea solo).

## Dónde corre: en la PC desde 2026-11-18 (la VM está caída)

La VM de GCP se apagó entre el 2026-10-27 y el 28 porque el proyecto tiene la **facturación deshabilitada**. Vertex
también queda sin servicio, así que el grafo corre **sin LLM** hasta cargar una key nueva en `~/heatsignal/.env`.
Ahora todo corre en la PC (WSL) como **servicios de usuario** (`systemctl --user`, linger activo):
- `~/heatsignal/` tiene la estructura de la VM: `backend`, `deploy` y `freqtrade` son symlinks al repo (corre el
  código de desarrollo, sin deploy); `data/` se restauró del respaldo de Drive `backups/vm/2026-10-25T0300Z`;
  `.env` es de la PC.
- Puertos (Windows reserva 7992–8991): panel `127.0.0.1:18001`, FreqUI Donchian `18081`, Freqtrade de hipótesis
  `18082`. Las credenciales de FreqUI están en `~/heatsignal/freqtrade/config.secrets.json` (nuevas, generadas en la PC).
- Freqtrade 2026.9 en `~/freqtrade/.venv`.
- Worker del laboratorio en **cola local** (`heatsignal.lab.worker --local`, timer cada 5 min; unidad versionada en
  `deploy/pc/heatsignal-lab-worker.service`): toma los pedidos de `~/heatsignal/data/heatsignal_eval.sqlite` y deja
  el resultado en `~/heatsignal/data/lab_requests/`, sin SSH.
- `paper-compare` en **modo local** (`heatsignal.lab.paper_compare --upload --local`, timer semanal dom 01:00; unidad
  versionada en `deploy/pc/`): lee `~/heatsignal/data/freqtrade_dryrun.sqlite` en solo lectura y copia
  `compare_<fecha>.json` + `paper_summary.json` a `~/heatsignal/data/`.
- Apagado porque depende de la VM: `heatsignal-tunnel`.
- Hueco en la prueba hacia adelante: del 2026-10-25 al 2026-11-18 (el paperbot lo registró como `hueco`, 575 h
  sin simular). Las -v2 arrancaron después del respaldo y quedaron sin su estado del 26/10.
- Si WSL se apaga, todo se detiene: la PC tiene que estar prendida y WSL corriendo.

Lo de abajo describe lo que corría en la VM; en la PC es igual salvo lo indicado arriba.

## Qué corre solo

VM e2-medium 4 GB / 20 GB, todo systemd; acceso por túnel permanente (`conexion.md`). Código en la rama
`ingestion-binance-bybit`; deploy = `git archive` + `scp` (manual).
- Ingesta 4 exchanges (Binance, Bybit, OKX, Hyperliquid; 7 criptos) + panel :8000 (SPA en `api/static/app/`,
  API en `api/API.md`; MemoryMax 1200M).
- Grafo `graph-v2-cio`, 4 sesiones/día (00:01, 01:01, 08:01, 14:31 UTC): 4 analistas → debate → CIO → CRO (vetos por
  código) → registro → redactor. 8 estrategias de 1h (5 tendencia + 3 lateralidad). Veredictos en `cio_verdicts`.
  El Analista de Contexto recibe macro (~320 tokens) y "Mundo" (16 feeds, `newstopics-v1`).
- Puente hipótesis → Freqtrade de hipótesis (:8082) y Freqtrade Donchian (:8081, FreqUI); ambos dry-run, sin -v2.
- Paperbot horario (:02): 30 estrategias + sus -v2 con SL/TP obligatorio (stop 3 ATR 8–20 %, objetivo 2×; desde
  2026-10-26). **Prueba hacia adelante desde 2026-10-20**: 3 principales (ruptura long / short / neta), criterio a
  3 meses (**2027-01-20**: Sharpe > 0 y > canasta) y 6 meses (p ≤ 0,05); parada P95/P99 en
  `paperbot/parada_preregistrada.json`.
- Timers: macro (:07), evaluación (5 min), archivado a Drive (80 %), historia 00:20, respaldo dom 03:00, revisión
  semanal lun 00:30 UTC (`#/semanal`). En la PC: worker del laboratorio (5 min) y paper-compare (dom 01:00).

## Resultados clave

- Laboratorio: 123 pruebas (lotes 1–6 + short). Ninguna pasa el listón (Romano–Wolf; mejor p 0,145). Tendencia LONG
  (rupturas 10–55 d, tsmom 28 d) Sharpe ~1–1,3 y robusta; las 12 short, negativas. Holdouts abiertos: ninguno confirma.
- Meta-modelo (etapas 0–3): sin señal (AUC ≤ 0,53); no pasa a etapa 4. Reintentar solo con datos o features nuevas
  pre-registradas.

## Pendientes del usuario

- **Fin de noviembre 2026:** decidir plata mínima real tras el paper y un listón previo para tendencia
  (`research/consulta-trader.md`); stop de catástrofe (propuesta 3 ATR, piso 8 %, techo 20 %).
- Seguridad VM: deletion protection; borrar `default-allow-rdp`; más adelante SSH solo por IAP.
- Rotar keys pegadas en el chat (Anthropic, Gemini, Vertex `AQ.`, FRED).
- Listón del programa (SR0): opción a/b/c (`decisions.md`, "Abiertas").
- 8 familias nuevas para el grafo (propuestas, sin hacer); métricas de tokens del grafo (baja prioridad).
- METAL (¿MTL?), lectura de ToS de exchanges.

## A vigilar

- El CRO veta por "hueco real" (p. ej. "trades: sin datos de bybit"): si veta seguido, revisar `features/gaps.py`.
- Noticias "Mundo": economía con pocos titulares, política con ruido local.

## Pendientes técnicos (resumen; detalle en el archivo de `historial.md`)

Refusals/fallback del LLM por nodo; OI de Binance; `reconnects` por hook explícito; supuesto de costos 10 bps;
calibrar `regime-v1`; deploy repetible; endpoints públicos, dashboard Next.js y reporte diario (Fase 4, sin iniciar).
