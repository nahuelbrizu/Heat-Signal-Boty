# Plan: etapas 1b–3 (datos históricos, laboratorio, paper trading)

Consolidado el 2026-10-17 a partir de 4 investigaciones (`research/`):
- `binance-datos.md`
- `ml4t-metodologia.md`
- `catalogo-hipotesis.md`
- `backtest-motores.md`

**Estado: PROPUESTA.** Las decisiones marcadas ❓ son del usuario. Ninguna estrategia está registrada.

## Lección central (ML4T, caso Crypto Perps)

2.800 variantes. La mejor tuvo Sharpe de validación 1,67 (p = 0,005) y su **Deflated Sharpe ≈ −0,1**. En
el holdout dio Sharpe −0,83 y −45% anual. Todo lo que hacemos abajo existe para no repetir eso: registrar
**cada** intento, holdout sellado que se abre una vez, y **descartar si el DSR ≤ 0**.

## Etapa 1b: historia que falta (todo gratis, Binance)

| Dato | Fuente | Tamaño (7 símbolos) |
|---|---|---|
| Funding completo (desde 2020) | REST `/fapi/v1/fundingRate` | < 2 MB |
| OI, long/short (top y global) y ratio taker, cada 5m | data.binance.vision `metrics` | ~150–180 MB zip → ~0,3 GB convertido |
| Premium index 1m (solo el close) | REST `premiumIndexKlines` | ~0,2 GB |
| Flujo taker 1m | **Ya lo tenemos** (`taker_buy_quote` de las klines) | — |
| Liquidaciones | **No hay historia pública.** Solo lo que grabamos (forceOrder de Binance subestima: 1/s) | — |

❓ **Dónde vive la historia larga.** Las klines 1m desde 2020 son ~22 M filas (~2 GB) y no entran bien en la VM
(3,5 GB libres).

Recomendación:
- **El laboratorio corre en la PC (WSL)** con toda la historia.
- **La VM** guarda el último año más lo que graba en vivo, y hace el dry-run/paper.
- Drive guarda el respaldo de ambos.

## Etapa 2: laboratorio (lo escribimos nosotros)

1. **`features/`**: funciones puras sobre `bars_1m` y `klines`. Es el mismo código en el laboratorio, en
   Freqtrade y en vivo.
   - **Reloj de disponibilidad:** cada barra se fecha a su **cierre**. Las agregaciones usan
     `label="right", closed="left"`.
   - Los features de flujo se usan con lag ≥ 1.
   - OI **en contratos**, no en USD.
   - Funding: solo el ya liquidado.
2. **Spec de estrategia versionado** (YAML/JSON con hash):
   - universo y reloj de decisión;
   - **ejecución en el open de la vela 1m siguiente**;
   - regla, horizonte y salida;
   - grilla de calibración chica, fijada de antemano;
   - costos.
3. **Simulador vectorizado:**
   - comisión 5 bps por lado;
   - slippage por símbolo (Roll/Corwin-Schultz, ×2–3 en liquidaciones extremas);
   - funding en cada liquidación;
   - P&L descompuesto en precio / funding / costos.
4. **Validación:**
   - walk-forward con purga y embargo;
   - holdout sellado: últimos 3 meses + todo lo forward;
   - umbrales siempre con ventanas trailing.
5. **Registro de intentos** (append-only, como el resto): cada corrida queda con su hash, y K alimenta el DSR.
6. **Métricas:**
   - Sharpe con IC por bootstrap por bloques;
   - PSR, **DSR**, MinTRL;
   - curva de Sharpe contra costo y costo de break-even;
   - hit rate con IC de Wilson;
   - MFE/MAE.
7. **Baselines obligatorios:**
   - buy & hold;
   - canasta de las 7;
   - **entradas al azar** con la misma frecuencia y tenencia (~1.000 permutaciones).
8. **Criterio para avanzar a la etapa 3:**
   - DSR > 0;
   - break-even > 2× el costo esperado;
   - le gana a los 3 baselines fuera de muestra;
   - funciona en varios símbolos.
9. **Panel:** resultados del laboratorio (regla de la UI siempre al día).

**Librerías:**
- `ml4t-diagnostic` (MIT): splitters, DSR/PSR/PBO, bootstrap.
- `ml4t-engineer`: triple barrier y estimadores de volatilidad.

Ambas en versión 0.1.x: fijar la versión exacta. Si pesan demasiado, DSR, PSR y bootstrap se escriben en
~100 líneas.

## Primer lote propuesto (❓ aprobar antes de registrar)

| # | Estrategia | Apuesta | Plazo | Backtest con historia |
|---|---|---|---|---|
| 1 | `donchian-ensemble-v1` (velas 4h, N = 20/40/80/160, tamaño según volatilidad) | Tendencia (long) | Días | Sí |
| 2 | `flujo-apertura-hora-v1` (desbalance taker en :00–:01 > p95) | Con el flujo | 4 h | Sí |
| 3 | `reversion-flujo-extremo-15m-v1` (\|ret\| > 2,5σ con flujo extremo) | En contra | 1 h | Sí; se espera que muera por costos |
| 4 | `capitulacion-long-v1` (liquidaciones de longs > p99,5 + caída > 3σ + desaceleración) | Rebote long | ≤ 4 h | Proxy con klines; real desde hoy |
| 5 | `squeeze-short-v1` (el espejo) | Short | ≤ 4 h | Ídem |
| 6 | `funding-crowding-xs-v1` (z-score del funding a 14 d entre las 7) | Neutral | 8–24 h | Sí, con funding histórico |
| 7 | `funding-crowding-short-v1` (funding > p95 + OI en contratos subiendo + quiebre) | Short | 24 h | Sí, con metrics |
| 8 | `funding-spread-hl-cex-v1` (Hyperliquid contra la mediana de las CEX) | Neutral | ≤ 72 h | Solo forward |

- **Controles:** los baselines, más `absorcion-flujo-v1`, que se espera que falle por costos (verifica que el
  laboratorio no "encuentre" ganancias falsas).
- **En observación, sin capital:** noticias, estacionalidad, lead-lag y premium entre exchanges.
- **Las dos XRP de 90 días** siguen como están.

## Etapa 3: simulado con Freqtrade

- Exportador de nuestros datos al formato de Freqtrade: OHLCV, mark, `funding_rate` y OI.
- Las estrategias que pasen la etapa 2 se escriben como `IStrategy` (importando `features/`).
- Primero `lookahead-analysis`. Después dry-run en demo de Binance (`demo-fapi.binance.com`) o en Bybit
  demo.
- Se compara **simulación contra realidad**, vela a vela.
- Hummingbot queda solo como ejecutor si alguna vez hacemos market making: su backtest no cobra funding.
- NautilusTrader queda como alternativa cuando su v2 sea estable.
- Binance Agent OS / MCP: no aporta. Pide OAuth incluso para datos públicos y no da historia.

## Orden de trabajo propuesto

1. Etapa 1b: backfill de funding, metrics y premium (en la PC y en la VM según ❓).
2. `features/` + reloj de disponibilidad + tests anti look-ahead.
3. Simulador, registro de intentos, métricas y baselines.
4. Codificar el lote en specs → **aprobación** → registro (pre-registro antes de mirar resultados).
5. Correr walk-forward, abrir el holdout una vez y llevar los resultados al panel.
6. Las que pasen → Freqtrade dry-run.
