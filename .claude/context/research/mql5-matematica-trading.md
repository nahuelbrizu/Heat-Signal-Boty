# Investigación: matemática del trading (MQL5 + hilos de X + Rankia)

> Revisado el 2026-10-19. Fuentes: los 6 links que pasó el usuario más una búsqueda en mql5.com de los
> temas que HeatSignal todavía **no** tiene. Nada de esto está implementado; son candidatos para el backlog.
> Regla del repo: lo que se adopte como estrategia va pre-registrado con `strategy_id` versionado.

## Qué ya tenemos (no rehacer)

`lab/stats.py`: Sharpe con IC por bootstrap estacionario, PSR, DSR, MinTRL, n efectivo de intentos, max
drawdown, Wilson, profit factor, curva de costos. `lab/diagnostics.py`: PBO por CSCV, vecinos de parámetros,
candado del holdout. `lab/baselines.py`: entradas al azar como nula + p-valor empírico. `evaluation/excursion.py`:
MAE/MFE. `evaluation/calibration.py`: Brier. O sea: el artículo 1492 de MQL5 y casi toda la parte de validación
de MQL5 (21603, 24150, 24238) ya están cubiertos, y en varios puntos mejor.

## Valoración de los 6 links

| Link | Qué es | ¿Nos sirve? |
|---|---|---|
| GoshawkTrades (X) | Lista de 4 cursos gratis: MIT Financial Mathematics, Yale Financial Theory, Oxford (Liu Peng), Kent Daniel sobre momentum | Como lectura. Lo de Kent Daniel (momentum time-series vs cross-sectional) encaja con `donchian-ensemble-v1` y `funding-crowding-xs-v1` |
| RohOnChain (X) | El curso del MIT aplicado a Polymarket | Sí, en conceptos: PCA/concentración de factores, GARCH, 3 métodos de VaR, ruina del jugador. Ver abajo |
| MQL5 1492 | Z-score de rachas, AHPR/GHPR, Sharpe, R de la equity, MAE/MFE, normalizar por lote | Casi todo ya está. Faltan **Z de rachas** y **GHPR** |
| Rankia | Murrey Math, octavas, "trading matemático" | No. Sin backtests y con publicidad; los niveles Murrey son numerología de rangos. Si alguien lo quiere probar, va como hipótesis con prior bajo |
| MQL5 19918 | Panel de controles para un EA en MT5 | No: es UI de MetaTrader |
| MQL5 17962 | Pin bar / envolvente + divergencia RSI | Solo como hipótesis de prior bajo. El artículo no da win-rate, n ni costos; el "alta tasa de acierto" no tiene datos |

## Lo nuevo que vale la pena (por prioridad)

### 1. Baseline analítico del win-rate: ruina del jugador (barato, alto valor)

Con paseo aleatorio sin deriva, la probabilidad de tocar TP antes que SL es `P = SL / (TP + SL)` (distancias
en la misma unidad, p. ej. ATR). TP = 2 ATR y SL = 1 ATR dan 33,3 % por azar. Así se compara cualquier
win-rate de triple barrera o de hipótesis con su nula exacta, sin simular. Complementa a `random_entries_null`
y cumple el principio 3 (baseline obligatorio). Ojo: con deriva o colas gordas la fórmula es aproximada. Se
usa como piso de referencia, no como prueba de significancia.

### 2. Monte Carlo de la secuencia de trades: drawdown esperable y probabilidad de ruina

MQL5 [22291](https://www.mql5.com/en/articles/22291), [23125](https://www.mql5.com/en/articles/23125), [23980](https://www.mql5.com/en/articles/23980):
- **Barajado** (permutar el orden, mismo PnL): da la distribución del max DD solo por el orden. Si el DD real
  queda cerca del P5, el backtest tuvo suerte con el orden.
- **Bootstrap con reposición**: mediana del max DD, **P95 del max DD** y **P(ruina)** = % de caminos con DD ≥ umbral.
- Los autores lo advierten: con IID se subestiman las rachas dentro de un régimen. Para nosotros va en
  **bloques** (ya tenemos `stationary_bootstrap_indices`). Con menos de 50 trades los percentiles no sirven;
  conviene tener 200 o más.
- Uso: poner "DD P95" y "P(ruina)" al lado del DD histórico en la ficha del laboratorio, y como límite antes de
  pasar de paper a plata real.

### 3. Tamaño de posición: Kelly empírico fraccional con calibración por ruina

MQL5 [23699](https://www.mql5.com/en/articles/23699), [24147](https://www.mql5.com/en/articles/24147), [4162](https://www.mql5.com/en/articles/4162) (Vince, optimal f):
- `f* = argmax_f mean(log(1 + f·R_i))` sobre los R-múltiplos reales (búsqueda por sección áurea en [0, f_max]).
  Si algún `1 + f·R_i ≤ 0`, se descarta esa f.
- IC de f* con bootstrap (500 remuestreos, P5–P95). Si el P5 está cerca de 0, el edge no alcanza para dimensionar.
- Calibración: bajar f desde el máximo hasta que P(ruina) ≤ X y DD P95 ≤ Y en el Monte Carlo del punto 2.
- 24147: el crecimiento máximo está en Kelly completo, pero el DD y la ruina suben rápido. Lo estándar es ½ Kelly o menos.
- Esto le da contenido concreto a "tamaño desde la probabilidad" de `plan-entrenamiento.md` (etapa del meta-modelo).
- Riesgo: estimar y calibrar sobre la misma muestra infla la confianza. Se calcula en dev y se valida en holdout/paper.

### 4. Z-score de rachas (runs test) y GHPR (barato)

- `Z = (N·(R − 0,5) − P) / sqrt(P·(P − N)/(N − 1))`, con `P = 2·W·L`, R = cantidad de rachas, N = trades.
  Si |Z| ≥ 2, hay dependencia entre trades consecutivos (Z < 0: las rachas se agrupan). Dos usos:
  (a) avisar si el bootstrap IID del punto 2 subestima rachas, y (b) dar el dato "racha máxima" del backlog de UI
  con su significancia. Los trades con PnL 0 cuentan como pérdida (convención de Vince).
- **GHPR** = `(equity_final / equity_inicial)^(1/N)`: es el crecimiento real por trade con reinversión. Si
  GHPR < 1 y AHPR > 1, la varianza se come el edge.

### 5. VPIN de verdad (tenemos lo que a MQL5 le falta)

MQL5 [22939](https://www.mql5.com/en/articles/22939), [22733](https://www.mql5.com/en/articles/22733): el VPIN canónico (Easley, López de Prado, O'Hara 2012) usa
**buckets de volumen fijo** y clasificación de agresor. MQL5 solo tiene velas, así que usa una aproximación
`frac = (C − L)/(H − L)` que el propio autor llama débil. **Nosotros recibimos trades con lado agresor por WS**
(`features/order_flow.py`), así que podemos calcular el VPIN exacto:
- Bucket = V/50 del volumen diario medio. Por bucket: `|V_buy − V_sell|`. `VPIN = media móvil de 50 buckets / V_bucket`.
- El valor predictivo está discutido (Andersen y Bondarenko 2014). Va como **feature de régimen o toxicidad**
  (meta-modelo, filtro de "no operar"), nunca como señal sola. Puede ser un buen complemento de los clusters de
  liquidación: VPIN alto + cluster cerca = riesgo de cascada.
- También tiene "OrderFlowMomentum" = OFI(10) − OFI(30), simple y barato.

### 6. Régimen: Hurst y HSMM (como filtro, no como señal)

- **Hurst** [22553](https://www.mql5.com/en/articles/22553): compuesto de R/S + varianza agregada + momentos absolutos. H > 0,6 es tendencia,
  H < 0,4 es reversión y lo del medio es zona neutra. Su propio test **no encontró poder predictivo** (mejor
  p = 0,094): describe el régimen, no lo pronostica. Hay que resetear por sesión, porque mezclar sesiones lleva H a valores falsos.
  Para cripto 24/7 importa menos, pero conviene medirlo por ventana fija.
- **HSMM** [24460](https://www.mql5.com/en/articles/24460): 4 estados (TrendUp, TrendDown, Range, HighVolChop) con duración explícita
  (binomial negativa). Features sin escala: `σ_realizada/(ATR/close) − 1`, efficiency ratio de Kaufman y skew,
  en 20 velas. Histéresis: confianza ≥ 0,55 durante 3 velas. **Resultado: PF 0,99 y Sharpe −0,41** en oro M5, sin ablación.
  Sirve como idea de features para el selector de régimen, no como estrategia.
- `plan-entrenamiento.md` ya deja el selector por ADX/volatilidad como línea base. El efficiency ratio y la
  vol realizada/ATR son features baratas para agregar ahí.

### 7. Riesgo de cartera: concentración en factores y VaR

Del hilo de RohOnChain (MIT) y MQL5 [23374](https://www.mql5.com/en/articles/23374), [23322](https://www.mql5.com/en/articles/23322), [16273](https://www.mql5.com/en/articles/16273):
- **PCA sobre los retornos de las 7 criptos**: casi seguro el 1.er componente (beta a BTC) explica la mayor
  parte. Tener 7 posiciones long no son 7 apuestas, es una sola apuesta a BTC. Para mostrar en el panel:
  "% de varianza del 1.er componente" y "n efectivo de apuestas" = `(Σλ)² / Σλ²`.
- **Covarianza EWMA** (23322) en vez de una ventana fija, porque la correlación sube justo en los crashes.
- **VaR/CVaR** (ya en el backlog de UI): calcular histórico, paramétrico y Monte Carlo, y **marcar cuando
  divergen**. Según el hilo, esa divergencia es la alarma de que la distribución asumida dejó de valer.
- **GARCH(1,1)** [15223](https://www.mql5.com/en/articles/15223), [20589](https://www.mql5.com/en/articles/20589): `σ²_t = ω + α·ε²_{t−1} + β·σ²_{t−1}`. La persistencia α+β dice cuánto
  dura un shock de vol. Sirve como feature y para escalar el tamaño. Un comentario en MQL5 dice que no le gana
  a ATR, así que hay que compararlo contra el ATR actual antes de adoptarlo.

### 8. Walk-Forward Efficiency (WFE)

MQL5 [23250](https://www.mql5.com/en/articles/23250): `WFE = SR_oos / SR_is` por ventana (IS = 252, OOS = 63), pasa con WFE ≥ 0,5, con
SR_is ≤ 0,25 la ventana falla, y hacen falta 8 ventanas o más. Hallazgo: optimizar no mejoró la robustez frente
a parámetros fijos. Esto es más débil que lo que ya tenemos (PBO/DSR), pero es un número fácil de leer para el panel.

## Propuesta de orden (si se decide implementar)

1. Ruina del jugador como baseline de hit-rate + Z de rachas + GHPR en `lab/stats.py`. Son horas de trabajo y
   suman a cada ficha.
2. Monte Carlo en bloques (DD P95, P(ruina)) en la ficha del laboratorio.
3. VPIN canónico en `features/order_flow.py` (datos que ya ingerimos), como feature del meta-modelo.
4. PCA/n efectivo de apuestas + VaR triple en el panel (backlog de UI #4).
5. Kelly fraccional calibrado, recién cuando haya ≥ 50–100 trades fuera de muestra de una estrategia.

Cada punto que toque datos visibles se muestra en el panel en el mismo cambio (regla del panel).

---

# Parte 2 (2026-10-19): categorías de MQL5 y artículos 18821 y 13942

## Alcance real

Las 5 categorías son listados: estadística (23 páginas), aprendizaje automático (18), trading (16), asesores
expertos (25) e indicadores (9), con unos 40 artículos por página, o sea unos 3.600 en total. Se leyeron la página 1
de cada una, el orden "mejores" de estadística, ML y trading, y la página 2 de estadística, y se abrieron a fondo
los que aplican a HeatSignal.
**Patrón general:** la mayoría son indicadores clásicos combinados (Wizard Parte N), redes neuronales de moda
(transformers, Mamba, "cuánticas"), metaheurísticas con nombres de animales y paneles de UI. Casi ninguno reporta
n, costos ni fuera de muestra. Lo útil son ideas de método, no estrategias.

## Los dos links puntuales

| Link | Qué es | ¿Nos sirve? |
|---|---|---|
| [18821](https://www.mql5.com/es/articles/18821) | Indicador de estacionalidad horaria: P(sube) por hora, movimiento medio y rango, para un símbolo o un spread | **La idea sí, el método no.** Con 30 días son unos 30 datos por hora, 24 horas probadas a la vez y sin test: un 64–70 % puede ser ruido puro. Ver "estacionalidad horaria bien hecha" abajo |
| [13942](https://www.mql5.com/es/articles/13942) | Serie didáctica de perceptrones: escalar las entradas para que converja | No. Es ML de manual. Lo único aplicable (normalizar features) ya está en `plan-entrenamiento.md` (ATR / z-score) |

## Lo que vale la pena (nuevo respecto de la parte 1)

### A. Estacionalidad horaria bien hecha (18821 + 19780)
Tenemos velas 1m y `features/sessions.py`; "estacionalidad" figura en observación en el catálogo. Cómo hacerlo:
- Por hora UTC (o por sesión) y por símbolo: retorno medio en ATR, P(vela verde) con **IC de Wilson** y n.
- **Corrección por comparaciones múltiples**: 24 horas × 7 símbolos = 168 tests. Hay que aplicar Benjamini-Hochberg
  (FDR 5 %), como hace [19780](https://www.mql5.com/es/articles/19780). Ese artículo es el ejemplo a seguir: buscó
  niveles de Fibonacci "ocultos" con KDE + bootstrap + FDR y **no encontró ninguno significativo** (p = 0,496).
- Estabilidad: partir la historia en mitades y ver si el signo se mantiene; si no, es ruido.
- Nula: barajar los retornos entre horas dentro de cada día (conserva la vol diaria).
- Esto se muestra en el panel como mapa de calor hora × símbolo, marcando solo las celdas que sobreviven al FDR.

### B. R² con signo de la curva de equity ([2358](https://www.mql5.com/es/articles/2358))
`R² = signo(final − inicial) · corr(equity, recta ajustada)²`. Mide qué tan regular sube la curva, no cuánto
gana. En 10.000 paseos aleatorios la "LR correlation" de MT5 daba entre ±0,75 y 0,95 (engaña); el R² queda cerca de 0.
Solo vale con tamaño fijo; con reinversión hay que usar GHPR. Es barato y suma a la ficha junto a la parte 1 §4.

### C. Estabilidad dentro de la muestra: Mann-Whitney primera vs. última parte ([4347](https://www.mql5.com/es/articles/4347))
Comparar la distribución de PnL del primer 70 % de los trades contra el último 30 % con Mann-Whitney:
`estabilidad = 1 − |1 − 2U/(n1·n2)|` (vale 1 si son iguales). Detecta un edge que se fue apagando antes de abrir el
holdout. El propio autor advierte que puede elegir variantes con pérdidas, así que se usa como diagnóstico, no
para optimizar.

### D. Tamaño adaptativo que solo reduce ([23913](https://www.mql5.com/en/articles/23913))
Lote final = base × racha × vol × DD, donde cada factor es ≤ 1:
- racha: `1 − 0,15·min(pérdidas_seguidas, 5)`, con piso 0,1 (llega a 0,25 con 5 pérdidas).
- vol: `min(1, ATR_media50 / ATR_actual)`.
- DD: no actúa hasta un DD del 10 % desde el pico de equity; después reduce en proporción.
Resultado en una estrategia **sin edge** (PF 0,96): el DD bajó de 73,5 % a 57 % y el factor DD fue el que más aportó.
Ojo: si hay edge, también recorta la recuperación, y el factor de vol se cuenta dos veces si el stop ya es en ATR.
Para nosotros: el freno por DD encaja como límite de riesgo del bot real; los otros dos hay que probarlos como
variantes versionadas, no activarlos por defecto.

### E. Cointegración antes de operar pares ([18702](https://www.mql5.com/es/articles/18702))
`pares-eth-btc-v1` (lote 3) ya anota el riesgo de "relación espuria si no hay cointegración". Pre-filtro concreto:
- Engle-Granger en **los dos órdenes** (el p-valor cambia: 0,082 contra 0,040 en su ejemplo) + Johansen (traza contra
  el valor crítico del 95 %) sobre log-precios.
- ADF + KPSS sobre el spread. Si se contradicen, la relación es frágil.
- Agregar la **vida media** de la reversión (el artículo no la tiene): `Δs_t = a + b·s_{t−1}`, `half_life = −ln2 / b`.
  Si la vida media supera el `max_hold_hours` = 72, la regla no tiene sentido.
- El autor perdió mucho en el backtest con demora aleatoria; la causa fue el slippage.

### F. Ensamble de estrategias: el optimizador elige las correlacionadas ([18847](https://www.mql5.com/es/articles/18847))
Al optimizar los pesos de un voto entre estrategias, el algoritmo genético les dio más peso a las dos que tenían
**ρ = 0,75**, que en la práctica eran la misma. Con los pesos fijos en 1 anduvo mejor fuera de muestra.
Para el lote: antes de combinar estrategias, mirar la matriz de correlación de sus retornos diarios y usar pesos
iguales o de riesgo igual; nunca pesos optimizados en la misma muestra.

### G. Salidas: la expectativa manda ([19211](https://www.mql5.com/es/articles/19211))
`E = p·RRR − (1 − p)` en R. El win-rate mínimo para no perder es `1/(1 + RRR)`. Esta fórmula es la versión en R del
baseline de ruina del jugador (parte 1 §1). Salir antes de tiempo de forma discrecional cambia p y RRR trade a
trade y destruye la expectativa. Con reglas fijas, no. Refuerza que en `paper_compare.py` se separe el efecto de
"timing de salida".

### H. Reproducibilidad entre fuentes de datos ([18133](https://www.mql5.com/es/articles/18133))
El mismo modelo andaba con un broker y no con otro. **Pero** un lector mostró que alineó las series por índice y no
por timestamp, así que los números no valen. La lección sí aplica: nuestros datos vienen de Binance, y el bot
opera en Binance. Si algún día opera en Bybit/OKX, hay que re-validar con datos de ese exchange (el lead-lag y el
premium entre exchanges ya están en observación).

### I. Selección de features ([2029](https://www.mql5.com/es/articles/2029))
Filtro por correlación > 0,90 (eliminar una de cada par), combinaciones lineales y varianza casi nula **antes**
del modelo, ajustando el preprocesamiento solo con train. Su objetivo con ZigZag usa datos futuros (la etiqueta
se calcula con la serie completa) y lo valida con un solo split, así que no sirve como ejemplo de validación;
nuestra validación purgada ya es mejor. El filtro por correlación sí conviene para la etapa 2 de
`plan-entrenamiento.md`.

## Descartado (para no volver a mirarlo)
Patrones armónicos (Crab, Gartley, 5-0), Fair Value Gaps, ángulos, Murrey, metaheurísticas (águila, dingo,
duelista, sueño, átomo), redes "cuánticas", transformers y N-BEATS (el propio autor de N-BEATS dice que no fue
rentable en 2025), paneles de UI, grid/zone recovery y promediación (martingala encubierta). Si alguno vuelve
como idea, va al catálogo con prior bajo y se pre-registra.

## Orden propuesto actualizado (une parte 1 y parte 2)
1. **Hecho 2026-10-19.** `lab/stats.py`: baseline de ruina / win-rate mínimo para no perder, Z de rachas, GHPR, R² con signo, estabilidad
   Mann-Whitney. Todo barato; va a la ficha del laboratorio y al panel.
2. Monte Carlo en bloques (DD P95, P(ruina)).
3. Estacionalidad horaria con FDR, como estudio en observación, con mapa de calor en el panel.
4. Pre-filtro de cointegración + vida media para `pares-eth-btc` (versión nueva `-v2` si cambia la regla).
5. VPIN canónico como feature.
6. Correlación entre estrategias antes de cualquier ensamble.
7. Freno por drawdown y Kelly fraccional, al acercarse a plata real.
