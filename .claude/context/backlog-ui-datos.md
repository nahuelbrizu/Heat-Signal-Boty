# Backlog: PnL y tipos de datos para la UI (análisis, para hacer después)

> Pedido del usuario (2026-10-18): "agregá PnL y distintos tipos de datos a la UI, pero dejalo para hacer luego;
> hacé un análisis de los datos más utilizados y explayalos". **Estado: BACKLOG, no implementado.**
> Regla vigente: todo dato nuevo se muestra en el panel (SPA en `api/static/app/`) y toda tasa lleva n e IC.

Leyenda de disponibilidad:
- ✅ **ya lo tenemos y se muestra.**
- 🟡 **tenemos el dato crudo, falta calcularlo o mostrarlo.**
- 🔴 **hay que conseguir el dato** (fuente nueva o más tiempo de grabación).

---

## 1. PnL: lo que más se mira y cómo conviene mostrarlo

Hoy hay PnL en tres lugares que no se cruzan entre sí:
- **Paper trading** (Freqtrade dry-run): realizado, no realizado, por par.
- **Laboratorio:** retorno OOS y descomposición precio / funding / comisiones / slippage.
- **Hipótesis del grafo:** PnL neto y alpha contra mantener la moneda.

Un trader mira el PnL en estos cortes, en orden de uso:

| # | Métrica | Qué responde | Fuente | Disp. |
|---|---|---|---|---|
| 1 | **PnL realizado vs no realizado** | ¿Cuánto gané de verdad y cuánto está "en el aire"? | Freqtrade | ✅ (vista Paper) |
| 2 | **PnL por período** (hoy, semana, mes, año, desde el inicio) con **calendario/heatmap diario** | ¿Cómo viene el mes? ¿Hay rachas? | Freqtrade (trades cerrados + equity diaria) | 🟡 |
| 3 | **PnL por estrategia** (paper, lab, grafo) en una sola tabla comparable | ¿Qué fuente de señales rinde? | las 3 fuentes | 🟡 (hay que unificar unidades: % sobre capital asignado) |
| 4 | **PnL por par y por dirección** (long/short) | ¿Dónde gano y dónde pierdo? | Freqtrade, hipótesis | 🟡 (por par sí; por dirección no) |
| 5 | **Descomposición del PnL**: precio, funding, comisiones, slippage | ¿El costo se come la ventaja? | lab ✅, paper 🟡 (fees y funding están en la base de Freqtrade) | 🟡 |
| 6 | **PnL vs referencia**: contra mantener cada moneda y contra la canasta, en la misma ventana | ¿Le gano a no hacer nada? | lab ✅, hipótesis ✅ (alpha), paper 🟡 | 🟡 |
| 7 | **Curva de capital + drawdown** (underwater) | ¿Cuánto llegué a perder desde el máximo y cuánto tardé en recuperar? | Freqtrade, lab | 🟡 (la curva está, el gráfico de drawdown no) |
| 8 | **PnL en R** (múltiplos del riesgo inicial) | ¿Cada trade arriesga lo mismo? | requiere definir el "riesgo" de cada trade (stop o volatilidad) | 🔴 conceptual |

**Propuesta de UI (vista nueva "PnL", o pestaña en Resumen):**
- Encabezado con PnL total, realizado, no realizado, hoy, 7 días, 30 días y desde el inicio. Todo en USDT y en % del capital.
- Calendario mensual coloreado por PnL diario (como el de FreqUI y los exchanges), con click al detalle del día.
- Curva de capital con el drawdown debajo, la canasta y buy & hold superpuestos, y marcas de cada trade.
- Tabla pivot: filas por estrategia, par o dirección; columnas con n, PnL, % del total, tasa de acierto (IC), PnL medio por trade y comisiones + funding.
- Cascada (waterfall) del PnL: bruto → comisiones → slippage → funding → neto.

---

## 2. Métricas de rendimiento y riesgo (las estándar de cualquier reporte)

| Métrica | Uso | Disp. |
|---|---|---|
| Tasa de acierto con IC (Wilson) | Frecuencia de aciertos | ✅ |
| Ganancia media / pérdida media y su ratio | Si ganar poco seguido compensa perder mucho a veces (Donchian acierta 21%) | 🟡 |
| **Expectancy** (PnL medio por trade) e IC | ¿Cada trade suma en promedio? | 🟡 |
| **Profit factor** (ganancias / pérdidas brutas) | Resumen rápido de la calidad | 🟡 (lab ✅) |
| Sharpe, **Sortino**, **Calmar** | Retorno por unidad de riesgo (Sortino solo castiga la baja; Calmar usa el DD) | Sharpe ✅; Sortino y Calmar 🟡 |
| Deflated Sharpe, PSR, MinTRL | ¿Es suerte? ¿Alcanza la muestra? | ✅ (lab) |
| Max drawdown, duración y tiempo de recuperación | Dolor máximo | 🟡 (valor sí, duración no) |
| Racha máxima de ganancias y de pérdidas | Tolerancia psicológica / tamaño | 🟡 |
| **MAE / MFE** por trade (máxima pérdida y ganancia flotante) | ¿Los stops y salidas están bien puestos? | 🟡 (el simulador los calcula; no se muestran) |
| Duración media de ganadoras vs perdedoras | ¿Corto las ganancias rápido? | 🟡 |
| **Exposición**: % del tiempo en mercado, nocional abierto por moneda | Cuánto riesgo hay puesto ahora | 🟡 |
| Turnover y costos como % del PnL bruto | Dependencia de costos | 🟡 |
| Calibración de la confianza (Brier, curva) | ¿La confianza del grafo significa algo? | ✅ |
| Correlación entre estrategias y con BTC | ¿Diversifican o son la misma apuesta? | 🟡 |
| VaR / CVaR diario (histórico) | Pérdida esperable en un mal día | 🟡 (con la equity diaria) |

**Propuesta de UI:** una "ficha de rendimiento" reutilizable (el mismo componente para paper, para cada estrategia
del lab y para el grafo), con estas métricas en grilla, cada una con un tooltip que explique qué significa y si la
muestra alcanza.

---

## 3. Datos de mercado que más se usan en derivados

| Dato | Para qué se usa | Disp. |
|---|---|---|
| Precio y velas multi-timeframe | Base de todo | ✅ |
| Volumen y **delta / CVD** (compras − ventas agresivas) | Quién empuja el precio | ✅ (barras y features) |
| **Open interest** y su cambio (en contratos) | Entra o sale apalancamiento | ✅ (snapshot); gráfico histórico 🟡 |
| **Funding** por exchange y su historia | Costo de estar long o short; posicionamiento | ✅ (actual); **heatmap moneda × exchange × tiempo** 🟡 |
| **Liquidaciones**: volumen por lado y en el tiempo | Cascadas, capitulaciones | ✅ (lista y totales); **mapa de calor precio × tiempo** 🟡 |
| **Mapa de niveles de liquidación estimados** (dónde se liquidarían las posiciones) | Zonas "imán" de precio | 🔴 (requiere modelo de apalancamiento; Coinglass lo vende, BYOK) |
| Ratio long/short (top traders y global) | Sentimiento del posicionamiento | 🔴 en vivo; 🟡 histórico (metrics de Binance en la PC) |
| **Basis / premium** perp vs spot (índice) | Euforia o pánico del perpetuo | 🟡 (premium index 1m en la PC; en vivo hace falta el index) |
| Spread de funding y de precio entre exchanges | Divergencias (estrategia forward de Hyperliquid) | 🟡 |
| Volatilidad realizada (ATR, percentil) y régimen | Contexto y tamaño de posición | ✅ (features) |
| **Correlación entre monedas** (rolling) | Si las 7 se mueven juntas, no hay diversificación | 🟡 |
| Profundidad del libro (±1–5%) | Liquidez y slippage esperable | 🔴 (bookDepth de Binance, opcional) |
| Dominancia BTC, stablecoins, flujos on-chain | Macro cripto | 🔴 (fuentes nuevas) |
| Noticias con tono y fuente | Contexto | ✅ |
| Calendario macro (FOMC, CPI) | Eventos de volatilidad | 🔴 |

**Propuesta de UI:**
- Panel de **"derivados"** por moneda: OI, funding, CVD y liquidaciones en paneles alineados al gráfico de
  precio (mismo eje de tiempo), como hacen las plataformas de derivados.
- **Heatmap de funding** (monedas × exchanges, color por tasa) y **heatmap de liquidaciones** (precio × tiempo)
  con nuestros propios datos, avisando que Binance y OKX están muestreados.
- **Matriz de correlación** de las 7 monedas (30 y 90 días).

---

## 4. Datos del sistema propio que conviene exponer

- **Grafo:** costo y latencia por ciclo y por proveedor (Anthropic, Gemini y sus respaldos), tasa de fallback y de error, tokens. 🟡 (hay logs, no métricas)
- **Hipótesis por estado del mercado:** aciertos según régimen, volatilidad y tono de noticias. 🟡
- **Laboratorio:** conteo de intentos del programa (K) y cómo se mueve el listón del DSR al sumar variantes. 🟡
- **Paper vs backtest:** desvío acumulado por componente a lo largo de las semanas. ✅ (historial semanal)
- **Calidad de datos en el tiempo:** huecos, reconexiones, liquidaciones por exchange. 🟡

---

## 5. Prioridad sugerida (para cuando se retome)

1. **Ficha de rendimiento + vista PnL** (secciones 1 y 2): calendario, curva con drawdown, pivot por estrategia,
   par y dirección, cascada de costos. Todo sale de datos que ya tenemos. Es lo de mayor valor para decidir.
2. **Panel de derivados por moneda + heatmaps de funding y liquidaciones** (sección 3): datos propios ya grabados.
3. **Métricas del grafo** (costo, latencia, fallback): ahora que hay cadena de proveedores, importa el gasto.
4. Correlaciones y VaR.
5. 🔴 Fuentes nuevas (long/short en vivo, profundidad, macro), solo si una estrategia las necesita.

Cada ítem: endpoint de solo lectura en `api/panel_api.py` (sin pandas en la VM), vista en `static/app/views/`,
tests, y n + IC en toda tasa.
