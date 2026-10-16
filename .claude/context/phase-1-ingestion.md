# Contexto: Fase 1 — Capa de Ingesta (WebSockets nativos de exchanges)

> Cargar al construir la ingesta. **Fuente primaria: WS nativos de Binance + Bybit + OKX (gratis).**
> CoinGlass queda como fuente opcional BYOK a futuro, no como dependencia del core.
> Esto son **servicios**, no agentes LLM. Corren 24/7 desde el MVP.

## Por qué WS nativos y no CoinGlass
- CoinGlass V4 no tiene tier gratuito; el uso comercial (dashboard público) exige el tier Standard (~299 USD/mes).
- Los datos públicos de mercado de los exchanges no arrastran la cláusula de redistribución comercial de
  un agregador pago → elimina casi por completo el Bloqueante A.
- Costo: lo que perdemos es la agregación lista y el heatmap ya calculado. Los construimos nosotros
  (ver "Modelo de heatmap propio" abajo) — lo cual además es más auditable (encaja con anti-p-hacking).

## Endpoints verificados (confirmar siempre contra doc oficial antes de codear)

### Binance USDⓈ-M Futures
- Liquidaciones: stream `<symbol>@forceOrder` (evento `forceOrder`) — ⚠️ **NO empuja todas**: solo la última
  liquidación por símbolo cada 1000ms (snapshot). Cobertura PARTIAL, como OKX.
  También existe `!forceOrder@arr` (all-market).
- Trades: `<symbol>@aggTrade` / `<symbol>@trade`.
- Ticker: `<symbol>@ticker` / mark price streams.
- Sin API key para streams públicos.
- ⚠️ **Rutas (oct 2026):** `wss://fstream.binance.com/market/...` para aggTrade/markPrice/forceOrder y
  `/public/...` para depth/bookTicker. El path legacy `/ws` acepta el handshake pero **no manda datos**.

### Bybit V5 (linear/USDT perp)
- Liquidaciones: usar `allLiquidation.<symbol>` (**empuja todas**, cada 500ms).
  ⚠️ NO usar `liquidation.<symbol>` — está **deprecado** y empuja como máximo 1 por segundo por símbolo
  (no captura todas → hueco de datos silencioso).
- Trades: `publicTrade.<symbol>`.
- Ticker: `tickers.<symbol>`.
- Sin API key para canales públicos.

### OKX V5
- Endpoint público: `wss://ws.okx.com:8443/ws/v5/public` (sin auth para canales públicos).
- Liquidaciones: canal `liquidation-orders` — ⚠️ **como máximo 1 update por segundo por contrato**
  (no captura todas, igual que el Bybit viejo). Documentar como cobertura parcial.
- Trades: canal `trades`.
- Ticker: canal `tickers`.
- Ping/pong: si no llega mensaje en ~20s, mandar `ping`; si faltan 2 pongs seguidos, reconectar.

## ⚠️ Limitación de datos a documentar (no esconder)
La cobertura de liquidaciones **no es uniforme**: solo `allLiquidation` de Bybit da el flujo completo; Binance (`forceOrder`),
OKX y el Bybit deprecado dan a lo sumo 1/seg por símbolo. Esto sesga cualquier comparación cross-exchange
de volumen de liquidaciones. Marcar el origen y la granularidad en cada registro; el Analista de
Liquidaciones y el Evaluador deben saber que Binance y OKX subrepresentan.

## Arquitectura de workers
- **1 worker WS por exchange** (no por stream): cada conexión multiplexa liquidaciones + trades + ticker
  de ese exchange. Binance/Bybit/OKX permiten múltiples suscripciones por conexión.
- Cada worker: reconexión con **backoff exponencial + jitter** (1s → máx 60s), replay de suscripciones al
  reconectar, heartbeat/ping-pong donde aplique, checkpointing.
- **Normalización**: cada exchange tiene su formato (campos, nombres, side "Sell"/"SELL"). Un adaptador por
  exchange traduce al esquema interno común antes de escribir. Timestamps en **UTC**.
- Buffers/agregación en ventanas → Redis. Persistencia → TimescaleDB.

## Modelo de heatmap propio
CoinGlass daba el heatmap ya calculado; ahora lo construimos a partir de liquidaciones crudas + (opcional)
profundidad de order book. Debe ser: versionado (`heatmap_model_id`), transparente y auditable. No es un
bloqueante de Fase 1 — se puede empezar con clusters simples por nivel de precio y refinar después.

## Reglas
- Verificar cada canal contra la doc oficial del exchange antes de implementar (los canales migran).
- Fallos tipados y explícitos — sin huecos silenciosos. Stream caído = marcado; nada aguas abajo corre
  como si el dato estuviera completo.
- Filtrar/marcar DEX chicos como "baja confianza" (esto ahora es menos relevante porque arrancamos con los
  3 grandes, pero mantener el campo de confianza por si se suman fuentes).

## Salida de Fase 1
3 workers (Binance, Bybit, OKX) corriendo 24/7, normalizando a esquema común, escribiendo a TimescaleDB +
buffers Redis, con métrica de uptime por worker y campo de granularidad/origen por registro.
