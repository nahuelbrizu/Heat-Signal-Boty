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

- **Apertura del holdout de `donchian-ensemble-v1`: aprobada por el usuario; criterio fijado ANTES de abrir
  (2026-10-17).** Procedimiento: la combinación se elige con la misma regla del walk-forward (mejor Sharpe en los
  últimos 180 días antes del holdout). Las señales son causales; se simula solo [holdout_start, último dato) con la
  posición vigente al arrancar. Se abre una vez (el registro lo bloquea). **Confirma si se cumplen las tres:**
  (a) retorno neto > 0; (b) Sharpe > Sharpe de la canasta en la misma ventana; (c) p contra entradas al azar ≤ 0,10.
  El umbral es más laxo que el del OOS porque 3 meses es una muestra chica.
  **Si no confirma, la estrategia no pasa a paper trading con plata real; se puede seguir solo en paper para
  juntar evidencia.** En cualquier caso, 3 meses no alcanzan para "probar": el paper trading sigue siendo
  necesario.

- **Holdout de `donchian-ensemble-v1` abierto (2026-10-17): NO confirma.** Ventana 2026-07-16 → 2026-10-16
  (92 días, 24 trades), `target_vol` 0,15 elegido con la regla del walk-forward. Sharpe 1,80 (IC 95% −3,7 a 5,1),
  retorno +4,3%, max DD 2,0%. Checks: retorno > 0 ✓; Sharpe > canasta ✗ (canasta 1,85, retorno +19,7%); azar
  ✗ (p 0,86: en un trimestre muy alcista, las entradas long al azar con la misma duración rinden más, mediana 2,43).
  Lectura: en un mercado que sube fuerte, una estrategia long con volatilidad objetivo 15% queda menos expuesta
  que la canasta. La muestra es chica (IC enorme), así que el resultado ni confirma ni refuta con fuerza.
  **Según la regla fijada: no pasa a plata real; puede seguir en paper trading para juntar evidencia.**

- **Paper trading de `donchian-ensemble-v1` con Freqtrade 2026.9 en DRY-RUN (2026-10-17).** Corre en la VM
  (`heatsignal-freqtrade.service`): wallet simulado de 10.000 USDT, los 7 pares de futuros, velas de 4h, precios
  reales de Binance, sin API keys de exchange. Estrategia en `freqtrade/user_data/strategies/DonchianEnsembleV1.py`
  (misma regla congelada, target_vol 0,15). Diferencias con el laboratorio: rebalanceo solo si cambia más del 5%
  del capital del par; el dry-run no cobra slippage; Freqtrade reinvierte las ganancias.
  Paridad del backtest (2021-03-13 → 2026-07-15): **635 trades, los mismos que el laboratorio**; +64% (CAGR
  9,7%), DD 7,4%. Base de trades: `data/freqtrade_dryrun.sqlite`. FreqUI en 127.0.0.1:8081 por túnel
  (`hs-ft`, `hs-ft-open`, `hs-ft-pass`); las credenciales se generan en la VM (`freqtrade/config.secrets.json`,
  fuera de git). Próximo: comparar semana a semana el dry-run contra el backtest de las mismas velas.

- **Lote 2 del laboratorio: aprobado por el usuario (2026-10-18), ANTES de ver resultados.** Specs en
  `estrategias-lote2.md`, congeladas por hash: `compresion-breakout-v1` a25d6babd6964b30; `momentum-intradia-v1` 4776269a35a6edde; `oi-precio-continuacion-v1` df0fd44676eb37c7; `carry-xs-funding-v1` 118bdbb71f36906a; `lunes-asia-v1` 73cb513730c5c018. Mismos criterios que el lote 1 (`lab/run.py::CRITERIA`).
  `momentum-intradia-v1` corre con BTC y ETH; `lunes-asia-v1` con BTC y en modo observación (sin capital aunque
  pase). El programa pasa de 20 a 29 variantes: el DSR del programa se calcula con K = 29.
  Benchmark de TA común: las 7 estrategias de futuros de freqtrade-strategies sin modificar
  (`lab/ft_benchmark.py`) en la misma ventana OOS 2021-03-13 → 2026-07-15.

- **Resultados del lote 2 y del benchmark de TA (2026-10-18): ninguna pasa.** OOS walk-forward 2021-03-13 →
  2026-07-15 (1950 días), costo 7 bps por lado:
  | estrategia | Sharpe | retorno | DD | DSR propio | break-even | falla en |
  |---|---|---|---|---|---|---|
  | compresion-breakout-v1 | −0,12 | −4,1% | 13% | 0,21 | 5,8 bps | DSR, costos, canasta, B&H |
  | momentum-intradia-v1 | −3,92 | −125% (suma simple) | 125% | 0,00 | <0 | todo, incluso azar |
  | oi-precio-continuacion-v1 | −0,09 | −7,6% | 26% | 0,33 | 6,1 bps | DSR, costos, canasta, B&H |
  | carry-xs-funding-v1 | −0,05 | −4,4% | 31% | 0,46 | 6,7 bps | DSR, costos, canasta, B&H |
  | lunes-asia-v1 (observación) | 0,38 | +59% | 38% | 0,82 | 16,5 bps | DSR, canasta, azar |
  Canasta igual-ponderada: Sharpe 0,63. Lectura: casi todas tienen algo de señal bruta (Sharpe 0,3–0,6 sin
  costos) pero el break-even queda en 6–7 bps por lado, justo el costo esperado: **la ventaja se la comen las
  comisiones**. Momentum intradía está mal planteada (pierde contra el azar).
  Benchmark de Freqtrade (sin reinversión, Sharpe sobre PnL realizado diario): VolatilitySystem +318% Sharpe 0,76
  (apalancamiento 2×), FSampleStrategy +91% 0,44, TrendFollowing +85% 0,39, FSupertrend +40% 0,30, FAdxSma −14%,
  FOtt −62%, FReinforced −86%. Ninguna pasa el listón del programa.
  **Listón del programa:** K = 36 (29 combinaciones del lab + 7 del benchmark); varianza de los 20 Sharpe OOS
  observados → SR0 ≈ 3,5 anual. DSR del programa ≈ 0 para todas, incluida `donchian-ensemble-v1` (Sharpe 1,08).
  Aun sacando las dos estrategias rotas (Sharpe < −3) de la varianza, SR0 ≈ 1,6 y el DSR de donchian es 0,11:
  **no hay evidencia de ventaja real en ninguna estrategia probada hasta ahora.** Se publica en el panel
  (columna «DSR prog.», listón y tabla del benchmark en Laboratorio).
  Lote 3 (6 specs, `estrategias-lote3.md`): programado y con tests, PROPUESTO, sin aprobar; K pasaría a 44.

- **Lote 3 aprobado por el usuario (2026-10-18), ANTES de ver resultados** ("corré estas estrategias en el bot
  y paper trading"). Hashes congelados: `tsmom-diario-v1` 7515d798e5d66a55; `low-vol-xs-v1` 307d3572e576e4c2;
  `pares-eth-btc-v1` c9c2243ebcef9393; `rango-reversion-v1` a03dba31be20137f; `reversion-xs-1h-v1` e2a331826092f698;
  `funding-prepago-v1` 3fc248f8641d0402. K del programa = 44. `pares` corre con ETH+BTC, `rango` con BNB+TRX+XLM,
  el resto con los 7.
  **Nulo de azar conjunto para las neutrales:** `lab.baselines.random_shift_joint` desplaza en círculo todas las
  patas el mismo número de pasos al azar (conserva cobertura y duraciones; solo rompe la sincronía con el precio).
  `run.null_kind`: `joint_shift` si `direction == "neutral"`, `per_symbol` si no; queda en cada reporte
  (`baselines.random_entries.kind`). Las neutrales del lote 2 (`carry-xs-funding`, `funding-crowding-xs`) se
  corrieron con el nulo por símbolo y no se re-corren (re-correr sería un intento más).
  **Paper trading en paralelo al backtest:** por pedido del usuario el lote 3 entra a paper trading (simulado,
  sin plata) antes de tener el veredicto del laboratorio. Es evidencia hacia adelante, limpia; no cambia la regla:
  nada pasa a plata real sin pasar el laboratorio, el holdout y la comparación paper vs backtest.

- **Resultados del lote 3 (2026-10-18): ninguna pasa; `tsmom-diario-v1` es la más cercana de todo el programa.**
  OOS 2021-03-13 → 2026-07-15, 7 bps por lado:
  | estrategia | Sharpe (IC 95%) | retorno | DD | DSR propio | break-even | p azar | falla en |
  |---|---|---|---|---|---|---|---|
  | tsmom-diario-v1 | 0,67 (−0,18 a 1,51) | +48,5% | 12% | 0,94 | 30,7 bps | 0,02 | solo DSR (0,94 < 0,95) |
  | low-vol-xs-v1 | −0,74 | −152% | 170% | 0,04 | <0 | 0,63 (conjunto) | todo |
  | pares-eth-btc-v1 | −1,38 | −86% | 92% | 0,00 | <0 | 0,98 (conjunto) | todo |
  | rango-reversion-v1 | −3,15 | −276% | 274% | 0,00 | <0 | 1,00 | todo |
  | reversion-xs-1h-v1 | −4,01 | −280% | 272% | 0,00 | 0,6 bps | 0,01 (conjunto) | DSR, costos, canasta, B&H |
  | funding-prepago-v1 | −3,52 | −237% | 227% | 0,00 | 1,2 bps | 0,01 | DSR, costos, canasta, B&H |
  (retornos como suma simple sin reinversión: por eso pueden pasar de −100%). Lectura: las de alta rotación
  (reversion-xs 13.757 trades, prepago 14.231, rango 3.379) tienen timing mejor que el azar (p 0,01) pero el
  costo las destruye; las de reversión de pares / baja volatilidad no tienen señal. `tsmom-diario` (target_vol
  0,15 elegido por el walk-forward) le gana a la canasta (0,67 vs 0,63), aguanta 4× el costo y le gana al azar.
  **Listón del programa:** K = 44 (37 del lab + 7 del benchmark), SR0 ≈ 3,85 → DSR del programa ≈ 0 para todas.
  Ninguna pasa a plata real. Holdout de tsmom sigue SELLADO (se abre una sola vez, cuando se decida).
- **XRP deja de ser estrategia (2026-10-18, pedido del usuario).** `xrp-acumulacion-v1/v2` "solo eran un
  comentario": la posición de XRP (comprada a 2,5 USD) es una tenencia personal de largo plazo. Se retiraron del
  grafo con un evento de estado `evaluated` (el registro es de solo agregar: no se borran filas; quedan como
  historia) y los JSON se movieron a `strategies/retiradas/`. Sin estrategias activas el grafo queda en
  `no_strategies` (no gasta LLM) hasta registrar las nuevas de 1h / sesiones. La tenencia se ve en el panel
  (Resumen → "Mi wallet", `GET /api/wallet`, archivo `data/wallet.json` en la VM, fuera de git; `qty` opcional).

- **Lote 4 aprobado por el usuario (2026-10-18), ANTES de ver resultados** ("dale, aprobadas, corré el lote 4").
  Specs en `estrategias-lote4.md`, congeladas: `ema-cruce-v1` a950979de8e216af; `ema-cruce-filtrado-v1`
  04d0e50a506fde17. Grilla timeframe 1h/4h en las dos: K del programa = 48. Cruce en la vela (no estado), tope de
  tamaño 1×, sin salida por señal contraria. Umbrales fijos: cualquier ajuste es una -v2.

- **Paper trading del lote 3 con bot propio (2026-10-18).** `heatsignal.paperbot`, corrida horaria a los :02 UTC
  (`heatsignal-paperbot.timer`, `MemoryMax=400M`, pico medido 143 MB). No Freqtrade: 4 de las 6 son de pares /
  cross-sectional y la VM no tiene RAM para otro Freqtrade. Llama a las mismas reglas congeladas del lab con velas
  cerradas en H y 64 días de ventana; ejecución como `lab/sim.py` (open 1m siguiente, 5 + 2 bps, funding real,
  10.000 USDT por estrategia sin reinversión). Base `data/paperbot.sqlite` append-only. Hueco de datos = la
  corrida queda en error y no opera; las horas perdidas no se simulan (se marcan). Parámetros en
  `paperbot/params.json` (tsmom target_vol 0,15 y pares entry_z 2,5 del walk-forward). El backtest no aprobó
  estas estrategias: el paper mide la distancia simulación–realidad, no es aprobación. El archivado conserva 90
  días de velas 1m (`--klines-keep-days 90`).
- **Hipótesis de 1h con tendencia diaria y sesiones de mercado (2026-10-18).** Contexto multi-temporalidad por
  código (`htf-trend-v1`, `features/htf.py`), solo velas de 1h cerradas de la base propia (REST solo para
  huecos, declarados). `daily_trend`: alcista si el cambio desde la apertura UTC ≥ +1,5 ATR(1h), cierre sobre la
  EMA10 diaria y EMA20 de 1h subiendo; bajista simétrico; resto lateral; `sin_datos` si falta historia. Cambiar
  la regla = versión nueva. Cada hipótesis guarda temporalidad, tendencia, a favor / en contra y sesión
  (`hypothesis_context`, aditiva). Las de 1h vencen en un cierre de vela y se resuelven con esa vela a mark
  price. Sesiones (Asia 09:00 Tokio, Europa 08:00 Londres, EE.UU. 09:30 Nueva York, cierre 23:00 UTC; DST por
  zoneinfo) se analizan al cerrar su primera vela de 1h y se suman a un diario append-only del día
  (`session_journal`) que reciben los análisis siguientes. Modo `HEATSIGNAL_ANALYSIS_MODE=sessions`: el grafo
  corre en cada sesión con todos los símbolos en las mismas llamadas. Las 5 familias nuevas
  (`backend/strategies/tendencia-diaria-1h/`) se registran solo con aprobación del usuario.
- **Gemini por Vertex AI exprés (2026-10-18).** La key nueva (formato `AQ.`) no sirve en AI Studio (403) y sí en
  Vertex; `llm_factory.gemini_uses_vertex` lo detecta. Sin el tope gratis de 20 pedidos/día/modelo; se factura por
  uso (`ON_DEMAND`) en el proyecto de Google Cloud de la key.

- **Resultados del lote 4 (2026-10-18): ninguna pasa; `ema-cruce-v1` falla solo por DSR.** OOS 2021-03-13 →
  2026-07-15, 7 bps por lado:
  | estrategia | Sharpe (IC 95%) | retorno | DD | trades | acierto | DSR propio | break-even | p azar |
  |---|---|---|---|---|---|---|---|---|
  | ema-cruce-v1 | 0,77 (−0,09 a 1,61) | +18,5% | 7% | 1.564 | 39% | 0,76 | 14,4 bps | 0,01 |
  | ema-cruce-filtrado-v1 | 0,64 (−0,16 a 1,50) | +4,1% | 2% | 156 | 43% | 0,77 | 21,7 bps | 0,02 |
  Canasta 0,63. El walk-forward eligió 4h en 52 de 65 ventanas (cruce) y 36 de 65 (filtrado). Retorno bajo por el
  tamaño por riesgo (1% hasta el stop: exposición chica), por eso el DD también es chico. Los filtros sacan el 90%
  de las operaciones y no mejoran el Sharpe. **Listón del programa:** K = 48, SR0 ≈ 3,9 → DSR del programa ≈ 0.
  Nota de método (abierta): la varianza de los Sharpe entre intentos está inflada por estrategias rotas (Sharpe
  −3 a −5), lo que sube SR0 hasta un nivel que ninguna estrategia real alcanza; ver "Abiertas".

- **5 estrategias del grafo en velas de 1h, registradas con aprobación del usuario (2026-10-18)** ("registrá
  todas"), antes de cualquier resultado. Archivos en `backend/strategies/tendencia-diaria-1h/`; registro inmutable
  en `heatsignal_eval.sqlite`: `1h-apertura-continuacion-a-favor-v1` 515b5470-d6e5-4bd9-adc7-818d43fc2dec;
  `1h-apertura-reversion-en-contra-v1` ffa65014-b229-4d42-9f7a-d319b5b5f820; `1h-continuacion-a-favor-fin-dia-v1`
  0c8b962a-e4d5-4402-83f2-31bf20b470b5; `1h-extremo-rango-diario-en-contra-v1` 5cd0c0b4-ae24-465c-90b9-9838c79ffa40;
  `1h-retroceso-ema-a-favor-v1` da81e854-6cba-48b4-b6a3-8d8395f459d9. Corren en las sesiones (modo `sessions`).
  Cada una es una variante distinta: sus tasas se comparan entre sí y contra mantener, con n e IC.

- **Base de conocimiento de análisis técnico del usuario (2026-10-18): qué se adopta.** Revisión en
  `research/base-conocimiento-ta.md`. Se adopta: (1) lote 5 (cartera de tendencia, ruptura del máximo de 10 días,
  reversión al VWAP), PROPUESTO en `estrategias-lote5.md`; (2) diagnósticos anti-sobreajuste en el lab: PBO por
  CSCV y robustez por parámetros vecinos y costos ×3 (diagnóstico, nunca selección; no suman K); (3) etapa
  "entrenar bots" con triple barrier + meta-labeling + purged K-fold, después de (2); (4) **límites duros
  obligatorios para la etapa de ejecución**: riesgo 0,5–1 % por trade hasta el stop, pérdida diaria máxima con
  corte hasta el día siguiente, máximo de operaciones por día, margen aislado y apalancamiento fijo con stop
  antes de liquidación, tope de exposición total por correlación, sin órdenes market en baja liquidez, kill switch
  si el drawdown supera 1,5 × el máximo histórico de la estrategia. No se adopta: ΔOI en USD (se mantiene en
  contratos), lotes de patrones de velas o SMC como estrategias (solo como features), cifras externas como
  umbrales sin validar.

- **Hipótesis de 1h ejecutadas en un segundo Freqtrade dry-run (2026-10-18).** Pedido del usuario: "enviá algunas
  operaciones al bot de freq". El Freqtrade de Donchian no sirve (solo long, `adjust_trade_position` lleva
  cualquier trade a su objetivo, su salida cierra los trades y ensuciaría `paper_compare`). Segunda instancia
  `heatsignal-freqtrade-hyp`: estrategia `HypothesisBridgeV1` sin señales, dry-run, futures, 7 pares, wallet 10.000,
  stake 1.000 (redondeado a la precisión del par), leverage 1, base `data/freqtrade_hyp.sqlite`, API solo en
  127.0.0.1:8082. Puente `paper/ft_bridge.py` (cada 5 min): `forceenter` por cada hipótesis nueva de 1h con tag
  `hyp:<id8>:<due_epoch>`; cierra con `custom_exit` al vencer. Stop de seguridad −20 % (no es parte de la
  hipótesis; el panel lo marca). Un trade por par: la segunda hipótesis del par queda "omitida: par ocupado".
  Registro append-only `ft_bridge_orders`. RAM medida 405 MB estables / 465 MB pico (`MemoryMax=550M`); vigilar
  swap: si crece, pasar a e2-medium. Mide la distancia simulación–ejecución; no aprueba estrategias.

- **Diagnósticos anti-sobreajuste: PBO (CSCV) y robustez por vecinos (2026-10-19).** `heatsignal.lab.diagnostics`
  (`pbo|robustez|ambos <id>` o `--all`) → `results/<id>/diagnostics_<ts>.json` y panel (columna PBO, tarjeta con
  gráfico de vecinos). PBO: matriz días × combinaciones de la grilla congelada sobre el desarrollo, S=16, C(16,8) =
  12.870 particiones; PBO = fracción con logit ≤ 0, más degradación y probabilidad de pérdida OOS (solo grillas de
  2+ combinaciones; con N=2 informa poco). Robustez: cada parámetro numérico ±20 %/±40 % de a uno, Sharpe del
  desarrollo con 7 bps y con costos ×3; meseta = fracción de vecinos con Sharpe ≥ 50 % del original. **Diagnóstico,
  no selección:** tabla aparte append-only `lab_diagnostics` (no suma K ni entra a SR0); ningún resultado cambia
  parámetros congelados (una mejora vista en un vecino solo se prueba como `-v2` pre-registrada); nunca usan el
  holdout. Primera corrida: `tsmom-diario-v1` meseta 100 %, Sharpe del desarrollo 0,98 → 0,60 con costos ×3.
  (El refactor de `lab/run.py` que usan los diagnósticos quedó dentro del commit 3aabefc.)

- **Diagnósticos de las 21 estrategias (2026-10-19).** Robustez (Sharpe del desarrollo, ×3 costos, meseta):
  `donchian-ensemble-v1` 1,40 → 1,21, meseta 100 % (también con ×3), caída máx. 8 %, PBO 0,15: **la más robusta**.
  `tsmom-diario-v1` 0,98 → 0,60, meseta 100 % (95 % con ×3), PBO 0,19. `ema-cruce-v1` 0,99 → 0,26, meseta 95 %
  (74 % con ×3), caída máx. 65 %, PBO 0,00: sensible a costos. `ema-cruce-filtrado-v1` 0,51 → 0,32, PBO 0,10.
  `lunes-asia-v1` 0,40 → −0,14, meseta 50 %: frágil. `reversion-flujo-extremo-15m-v1` PBO 0,56: sobreajuste. El
  resto tiene Sharpe ≤ 0 (meseta n/a). Diagnóstico, no selección: no cambia parámetros ni K.

- **Lote 5 aprobado por el usuario (2026-10-19), ANTES de ver resultados** ("sí, apruebo"). Specs congeladas en
  `estrategias-lote5.md`: `cartera-tendencia-v1` 3d7b32a429f494fc; `max-ruptura-10d-v1` 39cb9dee6d3e469a;
  `vwap-reversion-v1` 724e2b1e207828df. K del programa = 51.

- **Resultados del lote 5 (2026-10-19): dos pasan los criterios propios del lab.** OOS 2021-03-13 → 2026-07-15:
  | estrategia | Sharpe (IC 95%) | retorno | DD | trades | DSR propio | break-even | p azar | veredicto lab |
  |---|---|---|---|---|---|---|---|---|
  | cartera-tendencia-v1 | 0,81 (−0,16 a 1,81) | +26,8% | 7% | 3.908 | 0,97 | 14,3 bps | 0,01 | **pasa** |
  | max-ruptura-10d-v1 | 0,97 (−0,03 a 1,84) | +113,9% | 21% | 1.209 | 0,99 | 39,8 bps | 0,01 | **pasa** |
  | vwap-reversion-v1 | −6,26 | −1189% (suma) | — | 53.271 | 0,00 | <0 | 0,01 | no pasa: costos |
  Canasta 0,63. Ambas le ganan a canasta, B&H medio y azar. **Pero contra el listón del programa (K = 51) el DSR
  sigue ≈ 0** y los IC del Sharpe tocan 0: pasan a paper / holdout, no a plata real. La cartera combina 3 reglas
  ya evaluadas (comparte muestra con ellas). max-ruptura es solo long: en un período alcista parte del resultado es
  beta; le gana al B&H medio igual. vwap confirma que alta rotación en 15m muere por costos.

- **Apertura del holdout de `max-ruptura-10d-v1` y `cartera-tendencia-v1` (2026-10-19, pedido del usuario)**, ANTES
  de ver el resultado, con los criterios fijos de `run.HOLDOUT_CRITERIA` (los mismos que Donchian). Una sola vez:
  el resultado no se usa para cambiar nada; un cambio es una -v2 que necesita datos nuevos.

- **Holdout de `max-ruptura-10d-v1` y `cartera-tendencia-v1` (2026-07-17 → 10-17): ninguno confirma.**
  max-ruptura: Sharpe 2,21, +13,2 %, DD 2,7 %, 51 trades; le gana a la canasta (1,95) pero NO al azar (p 0,18 >
  0,10): en un trimestre muy alcista, entrar long al azar con las mismas duraciones rinde parecido.
  cartera-tendencia: Sharpe 0,01, +0,0 %, 204 trades; no le gana a la canasta (1,95) ni al azar (p 0,58).
  Según la regla fijada: no pasan a plata real. max-ruptura puede ir a paper para juntar evidencia en otro
  régimen (el holdout fue un trimestre alcista, poco informativo para una estrategia solo long).
- **Curvas de capital del lote 1 (2026-10-19).** Los 7 resultados previos a `oos.equity_daily` se reprodujeron
  (mismos datos hasta 2026-10-16, mismos parámetros, registro de intentos aparte: no suma K). Los 7 dieron
  idénticos (Sharpe y trades); se agregó solo la curva en `<run>_curvas.json` con el veredicto original.

- **max-ruptura-10d-v1 al bot propio de paper (2026-10-19, pedido del usuario).** Spec congelada
  39cb9dee6d3e469a, 7 símbolos, 10.000 USDT ficticios y canasta propia desde su primera corrida. El modo del bot
  (inicio/consecutiva/hueco) se decide por estrategia: entra "desde hoy" sin tocar la historia de las otras 6;
  `paperbot/params.json` define qué corre. No es aprobación: el holdout no confirmó (p azar 0,18 en un trimestre
  alcista); el paper junta evidencia en otro régimen. El panel muestra backtest y holdout al lado.
- **Revisión semanal automática (2026-10-19).** `heatsignal.paper.weekly` + `heatsignal-weekly.timer` (lunes
  00:30 UTC) cierran la semana UTC anterior por código (sin LLM) en `weekly_reviews` (append-only, idempotente;
  fuente caída = `partial`). Junta bot propio, Freqtrade Donchian (+ paper_compare), Freqtrade de hipótesis,
  hipótesis del grafo (n, IC de Wilson, alfa, cortes) y salud. Llamadas al LLM: no se cuentan todavía
  (`cycle_log` no las registra; va con las métricas del grafo). Una semana no prueba nada: sirve para operar.
  Vista `#/semanal`.

- **Contexto macro point-in-time (2026-10-19).** FRED/ALFRED (DFF, CPIAUCSL, DGS10, DTWEXBGS, VIXCLS) por vintage:
  un valor existe desde las 00:00 de Chicago del día siguiente a su publicación (`FRED_API_KEY` en el `.env` de la
  VM; sin key, apagado y declarado). Calendario FOMC/CPI/NFP en `macro/config/calendar.json` (fuentes oficiales,
  copiado a mano; el panel avisa si quedan < 45 días de fechas). Polymarket (Gamma, pública) con lista versionada
  en `macro/config/polymarket_markets.json`. Base append-only `heatsignal_macro.sqlite`, timer horario
  (`heatsignal-macro.timer`, :07), export a Drive. Bloque `macro-context-v1` (≤ 1150 caracteres, ~320 tokens) solo
  en el Analista de Contexto, sin llamadas extra. `lab/macro_features.py` para el meta-modelo. Adaptado de
  TradingAgents (Apache-2.0). Cambiar series, lista o formato es versión nueva. Polymarket no resuelve por DNS
  desde la red de la PC (proveedor de internet); desde la VM sí.
- **Diario de lecciones: solo lectura humana, NO se reinyecta (2026-10-19).** Una lección por hipótesis resuelta
  (LLM barato o por código, marcada), append-only en `hypothesis_lessons`. No entra a ningún prompt ni decisión
  (test estático y dinámico): reinyectarla cambiaría el sistema sin pre-registro. Usarlas sería una decisión aparte,
  pre-registrada y contada. Vistas `#/macro` y `#/lecciones`.

- **Listón del programa: regla aprobada por el usuario (2026-10-19), ANTES de calcular resultados con ella.**
  Auditoría previa (Etapa 1): (1) el DSR propio de `run.py` usa K = combinaciones de esa estrategia y su dispersión;
  con K = 1 la dispersión es 0 y el DSR = PSR (sin corrección): el "pasa" de max-ruptura y cartera no está corregido
  por el programa. (2) El DSR del programa (`publish.program`) usa la dispersión observada de Sharpe OOS (1,96 vs
  ~0,43 esperada por azar con T = 1950 días), mezcla unidades (K = combinaciones, dispersión = estrategias) y
  construcciones (Freqtrade = PnL realizado), ignora asimetría y curtosis, y no usa `effective_n_trials`. (3) No hay
  experimentos inválidos conocidos (los Sharpe muy negativos son pérdidas reales por costos); falta un estado
  explícito por intento. **Regla:** se calculan y publican tres métodos en paralelo: (i) DSR actual (referencia);
  (ii) DSR con dispersión nula analítica por estrategia (con asimetría y curtosis) y K efectivo por correlación de
  retornos diarios (Freqtrade cuenta en K como independiente, no en la dispersión; el registro sigue diciendo 51);
  (iii) test de máximo por bootstrap estacionario conjunto (bloques medios de 20 días, B = 5.000, nula impuesta por
  centrado, estadístico = máximo Sharpe del conjunto, Romano–Wolf escalonado para identificar cuáles).
  **Criterio de aprobación:** (iii) p ≤ 0,05 principal y (ii) ≥ 0,95 como confirmación; nada pasa a plata real sin
  paper trading. Si los métodos divergen se investiga; no se elige el que apruebe. El DSR propio con K = 1 se
  muestra como PSR. Estados por intento: válido / inválido técnico (con motivo) / sin resultado.
- **Stop loss y take profit (2026-10-19, pedido del usuario: "agregá esa info en todo el sistema").** Estado actual:
  solo algunas reglas tienen stop/objetivo (ema-cruce 1,5/3 ATR, rango y vwap 1,5 ATR + objetivo, pares z 4,
  capitulación/squeeze, funding-crowding-short); el resto sale por tiempo, canal o rebalanceo (Donchian, tsmom,
  max-ruptura, cartera). Las hipótesis de 1h salen por tiempo (pre-registradas así). Freqtrade Donchian sin stop
  (como el lab); Freqtrade de hipótesis con stop de seguridad −20 %. **Se agrega:** ficha de salidas por estrategia
  (stop, objetivo, tiempo, otra) en un mapa aparte que NO cambia los spec_hash, visible en lab, paper y Freqtrade;
  atribución de salidas (cuántas por stop, objetivo, tiempo, señal); MAE/MFE de cada hipótesis resuelta (cuánto fue
  en contra y a favor). Agregar stop/objetivo a una estrategia ya registrada es una -v2. **Para la etapa de plata
  real:** toda orden lleva stop del lado del exchange (reduce-only), obligatorio aunque la regla salga por tiempo.

- **Listón del programa: primer cálculo con la regla aprobada (2026-10-19). Ninguna estrategia pasa; los tres
  métodos coinciden.** `lab/program.py` (resultado `results/_program/program_20261018T232331.json`). K real 51
  (44 + 7), todas válidas; intersección de fechas 2021-03-13 → 2026-06-27 (1.933 días, sin holdout).
  (i) referencia: SR0 4,48. (ii) DSR corregido: dispersión nula 1/T, K efectivo 47,1 (20,1 por correlación entre
  estrategias + 20 combinaciones extra como independientes + 7 Freqtrade), SR0 0,975; DSR: donchian 0,60,
  max-ruptura 0,50, cartera 0,35, ema-cruce 0,31, tsmom 0,24, ema-filtrado 0,22. (iii) bootstrap del Sharpe máximo:
  máximo observado 1,08 (donchian), p = 0,24; el azar da un máximo mediano de 0,86 y un percentil 95 de 1,41;
  Romano–Wolf: donchian 0,24, max-ruptura 0,36, cartera 0,49, ema-cruce 0,59, tsmom 0,69. Lectura: con 24
  estrategias probadas sobre los mismos 5 años, el mejor Sharpe (1,08) está dentro de lo que el azar produce
  (percentil ~76). Las de tendencia son las mejores del programa pero todavía no hay evidencia estadística de
  ventaja. Seguir con: meta-labeling (plan de entrenamiento), datos nuevos hacia adelante y paper trading.

- **Stop loss / take profit implementado (2026-10-19).** Ficha `lab/exits.py` (`EXITS`, aparte del Spec: ningún
  spec_hash cambió; test contra `spec.params`). Atribución de salidas con `lab.diagnostics salidas|ambos`: misma
  lógica que `exits_with_levels`, verificada llamada por llamada en las reglas que la usan; en las que salen por
  transición de señal (Donchian, tsmom, carry, low-vol, funding-xs, pares, reversion-xs, prepago) el motivo se
  infiere de la ficha (no verificado). Solo desarrollo, no suma intentos. Paperbot: `exit_reasons` (append-only,
  desde ahora). Hipótesis: MAE/MFE en `hypothesis_excursions`; el corte de stops 1/1,5/2 ATR es descriptivo y NO
  pre-registrado. Freqtrade: stop y ROI configurados y `exit_reason` por trade en el panel. **Plata real:** toda
  orden lleva `STOP_MARKET` reduce-only en el exchange; si la regla no tiene stop, stop de catástrofe — PROPUESTA a
  confirmar por el usuario: 3 ATR(14) de la temporalidad, piso 8 %, techo 20 % del precio; si el stop no se puede
  colocar, la entrada se cancela. Verificar `STOP_MARKET`/`reduceOnly`/`workingType` contra la doc de Binance antes.
- **Listón del programa implementado** (`lab/program.py`, estados por intento en `lab_trial_status`, tres métodos
  en el panel). Resultado arriba.

- **3 estrategias del grafo para días laterales: aprobadas por el usuario (2026-10-19), ANTES de cualquier
  resultado.** Solo se activan si `daily_trend` (htf-trend-v1) = `lateral`. Velas de 1h CERRADAS, entrada verificada
  por código, el LLM solo explica. Como máximo UNA hipótesis por moneda y por sesión entre las tres. Costos del
  resolver (10 bps ida y vuelta). Definiciones fijas (cambiarlas = -v2):
  1. `1h-lateral-falsa-ruptura-asia-v1`: rango de Asia = máximo/mínimo de las velas 1h de 00:00–08:00 UTC del día. En
     la sesión de Europa (Londres) o EE.UU. (Nueva York), la última vela 1h cerrada tiene high > máx_Asia + 0,5·ATR14(1h)
     y close < máx_Asia → SHORT; o low < mín_Asia − 0,5·ATR y close > mín_Asia → LONG. Salida: cierre de la 3ª vela 1h.
  2. `1h-lateral-rechazo-dia-anterior-v1`: la última vela 1h cerrada toca el máximo del día UTC anterior (high ≥
     máx_ayer − 0,25·ATR) con mecha superior ≥ 50 % de su rango y close < máx_ayer → SHORT; simétrico en el mínimo de
     ayer (mecha inferior ≥ 50 %, close > mín_ayer) → LONG. Salida: cierre de la 4ª vela 1h.
  3. `1h-lateral-extremo-vwap-v1`: VWAP del día UTC (precio típico de velas 1m, anclado 00:00 UTC). Si |close_1h −
     VWAP| ≥ 2·ATR14(1h) y ADX14(1h) < 20 → hacia el VWAP (close > VWAP → SHORT; < → LONG). Salida: cierre de la 4ª
     vela 1h.

- **Lateralidad implementada (2026-10-19, sin cambiar umbrales).** Niveles con versión propia `htf-levels-v1`
  (`HtfContext.levels`); `htf-trend-v1` no cambia (las 5 de tendencia siguen válidas). VWAP con volumen base de las
  velas 1m (USD / precio típico en `bars_1m`); una hora sin 1m deja el VWAP en `sin_datos`. La entrada exige la vela
  recién cerrada. Tope `max_per_symbol_session = 1` por (familia `1h-lateralidad`, símbolo, sesión del día), contando lo
  ya registrado en la base. Si una vela cumple LONG y SHORT a la vez, no se entra. Con horario de verano británico
  (hasta el 25-10) la corrida de Europa (08:00 UTC) no puede disparar la falsa ruptura (su vela cae dentro del rango
  de Asia): esta semana esa estrategia solo puede salir en la de EE.UU.
  Registradas en la VM (2026-10-19 03:20 UTC): `1h-lateral-extremo-vwap-v1` 29c8efb0-e1d2-450c-8d1d-d20c12374a75;
  `1h-lateral-falsa-ruptura-asia-v1` b266a4c2-9818-4866-a276-ba0d410e1f27; `1h-lateral-rechazo-dia-anterior-v1`
  6f76eedc-4858-44eb-a39f-fb92d3a433a1. El grafo tiene 8 estrategias activas (5 de tendencia + 3 de lateralidad).

- **Meta-modelo, etapa 0: aprobada por el usuario y cerrada (2026-10-19).** Librerías fijas en la PC
  (`requirements-lab.txt`: scikit-learn 1.9.1, LightGBM 4.7.0). Pre-registro congelado en
  `backend/src/heatsignal/lab/meta/preregistro.json` (sha256 `5b49c49ea27b22e1`, test que lo congela):
  primarias Donchian 86061813c7d1691c, tsmom-diario 7515d798e5d66a55, ema-cruce a950979de8e216af, max-ruptura
  39cb9dee6d3e469a, con sus parámetros por defecto; entrenamiento y validación solo en el desarrollo de cada una
  (hasta el inicio de su holdout); **2026-07-16 → 2026-10-19 excluido** (holdouts ya abiertos o mirados); **examen
  final hacia adelante desde 2026-10-20**, mínimo 3 meses (datos nuevos + paper filtrada vs sin filtrar); presupuesto
  de 24 configuraciones contadas; logística primero y LightGBM chico (profundidad ≤ 4); purged k-fold k = 5 con
  embargo de 5 días; etiquetas triple barrier (ema-cruce con sus barreras; resto ±2 σ y tope = horizonte); calibración
  isotónica con ≥ 1.000 eventos por fold, si no Platt; semilla 20261019. **Enmienda 1 (2026-10-19, aprobada antes de
  construir el dataset):** `features-meta-v1` suma los grupos MACRO (FRED point-in-time, Polymarket, eventos próximos)
  y LATERALIDAD (rango de Asia, extremos de ayer, distancia al VWAP en ATR, ADX 1h, mechas); pre-registro sha256
  `36feb567ccef3b41`. Pendiente: stop de catástrofe para plata real.

- **Meta-modelo, etapa 1 (dataset de eventos y etiquetas) hecha (2026-10-19).** `lab/meta/dataset.py` →
  `~/heatsignal-lab/meta/events_v1.parquet` (n = 8.751, 2021-03 → 2026-07-16, nada del período excluido; pre-registro
  `36feb567ccef3b41`, mismo archivo que sha256 `267aebab…` contando el salto de línea final). Etiqueta 1 (PnL neto > 0):
  Donchian 39,7 % (n 635), tsmom 43,7 % (2.745 = 2.440 aperturas + 305 rebalanceos), ema-cruce 36,9 % (4.151; con su
  temporalidad por defecto 1h, por eso más eventos que el OOS del lab, que eligió 4h la mayoría de las ventanas),
  max-ruptura 46,9 % (1.220; los días encadenados de la misma posición no son eventos nuevos). PnL con 7 bps por lado y
  funding (idéntico a `simulate()`); barreras evaluadas desde la vela siguiente, empate = stop; pesos por unicidad.
  11 tests sin look-ahead (datos envenenados después del corte → resultado idéntico).

- **Avisos de datos clasificados + OI de Binance por REST (2026-10-19).** `features/gaps.py`: estructural (Binance
  forceOrder ≤ 1/s, 0 liquidaciones de un exchange conectado y sano, emisor sin RSS), respaldo (reconstruido por REST,
  completo) y real (todo lo demás, por defecto). Al LLM: los reales como "huecos" y el resto en una línea de "notas de
  cobertura (conocidas, no son fallas)"; sintetizador y redactor no las llaman severas. Panel y logs muestran todo con
  su clase. Contexto del grafo: ~15,4k → ~8k tokens por ciclo (se repetían avisos en cada analista). OI de Binance:
  `GET /fapi/v1/openInterest` (peso 1, doc oficial) cada 5 min en la ingesta, `TickerEvent coverage=rest_poll`, USD con
  el mark del WS; entra en `context.oi_*` y en la base histórica; si falla o tiene > 15 min, hueco real.

- **Meta-modelo, etapa 2a (features básicas) hecha (2026-10-19).** `lab/meta/features.py` (`GRUPOS`, `load_all()`,
  clave `evento_id`) → `features_basicas_v1.parquet`: 39 columnas (tendencia, volatilidad, momentum, velas, contexto),
  0 % de NaN salvo ≤ 0,21 % en 4 percentiles/EMA200 diaria; 15 tests sin look-ahead. `daily_trend` sale `sin_datos` en
  el 48 % (las primarias diarias deciden a las 00:00 UTC, sin velas del día cerradas: regla aplicada al pie de la
  letra). **Agregado antes de entrenar:** columna `daily_trend_dia_anterior` (htf-trend-v1 sobre el día UTC completo
  anterior a la decisión) en el grupo tendencia; no cambia la regla del grafo.

- **Meta-modelo, etapa 2b (derivados y mercado) hecha (2026-10-19).** `features_derivados_v1.parquet`: funding y z-score
  90 liquidaciones, ΔOI en contratos 1h/4h/24h, OI vs media 30d, cuadrante OI × funding, taker de la última vela 1h,
  long/short de top traders, premium; BTC (tendencia, vol, daily_trend), correlación 30d con BTC, cuántas de las 7 sobre
  su EMA200 diaria, retorno 7d de la canasta. Reloj: metrics en ts+5 min, funding ≤ t. NaN: long/short 28,8 % (Binance no
  lo publica en buena parte de 2022), ΔOI ~11,7 % (metrics de las alts arranca 2021-12). `daily_trend_dia_anterior`:
  lateral 54,9 %, alcista 28,2 %, bajista 16,9 %, sin_datos 0 %. **Agregado antes de entrenar:**
  `btc_daily_trend_dia_anterior` (misma razón: `btc_daily_trend` tiene 47,7 % sin_datos a las 00:00 UTC).

- **Meta-modelo, etapa 2c (macro y lateralidad) hecha (2026-10-19).** `features_macro_v1` (FRED point-in-time: DFF,
  CPI interanual, DGS10, DTWEXBGS 30d, VIX y percentil 1 año; 0 % NaN) y `features_lateralidad_v1` (iguales a
  `build_levels` del vivo; Asia NaN antes de 08:00 UTC 59 %, VWAP NaN a las 00:00 UTC 48 %, por construcción).
  `load_all()`: 80 columnas de features, 8.751 eventos. **Corrección antes de entrenar:** el calendario versionado solo
  cubría 2026 (días hasta CPI/NFP 100 % NaN, FOMC 92 %) → fechas históricas de CPI y NFP desde los vintages de FRED
  (`realtime_start` de CPIAUCSL y PAYEMS = día de publicación) y FOMC 2021–2025 desde el calendario oficial de la Fed,
  versionadas. *Hecho (2026-10-19):* `macro/config/calendar_historico.json` aparte (inmutable, solo laboratorio; el
  vigente queda chico y el vivo no cambia): 40 reuniones programadas 2021–2025 + 1 no programada (2025-08-22, notation
  vote, excluida de "próximo comunicado"), verificadas contra fomccalendars.htm. CPI/NFP: fecha = día en que FRED trae
  por primera vez una observación más nueva (08:30 ET), solo antes del primer evento del calendario BLS. PAYEMS sumada
  a `macro/fred.py` (vivo: bloque `macro-context-v2`). `dias_hasta_*`: 0 % NaN en 8.751 eventos.
  **Polymarket fuera del entrenamiento** (historia desde 2026-07-20: 0 % de cobertura); sigue en vivo como
  contexto; evaluarlo en una v2 cuando acumule meses.

- **Meta-modelo, etapa 2 completa y etapa 3 iniciada (2026-10-19).** Dataset final: 8.751 eventos × 80 features
  (calendario histórico corregido: `dias_hasta_*` 0 % NaN; Polymarket fuera). Etapa 3 en subtareas: 3a marco de
  validación (purged k-fold k = 5, embargo 5 días, registro de configuraciones contra el presupuesto de 24, métricas),
  sin entrenar con datos reales; 3b regresión logística (línea base); 3c LightGBM chico. Parámetros del pre-registro.
  **Etapa 3 aprobada por el usuario (2026-10-19)** antes de entrenar: 3b (logística) y 3c (LightGBM) se corren con los
  parámetros del pre-registro una vez validado el marco (3a), dentro del presupuesto de 24 configuraciones.

- **Meta-modelo 3a (marco de validación) hecho y 3b pre-registrada (2026-10-19, ANTES de entrenar).**
  `lab/meta/validation.py`: purged k-fold (k = 5, embargo 5 d, purga por solape en todo el dataset), base propia
  `~/heatsignal-lab/meta_trials.sqlite` (append-only; no suma al K del lab), presupuesto 24 por hash de configuración
  (un intento caído cuenta), calibración isotónica/Platt con k-fold purgado interno, métricas con pesos de unicidad,
  PnL imputado al día de salida (igual para filtrada y sin filtrar), bootstrap pareado, DSR con K del meta-modelo, PBO.
  Tests: ruido puro → AUC ≈ 0,5 y p > 0,05; señal sintética → AUC ≈ 0,83 y p < 0,05.
  **Configuraciones de 3b (4 de 24), fijas:** un solo modelo para las 4 primarias (primaria como categórica), pesos de
  unicidad. Preproceso dentro de cada fold: mediana + indicador de faltante (columnas con > 5 % NaN), estandarización,
  one-hot de categóricas (primaria, lado, tipo, daily_trend, daily_trend_dia_anterior, btc_daily_trend*, sesion,
  cuadrante_oi_funding). Polymarket (`pm_*`) excluido. Núcleo a priori (12): dist_ema200_1d_atr, adx_1d,
  atr_pct_1d_pctl_1a, ret_log_72h, rsi_1d, daily_trend_dia_anterior, funding_z_30d, delta_oi_24h, cuadrante_oi_funding,
  btc_daily_trend_dia_anterior, corr_btc_30d, fred_vixcls_pctl_1a.
  L1 = logística núcleo C = 1; L2 = núcleo C = 0,1; L3 = las 78 features C = 0,1; L4 = las 78 C = 1. Umbral fijo 0,5.

- **Meta-modelo 3b (logística, línea base) — resultado (2026-10-19): sin señal.** 4 de 24 configuraciones usadas,
  corridas una vez (`~/heatsignal-lab/meta/baseline_20261019T055955Z.json`, 8.749 eventos). AUC: L1 0,509, L2 0,534,
  L3 0,505, L4 0,504. Brier vs base 0,2414: solo L2 apenas mejor (0,2405). Filtrada vs sin filtrar: ninguna diferencia
  significativa (p del bootstrap pareado 0,13–0,82). PBO de las 4 = 0,80. Con tasas base de 37–47 % y umbral fijo 0,5,
  el filtro casi no deja operaciones (n 0–230 de miles): la comparación económica es poco informativa.
  Observación de diseño: el PnL de la evaluación económica es el de las barreras (triple barrier), no el de la salida
  real de cada regla; por eso los Sharpe "sin filtrar" (don −0,29, ema −0,23, max 0,62, tsm 0,41) no son los del lab.
  Cualquier cambio (umbral relativo a la tasa base, PnL real de la regla) es una configuración nueva pre-registrada
  que consume presupuesto; no se re-corren L1–L4.

- **Meta-modelo 3c pre-registrada (2026-10-19, aprobada por el usuario, ANTES de correr).** Misma etiqueta (triple
  barrier, pre-registro), mismo preproceso y mismo marco (purged k-fold k = 5, embargo 5 d, calibración, pesos de
  unicidad, primaria como categórica, `pm_*` fuera). Cambios de evaluación, fijos desde ahora:
  (1) **umbral = tasa base ponderada de la etiqueta en el train de cada fold** (tomar si p > b); tamaño por probabilidad
  = clip((p − b) / b, 0, 1). (2) **Evaluación económica con el PnL REAL de cada operación** (salida de la regla en
  `t_fin_regla`, 7 bps por lado + funding, misma convención que `simulate()`), columna nueva `pnl_regla` en el dataset;
  el PnL de barreras se informa al lado para comparar. Configuraciones (4 de 24, total 8):
  G1 = LightGBM núcleo, max_depth 3, num_leaves 7, n_estimators 200, learning_rate 0,03, min_child_samples 100,
  reg_lambda 5, subsample 0,8 (bagging_freq 1), colsample_bytree 0,8, semilla 20261019;
  G2 = núcleo, max_depth 2, num_leaves 3, n_estimators 300, learning_rate 0,03, min_child_samples 200, reg_lambda 10,
  subsample 0,8, colsample_bytree 0,8; G3 = las 78 con los hiperparámetros de G1; G4 = las 78 con los de G2.
  L1–L4 no se re-corren. Criterio para seguir a la etapa 4: alguna configuración con AUC OOS > 0,55, Brier mejor que la
  base y filtrada > sin filtrar con p ≤ 0,05 (bootstrap pareado) en el conjunto, y PBO < 0,5 sobre las 8.

- **Meta-modelo 3c (LightGBM) — resultado (2026-10-19): no cumple el criterio pre-registrado. Conclusión: con los
  datos y features actuales NO hay señal filtrable.** G1–G4 corridas una vez (`lgbm_20261019T142825Z.json`; 8 de 24
  configuraciones usadas). AUC 0,524–0,530 (criterio > 0,55: falla). Brier apenas mejor que la base (0,2405–0,2409 vs
  0,2414). Conjunto con PnL real de la regla, Sharpe sin filtrar 1,44 → filtrada G1 0,93 (p 0,93), G2 1,47 (p 0,40),
  G3 1,27 (p 0,73), G4 1,46 (p 0,49): ninguna mejora significativa (criterio p ≤ 0,05: falla). PBO 0,40 (solo G1–G4:
  las predicciones OOS de L1–L4 no se guardaron; no se re-entrenaron). DSR con K = 8 mezcla evaluaciones (L1–L4 con la
  vieja). `pnl_regla` congela el nocional de apertura (no incluye rebalanceos de tsmom ni re-ajustes por vol target de
  Donchian; documentado). **Decisión:** el meta-modelo NO pasa a la etapa 4. No se gastan las 16 configuraciones
  restantes "para probar". Se reintenta solo con datos nuevos (≥ 3 meses hacia adelante) o features nuevas
  pre-registradas con una hipótesis concreta. Las estrategias primarias siguen en paper.

- **Ingesta trabándose por memoria: causa y arreglo (2026-10-19).** Síntoma: OKX/Hyperliquid con decenas de
  reconexiones por hora ("socket zombie"), intervalo de health de 60 s → hasta 321 s, creciendo con las horas. Causa
  medida: el proceso llegaba a `MemoryMax=700M` y paginaba contra swap (pico 628 MB de swap): la ventana en memoria
  guardaba objetos pydantic de ~1,2–1,5 KB por evento (~640 MB en régimen). Arreglo: ventana compacta con `__slots__`
  (232 B por trade, 320 B por ticker, ~130 MB en régimen), monitor del event loop (`ingestion/loop_monitor.py`: retraso
  p50/p99/máx, RSS y swap cada minuto; tiempos de endpoints; en `/api/system` y en Sistema), executor propio de 2 hilos
  para cálculos pesados (el DNS de las reconexiones no espera detrás). Servicio: `MemoryHigh=900M`, `MemoryMax=1200M`.
  Descartado midiendo: executor saturado y costo de `/api/htf` (5 ms con cache incremental).

- **Lote 6 exploratorio (60 variantes) aprobado por el usuario (2026-10-20), lista y criterio fijados ANTES de
  programar** (`estrategias-lote6-exploratorio.md`): rupturas de N días (24), momentum de serie (12), calendario y
  sesiones (11), derivados (5), RSI(2) diario (4), régimen (4). K del programa 51 → 111. Exploratorio: nada se aprueba
  por su resultado; pasan a confirmación en paper TODAS las que cumplan veredicto propio + robustez (meseta ≥ 80 %,
  Sharpe ×3 costos > 0) + Romano–Wolf ≤ 0,10 con el K actualizado. Confirmación con datos desde 2026-10-20.
  Programado 2026-10-20: 60 specs (`LOTE6`), hashes congelados en `estrategias-lote6-hashes.json` y en el test, ANTES
  de correr. Notas: `l6-ruptura-n10-h1-long-v1` repite la regla de `max-ruptura-10d-v1` (queda: es de la lista y cuenta);
  al medir tiempos se vieron Sharpe de 5 variantes sobre BTC con `--perm 0` en un registro temporal borrado (no se usaron
  para nada). Corrida: 7 símbolos, `--perm 100`, de a 2 (~6–8 h).

- **Lote 6 exploratorio — resultado (2026-10-20): ninguna de las 60 cumple el criterio; ninguna pasa a confirmación.**
  60/60 corridas sin error; robustez y PBO de las 60; listón con K real 111. Veredicto propio + robustez: 20 de 60
  (todas long de tendencia: rupturas de 10/20/55 días mantenidas 1–5 d, tsmom 28 d con vol-target, régimen ADX/ATR).
  Romano–Wolf: mejor `l6-ruptura-n20-h3-long-v1` Sharpe 1,31, p ajustado 0,135 (criterio ≤ 0,10: falla); siguen
  n55-h5 0,143, n10-h3 0,159, n55-h3 0,170, tsmom-l28-vol-long 0,237. p del máximo del programa 0,135. Calendario,
  RSI(2), derivados y casi todas las "ambos" (long y short) sin ventaja. Lectura: patrón consistente en la familia
  "tendencia long" (muchas variantes vecinas dan Sharpe ~1–1,3, meseta 100 %, aguantan costos ×3), pero con 111
  pruebas sobre el mismo pasado no alcanza para descartar suerte; además el período 2021–2026 fue mayormente alcista
  (aunque le ganan a la canasta y al B&H). No se cambia el criterio después de ver el resultado.

- **Prueba hacia adelante con datos vivos (2026-10-20, pedido del usuario: "mandá bots a probar con datos vivos y no
  usados"). Pre-registrada ANTES de que corra un solo dato.** Período de prueba: desde la primera corrida del bot en
  2026-10-20. Nada de ese período se usó para elegir.
  **Hipótesis principal (1 sola, la que decide):** `l6-familia-ruptura-long-v1` = cartera igual-ponderada de las 12
  rupturas long del lote 6 (N ∈ {5, 10, 20, 55} × H ∈ {1, 3, 5}), TODAS, sin elegir por resultado; los 7 símbolos.
  Criterio: a los 3 meses, Sharpe > 0 y > el de la canasta igual-ponderada del mismo período; a los 6 meses, además
  p ≤ 0,05 de que le gana a la canasta (bootstrap pareado por bloques de 5 días). Si falla a los 3 meses, se registra.
  **Seguimiento secundario (exploratorio, NO decide nada):** las 20 variantes del lote 6 que pasaron su backtest y
  robustez, cada una en paper, con corrección Benjamini–Hochberg al evaluarlas; y la familia "ambos" no se suma.
  Todo en el bot de paper propio (simulado, sin plata).
  **Enmienda (2026-10-20, pedido del usuario, ANTES de que corra un solo dato): familia espejo SHORT.**
  `l6-ruptura-n{5,10,20,55}-h{1,3,5}-short-v1` (12 specs nuevas: cierre diario = mínimo de N días → short por H días;
  misma regla que la pata short de las "ambos") y `l6-familia-ruptura-short-v1` (1/12 cada una). Se backtestean en el
  lab como las demás (12 intentos más: K 111 → 123; la familia no se backtestea) para ver su resultado histórico, y la
  familia short corre en paper al lado de la long. Criterio simétrico (3 meses: Sharpe > 0 y > el de la canasta en
  corto, es decir > −Sharpe de la canasta; 6 meses: p ≤ 0,05). Lectura conjunta pre-registrada: si las dos familias
  ganan cuando el mercado va a su favor y pierden poco cuando va en contra → la regla tiene ventaja propia; si solo
  gana el lado del mercado del período → es beta, no ventaja.
  **Enmienda 2 (2026-10-20, pedido del usuario: "que compitan las dos posturas y gane la de mayor peso"), ANTES de
  correr datos:** `l6-familia-ruptura-neta-v1` = en cada símbolo, (n.º de rupturas long activas − n.º de rupturas short
  activas) / 12, posición entre −1 y +1; sin parámetros nuevos (= familia long + familia short). Corre en paper como
  tercera principal, con el mismo criterio a 3 y 6 meses contra la canasta (Sharpe > 0 y > canasta). El panel muestra
  por símbolo el "balance de fuerzas" (votos long vs short) en cada corrida. No se backtestea (sería volver a mirar el
  mismo pasado): se juzga solo hacia adelante.
  **Implementado y desplegado (2026-10-20):** hashes familia long `f7b68dcd66c5c7e4`, short `d39d02babf1466e2`, neta
  `81005404f0426130`; 12 short en `LOTE6_SHORT` (las 60 del lote 6 sin cambios). Paperbot con 30 estrategias (3
  principales, 20 seguimiento, 7 paper previo), velas diarias en SQL desde 1m o REST `/fapi/v1/klines?interval=1d`
  cacheadas (append-only); historia mínima por spec (máx. 590 días para `reg-atrp50`). Medido en la VM: 151 MB de pico,
  19 s por corrida. Panel `#/paperbot`: tres grupos, criterio con días hasta los hitos, balance de fuerzas por símbolo.

- **Backtest de las 12 rupturas short (2026-10-20): todas negativas** (Sharpe −0,04 a −0,78, DD 43–157 %), con 2022
  incluido. K real 123; p del máximo del programa 0,145 (ruptura n20-h3 long). Coincide con la consulta al trader: las
  caídas en cripto se concentran en pocos días y una ruptura de 5–55 días hacia abajo se activa tarde; los rebotes
  castigan a los shorts. La familia short sigue en paper como espejo pre-registrado (no se saca por este resultado).
- **Consulta a un trader profesional (2026-10-20)** → `research/consulta-trader.md`. Verificado: el funding real ya
  está descontado (ruptura n20-h3 long: precio +2,03, funding −0,15, comisiones −0,09, slippage −0,04), Binance ya
  figura como `PARTIAL`, y las rupturas long le ganan a la canasta (0,63) y al B&H medio (0,02). Se incorpora ahora:
  criterios de parada de la prueba hacia adelante por bootstrap del drawdown (P95 → alerta y mitad de tamaño, P99 →
  parar) y estrés de costos con slippage de 7 bps en TRX y XLM (diagnóstico). Para la etapa de plata real: checklist de
  errores de bots, stops por mark price en el exchange, entradas IOC con tope 5–15 min después de las 00:00, tamaño por
  volatilidad de la canasta con correlaciones e informe diario. Abiertas para fin de noviembre: plata mínima real
  después de 1–2 meses de paper, y listón previo declarado para la familia tendencia.

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

- **Cómo estimar el listón del programa (SR0) (abierta, 2026-10-18).** Hoy SR0 usa la varianza de los Sharpe OOS
  de TODOS los intentos (lab + benchmark): con estrategias rotas (Sharpe −3 a −5) la varianza explota y SR0 ≈ 3,9,
  imposible de alcanzar. Opciones: (a) dejarlo así (muy conservador); (b) K efectivo por correlación de retornos
  (`stats.effective_n_trials`) y varianza solo de las familias comparables; (c) listón por familia. Decidir ANTES
  de mirar qué opción hace pasar a alguna estrategia.

## Plantilla para nuevas decisiones
```
- **[Título] → [elección].** Sobre [alternativa]. Razón: [...]. Fecha: [...].
```

- **Almacenamiento por niveles: caliente en la VM, frío en Drive (2026-10-18).** Pedido del usuario: "cuando la VM
  esté al 80 % de su almacenamiento, que migre al Drive y lea de ahí". `history/archive.py` (`heatsignal-archive.timer`,
  cada hora): si el uso de `data/` (como `df`) es >= 80 %, archiva lo más viejo hasta un uso proyectado de 70 %, en
  orden: exports locales ya verificados en Drive → velas 1m > 60 días (re-descargables, igual se respaldan) →
  bars_1m/liquidations/news > 60 días → checkpoints del grafo > 30 días (reconstruibles solo en parte: se respaldan
  y el panel los relee de Drive). Cada unidad: CSV.gz con el formato del export (gzip determinista) → `rclone copyto
  --immutable` a `gdrive:archive/...` → verificación tamaño + md5 → DELETE + alta en `archive_catalog` en UNA
  transacción (el catálogo vive en la misma base histórica: un rango está en la VM o en Drive, nunca en los dos ni
  en ninguno). Si algo falla, no se borra nada. Catálogo y corridas solo-agregar (triggers). **Nunca**
  `heatsignal_eval.sqlite` ni `freqtrade_dryrun.sqlite` (el archivador se niega a arrancar si una ruta apunta ahí).
  Lectura: `/api/history/bars`, `/liquidations` (con `since`) y `/api/cycles/{run_id}` bajan lo archivado a un cache
  LRU de 300 MB (`data/cold_cache/`) en otro hilo; responden `source: local|drive|mixed`; Drive caído → 503,
  `partial` solo si se pide. Espacio: la base hoy no tiene auto_vacuum; el primer `run` (o `archive vacuum`) hace
  un VACUUM completo si hay ~2× la base libre y la deja en `auto_vacuum=INCREMENTAL`; si no alcanza, lo borrado se
  reusa dentro de la base y el uso proyectado lo descuenta. Respaldo completo semanal: `heatsignal-backup.timer`
  (domingo 03:00 UTC, `deploy/backup-vm.sh`, ahora no arranca sin espacio).
