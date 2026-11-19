"""lab.publish --local / --if-changed (2026-11-19): el panel corre en la PC y lee los detalles de su dir de datos."""

from __future__ import annotations

import os
import time

from heatsignal.lab import publish


def test_needs_publish_only_with_newer_results(tmp_path) -> None:
    home, dest = tmp_path / "lab", tmp_path / "panel"
    (home / "results" / "x-v1").mkdir(parents=True)
    dest.mkdir()
    assert publish.needs_publish(home)  # nunca se publico
    (home / "lab_summary.json").write_text("{}")
    assert publish.needs_publish(home, dest)  # falta en el panel
    publish.copy_to([home / "lab_summary.json"], dest)
    assert not publish.needs_publish(home, dest)
    r = home / "results" / "x-v1" / "run.json"
    r.write_text("{}")
    later = time.time() + 5
    os.utime(r, (later, later))
    assert publish.needs_publish(home, dest)


def test_copy_to_is_atomic_and_overwrites(tmp_path) -> None:
    src = tmp_path / "lab_detail_a-v1.json"
    src.write_text('{"v": 2}')
    dest = tmp_path / "data"
    dest.mkdir()
    (dest / src.name).write_text('{"v": 1}')
    publish.copy_to([src], dest)
    assert (dest / src.name).read_text() == '{"v": 2}'
    assert not list(dest.glob(".*.tmp"))


def test_local_panel_dir_follows_eval_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HEATSIGNAL_EVAL_DB", str(tmp_path / "d" / "eval.sqlite"))
    assert publish.local_panel_dir() == tmp_path / "d"
