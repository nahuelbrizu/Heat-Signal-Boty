"""
Monitor liviano del event loop (proceso de ingesta + panel + grafo en un solo asyncio).

- Retraso del loop: una tarea duerme `interval_s` y mide cuanto tardo de verdad. El exceso es el tiempo en que
  el loop no pudo atender nada (callbacks largos, GC, paginacion por swap). p50/p99/max por minuto, logueados
  en el log de health ("loop {...}") y expuestos en `/api/system` -> `runtime`.
- Executors: el default de asyncio (`asyncio.to_thread`, `loop.getaddrinfo` del DNS al reconectar) y uno propio
  y acotado para el trabajo pesado (`run_heavy`: contexto 1h, features, sesiones, macro), asi el DNS y los
  `to_thread` cortos nunca esperan detras de un calculo largo. De cada uno: activos, en cola, espera en cola.
- Endpoints pesados del panel: duracion p50/p99/max de los ultimos pedidos por ruta.
- Memoria del proceso: RSS y swap (de /proc/self/status). El cuelgue del 19/10 fue swap contra MemoryMax=700M.

Todo acotado: deques con maxlen, sin estructuras que crezcan con el tiempo.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

log = logging.getLogger("heatsignal.ingestion.health")

T = TypeVar("T")

HEAVY_WORKERS = int(os.environ.get("HEATSIGNAL_HEAVY_WORKERS", "2"))
TIMED_PREFIXES = ("/api/htf", "/api/market", "/api/sessions", "/api/macro")


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def _summary(vals: list[float]) -> dict[str, Any]:
    s = sorted(vals)
    r = (lambda x: None if x is None else round(x, 1))
    return {"n": len(s), "p50_ms": r(_pct(s, 0.5)), "p99_ms": r(_pct(s, 0.99)), "max_ms": r(s[-1] if s else None)}


def process_memory() -> dict[str, int | None]:
    """RSS y swap del proceso en bytes (Linux); None donde no hay /proc."""
    out: dict[str, int | None] = {"rss_bytes": None, "swap_bytes": None}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_bytes"] = int(line.split()[1]) * 1024
                elif line.startswith("VmSwap:"):
                    out["swap_bytes"] = int(line.split()[1]) * 1024
    except OSError:
        pass
    return out


class InstrumentedExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor que cuenta tareas activas/en cola y la espera en cola (ultimas 512)."""

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = "") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.name = thread_name_prefix or "executor"
        self._mx = threading.Lock()
        self.submitted = 0
        self.active = 0
        self.max_active = 0
        self._waits: deque[float] = deque(maxlen=512)
        self._runs: deque[float] = deque(maxlen=512)

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:  # type: ignore[override]
        t_sub = time.perf_counter()

        def run() -> T:
            t0 = time.perf_counter()
            with self._mx:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self._waits.append((t0 - t_sub) * 1000)
            try:
                return fn(*args, **kwargs)
            finally:
                with self._mx:
                    self.active -= 1
                    self._runs.append((time.perf_counter() - t0) * 1000)

        with self._mx:
            self.submitted += 1
        return super().submit(run)

    def snapshot(self) -> dict[str, Any]:
        with self._mx:
            waits, runs = list(self._waits), list(self._runs)
            active, max_active, submitted = self.active, self.max_active, self.submitted
        return {"max_workers": self._max_workers, "active": active, "max_active": max_active,
                "queued": self._work_queue.qsize(), "submitted": submitted,
                "queue_wait": _summary(waits), "run": _summary(runs)}


_heavy: InstrumentedExecutor | None = None
_heavy_lock = threading.Lock()


def heavy_executor() -> InstrumentedExecutor:
    global _heavy
    with _heavy_lock:
        if _heavy is None:
            _heavy = InstrumentedExecutor(max_workers=max(1, HEAVY_WORKERS), thread_name_prefix="heavy")
        return _heavy


async def run_heavy(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Como `asyncio.to_thread`, pero en el executor acotado de trabajo pesado (no el default)."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await loop.run_in_executor(heavy_executor(), call)


class RouteTimer:
    """Duracion de los endpoints pesados del panel (ultimos 256 pedidos por ruta)."""

    def __init__(self, prefixes: tuple[str, ...] = TIMED_PREFIXES) -> None:
        self.prefixes = prefixes
        self._d: dict[str, deque[float]] = {p: deque(maxlen=256) for p in prefixes}

    def match(self, path: str) -> str | None:
        return next((p for p in self.prefixes if path == p or path.startswith(p + "/")), None)

    def record(self, prefix: str, ms: float) -> None:
        self._d[prefix].append(ms)

    def snapshot(self) -> dict[str, Any]:
        return {p: _summary(list(d)) for p, d in self._d.items()}


class LoopMonitor:
    """Retraso del event loop + estado de executors + memoria; un resumen por minuto al log de health."""

    def __init__(self, *, interval_s: float = 1.0, log_every_s: float = 60.0) -> None:
        self.interval_s = interval_s
        self.log_every_s = log_every_s
        self._lags: deque[float] = deque(maxlen=max(10, int(log_every_s / interval_s) * 2))
        self.minutes: deque[dict[str, Any]] = deque(maxlen=60)  # ultima hora de resumenes por minuto
        self.max_lag_ms = 0.0
        self.routes = RouteTimer()
        self.default_executor: InstrumentedExecutor | None = None

    def install_default_executor(self, loop: asyncio.AbstractEventLoop) -> InstrumentedExecutor:
        """Mismo tamano que el default de asyncio (min(32, cpus+4)), pero instrumentado."""
        ex = InstrumentedExecutor(max_workers=min(32, (os.cpu_count() or 1) + 4), thread_name_prefix="asyncio")
        loop.set_default_executor(ex)
        self.default_executor = ex
        return ex

    def record_lag(self, lag_ms: float) -> None:
        self._lags.append(lag_ms)
        self.max_lag_ms = max(self.max_lag_ms, lag_ms)

    def minute_summary(self) -> dict[str, Any]:
        s = _summary(list(self._lags))
        mem = process_memory()
        s.update({"ts_ms": int(time.time() * 1000),
                  "rss_mb": None if mem["rss_bytes"] is None else round(mem["rss_bytes"] / 2**20),
                  "swap_mb": None if mem["swap_bytes"] is None else round(mem["swap_bytes"] / 2**20)})
        if self.default_executor is not None:
            d = self.default_executor.snapshot()
            s["default_ex"] = {"max_active": d["max_active"], "queued": d["queued"], "wait_p99_ms": d["queue_wait"]["p99_ms"]}
        if _heavy is not None:
            h = _heavy.snapshot()
            s["heavy_ex"] = {"max_active": h["max_active"], "queued": h["queued"], "wait_p99_ms": h["queue_wait"]["p99_ms"]}
        return s

    def snapshot(self) -> dict[str, Any]:
        return {
            "loop_lag": {**_summary(list(self._lags)), "window_s": round(len(self._lags) * self.interval_s),
                         "max_since_start_ms": round(self.max_lag_ms, 1)},
            "loop_lag_by_minute": list(self.minutes),
            "memory": process_memory(),
            "executors": {"default": None if self.default_executor is None else self.default_executor.snapshot(),
                          "heavy": None if _heavy is None else _heavy.snapshot()},
            "endpoints": self.routes.snapshot(),
        }

    async def run(self) -> None:
        last_log = time.monotonic()
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(self.interval_s)
            now = time.monotonic()
            self.record_lag(max(0.0, (now - t0 - self.interval_s) * 1000))
            if now - last_log >= self.log_every_s:
                last_log = now
                m = self.minute_summary()
                self.minutes.append(m)
                self._lags.clear()
                bad = (m["p99_ms"] or 0) >= 500 or (m.get("swap_mb") or 0) >= 50
                log.log(logging.WARNING if bad else logging.INFO, "loop %s", m)


_current: LoopMonitor | None = None


def current() -> LoopMonitor | None:
    return _current


def start(loop: asyncio.AbstractEventLoop, *, log_every_s: float = 60.0) -> tuple[LoopMonitor, asyncio.Task]:
    """Instala el executor default instrumentado y arranca la tarea de medicion."""
    global _current
    mon = LoopMonitor(log_every_s=log_every_s)
    mon.install_default_executor(loop)
    heavy_executor()
    _current = mon
    return mon, loop.create_task(mon.run(), name="loop-monitor")
