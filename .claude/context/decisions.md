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
