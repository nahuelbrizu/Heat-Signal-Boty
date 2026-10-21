"""Estres de costos TRX/XLM 7 bps (lab/costos.py) en el detalle publicado (lab/publish.py)."""

from __future__ import annotations

import json

from heatsignal.lab import publish


def _w(folder, name, rep):
    (folder / name).write_text(json.dumps(rep))


def test_costos_block_en_detalle(tmp_path):
    f = tmp_path / "x-v1"
    f.mkdir()
    _w(f, "diagnostics_20261019T000000.json", {"run": "20261019T000000", "generated_at": "a",
                                               "robustez": {"base": {"sharpe": 1.0, "sharpe_x3": 0.8}}})
    _w(f, "diagnostics_costos_20261021T000000.json", {
        "run": "20261019T000000", "generated_at": "b",
        "costos": {"label": "costos realistas TRX/XLM 7 bps", "sharpe_dev": 1.0, "sharpe_dev_estres": 0.9, "delta": -0.1,
                   "costos_estres": {"slippage_bps": {"TRXUSDT": 7.0, "XLMUSDT": 7.0}}}})
    b = publish.diagnostics_block(f, "20261019T000000", full=True)
    assert b["sharpe_dev"] == 1.0  # la robustez sigue saliendo del diagnostico anterior
    c = b["costos"]
    assert c["label"] == "costos realistas TRX/XLM 7 bps"
    assert (c["sharpe_dev"], c["sharpe_dev_estres"], c["stale"]) == (1.0, 0.9, False)
    assert publish.diagnostics_block(f, "otro", full=False)["costos"]["stale"] is True


def test_solo_costos_y_sin_costos(tmp_path):
    f = tmp_path / "y-v1"
    f.mkdir()
    assert publish.diagnostics_block(f, None) is None
    _w(f, "diagnostics_costos_20261021T000000.json", {"run": "r", "costos": {"sharpe_dev": -0.5, "sharpe_dev_estres": -0.6}})
    b = publish.diagnostics_block(f, "r", full=True)
    assert b["costos"]["sharpe_dev_estres"] == -0.6 and b["pbo"] is None and b["salidas"] is None
