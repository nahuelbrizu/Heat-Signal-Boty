# Lote 5 — de la base de conocimiento del usuario (2026-10-18)

**Estado: APROBADO por el usuario el 2026-10-19 (antes de ver resultados).** Código en `lab/strategies.py` (`LOTE5`), tests en
`tests/test_lab_strategies_lote5.py`. Nada corrido con datos reales. Revisión del documento en
`research/base-conocimiento-ta.md`.

| strategy_id | qué hace | grilla | spec_hash |
|---|---|---|---|
| `cartera-tendencia-v1` | un tercio del capital de cada símbolo para donchian-ensemble-v1 (4h, target_vol 0,20), tsmom-diario-v1 (0,15) y ema-cruce-v1 (1h); suma de posiciones | ninguna | `3d7b32a429f494fc` |
| `max-ruptura-10d-v1` | velas diarias: si el cierre es el máximo de 10 días, long 1 día; solo long | ninguna | `39cb9dee6d3e469a` |
| `vwap-reversion-v1` | velas 15m: distancia al VWAP del día UTC en ATR(14) < −2,5 long / > +2,5 short, desde la 01:00 UTC; objetivo el VWAP de la entrada, stop 1,5 ATR, máx. 4 h | ninguna | `724e2b1e207828df` |

**Intentos:** 3 → el programa pasa de K = 48 a K = 51.

## Decisiones tomadas al congelar
1. **Cartera con parámetros por defecto**, no con los que eligió el walk-forward (p. ej. ema-cruce eligió 4h en
   52 de 65 ventanas). Elegirlos mirando resultados sería ajustar; la cartera hace su propio walk-forward sin
   grilla (1 intento).
2. **Un tercio de capital por componente**, no "mismo riesgo": igualar riesgo exige estimar la volatilidad de cada
   estrategia con sus propios retornos y es otra estrategia (v2 posible). ema-cruce arriesga poco (1 % hasta el
   stop), así que pesa menos en la práctica.
3. **max-ruptura solo long**, como la pata MAX de Quantpedia; mantener 1 día exacto.
4. **vwap-reversion:** opera seguido en 15m, y lo que opera seguido murió por costos en los lotes 2 y 3. Va igual
   porque el usuario la propuso y no está probada; se espera que falle si no tiene ventaja grande.
5. Si una salida coincide con un cierre de vela con señal nueva, la posición sigue (o se da vuelta) sin pasar por 0
   (convención de `exits_with_levels` en todo el lab).
