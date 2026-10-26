# Investigación: infinity-grid (grid trading)

> Revisado el 2026-10-26 a pedido del usuario ("revisá este proyecto y sacá lo que nos sirva"). Repo:
> https://github.com/btschwertfeger/infinity-grid (commit `ac0b9a9`, 2026-10-01; ~5.300 líneas de Python; autor
> Benjamin Thomas Schwertfeger). Solo lectura: no se corrió nada del repo.

## Qué es

Bot de grilla para **spot de Kraken** (Binance y Coinbase figuran como "planned"). Un bot por par y por contenedor,
estado en PostgreSQL/SQLite, órdenes límite reales en el exchange, avisos por Telegram. Cuatro variantes:

| Variante | Qué hace | Salida |
|---|---|---|
| GridHODL | `n` compras límite escalonadas cada `interval` % debajo del precio; cada compra llenada pone una venta a +`interval` por el mismo monto en quote → acumula un poco de base por ciclo | venta a +interval (parcial: se queda la diferencia en base) |
| GridSell | igual, pero cada venta es por **toda** la cantidad comprada → realiza la ganancia completa | venta a +interval |
| SWING | GridHODL + vende base acumulada por encima de la compra más alta | ventas escalonadas arriba |
| cDCA | solo compras cada `interval` % de caída; nunca vende | ninguna |

Mecánica clave (`strategies/grid_base.py`):
- Precio de la próxima compra: `último × 1/(1 + interval)` (si queda arriba del precio, desde el precio actual).
- Venta: `compra × (1 + interval)` (si el precio ya está arriba, desde el precio actual).
- **Corrimiento hacia arriba:** si el precio supera la compra abierta más alta × (1 + interval)² × 1,001, cancela todas
  las compras y rearma la grilla debajo del precio nuevo ("infinita": nunca se queda afuera por arriba).
- Compras demasiado juntas (< interval/2) → cancela la de arriba; más de `n` compras abiertas → cancela la más baja.
- Tope `max_investment` (suma de lo invertido a precio pagado); compras post-only (maker).
- **No tiene stop loss.** Si el precio cae fuerte, se llena de compras y espera. El riesgo de cola es justamente ese.
- **No tiene backtest** ("planned", `doc/07_backtesting.rst`). El README dice que "es efectivo" en mercados laterales y
  volátiles, sin n, sin IC y sin benchmark: según nuestros principios es una **idea para probar, no evidencia**.

Licencia propia ("Custom License Agreement 2.0"): uso personal y no comercial permitido (incluso modificar), atribución
obligatoria si se usa en público, uso comercial prohibido sin contrato. **Conclusión: tomar ideas y reimplementar en lo
nuestro; no copiar el código tal cual. Si HeatSignal algún día se publica, citar el proyecto.**

## Qué nos sirve

### 1. Una familia de estrategias nueva: grilla (para el laboratorio, previa aprobación)

Es exactamente el tipo de regla que cubre el hueco de nuestros resultados: el laboratorio solo encontró algo en
tendencia LONG; en los días laterales tenemos 3 estrategias del grafo pero ninguna en el bot. Una grilla es una
estrategia de **rango** y además encaja con dos cosas que ya tenemos:
- `htf-trend-v1` clasifica cada día en alcista / bajista / **lateral** → activar la grilla solo en lateral.
- Velas 1m en el laboratorio → se puede simular el llenado de órdenes límite (que el mínimo de la vela cruce el precio).

**Conflicto con la regla de SL/TP obligatorio (2026-10-26):** la grilla original no tiene stop. Cada lote ya tiene su
"take profit" (+interval), pero falta el stop. Hay que decidir uno ANTES de ver resultados (ver propuesta abajo). Con un
stop por lote de 8 % y un objetivo de 2–4 %, el R:R es 1:4 a 1:2 en contra: hace falta acertar 67–80 % para no perder.
La grilla vive de eso (muchas ganancias chicas, pocas pérdidas grandes): nuestras métricas de calidad de trades
(`lab/stats.py`: win-rate mínimo, rachas, payoff) son las que tienen que decidir si la cola se la come.

### 2. Trailing stop profit (`doc/tdoc/02_trailing_stop_profit.md`, borrador, sin implementar)

Venta inicial a interval + 2·TSP; al tocar interval + TSP se activa un piso que va subiendo de a TSP; si el precio
vuelve al piso, vende ahí (nunca por debajo de interval). Es una forma de "dejar correr" el objetivo sin perder el
mínimo. Aplica a nuestras -v2 con SL/TP como posible variante futura (-v3 con objetivo móvil); **no** cambia las -v2.

### 3. Patrones de ingeniería para la etapa de plata real (los más valiosos a mediano plazo)

| Patrón | En infinity-grid | Para nosotros |
|---|---|---|
| Intención escrita antes de mandar la orden | tabla `unsold_buy_order_txids`: se anota la compra llenada ANTES de poner su venta; al arrancar, `__add_missed_sell_orders` crea las ventas que faltaron | toda entrada llenada sin su STOP_MARKET/TAKE_PROFIT confirmados en el exchange → recuperar al arrancar (o cerrar). Es el caso que rompe "nunca sin stop" |
| Órdenes pendientes | `pending_txids`: orden mandada pero no confirmada; no se evalúa la grilla hasta resolverlas | no decidir nada nuevo con órdenes en vuelo |
| Corte por precio viejo | sale si no hubo precio en 10 min | kill switch del ejecutor real (el paperbot ya falla por hueco de datos) |
| Identificador por instancia | `userref` por bot; el bot solo toca sus órdenes | `newClientOrderId` con prefijo por estrategia en Binance (verificar formato en la doc) |
| Tope de inversión | `max_investment` + espera si falta saldo (evita rate limit) | tope por estrategia en la etapa de plata real |
| Apagado seguro | SIGTERM termina la orden en curso y guarda | idem en el ejecutor real |
| Llenados parciales | guarda lo llenado de una compra cancelada y lo vende cuando junta un lote | modelo de llenado parcial cuando haya órdenes límite reales |
| Resumen horario | Telegram con grilla, inversión y órdenes | ya lo cubre el panel; Telegram como alerta opcional futura |

### Qué NO sirve
- El adaptador de Kraken (operamos Binance USDS-M; Binance en infinity-grid es "planned").
- cDCA (compra sin salida: contradice la regla de SL/TP) y SWING (vende base acumulada: es spot, no aplica a futuros).
- Lo de impuestos FIFO, PostgreSQL y Docker (no cambia nada nuestro).

## Propuesta de pre-registro (PROPUESTA: no corre nada hasta que el usuario la apruebe)

Lote 7, familia `grilla`, estrategias del laboratorio (cada una con su `strategy_id`; las grillas cuentan como intentos):

1. `grilla-long-v1`: futuros a 1x, los 7 símbolos, capital del símbolo en `n = 5` lotes iguales. Compras límite cada
   `interval` debajo (mecánica de GridSell: venta del lote entero a compra × (1 + interval)); corrimiento hacia arriba
   igual que el original. Grilla del walk-forward: `interval ∈ {2 %, 4 %}` (2 intentos).
2. `grilla-long-lateral-v1`: igual, pero solo pone compras nuevas los días que `htf-trend-v1` da `lateral`; al salir de
   lateral cancela las compras pendientes y los lotes abiertos siguen con su venta y su stop.
3. Stop (A DECIDIR por el usuario antes de correr, una sola opción):
   a. por lote, como las -v2: 3 ATR(14) 1h con piso 8 % y techo 20 % del precio del lote;
   b. de la grilla entera: si el precio cae X % debajo de la compra llenada más baja, se cierran todos los lotes.
4. Simulación: compra llena si el mínimo de una vela 1m queda ESTRICTAMENTE debajo del precio límite (conservador: tocar
   no alcanza), al precio límite; venta igual con el máximo; stop al open siguiente (convención actual). Comisión maker
   para las límite y taker para el stop: **verificar las tarifas vigentes de Binance USDS-M contra la doc oficial antes
   de fijarlas**. Funding sobre los lotes abiertos.
5. Benchmarks de siempre: canasta igual-ponderada, azar y el listón del programa (Romano–Wolf / DSR con el K nuevo).
6. Si alguna pasa: -v en el bot de paper como las demás (rol a definir), nunca directo a plata real.
