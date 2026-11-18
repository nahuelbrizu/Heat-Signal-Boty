"""Cola local del worker del laboratorio (sin SSH): claim -> resultado en lab_requests/ -> done, contra una base real."""

from __future__ import annotations

import json
from pathlib import Path

from heatsignal.evaluation.lab_requests import LabRequestStore
from heatsignal.lab import worker as W
from heatsignal.lab.strategies import CATALOG


def _fake_eval(cfg, registry):
    return {"oos": {"sharpe": 1.0, "trades": 3}, "baselines": {}, "verdict": {"pasa": False}}


def _backtestable_id() -> str:
    return next(sid for sid, (spec, _) in CATALOG.items() if spec.backtestable)


def test_local_remote_full_cycle(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "data" / "heatsignal_eval.sqlite"
    store = LabRequestStore(db)
    rid = store.create([_backtestable_id()], ["BTCUSDT"], note="prueba")["request_id"]
    store.close()

    monkeypatch.setattr(W, "detail", lambda report, files: {"ok": True})
    out = W.run_once(remote=W.LocalRemote(db), home=tmp_path / "lab", registry=None, evaluate_fn=_fake_eval)

    assert len(out) == 1 and "error" not in out[0]
    result = db.parent / "lab_requests" / f"{rid}.json"
    payload = json.loads(result.read_text())
    assert payload["holdout"].startswith("SELLADO")
    assert payload["strategies"][0]["status"] == "ok"
    assert not list(result.parent.glob(".*.tmp"))  # escritura atomica, sin restos
    store = LabRequestStore(db)
    try:
        assert store.get(rid)["status"] == "done"
    finally:
        store.close()


def test_local_remote_all_fail_marks_error(tmp_path: Path) -> None:
    db = tmp_path / "heatsignal_eval.sqlite"
    store = LabRequestStore(db)
    rid = store.create(["no-existe-v1"], ["BTCUSDT"])["request_id"]
    store.close()

    W.run_once(remote=W.LocalRemote(db), home=tmp_path, registry=None, evaluate_fn=_fake_eval)
    store = LabRequestStore(db)
    try:
        assert store.get(rid)["status"] == "error"
    finally:
        store.close()


def test_local_remote_empty_queue(tmp_path: Path) -> None:
    db = tmp_path / "heatsignal_eval.sqlite"
    LabRequestStore(db).close()
    assert W.run_once(remote=W.LocalRemote(db), home=tmp_path, registry=None, evaluate_fn=_fake_eval) == []
