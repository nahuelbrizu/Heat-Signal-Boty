# Contexto: Decisiones (ADR liviano)

> Registro de decisiones. Al cerrar una, actualizar aquí en el mismo commit.

## Cerradas
- **Fuente de datos → WS nativos de exchanges (Binance + Bybit + OKX).** Sobre CoinGlass pago. Razón:
  gratis, sin cláusula de redistribución comercial (elimina el Bloqueante A), y control total sobre el
  modelo de heatmap. Costo: hay que agregar/normalizar cada exchange y construir el heatmap propio.
  CoinGlass queda como **fuente BYOK opcional** a futuro (el usuario pone su clave). Fecha: 2026-07-08.
- **Orquestación → LangGraph.** Sobre CrewAI. Razón: durabilidad de producción, manejo de estado preciso,
  y mejor performance para grafos de 5+ agentes / 10+ pasos. Además el checkpoint resume por nodo resuelve
  la idempotencia del pipeline gratis. (Ref: patrón TradingAgents.)
- **Ingesta = servicios, no agentes LLM.** 4 workers WS + 1 poller REST. Razón: los recolectores no
  razonan; gastar tokens ahí no aporta y escala mal.
- **Bloqueante C (benchmark/baseline) → resuelto.** Implementado en `backend/src/heatsignal/evaluation/`:
  3 baselines pasivos reproducibles por seed (`Random`/`AlwaysLong`/`Momentum`), Evaluador con IC de
  Wilson, p-value binomial (scipy) y Sharpe anualizado, y un criterio de aprobación fijo
  (n>=30, p<0.05, wilson_ci[0]>0.50, sharpe>1.0, y superar los 3 baselines). Razón para el criterio
  "supera baseline" compuesto (avg_return + sharpe + max_drawdown, no solo avg_return): un avg_return más
  alto con volatilidad/drawdown peores no es edge real, es más riesgo. Pre-registro de hipótesis inmutable
  (`StrategyRegistry`) y log de resultados que nunca pisa una evaluación previa (`HypothesisLog`), ambos
  sobre SQLite append-only con triggers a nivel de motor. Fecha: 2026-07-10.
- **Backend → FastAPI.** Sobre NestJS. Razón: mismo lenguaje que el resto del stack de agentes
  (LangGraph, evaluation/, ingestion/ ya son Python) — un solo runtime, sin puente entre proceso
  Node y proceso Python para el mismo dominio. Esqueleto mínimo en
  `backend/src/heatsignal/api/app.py` (`create_app()` + `GET /health`); el dashboard, el reporte diario
  y la API pública de señales quedan para Fase 4. Fecha: 2026-07-10.
- **Datos de mercado en la etapa de prueba → solo en memoria (pass-through).** Sobre persistirlos en
  TimescaleDB/Redis. Razón: es un proyecto de prueba; el usuario pidió que los datos solo pasen, sin
  guardarse ni exponerse en otro lado. También evita disco/RAM extra en la VM e2-small y no guarda ni
  redistribuye datos crudos de los exchanges. `HEATSIGNAL_STORAGE=memory` (default) usa
  `storage/memory.py::MemoryWindowSink`: ventana en RAM por tipo y símbolo (trades 30 min, tickers 2 h,
  liquidaciones 24 h) con tope duro de eventos. Costo aceptado: cada reinicio vacía la ventana
  (calentamiento) y no hay historia para recalcular features con un heatmap nuevo. Sin fallos silenciosos:
  `data_gaps()` declara el calentamiento, la retención menor a la ventana pedida y los huecos por tope.
  **Lo que SÍ persiste:** hipótesis y resoluciones (`pending_store`, SQLite append-only). Sin eso no hay
  win-rate medible y se rompe el anti-p-hacking. El precio de salida sale de klines REST de Binance, no de
  datos guardados. Los features agregados (no los eventos crudos) se envían al proveedor LLM cuando corre
  el grafo. El modo `persistent` (compose) queda en el código, apagado. Fecha: 2026-10-17.

- **Una hipótesis abierta por estrategia y símbolo.** El Sintetizador no abre otra hipótesis de la misma
  estrategia en el mismo símbolo mientras haya una sin resolver: queda `no_hypothesis` con motivo "ya hay
  una hipótesis abierta". El candado está en código (`apply_proposals(open_positions=...)`, que lee
  `PendingHypothesisStore.open_positions()`), no en el prompt. Al LLM se le listan las abiertas solo como
  información. Por qué: con un ciclo cada 15 min, una estrategia de horizonte largo abriría cientos de
  hipótesis casi iguales sobre el mismo movimiento, contaría una sola apuesta como n aciertos e inflaría el
  intervalo de confianza. También ordena las estrategias de horas. Fecha: 2026-10-17.

- **Noticias por RSS, solo en memoria.** Fuentes: CoinDesk, Cointelegraph y Decrypt (medios) + notas de
  prensa de la SEC (primaria, filtradas por palabras cripto). Verificadas el 2026-10-17. No se scrapea
  CoinMarketCap: sus Términos de Uso lo prohíben y su API ya no tiene tier gratis. Ripple/XRPL no publican
  RSS, así que XRP no tiene fuente primaria del emisor: se declara en `data_gaps`. Ventana de 48 h en RAM
  (`news/window.py`), poll cada 5 min. El grafo solo ve noticias con `fetched_ms <= ts` del ciclo (sin
  look-ahead). Lo que vio cada corrida queda en su checkpoint (`run_id`). Nuevo nodo `news_analyst`
  (modelo económico). En código: los ids citados se filtran contra la entrada, y
  `material_primary_negative` exige citar una fuente primaria. El texto de terceros va delimitado y se trata
  como datos. Versión `newsrss-v1`. Para medir si las noticias aportan, `xrp-acumulacion-v2` (con
  noticias) corre en paralelo con `v1` (sin noticias). Fecha: 2026-10-17.

- **Hyperliquid como cuarto exchange (solo datos).** Worker `ingestion/hyperliquid_worker.py`, verificado contra
  la doc oficial y la respuesta real (2026-10-17): `trades` (lado del agresor) + `activeAssetCtx` (mark,
  funding, OI). El funding de Hyperliquid es **horario** y se guarda multiplicado por 8 (equivalente 8 h)
  para compararlo con Binance/Bybit/OKX. No hay stream público de liquidaciones (solo por usuario), así que
  la cobertura figura como `na`, sin hueco repetido en cada ciclo. Los perps cotizan en USD/USDC y se
  mapean al símbolo interno `XXX-USDT`. Efecto sobre `xrp-acumulacion-v1/v2`: la condición "funding ≤ 0 en
  todos los exchanges que informan" ahora incluye Hyperliquid y queda más estricta. Se acepta porque
  todavía no hay hipótesis. El texto de la estrategia no cambia, pero el universo de exchanges sí, y
  queda anotado acá. Fecha: 2026-10-17.
- **XRPL DEX → postergado hasta la etapa de ejecución.** Como fuente de datos aporta poco: es spot, no tiene
  funding, OI ni liquidaciones, el volumen es chico y los trades salen de parsear la metadata del ledger.
  Se retoma si se arma el plan de bots, donde Hummingbot ya tiene conector `xrpl`. Fecha: 2026-10-17.

- **El grafo solo analiza símbolos con estrategia.** Una estrategia con `entry_conditions.symbol` aplica solo
  a ese símbolo; una sin `symbol` aplica a todos (`pipeline/analysis.py::symbols_for_graph`). El resto
  sigue en features y panel, pero no gasta LLM; el ciclo lo declara en el detalle. Hoy solo XRP pasa por
  el grafo: el costo baja a ~1/3. Precios y ATR van a los prompts con cifras significativas
  (`fmt_price`), porque con 2 decimales fijos el ATR de XRP se leía "0.00". Fecha: 2026-10-17.

- **Objetivo del proyecto → herramienta personal que opera por el usuario (2026-10-17).** Reemplaza a
  "dashboard público, no un bot de ejecución". Plan por etapas: (1) datos guardados, (2) laboratorio de
  simulación con separación entrenamiento/prueba, en velas 1m/5m/15m/30m/1h, (3) bots en cuentas de prueba
  (Hummingbot) comparando simulación vs realidad, (4) dinero real chico con límites, (5) escalar. Lo que no
  cambia: las órdenes las manda código determinista (el LLM filtra y revisa); toda estrategia se versiona
  y se prueba fuera de muestra antes de operar.
- **Datos de mercado → se guardan, privados (revierte "solo en memoria", 2026-10-17).** Para entrenar y
  simular hace falta historia, y la de liquidaciones, flujo por exchange, funding y OI a 1 minuto no existe
  gratis: solo la que guardemos. `history/`: `MinuteRecorder` (agregados por minuto, exchange y símbolo,
  más cada liquidación y cada noticia) sobre SQLite en la VM (`data/heatsignal_history.sqlite`). Velas
  históricas 1m de Binance por REST (`history/klines.py`); 5m–1h se derivan exactas de 1m. Export diario
  a `.csv.gz` y respaldo con `rclone copy` a la carpeta personal de Drive del usuario
  (`heatsignal-history.timer`, 00:20 UTC). Nada se publica. La ventana en RAM del análisis sigue igual.

- **Siete criptos y análisis del grafo por turnos (2026-10-17).** Símbolos: BTC, SOL, XRP, ETH, XLM, BNB,
  TRX (los cuatro nuevos verificados como perpetuo USDT en Binance, Bybit, OKX y Hyperliquid). "METAL" no
  existe como perpetuo en ninguno; MTL (Metal DAO) solo en Binance y Bybit: queda pendiente de confirmar
  cuál quiso el usuario. Rotación (`HEATSIGNAL_GRAPH_ROTATION`, default 2): cada ciclo con grafo analiza
  los símbolos con estrategia más 2 del resto por turnos (`rotation_pick`). El turno sigue tras un
  reinicio porque se cuenta desde `cycle_log`. Con 7 símbolos, cada cripto sin estrategia se analiza cada
  ~45 min y el costo por ciclo es el de ~3 símbolos. El panel muestra el último análisis de cada cripto.

- **Lote 1 del laboratorio → aprobado por el usuario (2026-10-17), ANTES de ver resultados.** Specs en
  `estrategias-lote1.md`, congeladas por hash: `donchian-ensemble-v1` 86061813c7d1691c; `flujo-apertura-hora-v1` 676f9a08f9af3828; `reversion-flujo-extremo-15m-v1` cd15e9623abe8bfa; `capitulacion-long-v1` 3717fbb833f28a96; `squeeze-short-v1` f146c3d219bc056b; `funding-crowding-xs-v1` 4aa52e735fc55460; `funding-crowding-short-v1` 38869de0fc4bc760; `funding-spread-hl-cex-v1` 09cb4efe470c9fef; `absorcion-flujo-v1` a3768ae5a4adaadd. Cualquier cambio a partir de acá es una versión
  nueva (`-v2`). Criterios para pasar a paper trading, fijados de antemano en `lab/run.py::CRITERIA`: DSR ≥ 0,95,
  break-even ≥ 2× el costo esperado, le gana a la canasta, al buy & hold medio y a las entradas al azar (p ≤ 0,05).
  Holdout: los últimos 3 meses, sellados.

- **LLM del grafo: Anthropic con fallback a Gemini (2026-10-17).** La cuenta de Anthropic se quedó sin crédito y
  el usuario sumó una key de Gemini (Google AI Studio). `llm_factory.make_llms` arma la cadena
  Anthropic → `gemini-3.5-flash(-lite)` → `gemini-2.5-flash(-lite)`, porque los 3.5 devolvieron 503 "high demand"
  en un pico. Cada pedido estructurado prueba el siguiente proveedor si el anterior falla; si fallan todos,
  el ciclo queda `error`. Verificado contra la API real: el grafo completo corre solo con Gemini en ~47 s.
  Gemini da confianzas más altas que Claude (0,85–0,95 contra ~0,5): mirar la calibración (Brier) por proveedor.

- **Resultados del lote 1 del laboratorio (2026-10-17).** Walk-forward OOS del 2021-03-13 al 2026-07-15 (1.950 días),
  holdout del 2026-07-15 en adelante **sellado**. Comparación: canasta Sharpe 0,63; buy & hold medio 0,02.
  - **Pasa sus criterios pre-registrados: solo `donchian-ensemble-v1`.** Sharpe 1,08 (IC 95% 0,10–2,01), retorno
    +43% con max DD 9,8%, 635 trades, acierto 21% (pocas ganancias grandes), break-even 77 bps por lado, p contra
    azar 0,0099, DSR 0,994 con K = sus 3 variantes. PnL: precio +0,53, funding −0,05, costos −0,04.
  - **PERO con el ajuste estricto de todo el programa (K = 20 variantes del lote, varianza de sus Sharpe)**: SR0 anual
    ≈ 2,67 y DSR ≈ 0. Ni el mejor Sharpe de desarrollo (1,40) supera lo esperable por azar entre 20 intentos tan
    dispares. **Conclusión: candidata, no probada.** El siguiente paso es evidencia fresca: holdout (una vez)
    y paper trading hacia adelante.
  - **No pasan (7):** `flujo-apertura-hora` (Sharpe −1,93), `reversion-flujo-extremo-15m` (−0,87),
    `capitulacion-long` (−0,18; 61 trades), `squeeze-short` (−0,40; 37 trades), `funding-crowding-xs` (−1,30; los
    costos −0,68 se comen el funding cobrado +0,08), `funding-crowding-short` (−0,78) y el control
    `absorcion-flujo` (−4,99, falló como se esperaba: el laboratorio no inventa ganancias). Varias tienen bruto
    positivo y mueren por comisiones y slippage (break-even de 0,4–3,4 bps contra 7 bps de costo).

## Abiertas
- ~~Plan de CoinGlass a contratar.~~ **Ya no aplica al core** — la fuente primaria pasó a WS nativos.
  Solo se retomaría si se implementa la fuente BYOK opcional (y ahí el plan lo elige el usuario).
- **Proveedor LLM del grafo → Anthropic (PROPUESTA, falta confirmación humana).** Implementado como
  default en `backend/src/heatsignal/agents/llm_factory.py` (`make_llms`) vía `langchain-anthropic`:
  `claude-haiku-5-5` (analistas, modelo económico) y `claude-sonnet-5-5` (debate, sintetizador,
  redactor). IDs confirmados contra la tabla de modelos vigente del skill `claude-api` (2026-10).
  Structured output con `method="json_schema"` (Sonnet 5.5 rechaza `tool_choice` forzado, que es lo
  que usa el método por defecto de langchain). Los nodos reciben el LLM inyectado, así que cambiar de
  proveedor no toca el grafo. Pendiente: confirmar proveedor + presupuesto, y validar con una API key
  real (no hay key en la máquina de desarrollo; todo está testeado con modelos falsos). El grafo ya
  está cableado en la VM y arranca solo cuando aparezca `ANTHROPIC_API_KEY` en su `.env`.
- **Símbolos iniciales.** Corriendo BTCUSDT + SOLUSDT. El usuario pidió sumar XRPUSDT (para la estrategia de
  largo plazo `xrp-acumulacion-v1`); falta aplicarlo en el `.env` de la VM. ETHUSDT sigue como propuesta.
- **Estrategias de largo plazo (meses).** Borrador `strategies/xrp-acumulacion-v1.json`, a 90 días. Antes de
  publicar un número: el evaluador no descuenta funding (en un perpetuo a 90 días pesa mucho), así que se
  evalúa como spot. Además, con una hipótesis abierta a la vez salen como mucho unas 4 por año: el n es
  chico por construcción y el intervalo de confianza va a ser ancho durante años.
- **Supuesto de costos publicado.** 10 bps round-trip (taker 5 bps × 2, sin slippage ni funding). Decidir
  si se publica así o se modela slippage/funding antes de mostrar números.
- **Umbral de régimen (`regime-v1`).** 2 ATR de desplazamiento en 20 barras de 60 s; calibrar con datos
  del shadow (cambiarlo = nuevo id de versión).

## Plantilla para nuevas decisiones
```
- **[Título] → [elección].** Sobre [alternativa]. Razón: [...]. Fecha: [...].
```
