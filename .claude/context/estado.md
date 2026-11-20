# Contexto: Estado actual y pendientes

> Foto del proyecto **hoy**, corta a propósito (se lee al inicio de cada sesión). Se **reescribe**, no se acumula:
> lo histórico va a `historial.md` (las "Novedades" viejas están al final, en "Archivo de estado.md"), las
> decisiones a `decisions.md` (leer solo la sección que haga falta, ver su índice). Contenido al día hasta
> **2026-10-27**; achicado el 2026-11-18.

## Cómo trabajar barato

Tareas chicas directo, sin subagentes; agentes solo para trabajo grande y, si es rutina, con modelo liviano (Sonnet);
nada de polling; revisar una vez por semana (lunes, con la revisión semanal). No leer `decisions.md` ni `historial.md`
enteros: buscar con grep y leer el tramo. El grafo usa la API de Anthropic (se paga aparte del plan de Claude, con tope de US$5 por semana).

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
- `paper-compare` en **modo local** (`heatsignal.lab.paper_compare --upload --local --since 2026-11-18`: la ventana
  arranca cuando Freqtrade volvió a correr, para que el hueco no infle el timing; timer semanal dom 01:00; unidad
  versionada en `deploy/pc/`): lee `~/heatsignal/data/freqtrade_dryrun.sqlite` en solo lectura y copia
  `compare_<fecha>.json` + `paper_summary.json` a `~/heatsignal/data/`.
- Apagado porque depende de la VM: `heatsignal-tunnel`.
- LLM del grafo: Anthropic (`ANTHROPIC_API_KEY` en `~/heatsignal/.env`, cobra por uso, aparte del plan de Claude).
- **Grafo cada hora (2026-11-19)**: `HEATSIGNAL_ANALYSIS_MODE=hourly`, todo en Haiku 5.5 (~US$0,024 por ciclo medido,
  ~US$2,9 por semana), cierre de cada vela 1h + sesiones; 18 estrategias activas: 8 de 1h + 10 nuevas de 2h/4h
  (`backend/strategies/*-2h`, `*-4h`; decisions.md "Grafo cada hora"). Cada ciclo deja, por cripto, la decisión
  y por qué no entró cada estrategia.
- **Bot paper: enviar estrategias desde el panel (2026-11-19)**: Bot paper -> "Enviar estrategias del laboratorio al
  bot" (ordenadas por Sharpe OOS; entran como seguimiento). El laboratorio se publica solo al panel local despues de
  cada pasada del worker (`lab.publish --local --if-changed`).
- **En vivo (2026-11-19)**: velas de 5m/15m cerradas con los bars_1m propios (`features/live.py`); panel "En vivo"
  sigue cada hipotesis abierta con alertas de codigo; el Analista de Contexto recibe una linea por cripto. No abre ni
  cierra hipotesis.
- **Demo de Bybit (2026-11-20, armada, falta la clave)**: `heatsignal-freqtrade-bybitdemo` (deshabilitado hasta que
  el usuario pegue la clave demo en `freqtrade/config.bybitdemo.secrets.json`; ExecCondition lo frena con el marcador
  PEGAR_) corre donchian-ensemble-v1 en la demo de Bybit, API :8083. Panel Paper trading -> "Demo de Bybit".
- **Alertas de oportunidad (2026-11-21, fase 3)**: timer `heatsignal-alerts` cada 5 min -> `data/alerts.sqlite`;
  panel "Oportunidades" (mandar a la demo / descartar / automatico por estrategia). Ejecutor (fase 4) en una cuenta
  demo de Bybit APARTE: falta que el usuario cree la subcuenta y pegue la clave en
  ~/heatsignal/secrets/bybit_demo_alertas.json; hasta entonces lo mandado queda en cola (candado: 'sin clave').
- **Estrategias generadas (2026-11-21, fases 1 a 4)**: constructor por bloques `lab/builder.py` (registro
  `data/lab_generated.jsonl`); el agente de investigacion propone hasta 5 por semana (lunes 02:00 UTC, agente v4) que
  se validan, registran y van solas al laboratorio; panel Laboratorio -> "Estrategias generadas" e Investigacion.
- **La PC se suspende**: 2026-11-19 03:00-10:10, 11:01-11:12, 15:44 -> 2026-11-20 14:19 y 14:37-15:08 UTC sin
  datos. Hay que desactivar la suspension de Windows.
- **Macro caido (2026-11-19)**: el DNS de la red de la PC devuelve NXDOMAIN para `gamma-api.polymarket.com`
  (1.1.1.1 y 8.8.8.8 si resuelven) y falta `FRED_API_KEY` en `~/heatsignal/.env`. El calendario si se exporta.
- **Tope de LLM (2026-11-18, pedido del usuario):** US$5 por semana para todo lo que se analiza (grafo + lecciones +
  investigación), y LLM solo lunes a viernes UTC (`HEATSIGNAL_LLM_WEEKLY_USD=5`, `HEATSIGNAL_LLM_DAYS=0-4`).
  `agents/spend.py`: cada llamada anota tokens y costo de lista en `~/heatsignal/data/llm_spend.sqlite`; al llegar
  al tope (o en fin de semana) los ciclos quedan `no_llm` con el motivo y las lecciones se escriben por código.
  Panel: "Gasto LLM de la semana" en el Resumen. Lo gastado antes de 2026-11-18 ~04:20 UTC no quedó anotado.
  Vertex `AQ.` del proyecto 985048017093 guardada comentada (falta habilitar aiplatform.googleapis.com).
- **Investigación semanal** (Claude Managed Agents, beta; `heatsignal.research.managed`): agente Haiku 5.5 con
  esfuerzo bajo, red `limited` a sitios de investigación (`HOSTS`), permiso `auto`, **tope US$0,05 por corrida**
  (pedido del usuario: web_search cuesta US$0,01, así que ≤ 2 búsquedas + API de arXiv por web_fetch, sin
  evaluador de rúbrica); activo desde 2026-11-18, lunes 02:00 UTC. Recibe `contexto.md` generado desde
  `lab_summary.json` y deja `informe.md`. Solo propone. Timer
  `heatsignal-research` (diario 01:30 UTC + lunes cada hora) refresca el contexto, baja informes a
  `~/heatsignal/data/research/` y niega llamadas que quedaron esperando aprobación. Panel `#/investigacion`. IDs
  en `~/heatsignal/data/research/managed.json`. Comandos: `provision`, `run-now`, `pause`, `unpause`, `sync`, `status`.
- Hueco en la prueba hacia adelante: del 2026-10-25 al 2026-11-18 (el paperbot lo registró como `hueco`, 575 h
  sin simular). Las -v2 arrancaron después del respaldo y quedaron sin su estado del 26/10.
- Si WSL se apaga, todo se detiene: la PC tiene que estar prendida y WSL corriendo.

Lo de abajo describe lo que corría en la VM; en la PC es igual salvo lo indicado arriba.

## Qué corre solo

VM e2-medium 4 GB / 20 GB, todo systemd; acceso por túnel permanente (`conexion.md`). Código en la rama
`ingestion-binance-bybit`; deploy = `git archive` + `scp` (manual).
- Ingesta 4 exchanges (Binance, Bybit, OKX, Hyperliquid; 7 criptos) + panel :8000 (SPA en `api/static/app/`,
  API en `api/API.md`; MemoryMax 1200M).
- Grafo `graph-v2-cio`, 4 sesiones/día (en la PC además cada hora, ver arriba): 4 analistas → debate → CIO → CRO (vetos por
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
