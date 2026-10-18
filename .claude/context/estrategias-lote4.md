# Lote 4 — cruce de EMAs (propuesta del usuario, 2026-10-18)

**Estado: PROPUESTA, sin aprobar.** Código en `lab/strategies.py` (`ema_cruce`, `LOTE4`), indicadores nuevos en
`lab/features.py` (`dmi`, `rsi`, `bb_width`), tests sintéticos en `tests/test_lab_strategies_lote4.py`.
Nada corrido con datos reales.

| strategy_id | qué hace | grilla | spec_hash |
|---|---|---|---|
| `ema-cruce-v1` | EMA9 cruza a EMA21 **en la vela**, con close del lado de la EMA200 y volumen > SMA20 del volumen | timeframe 1h / 4h | `a950979de8e216af` |
| `ema-cruce-filtrado-v1` | lo anterior + ADX(14) ≥ 25 con DI a favor, RSI(14) en 50–70 (long) / 30–50 (short), ancho de Bollinger(20,2) > su SMA20 | timeframe 1h / 4h | `04d0e50a506fde17` |

Comunes: los 7 símbolos; stop a 1,5 ATR(14) del close de entrada; objetivo a 3 ATR (R:R 1:2); stop y objetivo
evaluados en velas 1m (si tocan los dos en el mismo minuto, cuenta el stop); una posición por símbolo; tamaño
por riesgo = 1% del capital del símbolo hasta el stop (w = 0,01·precio / (1,5·ATR)), **tope 1× (sin
apalancamiento)**; tope de seguridad de 30 días por operación; costos del lab (7 bps por lado) y funding real.

**Intentos:** 4 combinaciones → el programa pasa de K = 44 a K = 48.

## Decisiones tomadas al congelar (revisar antes de aprobar)
1. **Cruce en la vela vs estado.** El texto pide el cruce (parte 1), pero el ejemplo de código usa
   `ema9 > ema21` (estado). Se congeló el **cruce en la vela**, con todos los filtros evaluados en esa misma vela
   (compuerta estricta). Con estado, entraría en cualquier vela de la tendencia: es otra estrategia.
2. **Tope de tamaño 1×.** En 1h el ATR ronda 0,5–1% del precio: con 1% de riesgo y stop a 1,5 ATR la fórmula
   pide 0,7–1,3× del capital del símbolo. Se topea en 1× para no apalancar.
3. **Sin salida por señal contraria.** El texto solo define stop y objetivo; no se agregó ninguna otra.
4. **No se ajustan umbrales con los resultados** (p. ej. ADX 25 → 30): eso es p-hacking. Un cambio de umbral
   es una `-v2` pre-registrada y suma intentos.
5. **No se usan Docker ni Supabase** (sugeridos en el texto): los datos son privados y se quedan en SQLite.
   El costo de cálculo es chico.
