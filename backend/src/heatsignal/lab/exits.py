"""
Salidas de cada estrategia (stop, objetivo, tiempo y otra salida) y atribucion de cada cierre a su motivo.

Pedido del usuario (2026-10-19): "¿Estamos poniendo stop loss y take profits? Agrega esa info en todo el sistema".
Ver decisions.md, "Stop loss y take profit (2026-10-19)".

1. Ficha de salidas: `EXITS[strategy_id]`, un mapa APARTE del `Spec` (agregar un campo al Spec cambiaria los
   spec_hash congelados de todos los lotes). Los valores salen de `spec.params` y del docstring de cada regla en
   `lab/strategies.py`; cada valor numerico lleva el nombre del parametro de donde sale (`param`) y un test verifica
   que coincidan (`check_params`). Agregar un stop u objetivo a una estrategia ya registrada es una -v2, NO un
   cambio de esta ficha: la ficha solo describe lo que la regla congelada ya hace.

   Campos: stop / take_profit (tipo, valor, param, base, si se evalua en velas 1m y que pasa en el empate),
   tiempo (valor, unidad, param), otra (canal, senal contraria, rebalanceo, cambio de composicion), nota corta y los
   motivos con los que se clasifican los cierres que no salen de un nivel (`cierre`: la posicion vuelve a 0;
   `giro`: pasa de long a short o al reves). La cartera hereda las salidas de sus componentes (`hereda`).

2. Atribucion: cuantas operaciones cerraron por stop, por objetivo, por tiempo, por senal o por rebalanceo.
   `strategies.exits_with_levels` ya sabe que barrera toco, pero su salida (una Series de posiciones) no se toca.
   `exit_reasons` REPRODUCE su logica por fuera y devuelve el motivo de cada salida; `capture_exits` envuelve
   temporalmente `exits_with_levels` / `hold_events` del modulo de reglas para registrar las entradas y niveles
   reales de cada llamada (la regla y su resultado no cambian) y verifica, llamada por llamada, que la
   reconstruccion arme exactamente la misma Series (`verificacion.coincide`). Las reglas que no usan esos helpers
   (canal, momentum, rebalanceos, pares) se clasifican por las transiciones de su senal con la ficha.
   Lo corre `lab/diagnostics.py salidas|ambos` sobre el desarrollo (nunca el holdout) y no suma intentos.

Sin pandas ni numpy al importar: la ficha la usan el panel y el bot de paper (que no cargan pandas). La atribucion
importa pandas adentro de las funciones.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Any

MOTIVOS = ("stop", "objetivo", "tiempo", "senal", "rebalanceo")
MOTIVO_LABEL = {"stop": "stop", "objetivo": "objetivo", "tiempo": "tiempo", "senal": "señal", "rebalanceo": "rebalanceo",
                "abierta": "abierta al final", "sin_dato": "sin dato"}
OTRA_LABEL = {"canal": "canal", "senal": "señal contraria", "composicion": "cambio de composición",
              "rebalanceo": "rebalanceo", "programada": "giro programado"}

_ATR_1M = {"velas_1m": True, "empate": "stop"}


def _t(valor: float, unidad: str, param: str | None, fuente: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"valor": valor, "unidad": unidad, "param": param}
    if fuente:
        out["fuente"] = fuente
    return out


# ---- ficha de salidas -----------------------------------------------------------------------------------------
# Cada entrada: stop, take_profit, tiempo, otra (None = no tiene), nota, cierre/giro (motivo de los cierres que no
# salen de un nivel). `param` = nombre en spec.params (o expresion "a*b" / "a+b"); None = fijo en el codigo (fuente).

EXITS: dict[str, dict[str, Any]] = {
    "donchian-ensemble-v1": {
        "stop": None, "take_profit": None, "tiempo": None,
        "otra": {"tipo": "canal", "detalle": "cada canal N (20/40/80/160 velas de 4h) se apaga cuando el close cae bajo el "
                 "mínimo de las N/2 velas anteriores; la posición es el promedio de los 4 canales y se ajusta por "
                 "volatilidad en cada vela", "param": "channels"},
        "nota": "Sin stop ni objetivo: sale por canal (igual que Freqtrade, stoploss −0,99).",
        "cierre": "senal", "giro": "senal",
    },
    "flujo-apertura-hora-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(4, "h", "hold_hours"), "otra": None,
        "nota": "Sin stop ni objetivo: cierra a las 4 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "reversion-flujo-extremo-15m-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(1, "h", "hold"), "otra": None,
        "nota": "Sin stop ni objetivo: cierra a la hora.", "cierre": "tiempo", "giro": "tiempo",
    },
    "capitulacion-long-v1": {
        "stop": {"tipo": "sigma", "valor": 0.5, "param": "sl_sigma",
                 "base": "mínimo de la vela de 15m de la señal − 0,5 σ de 60m (en precio)", **_ATR_1M},
        "take_profit": {"tipo": "retroceso", "valor": 0.5, "param": "tp_retrace",
                        "base": "close − 50 % del movimiento de los últimos 60 min", **_ATR_1M},
        "tiempo": _t(4, "h", "max_hold"), "otra": None,
        "nota": "Stop bajo el extremo de la cascada, objetivo en el 50 % de retroceso, máximo 4 h.",
        "cierre": "tiempo", "giro": "tiempo",
    },
    "squeeze-short-v1": {
        "stop": {"tipo": "sigma", "valor": 0.5, "param": "sl_sigma",
                 "base": "máximo de la vela de 15m de la señal + 0,5 σ de 60m (en precio)", **_ATR_1M},
        "take_profit": {"tipo": "retroceso", "valor": 0.5, "param": "tp_retrace",
                        "base": "close − 50 % del movimiento de los últimos 60 min", **_ATR_1M},
        "tiempo": _t(4, "h", "max_hold"), "otra": None,
        "nota": "Espejo de capitulación: stop sobre el extremo, objetivo en el 50 % de retroceso, máximo 4 h.",
        "cierre": "tiempo", "giro": "tiempo",
    },
    "funding-crowding-xs-v1": {
        "stop": None, "take_profit": None, "tiempo": None,
        "otra": {"tipo": "composicion", "detalle": "en cada liquidación, con al menos 24 h desde el último cambio, se "
                 "re-arma long al menor z-score de funding y short al mayor; sale el que deja de ser extremo",
                 "valor": 24, "unidad": "h", "param": "hold_hours"},
        "nota": "Sin stop: sale por cambio de composición (mínimo 24 h).", "cierre": "rebalanceo", "giro": "rebalanceo",
    },
    "funding-crowding-short-v1": {
        "stop": {"tipo": "sigma", "valor": 2.0, "param": "sl_sigma",
                 "base": "close de 1h × (1 + 2 σ de los retornos de 4h)", **_ATR_1M},
        "take_profit": None, "tiempo": _t(24, "h", "hold_hours"), "otra": None,
        "nota": "Stop a 2 σ sobre la entrada, sin objetivo, cierra a las 24 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "funding-spread-hl-cex-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(72, "h", "max_hold_hours"),
        "otra": {"tipo": "senal", "detalle": "el spread de funding vuelve bajo su p50", "valor": 0.5, "param": "exit_pct"},
        "nota": "Sin backtest (necesita la grabación multi-exchange): sale cuando el spread se normaliza o a las 72 h.",
        "cierre": "senal", "giro": "senal",
    },
    "absorcion-flujo-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(1, "h", None, "código: H = 1 h fijo en la regla"), "otra": None,
        "nota": "Control: sin stop ni objetivo, cierra a la hora.", "cierre": "tiempo", "giro": "tiempo",
    },
    "compresion-breakout-v1": {
        "stop": {"tipo": "rango", "valor": None, "param": None,
                 "base": "lado opuesto del rango de la racha comprimida", **_ATR_1M},
        "take_profit": None, "tiempo": _t(12, "h", "hold_hours"), "otra": None,
        "nota": "Stop en el otro lado del rango, sin objetivo, cierra a las 12 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "momentum-intradia-v1": {
        "stop": None, "take_profit": None,
        "tiempo": _t(30, "min", None, "de las 23:30 a las 00:00 UTC (1 día − entry)"), "otra": None,
        "nota": "Sin stop ni objetivo: abre 23:30 y cierra 00:00 UTC.", "cierre": "tiempo", "giro": "tiempo",
    },
    "oi-precio-continuacion-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(8, "h", "hold_hours"), "otra": None,
        "nota": "Sin stop ni objetivo: cierra a las 8 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "carry-xs-funding-v1": {
        "stop": None, "take_profit": None, "tiempo": None,
        "otra": {"tipo": "composicion", "detalle": "en cada liquidación se re-ordena por funding promedio; sale el "
                 "símbolo que deja de estar entre los 2 de menor o mayor funding"},
        "nota": "Sin stop: se queda hasta que cambia la composición.", "cierre": "rebalanceo", "giro": "rebalanceo",
    },
    "lunes-asia-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(24, "h", "hold_hours"), "otra": None,
        "nota": "Observación: sin stop, cierra a las 24 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "tsmom-diario-v1": {
        "stop": None, "take_profit": None, "tiempo": None,
        "otra": {"tipo": "senal", "detalle": "cambio de signo del momentum de 7 días (señal contraria); el tamaño se "
                 "rebalancea a las 00:00 UTC si cambia más de 20 % relativo", "valor": 0.2, "param": "rebalance_threshold"},
        "nota": "Sin stop: sale con la señal contraria (siempre posicionada).", "cierre": "senal", "giro": "senal",
    },
    "low-vol-xs-v1": {
        "stop": None, "take_profit": None, "tiempo": None,
        "otra": {"tipo": "composicion", "detalle": "cada día a las 00:00 UTC se re-arma la canasta (2 de menor y 2 de "
                 "mayor volatilidad); sale el que cambia de grupo"},
        "nota": "Sin stop: rebalanceo diario.", "cierre": "rebalanceo", "giro": "rebalanceo",
    },
    "pares-eth-btc-v1": {
        "stop": {"tipo": "z", "valor": 4.0, "param": "stop_z", "base": "|z| del spread al cierre de la vela de 1h",
                 "velas_1m": False, "empate": None},
        "take_profit": {"tipo": "z", "valor": 0.5, "param": "exit_z", "base": "|z| < 0,5: el spread volvió a la media",
                        "velas_1m": False, "empate": None},
        "tiempo": _t(72, "h", "max_hold_hours"), "otra": None,
        "nota": "Stop si el spread se estira a |z| > 4, objetivo en |z| < 0,5, máximo 72 h (velas de 1h).",
        "cierre": "senal", "giro": "senal", "patas": "leg_y",
    },
    "rango-reversion-v1": {
        "stop": {"tipo": "atr", "valor": 1.5, "param": "sl_atr", "base": "ATR(14) de 1h desde el close de entrada", **_ATR_1M},
        "take_profit": {"tipo": "ema", "valor": 20, "param": "ema_n", "base": "EMA20 de 1h al momento de entrada (nivel fijo)",
                        **_ATR_1M},
        "tiempo": _t(12, "h", "hold_hours"), "otra": None,
        "nota": "Stop 1,5 ATR, objetivo en la EMA20, máximo 12 h.", "cierre": "tiempo", "giro": "tiempo",
    },
    "reversion-xs-1h-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(4, "h", "hold_hours"), "otra": None,
        "nota": "Sin stop ni objetivo: cierra a las 4 h (la pata BTC es la cobertura).", "cierre": "tiempo", "giro": "tiempo",
        "mismo_signo": "tiempo", "excluir": "btc",
    },
    "funding-prepago-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(120, "min", "pre_minutes+post_minutes"),
        "otra": {"tipo": "programada", "detalle": "en la liquidación T da vuelta la posición (60 min antes y 60 min después)",
                 "valor": 60, "unidad": "min", "param": "pre_minutes"},
        "nota": "Sin stop ni objetivo: 60 min antes de la liquidación, giro en T, cierra 60 min después.",
        "cierre": "tiempo", "giro": "senal",
    },
    "ema-cruce-v1": {
        "stop": {"tipo": "atr", "valor": 1.5, "param": "sl_atr", "base": "ATR(14) de la temporalidad (1h o 4h) desde el close de entrada",
                 **_ATR_1M},
        "take_profit": {"tipo": "atr", "valor": 3.0, "param": "rr*sl_atr", "base": "R:R 1:2 (2 × el stop)", **_ATR_1M},
        "tiempo": _t(720, "h", "max_hold_hours", "tope de seguridad"), "otra": None,
        "nota": "Stop 1,5 ATR y objetivo 3 ATR; el tope de 30 días es solo de seguridad.", "cierre": "tiempo", "giro": "tiempo",
    },
    "ema-cruce-filtrado-v1": {
        "stop": {"tipo": "atr", "valor": 1.5, "param": "sl_atr", "base": "ATR(14) de la temporalidad (1h o 4h) desde el close de entrada",
                 **_ATR_1M},
        "take_profit": {"tipo": "atr", "valor": 3.0, "param": "rr*sl_atr", "base": "R:R 1:2 (2 × el stop)", **_ATR_1M},
        "tiempo": _t(720, "h", "max_hold_hours", "tope de seguridad"), "otra": None,
        "nota": "Igual que ema-cruce-v1: stop 1,5 ATR, objetivo 3 ATR, tope 30 días.", "cierre": "tiempo", "giro": "tiempo",
    },
    "cartera-tendencia-v1": {
        "stop": None, "take_profit": None, "tiempo": None, "otra": None,
        "hereda": ["donchian-ensemble-v1", "tsmom-diario-v1", "ema-cruce-v1"],
        "nota": "Hereda las salidas de sus 3 componentes (cada uno con sus parámetros por defecto).",
        "cierre": "senal", "giro": "senal",
    },
    "max-ruptura-10d-v1": {
        "stop": None, "take_profit": None, "tiempo": _t(1, "d", "hold_days"), "otra": None,
        "nota": "Sin stop ni objetivo: cierra al día.", "cierre": "tiempo", "giro": "tiempo",
    },
    "vwap-reversion-v1": {
        "stop": {"tipo": "atr", "valor": 1.5, "param": "sl_atr", "base": "ATR(14) de 15m desde el close de entrada", **_ATR_1M},
        "take_profit": {"tipo": "vwap", "valor": None, "param": None, "base": "VWAP del día UTC al momento de entrada (nivel fijo)",
                        **_ATR_1M},
        "tiempo": _t(4, "h", "hold_hours"), "otra": None,
        "nota": "Stop 1,5 ATR, objetivo en el VWAP, máximo 4 h.", "cierre": "tiempo", "giro": "tiempo",
    },
}

# Freqtrade (paper): lo configurado en cada estrategia (freqtrade/**/strategies). Un test lo compara con los archivos.
FREQTRADE_EXITS: dict[str, dict[str, Any]] = {
    "DonchianEnsembleV1": {
        "strategy_id": "donchian-ensemble-v1", "stoploss": -0.99, "minimal_roi": {"0": 100}, "trailing_stop": False,
        "nota": "Sin stop (−0,99, como el laboratorio) y sin ROI: sale por la señal de canal (exit_signal).",
    },
    "HypothesisBridgeV1": {
        "strategy_id": None, "stoploss": -0.20, "minimal_roi": {"0": 100.0}, "trailing_stop": False,
        "nota": "Sale por tiempo (custom_exit 'hyp_due' al vencer la hipótesis). Stop de SEGURIDAD −20 %: no es parte de "
                "la hipótesis (el Resolver no tiene stop); si salta figura como 'stop_loss'. ROI desactivado.",
    },
}
FT_EXIT_REASON_LABEL = {"exit_signal": "señal", "stop_loss": "stop", "stoploss_on_exchange": "stop (exchange)",
                        "trailing_stop_loss": "stop móvil", "roi": "objetivo (ROI)", "hyp_due": "tiempo (vencimiento)",
                        "hyp_tag_invalido": "tag inválido", "force_exit": "cierre forzado", "liquidation": "liquidación",
                        "emergency_exit": "salida de emergencia", "custom_exit": "salida propia"}

# Hipotesis del grafo: todas salen por tiempo (pre-registradas asi; el Resolver no tiene stop ni objetivo).
HYPOTHESIS_EXIT = {"stop": None, "take_profit": None, "otra": None,
                   "nota": "Por tiempo: se resuelve con el precio al vencimiento. Sin stop ni objetivo (pre-registrada así)."}

# Stops del corte descriptivo de MAE (hipotesis): NO pre-registrados, NO se usan para decidir nada.
DESCRIPTIVE_STOPS_ATR = (1.0, 1.5, 2.0)


# ---- textos -----------------------------------------------------------------------------------------------------


def _n(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:g}".replace(".", ",") if isinstance(v, (int, float)) else str(v)


def time_text(t: dict[str, Any] | None) -> str | None:
    if not t:
        return None
    v, u = t["valor"], t["unidad"]
    if u == "min" and v >= 60 and v % 60 == 0:
        v, u = v // 60, "h"
    if u == "h" and v >= 48 and v % 24 == 0:
        v, u = v // 24, "d"
    return f"{_n(v)} {u}"


def stop_text(s: dict[str, Any] | None) -> str | None:
    if not s:
        return None
    k, v = s["tipo"], s.get("valor")
    return {"atr": f"stop {_n(v)} ATR", "sigma": f"stop {_n(v)} σ", "z": f"stop |z| > {_n(v)}", "pct": f"stop {_n(v)} %",
            "rango": "stop: otro lado del rango"}.get(k, f"stop {k}")


def tp_text(s: dict[str, Any] | None) -> str | None:
    if not s:
        return None
    k, v = s["tipo"], s.get("valor")
    if k == "retroceso":
        return f"objetivo {_n(round(v * 100))} % de retroceso"
    return {"atr": f"objetivo {_n(v)} ATR", "z": f"objetivo |z| < {_n(v)}", "ema": f"objetivo EMA{_n(v)}",
            "vwap": "objetivo VWAP", "pct": f"objetivo {_n(v)} %"}.get(k, f"objetivo {k}")


def chips(f: dict[str, Any] | None) -> list[str]:
    """'stop 1,5 ATR · objetivo 3 ATR · máx. 30 d' o 'sin stop: canal' como lista de textos cortos."""
    if not f:
        return ["sin ficha"]
    if f.get("hereda"):
        return [f"hereda de {len(f['hereda'])} componentes"]
    out: list[str] = []
    st, tp, t, o = stop_text(f.get("stop")), tp_text(f.get("take_profit")), time_text(f.get("tiempo")), f.get("otra")
    otra = OTRA_LABEL.get(o["tipo"], o["tipo"]) if o else None
    if st:
        out.append(st)
    elif otra:
        out.append("sin stop: " + otra)
        otra = None
    elif t:
        out.append("sin stop: tiempo " + t)
        t = None
    else:
        out.append("sin stop")
    if tp:
        out.append(tp)
    if t:
        out.append("máx. " + t)
    if otra:
        out.append(otra)
    return out


def ficha_json(strategy_id: str) -> dict[str, Any] | None:
    """Ficha para el panel: los campos + chips + resumen; la cartera trae la ficha de cada componente."""
    f = EXITS.get(strategy_id)
    if f is None:
        return None
    out = {k: v for k, v in f.items() if k not in ("hereda",)}
    out["strategy_id"] = strategy_id
    out["chips"] = chips(f)
    out["resumen"] = " · ".join(out["chips"])
    out["tiempo_texto"] = time_text(f.get("tiempo"))
    if f.get("hereda"):
        out["componentes"] = [ficha_json(c) for c in f["hereda"]]
    return out


def all_fichas() -> dict[str, dict[str, Any]]:
    return {sid: ficha_json(sid) for sid in EXITS}  # type: ignore[misc]


def freqtrade_exits(strategy: str | None, show_config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Stop y ROI de un bot de Freqtrade: lo de la API (`show_config`) si respondio; si no, lo de la estrategia."""
    base = FREQTRADE_EXITS.get(strategy or "")
    cfg = show_config or {}
    if base is None and not cfg:
        return None
    base = base or {}
    api_sl, api_roi = cfg.get("stoploss"), cfg.get("minimal_roi")
    sl = api_sl if isinstance(api_sl, (int, float)) else base.get("stoploss")
    roi = api_roi if isinstance(api_roi, dict) else base.get("minimal_roi")
    roi_off = isinstance(roi, dict) and roi and all(isinstance(v, (int, float)) and v >= 10 for v in roi.values())
    return {"strategy": strategy, "stoploss": sl, "minimal_roi": roi, "trailing_stop": cfg.get("trailing_stop", base.get("trailing_stop")),
            "source": "api" if isinstance(api_sl, (int, float)) else "estrategia",
            "stop_text": None if sl is None else ("sin stop (−99 %)" if sl <= -0.98 else f"stop {_n(round(sl * 100, 2))} %".replace("-", "−")),
            "roi_text": None if roi is None else ("ROI desactivado" if roi_off else "ROI " + ", ".join(
                f"{k} min: {_n(round(v * 100, 2))} %" for k, v in roi.items())),
            "nota": base.get("nota")}


def check_params(strategy_id: str, params: dict[str, Any]) -> list[str]:
    """Diferencias entre los valores numericos de la ficha y `params` (vacio = coincide). Lo usa el test."""
    import re

    def value(expr: str, unidad: str | None) -> float:
        tot = 0.0
        for term in expr.split("+"):
            prod = 1.0
            for name in term.split("*"):
                v = params[name.strip()]
                if isinstance(v, str):  # '4h', '1h': se convierte a la unidad de la ficha
                    m = re.fullmatch(r"(\d+(?:\.\d+)?)(h|min|d)", v.strip())
                    if not m:
                        raise ValueError(f"{name}={v!r} no es una duracion simple")
                    mins = float(m.group(1)) * {"h": 60, "min": 1, "d": 1440}[m.group(2)]
                    v = mins / {"h": 60, "min": 1, "d": 1440}[unidad or "h"]
                prod *= float(v)
            tot += prod
        return tot

    f = EXITS[strategy_id]
    bad: list[str] = []
    for key in ("stop", "take_profit", "tiempo", "otra"):
        part = f.get(key)
        if not part or not part.get("param") or part.get("valor") is None:
            continue
        if part["param"] == "channels":
            if "channels" not in params:
                bad.append(f"{key}: falta channels")
            continue
        try:
            got = value(part["param"], part.get("unidad"))
        except (KeyError, ValueError) as exc:
            bad.append(f"{key}: {exc}")
            continue
        if not math.isclose(got, float(part["valor"]), rel_tol=1e-9, abs_tol=1e-12):
            bad.append(f"{key}: ficha {part['valor']} != params {part['param']} = {got}")
    return bad


# ---- atribucion -----------------------------------------------------------------------------------------------------


def exit_reasons(entries, k1m, horizon, tp=None, sl=None) -> list[dict[str, Any]]:
    """Misma logica que `strategies.exits_with_levels` (que NO se modifica), devolviendo el motivo de cada salida.

    Una posicion por vez; niveles evaluados en velas 1m desde la vela que abre en la decision; si en la misma vela
    se tocan objetivo y stop -> stop (conservador); salida decidida al cierre de la vela que toca (t_vela + 1m);
    sin toque -> por tiempo en t + horizon."""
    import numpy as np
    import pandas as pd

    one = pd.Timedelta(minutes=1)
    out: list[dict[str, Any]] = []
    busy = pd.Timestamp.min.tz_localize("UTC")
    highs, lows = k1m["high"], k1m["low"]
    for t, d in entries[entries != 0].items():
        if t < busy:
            continue
        d = float(np.sign(d))
        end = t + horizon
        hi, lo = highs.loc[t:end - one], lows.loc[t:end - one]
        tp_lvl = None if tp is None else tp.get(t)
        sl_lvl = None if sl is None else sl.get(t)
        hit_sl = hit_tp = None
        if sl_lvl is not None and np.isfinite(sl_lvl):
            m = (lo <= sl_lvl) if d > 0 else (hi >= sl_lvl)
            hit_sl = m.idxmax() if m.any() else None
        if tp_lvl is not None and np.isfinite(tp_lvl):
            m = (hi >= tp_lvl) if d > 0 else (lo <= tp_lvl)
            hit_tp = m.idxmax() if m.any() else None
        if hit_sl is not None and (hit_tp is None or hit_sl <= hit_tp):
            exit_t, motivo = hit_sl + one, "stop"
        elif hit_tp is not None:
            exit_t, motivo = hit_tp + one, "objetivo"
        else:
            exit_t, motivo = end, "tiempo"
        out.append({"entry": t, "exit": exit_t, "side": d, "motivo": motivo,
                    "sl": None if sl_lvl is None or not np.isfinite(sl_lvl) else float(sl_lvl),
                    "tp": None if tp_lvl is None or not np.isfinite(tp_lvl) else float(tp_lvl)})
        busy = exit_t
    return out


def hold_reasons(entries, horizon) -> list[dict[str, Any]]:
    """Misma logica que `strategies.hold_events`: todas salen por tiempo."""
    import numpy as np
    import pandas as pd

    out: list[dict[str, Any]] = []
    busy = pd.Timestamp.min.tz_localize("UTC")
    for t, d in entries[entries != 0].items():
        if t < busy:
            continue
        out.append({"entry": t, "exit": t + horizon, "side": float(np.sign(d)), "motivo": "tiempo"})
        busy = t + horizon
    return out


def series_from_trades(trades: list[dict[str, Any]]):
    """Reconstruye la Series de cambios de posicion como la arman exits_with_levels / hold_events."""
    from .strategies import _changes

    out: dict[Any, float] = {}
    for tr in trades:
        out[tr["entry"]] = tr["side"]
        out[tr["exit"]] = 0.0
    return _changes(out)


@contextmanager
def capture_exits():
    """Envuelve `exits_with_levels` y `hold_events` del modulo de reglas mientras dura el bloque: cada llamada se
    registra con sus trades reconstruidos (con motivo) y si la reconstruccion coincide con el resultado real. Las
    reglas reciben exactamente el mismo resultado que sin envoltura."""
    from . import strategies as S

    rec: list[dict[str, Any]] = []
    orig_l, orig_h = S.exits_with_levels, S.hold_events

    def ewl(entries, k1m, horizon, tp=None, sl=None):
        res = orig_l(entries, k1m, horizon, tp=tp, sl=sl)
        trades = exit_reasons(entries, k1m, horizon, tp=tp, sl=sl)
        rec.append({"kind": "niveles", "key": id(k1m), "trades": trades, "coincide": series_from_trades(trades).equals(res)})
        return res

    def he(entries, horizon, index):
        res = orig_h(entries, horizon, index)
        trades = hold_reasons(entries, horizon)
        rec.append({"kind": "tiempo", "key": id(index), "trades": trades, "coincide": series_from_trades(trades).equals(res)})
        return res

    S.exits_with_levels, S.hold_events = ewl, he
    try:
        yield rec
    finally:
        S.exits_with_levels, S.hold_events = orig_l, orig_h


def _empty_counts() -> dict[str, int]:
    return {m: 0 for m in MOTIVOS}


def _pares_motivo(data, params, t, t_in) -> str:
    """pares-eth-btc: el motivo de un cierre en t se reconstruye con el z del spread (misma funcion de la regla)."""
    import numpy as np
    import pandas as pd

    from .strategies import _hourly_close, pair_zscore

    if t_in is not None and t >= t_in + pd.Timedelta(hours=params["max_hold_hours"]):
        return "tiempo"
    cy, cx = _hourly_close(data[params["leg_y"]].klines), _hourly_close(data[params["leg_x"]].klines)
    zb = pair_zscore(np.log(cy), np.log(cx), params["window_hours"])
    z = zb["z"].get(t)
    if z is not None and np.isfinite(z):
        if abs(z) < params["exit_z"]:
            return "objetivo"
        if abs(z) > params["stop_z"]:
            return "stop"
    return "senal"


def _transitions(sid: str, ficha: dict[str, Any], signals: dict[str, Any], data, params, end) -> dict[str, Any]:
    """Clasifica cierres por las transiciones de la senal (reglas sin niveles ni horizonte fijo)."""
    import numpy as np

    counts, by_sym = _empty_counts(), {}
    open_end = adjust = 0
    skip = set()
    if ficha.get("excluir") and params.get(ficha["excluir"]):
        skip.add(params[ficha["excluir"]])
    only = params.get(ficha["patas"]) if ficha.get("patas") else None
    for sym, sig in signals.items():
        if sym in skip or (only and sym != only) or sig is None:
            continue
        s = sig.dropna().sort_index(kind="stable")
        s = s[~s.index.duplicated(keep="last")]
        prev, t_in = 0.0, None
        c = by_sym.setdefault(sym, _empty_counts())
        for t, w in s.items():
            w = float(w)
            if prev != 0 and (w == 0 or np.sign(w) != np.sign(prev)):
                if sid == "pares-eth-btc-v1":
                    m = _pares_motivo(data, params, t, t_in)
                else:
                    m = ficha.get("cierre" if w == 0 else "giro") or "senal"
                counts[m] += 1
                c[m] += 1
                t_in = t if w != 0 else None
            elif prev != 0 and w != prev:
                if ficha.get("mismo_signo"):  # cierre por tiempo y re-entrada en la misma vela, mismo lado
                    counts[ficha["mismo_signo"]] += 1
                    c[ficha["mismo_signo"]] += 1
                else:
                    adjust += 1
            elif prev == 0 and w != 0:
                t_in = t
            prev = w
        if prev != 0:
            open_end += 1
    return {"por_motivo": counts, "por_simbolo": by_sym, "abiertas_al_final": open_end, "ajustes": adjust,
            "metodo": "transiciones de la señal clasificadas con la ficha"}


def reason_map(strategy_id: str, data: dict[str, Any], signals: dict[str, Any], rec: list[dict[str, Any]],
               params: dict[str, Any]) -> dict[tuple[str, Any], str]:
    """(simbolo, instante de decision) -> motivo de cada cierre o giro de la senal. Lo usa el bot de paper para
    registrar por que cerro cada orden. `rec` = lo capturado por `capture_exits` al correr la regla."""
    import numpy as np

    ficha = EXITS.get(strategy_id, {"cierre": "senal", "giro": "senal"})
    keys = {}
    for s, md in data.items():
        keys[id(md.klines)] = s
        keys[id(md.klines.index)] = s
    out: dict[tuple[str, Any], str] = {}
    for call in rec:
        sym = keys.get(call["key"])
        for tr in call["trades"]:
            out[(sym, tr["exit"])] = tr["motivo"]
    for sym, sig in signals.items():
        if sig is None:
            continue
        s = sig.dropna().sort_index(kind="stable")
        s = s[~s.index.duplicated(keep="last")]
        prev, t_in = 0.0, None
        for t, w in s.items():
            w = float(w)
            if prev != 0 and (w == 0 or np.sign(w) != np.sign(prev)) and (sym, t) not in out:
                if strategy_id == "pares-eth-btc-v1":
                    out[(sym, t)] = _pares_motivo(data, params, t, t_in)
                else:
                    out[(sym, t)] = ficha.get("cierre" if w == 0 else "giro") or "senal"
            if (prev == 0 and w != 0) or (prev != 0 and w != 0 and np.sign(w) != np.sign(prev)):
                t_in = t
            prev = w
    return out


def attribute(strategy_id: str, rule, data: dict[str, Any], params: dict[str, Any],
              ficha: dict[str, Any] | None = None) -> dict[str, Any]:
    """Atribucion de salidas de una regla sobre `data` (el que llama corta el holdout ANTES). No simula nada."""
    import pandas as pd

    ficha = ficha if ficha is not None else EXITS.get(strategy_id, {"cierre": "senal", "giro": "senal"})
    if ficha.get("hereda"):
        from .strategies import CATALOG

        comps = {c: attribute(c, CATALOG[c][1], data, dict(CATALOG[c][0].params)) for c in ficha["hereda"]}
        tot = _empty_counts()
        for r in comps.values():
            for m in MOTIVOS:
                tot[m] += r["por_motivo"][m]
        return {"strategy_id": strategy_id, "por_motivo": tot, "trades": sum(tot.values()),
                "abiertas_al_final": sum(r["abiertas_al_final"] for r in comps.values()),
                "ajustes": sum(r["ajustes"] for r in comps.values()),
                "metodo": "hereda: suma de los componentes (cada uno con sus parámetros por defecto)",
                "verificacion": {"coincide": all(r["verificacion"]["coincide"] for r in comps.values()),
                                 "llamadas": sum(r["verificacion"]["llamadas"] for r in comps.values())},
                "componentes": comps, "ficha": ficha_json(strategy_id)}
    ends = [md.klines.index[-1] + pd.Timedelta(minutes=1) for md in data.values() if md.klines is not None and len(md.klines)]
    end = max(ends) if ends else None
    with capture_exits() as rec:
        signals = rule(data, dict(params))
    keys = {}
    for s, md in data.items():
        keys[id(md.klines)] = s
        keys[id(md.klines.index)] = s
    if rec:
        counts, by_sym, open_end = _empty_counts(), {}, 0
        for call in rec:
            sym = keys.get(call["key"], "?")
            c = by_sym.setdefault(sym, _empty_counts())
            for tr in call["trades"]:
                if end is not None and tr["exit"] > end:
                    open_end += 1
                    continue
                counts[tr["motivo"]] += 1
                c[tr["motivo"]] += 1
        out = {"por_motivo": counts, "por_simbolo": by_sym, "abiertas_al_final": open_end, "ajustes": 0,
               "metodo": "niveles reconstruidos (misma lógica que exits_with_levels / hold_events, verificada llamada por llamada)"}
        ok = all(c["coincide"] for c in rec)
    else:
        out = _transitions(strategy_id, ficha, signals, data, params, end)
        ok = True
    out.update({"strategy_id": strategy_id, "trades": sum(out["por_motivo"].values()),
                "verificacion": {"coincide": ok, "llamadas": len(rec)}, "ficha": ficha_json(strategy_id)})
    return out
