"""
Parametros del bot: los congelados del spec + lo elegido de la grilla en `params.json` (versionado).

Reglas (anti p-hacking, decisions.md):
  - Solo se pueden fijar claves de la grilla del spec y solo con valores de esa grilla.
  - El `spec_hash` de `params.json` tiene que coincidir con el del catalogo: si la regla cambio, es otra estrategia
    y el bot se niega a correr hasta que alguien revise el archivo.
  - Nada se optimiza en vivo. Cuando el walk-forward del laboratorio elija, `params_from_lab_detail` toma el ultimo
    `selected_params` publicado (holdout si se abrio; si no, la ultima ventana del walk-forward) y deja el origen.
Sin pandas (lo importa tambien el panel para mostrar el origen).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PARAMS_PATH = Path(__file__).with_name("params.json")


class ParamsError(Exception):
    pass


def load(path: Path | str = PARAMS_PATH) -> dict[str, dict[str, Any]]:
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParamsError(f"no se pudo leer {path}: {exc}") from exc
    strategies = cfg.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise ParamsError(f"{path}: falta 'strategies'")
    return strategies


def effective(strategy_id: str, entry: dict[str, Any], spec: Any) -> dict[str, Any]:
    """Parametros finales = spec.params pisados por la eleccion de la grilla. Valida hash, claves y valores."""
    if entry.get("spec_hash") != spec.spec_hash():
        raise ParamsError(f"{strategy_id}: spec_hash de params.json ({entry.get('spec_hash')}) != catalogo "
                          f"({spec.spec_hash()}): la regla cambio; revisar params.json antes de seguir")
    chosen = entry.get("params") or {}
    for k, v in chosen.items():
        if k not in spec.grid:
            raise ParamsError(f"{strategy_id}: '{k}' no esta en la grilla congelada {sorted(spec.grid)}")
        if v not in spec.grid[k]:
            raise ParamsError(f"{strategy_id}: {k}={v!r} fuera de la grilla {spec.grid[k]}")
    missing = [k for k in spec.grid if k not in chosen]
    if missing:
        raise ParamsError(f"{strategy_id}: falta elegir {missing} de la grilla en params.json")
    universe = entry.get("universe") or []
    if not universe:
        raise ParamsError(f"{strategy_id}: falta 'universe' en params.json")
    return {**spec.params, **chosen}


def params_from_lab_detail(detail: dict[str, Any], spec: Any) -> tuple[dict[str, Any], str]:
    """(params de la grilla, origen) a partir de un `lab_detail_<id>.json` publicado por `lab.publish`.

    Prioridad: `holdout.selected_params` (si el holdout ya se abrio) > ultima ventana de `walk_forward.chosen`.
    Falla si el detalle es de otra version de la regla o no trae eleccion."""
    if detail.get("spec_hash") != spec.spec_hash():
        raise ParamsError(f"el detalle del laboratorio es de otra version de la regla ({detail.get('spec_hash')} != "
                          f"{spec.spec_hash()})")
    run = detail.get("run") or "?"
    hold = detail.get("holdout") or {}
    if isinstance(hold.get("selected_params"), dict):
        return dict(hold["selected_params"]), f"selected_params del holdout del laboratorio (corrida {run})"
    chosen = ((detail.get("walk_forward") or {}).get("chosen")) or []
    if chosen and isinstance(chosen[-1].get("params"), dict):
        test = chosen[-1].get("test") or ["?", "?"]
        return dict(chosen[-1]["params"]), (f"walk-forward del laboratorio, ultima ventana (test {test[0]} a {test[1]}, "
                                            f"corrida {run})")
    raise ParamsError("el detalle del laboratorio no trae selected_params ni ventanas del walk-forward")


def write_choice(strategy_id: str, params: dict[str, Any], origin: str, spec: Any, path: Path | str = PARAMS_PATH) -> None:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entry = raw["strategies"][strategy_id]
    entry.update({"spec_hash": spec.spec_hash(), "params": params, "origen": origin,
                  "desde": datetime.now(timezone.utc).date().isoformat()})
    effective(strategy_id, entry, spec)  # valida antes de escribir
    Path(path).write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
