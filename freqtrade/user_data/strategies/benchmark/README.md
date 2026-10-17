# Benchmark de "análisis técnico común" (estrategias de terceros)

Estas 7 estrategias **no son nuestras**. Son copias sin modificar de la carpeta `user_data/strategies/futures/` del
repo oficial de ejemplos de Freqtrade:

- Repo: https://github.com/freqtrade/freqtrade-strategies
- Commit: `f3340ce11f5bdf62f598522e64d1f5638eaa13f5` (2026-09-08)
- Copiadas el 2026-10-18, byte a byte (sin cambios de parámetros, ROI, stoploss ni leverage).
- **Licencia: GPL-3.0** (texto completo en `LICENSE`, copiado del mismo repo). Cualquier redistribución de
  estos archivos tiene que respetar la GPL-3.

| Archivo | Timeframe | Shorts | Nota |
|---|---|---|---|
| `FAdxSmaStrategy.py` | 1h | sí | |
| `FOttStrategy.py` | 1h | **no** (no declara `can_short`: Freqtrade ignora sus `enter_short`) | stoploss −0,265, trailing |
| `FReinforcedStrategy.py` | 5m | sí | usa `technical` (resample a 60m) |
| `FSampleStrategy.py` | 1h | sí | |
| `FSupertrendStrategy.py` | 1h | **no** (ídem) | usa `technical`; stoploss −0,265 |
| `TrendFollowingStrategy.py` | 5m | **no** (ídem) | stoploss −0,265 |
| `VolatilitySystem.py` | 1h | sí | **leverage 2,0** fijado por la estrategia, stoploss −1, ajusta posición |

sha256 de cada archivo (para verificar que siguen intactos):

```
43ae02ecca4553b8a609d4533cb4d5b6517292d35b41fc4c77f54f8e959ced89  FAdxSmaStrategy.py
6e7dd4802915f6952c9f7be6b5fe9840d1c894a89476e1b274fb163b096d930e  FOttStrategy.py
4148d3d2d736a454a470649646a59b77a62f7d4ccb7f7bc6d2ecc5ad0ec01152  FReinforcedStrategy.py
3fcbe2ed16dd097fd6e4548172acbde871bdfe64c5a7341f337f19caf3daa225  FSampleStrategy.py
93f31258c4b6c05cf2380fc81f235f37355d97a3e4d2ebdf075e948e07978e19  FSupertrendStrategy.py
6b8f3e272acfd4923539120364b9ddceccc17a385117d12a249dc1460ea53c4f  TrendFollowingStrategy.py
10a32ea131fb8f4684741b48cd3eae70aa4d4990319b49a7acb7a2f9c7c8a672  VolatilitySystem.py
```

## Compatibilidad con Freqtrade 2026.9 (numpy 2 + pandas 3): `compat/`

Dos originales no corren tal cual con las versiones instaladas. **Los archivos originales no se tocan** (el script
verifica su sha256); `compat/` tiene subclases propias que heredan todo del original y no cambian parámetros,
ROI, stoploss, leverage ni reglas:

- `FOttStrategyCompat`: carga `FOttStrategy.py` con 3 parches en memoria (cada uno tiene que aparecer exactamente
  una vez, si no falla):
  1. `from numpy.lib import math` → `import math` (numpy 2 lo quitó; el original no usa `math`);
  2. `df["Var"].iat[i] = ...` → `df.iat[i, <col Var>] = ...`. Es asignación encadenada: con pandas 3
     (Copy-on-Write) **no escribe nada**, `Var` queda en 0 y la estrategia nunca opera (falla silenciosa, 0
     trades). El parche restaura la semántica de pandas < 3 con la que se publicó;
  3. `df["shortstop"] = 999999999999999999` se crea como float (pandas 3 ya no convierte int64 → float64 al
     asignarle floats; pandas < 3 sí lo hacía).
- `FReinforcedStrategyCompat`: el original declara `IntParameter` sin `space` y Freqtrade 2026.9 aborta
  ("Cannot determine parameter space"). Se re-declaran con el mismo rango y default y `space="buy"`; en
  backtesting solo se usa el default.

Los wrappers de `compat/` derivan de código GPL-3, así que también quedan bajo GPL-3.

**Tiempo de corrida:** `FOttStrategy` tiene en el original dos bucles `for i in df["UD"]` con operaciones
vectoriales adentro (O(n²) en la cantidad de velas). Con 2 meses × 2 pares tarda ~1 min; con el rango completo
en 1h puede tardar muchas horas. Es el código tal cual: no se optimiza.

## Para qué están

Responden una pregunta: **¿nuestras hipótesis le ganan al TA estándar?** (ver
`.claude/context/research/freqtrade-strategies.md`). Se corren **tal cual, sin re-optimizar**: sus parámetros
salieron de hyperopt sobre otros períodos, así que nuestros datos son fuera de muestra para ellas. Cada una
cuenta como un intento más en el Deflated Sharpe del programa. **No** se elige "la mejor" y se la ajusta: ese es
el error del caso ML4T.

## Cómo se corren

`backend/src/heatsignal/lab/ft_benchmark.py` (desde `backend/`):

```
PYTHONPATH=src .venv/bin/python -m heatsignal.lab.ft_benchmark --start 2021-03-13 --end 2026-07-15
```

Usa el Freqtrade de `~/heatsignal-lab/ft/.venv` (TA-Lib y `technical` instalados), baja con `download-data`
las velas que falten (1h y 5m de futuros) y guarda un resumen JSON comparable con el laboratorio en
`~/heatsignal-lab/results/_benchmark_ta/`. Estos archivos están en una subcarpeta para que el dry-run de
`DonchianEnsembleV1` (que busca estrategias solo en `user_data/strategies/`, sin recursión) no los cargue.
