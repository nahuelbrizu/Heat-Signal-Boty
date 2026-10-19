"""Monitor del event loop (ingestion/loop_monitor.py): retraso medido, executors instrumentados, tiempos por ruta."""

from __future__ import annotations

import asyncio
import threading
import time

from heatsignal.ingestion import loop_monitor
from heatsignal.ingestion.loop_monitor import InstrumentedExecutor, LoopMonitor, RouteTimer, run_heavy


async def test_lag_is_measured_when_the_loop_is_blocked() -> None:
    mon = LoopMonitor(interval_s=0.05, log_every_s=0.3)
    task = asyncio.create_task(mon.run())
    await asyncio.sleep(0.12)
    time.sleep(0.25)  # bloquea el loop: la proxima vuelta llega ~250 ms tarde
    await asyncio.sleep(0.5)
    task.cancel()
    assert mon.max_lag_ms >= 150
    assert mon.minutes and mon.minutes[0]["max_ms"] is not None
    snap = mon.snapshot()
    assert snap["loop_lag"]["max_since_start_ms"] >= 150
    assert set(snap["endpoints"]) == {"/api/htf", "/api/market", "/api/sessions", "/api/macro"}


async def test_run_heavy_uses_its_own_bounded_executor_not_the_default() -> None:
    mon = LoopMonitor()
    default = mon.install_default_executor(asyncio.get_running_loop())
    names: list[str] = []
    await run_heavy(lambda: names.append(threading.current_thread().name))
    await asyncio.to_thread(lambda: names.append(threading.current_thread().name))
    assert names[0].startswith("heavy") and names[1].startswith("asyncio")
    assert loop_monitor.heavy_executor().snapshot()["max_workers"] == loop_monitor.HEAVY_WORKERS
    assert default.snapshot()["submitted"] >= 1


def test_instrumented_executor_counts_active_and_queue_wait() -> None:
    ex = InstrumentedExecutor(max_workers=1, thread_name_prefix="t")
    gate = threading.Event()
    f1 = ex.submit(gate.wait, 5)
    f2 = ex.submit(lambda: 42)
    time.sleep(0.05)
    s = ex.snapshot()
    assert s["active"] == 1 and s["queued"] == 1
    gate.set()
    assert f1.result(5) and f2.result(5) == 42
    s = ex.snapshot()
    assert s["active"] == 0 and s["max_active"] == 1 and s["queue_wait"]["n"] == 2
    assert s["queue_wait"]["max_ms"] >= 40  # la segunda espero detras de la primera
    ex.shutdown()


def test_route_timer_matches_prefixes_only() -> None:
    rt = RouteTimer()
    assert rt.match("/api/htf/BTCUSDT") == "/api/htf"
    assert rt.match("/api/market/ETHUSDT") == "/api/market"
    assert rt.match("/api/macro") == "/api/macro"
    assert rt.match("/api/marketing") is None and rt.match("/api/status") is None
    rt.record("/api/htf", 12.0)
    assert rt.snapshot()["/api/htf"]["n"] == 1
