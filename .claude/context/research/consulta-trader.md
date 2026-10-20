# Consulta a un trader profesional (2026-10-20)

> Respuestas de un trader con experiencia a 25 preguntas técnicas sobre el sistema, resumidas, con nuestra verificación al
> lado. Son opiniones expertas, no evidencia nuestra: lo que se adopte entra como versión nueva pre-registrada.
> Decisiones derivadas: `decisions.md` (entrada "Consulta a un trader profesional").

## Las tres advertencias principales (verificadas)
| Advertencia | Verificación en nuestro sistema | Estado |
|---|---|---|
| El funding de los longs puede inflar el Sharpe (~11 % anual de base) | El simulador cobra el funding real liquidado. Ruptura n20-h3 long: precio +2,03, funding −0,15, comisiones −0,09, slippage −0,04 → neto +1,75 | ✅ ya descontado |
| El stream de liquidaciones de Binance es parcial (≤ 1 por segundo por símbolo desde 2021-04-27) | `Coverage.PARTIAL` para Binance y OKX; solo Bybit es `FULL`; el grafo lo trata como estructural | ✅ ya contemplado |
| Comparar contra buy & hold: si es parecido, la ruptura aporta menos drawdown, no alfa | Mismo período: canasta 0,63, B&H medio por moneda 0,02; rupturas long 1,25–1,31 con DD 9–13 % | ✅ le gana a ambos |

## Ejecución y costos
- **Slippage** con 1–10 k USDT: BTC/ETH < 1 bp; SOL/XRP/BNB 1–2 bps; **TRX/XLM 5–10 bps** en rupturas. Comisiones 2 bps
  maker / 5 bps taker por lado (−10 % pagando con BNB). Prueba: duplicar costos; mejor: medir con órdenes reales chicas.
  → Estrés TRX/XLM a 7 bps (diagnóstico, 2026-10-20). Costos ×3 ya se miden en la robustez.
- **Market vs limit:** en rupturas, el limit tiene selección adversa (no se llenan las que se escapan). Usar **limit
  IOC agresiva con tope de slippage** (0,1–0,2 %). → Etapa de ejecución.
- **Horario:** más liquidez 13–17 UTC; el riesgo del finde son las mechas en libros finos (TRX/XLM).
- **00:00 UTC:** congestión (cierre diario + funding + bots). Decidir al cierre y **ejecutar 5–15 min después** o en
  TWAP de 15–30 min. → Versión nueva pre-registrada en la etapa de ejecución.

## Tendencia y rupturas
- **Long sí, short no:** el período incluye 2022 (bajista completo). Las caídas en cripto son pocos días violentos y una
  ruptura de 20–55 días hacia abajo llega tarde; los rebotes castigan al short. Una sola observación de mercado bajista
  completo: no se sabe si es permanente. Habitual: long o liquidez. → **Confirmado por nuestro backtest de las 12 short:
  todas negativas (Sharpe −0,04 a −0,78).**
- **Rupturas falsas:** son el costo del negocio (acierto 30–40 %); los filtros suelen recortar operaciones sin mejorar
  el Sharpe y suman intentos. Si se prueba alguno, máximo dos; el más defendible: margen de 0,25–0,5 ATR.
- **Correlación:** con ρ ≈ 0,7, 3 longs en altcoins ≈ 1,25 apuestas; en crisis ≈ 1. Dividir el presupuesto de riesgo:
  tamaño por volatilidad objetivo de la canasta con la matriz de correlaciones móvil. → Etapa de ejecución.
- **Cuándo parar:** no contar meses malos (con Sharpe 1, P(año negativo) ≈ 16 %; 12–18 meses bajo el máximo es normal).
  Bootstrap del drawdown máximo y de su duración: P95 → revisar, P99 → parar. También parar si los costos reales
  superan mucho lo modelado o si las señales en vivo no coinciden con el backtest. → **Implementado para la prueba hacia
  adelante (2026-10-20).**

## Lateral y short
- Días laterales: fuera o tamaño chico, sin inventar operaciones. Verificar que el régimen se calcula con información
  del día (nuestro `daily_trend` usa solo velas cerradas: tests de look-ahead).
- Reversión neta de costos: muy difícil intradía; mejor a varios días tras extremos o **después de cascadas de
  liquidaciones** (donde nuestros datos dan ventaja).
- Short solo en régimen bajista confirmado, con menos tamaño y stops más cortos; funding muy negativo = riesgo de squeeze.

## Derivados
- **Funding:** detecta posicionamiento saturado más que giros; usar como filtro de tamaño (no abrir longs nuevos o
  achicar) con un percentil por moneda (p. ej. > P95 de 90 días); > 0,05 % cada 8 h sostenido indica saturación.
- **OI × precio:** contexto; ruidoso en diario; gana combinado con funding (precio y OI suben con funding alto = suba
  frágil). Pocas rupturas para probarlo: una sola versión pre-registrada.
- **Cascadas de liquidaciones:** durante, advertencia; al agotarse (liquidaciones normalizándose, funding reseteado,
  precio recuperando un nivel, en horas), oportunidad de reversión. Medir el tamaño con Bybit (completo); Binance solo
  presencia y momento.
- **Otros datos** (heurísticas): basis trimestral anualizado, suba empujada por spot vs perpetuo, prima de Coinbase,
  skew 25Δ de Deribit (API pública), flujos on-chain a exchanges. → Backlog.

## Riesgo
- 0,25–1 % por operación o tamaño por volatilidad objetivo.
- **Stop de catástrofe** (3 ATR, piso 8 %, techo 20 %): razonable si se backtestea, se dispara por **mark price**, vive
  en el exchange; ojo, en altcoins con ATR diario de 6–8 % el techo de 20 % actúa seguido como stop fijo.
- Apalancamiento efectivo 1–1,5×; margen aislado con 2–3× en el exchange para que la liquidación quede lejos del stop.
- Corte diario como **detector de errores**: si se pierde más que la suma del riesgo de los stops abiertos → parar y
  alertar. Drawdown: mitad de tamaño en un nivel y parar en otro, ambos del bootstrap.
- Escalar por escalones según el **proceso** (señales = backtest, costos ≤ modelados, sin incidentes): 25 % → 50 % →
  100 %; nunca tras una racha ganadora.

## Método
- Listón: Bonferroni con 123 pruebas pide t ≈ 3,5 y penaliza de más con variantes correlacionadas; mejor DSR con K
  efectivo (ya implementado). Con 5 años, t ≈ Sharpe × √años ≈ 2,2–2,9. La tendencia tiene décadas de evidencia externa:
  es legítimo exigirle menos **si se declara antes**. → Decisión abierta (fin de noviembre).
- **Paper valida la operativa, no la ventaja:** 1–2 meses de paper y después capital real mínimo como validación, con
  criterios de proceso. → Decisión abierta (fin de noviembre).
- Esperar conservar ≤ 50 % del Sharpe del backtest en vivo (planificar con 0,5–0,7).
- **Candidata para plata chica:** rupturas long de 20 y 55 días (combinadas) solo en BTC y ETH, sin apalancamiento
  efectivo, tamaño por volatilidad, stop de catástrofe en el exchange.

## Bot: errores que cuestan plata (checklist obligatorio para la etapa de ejecución)
- [ ] Órdenes duplicadas tras timeout: `newClientOrderId` y consultar por ese id antes de reintentar.
- [ ] Stop no colocado (rechazo por precisión, `reduceOnly` o modo de posición): verificar después de cada entrada.
- [ ] Stop sin `reduceOnly`: si la posición ya cerró, abre una en contra.
- [ ] WebSocket conectado pero mudo: chequeo de datos viejos con reconexión y alerta (ya existe en la ingesta).
- [ ] `listenKey` vencido: renovarlo o el bot deja de enterarse de sus llenados.
- [ ] Vela abierta a las 00:00: exigir vela cerrada (`x=true`).
- [ ] Al reiniciar, la fuente de verdad es el exchange: reconciliar posiciones y órdenes antes de operar.

## Informe diario de 5 minutos (etapa de ejecución)
- [ ] Posiciones del exchange = estado del bot.
- [ ] Cada posición con su stop en el exchange (lado y tamaño correctos).
- [ ] Señales de hoy = backtest re-corrido sobre los mismos datos.
- [ ] Slippage y funding reales vs supuestos.
- [ ] Distancia a liquidación.
- [ ] Errores y reconexiones del día.

## Fuentes citadas por el trader
Binance – Futures Fee Structure & Fee Calculations; Binance Developers – Liquidation Order Streams; Coin Metrics –
Futures Liquidations.
