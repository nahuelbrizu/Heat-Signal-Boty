# Lote 6 — exploración masiva (60 variantes), aprobado por el usuario 2026-10-20

**Estado: APROBADO, lista fijada antes de programar y correr.** Es EXPLORATORIO: ninguna variante se aprueba por su
resultado acá; las que cumplan el criterio de abajo pasan a CONFIRMACIÓN en paper con datos nuevos (desde 2026-10-20).
Todas cuentan como intentos del programa (K pasa de 51 a 111). Los 7 símbolos salvo indicación. Costos y funding del
lab, walk-forward y holdout como siempre (el holdout NO se abre en este lote). Sin grilla: cada variante es una spec
con parámetros fijos (1 intento cada una).

## Familias y variantes
1. **Ruptura de N días** (`ruptura-n`) — 24: cierre diario = máximo de N días → long por H días; en "ambos", además
   cierre = mínimo de N días → short. N ∈ {5, 10, 20, 55} × H ∈ {1, 3, 5} × lado ∈ {long, ambos}.
2. **Momentum de serie** (`tsmom-n`) — 12: signo del retorno de L días (decisión diaria 00:00 UTC, mantener hasta la
   próxima decisión). L ∈ {7, 28, 84} × tamaño ∈ {fijo, vol-target 20 % anual con vol 30 d, tope 1×} × lado ∈
   {long, ambos}.
3. **Calendario y sesiones** (`cal-*`) — 11: long 24 h desde las 00:00 UTC de cada día de la semana (7: lun…dom);
   long del viernes 00:00 al domingo 24:00 UTC (fin de semana, 1); long 4 h desde la apertura de Asia (00:00 UTC),
   Londres (Europe/London 08:00) y Nueva York (America/New_York 09:30), con DST (3).
4. **Derivados** (`der-*`) — 5: funding z-score 30 d > +2 → short / < −2 → long, mantener 24 h (1) y 72 h (1);
   cuadrante OI ↓ y funding < 0 (24 h, OI en contratos) → long, mantener 3 d (1) y 7 d (1); ratio taker de la última
   1 h en el percentil 95 de 30 d → short, ≤ percentil 5 → long, mantener 4 h (1).
5. **Reversión diaria** (`rsi2-*`) — 4: RSI(2) diario < 10 → long, mantener 1 d (1) y 3 d (1); RSI(2) diario > 90 →
   short, mantener 1 d (1) y 3 d (1).
6. **Régimen** (`reg-*`) — 4: ruptura de 20 días mantenida 5 d con filtro ADX(14) diario > 25 (long / ambos, 2) y con
   filtro percentil de ATR/precio de 1 año < 50 (long / ambos, 2).

Total: 24 + 12 + 11 + 5 + 4 + 4 = **60**.

## Criterio para pasar a confirmación (fijado antes de correr)
Todas las condiciones, en el período de desarrollo (OOS walk-forward):
1. veredicto propio del lab: Sharpe OOS > canasta y > B&H medio, break-even ≥ 2 × costo esperado, p contra azar ≤ 0,05;
2. robustez: meseta ≥ 80 % (vecinos ±20/40 %) y Sharpe con costos ×3 > 0;
3. listón del programa con K actualizado: p de Romano–Wolf (bootstrap del máximo, regla del 2026-10-19) ≤ 0,10
   (más laxo que el 0,05 de aprobación porque esto es para CONFIRMAR, no para operar).

Pasan **todas** las que cumplan (sin número fijo). Si no pasa ninguna, no pasa ninguna y se registra. Las que pasen
van al bot de paper con su hash, y se evalúan con datos desde 2026-10-20 (mínimo 3 meses) con el criterio de
plata real de `plan-entrenamiento.md` (etapa 5).
