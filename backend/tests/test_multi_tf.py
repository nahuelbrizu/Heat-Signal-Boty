"""Grafo cada hora con velas de 1h, 2h y 4h (2026-11-19): agregacion, horizontes, frescura de la vela, motivos de no
entrada por codigo y el reloj horario. Sin red ni LLM."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from heatsignal.agents.cio import code_candidates, group_misses
from heatsignal.evaluation.register_strategy import validate_hourly
from heatsignal.evaluation.strategy_registry import StrategyHypothesis
from heatsignal.evaluation.timeframes import check_htf_entry, hourly_horizon, htf_for
from heatsignal.features.htf import DAY_MS, HOUR_MS, aggregate_bars, build_htf_context
from heatsignal.features.sessions import parse_sessions
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.schema.features import Candle1h, HtfContext, HtfSetups
from heatsignal.storage.memory import MemoryWindowSink

sys.path.insert(0, str(Path(__file__).parent))
from _agent_fakes import make_feature_set  # noqa: E402

DAY0 = 1_792_195_200_000  # 2026-10-18 00:00 UTC
STRATEGIES = Path(__file__).resolve().parents[1] / "strategies"


def _series(n: int, end_open_ms: int, p: float = 100.0, step: float = 0.05) -> list[Candle1h]:
    start = end_open_ms - (n - 1) * HOUR_MS
    out = []
    for i in range(n):
        o, c = p, p + step
        out.append(Candle1h(open_ms=start + i * HOUR_MS, open=o, high=max(o, c) + 0.3, low=min(o, c) - 0.3, close=c,
                            source="klines", vwap_pv=(o + c) / 2 * 60, vwap_vol=60.0))
        p = c
    return out


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ---- agregacion ---------------------------------------------------------------------------------------

def test_aggregate_4h_only_closed_and_complete() -> None:
    cs = _series(14, DAY0 + 13 * HOUR_MS)  # 00:00 .. 13:00 (la de 13:00 cierra a las 14:00)
    del cs[5]  # falta la de 05:00 -> la vela 04:00-08:00 queda incompleta
    gaps: list[str] = []
    bars = aggregate_bars(cs, 4, DAY0 + 14 * HOUR_MS, gaps)
    assert [b.open_ms for b in bars] == [DAY0, DAY0 + 8 * HOUR_MS]  # 12:00-16:00 sigue abierta
    b0 = bars[0]
    assert b0.open == cs[0].open and b0.close == cs[3].close and b0.minutes == 240
    assert b0.high == max(c.high for c in cs[:4]) and b0.vwap_vol == 240.0
    assert any("4h" in g and "descartadas" in g for g in gaps)


def test_tf_context_uses_daily_trend_of_1h() -> None:
    now = DAY0 + 20 * DAY_MS + 12 * HOUR_MS
    cs = _series(24 * 20 + 12, now - HOUR_MS, step=0.2)
    base = build_htf_context("BTC-USDT", cs, now)
    ctx = build_htf_context("BTC-USDT", cs, now, bar_hours=4, trend_from=base)
    assert ctx.timeframe == "4h" and base.timeframe == "1h"
    assert ctx.daily_trend == base.daily_trend
    assert ctx.last_closed_open_ms == now - 4 * HOUR_MS
    assert ctx.atr14_1h is not None and ctx.atr14_1h > base.atr14_1h  # ATR en unidades de 4h
    assert build_htf_context("BTC-USDT", cs, now, bar_hours=2).daily_trend == "sin_datos"


# ---- horizonte -----------------------------------------------------------------------------------------

def test_horizon_aligned_to_tf_grid() -> None:
    created = datetime(2026, 11, 19, 3, 10, tzinfo=timezone.utc)
    hs, bars = hourly_horizon({"timeframe": "2h", "horizon_bars": 4}, created)
    assert bars == 4 and _dt(int(created.timestamp() * 1000) + hs * 1000).hour == 12  # 04:00 + 4 x 2h
    hs, bars = hourly_horizon({"timeframe": "4h", "horizon_bars": 4}, created)
    assert _dt(int(created.timestamp() * 1000) + hs * 1000) == datetime(2026, 11, 19, 20, tzinfo=timezone.utc)
    late = datetime(2026, 11, 19, 17, 5, tzinfo=timezone.utc)
    assert isinstance(hourly_horizon({"timeframe": "4h", "horizon": "end_of_day_utc", "min_bars": 2}, late), str)
    assert hourly_horizon({"timeframe": "1h", "horizon_bars": 4}, created)[0] == 50 * 60 + 4 * 3600  # sin cambios


# ---- frescura de la vela -------------------------------------------------------------------------------

def _ctx(tf: str, last_open: int, **setups) -> HtfContext:
    return HtfContext(symbol="BTC-USDT", ts_ms=last_open, status="ok", timeframe=tf, day_start_ms=DAY0,
                      last_closed_open_ms=last_open, daily_trend="alcista", setups=HtfSetups(**setups))


ENTRY = {"htf_setup": "pullback_ema20_1h_a_favor", "daily_trend_in": ["alcista", "bajista"], "alignment": "a_favor"}


def test_entry_only_in_the_hour_the_bar_closes() -> None:
    h = _ctx("4h", DAY0 + 4 * HOUR_MS, pullback_ema20_1h_a_favor=True)  # vela 04:00-08:00
    assert check_htf_entry(ENTRY, h, "LONG", now_ms=DAY0 + 8 * HOUR_MS + 90_000) is None
    late = check_htf_entry(ENTRY, h, "LONG", now_ms=DAY0 + 9 * HOUR_MS + 90_000)
    assert late and "no cerro una vela de 4h" in late
    session = {**ENTRY, "htf_setup": "apertura_continuacion_a_favor"}
    assert "no aplica en 4h" in check_htf_entry(session, h, "LONG", now_ms=DAY0 + 8 * HOUR_MS + 90_000)


def test_htf_for_picks_the_strategy_timeframe() -> None:
    fs = make_feature_set("BTC-USDT").model_copy(update={"htf": _ctx("1h", DAY0), "htf_tf": {"2h": _ctx("2h", DAY0)}})
    assert htf_for(fs, {"timeframe": "1h"}).timeframe == "1h"
    assert htf_for(fs, {"timeframe": "2h"}).timeframe == "2h"
    assert htf_for(fs, {"timeframe": "4h"}) is None


# ---- por que no entro ----------------------------------------------------------------------------------

def _strategy(name: str, tf: str, **entry) -> StrategyHypothesis:
    return StrategyHypothesis(description=f"{name}: prueba", author="test",
                              entry_conditions={**ENTRY, "timeframe": tf, **entry},
                              exit_conditions={"timeframe": tf, "horizon_bars": 2})


def test_every_strategy_without_entry_says_why() -> None:
    now = DAY0 + 8 * HOUR_MS + 90_000
    fs = make_feature_set("BTC-USDT").model_copy(update={
        "htf": _ctx("1h", DAY0 + 7 * HOUR_MS),  # sin setup
        "htf_tf": {"4h": _ctx("4h", DAY0 + 4 * HOUR_MS, pullback_ema20_1h_a_favor=True)},
    })
    s1, s4, s2 = _strategy("1h-a", "1h"), _strategy("4h-a", "4h"), _strategy("2h-a", "2h")
    misses: list[str] = []
    cands = code_candidates(fs, [s1, s4, s2], now=_dt(now), misses=misses)
    assert [(c.strategy_id, c.direction) for c in cands] == [(str(s4.strategy_id).lower(), "LONG")]
    assert any(m.startswith("1h-a: LONG: el setup pullback_ema20_1h_a_favor no se cumple") for m in misses)
    assert any(m.startswith("2h-a:") and "sin contexto de 2h" in m for m in misses)
    assert not any(m.startswith("4h-a") for m in misses)  # tiene candidato
    text = group_misses(["a: x", "b: x", "c: y"])
    assert text == "x [a, b] | y [c]"


# ---- reloj horario -------------------------------------------------------------------------------------

def test_due_hour_skips_grace_repeats_and_session_hours(tmp_path) -> None:
    sessions = parse_sessions("asia=Asia/Tokyo@09:00,cierre=UTC@23:00")
    loop = AnalysisLoop(MemoryWindowSink(), ["BTC-USDT"], registry=None, mode="hourly", sessions=sessions)  # type: ignore[arg-type]
    h10 = DAY0 + 10 * HOUR_MS
    assert loop.due_hour(h10 + 30_000) is None  # dentro de la gracia
    assert loop.due_hour(h10 + 120_000) == h10
    loop._hours_done.add(h10)
    assert loop.due_hour(h10 + 600_000) is None
    assert loop.due_hour(DAY0 + 1 * HOUR_MS + 120_000) is None  # Asia (09:00 Tokio = 00:00 UTC) dispara a la 01:00
    assert loop.due_hour(DAY0 + 120_000) is None  # cierre (23:00 UTC) dispara a las 00:00


def test_missed_and_failed_hours_are_reviewed_by_code_in_the_next_ran_cycle() -> None:
    """PC suspendida 02-09 + 10:00 con error de red: se reintenta la hora y el primer ciclo que corre recibe la
    revision del hueco (sin re-correr horas viejas con el LLM)."""
    import asyncio

    from heatsignal.pipeline.analysis import CycleResult

    class Live:
        def filled_minutes(self, sym, start, end):
            return [(start, 100.0, 103.0, 98.0, 101.0, 1.0), (end - 60_000, 101.0, 101.5, 100.5, 99.0, 1.0)], []

    loop = AnalysisLoop(MemoryWindowSink(), ["BTC-USDT"], registry=None, mode="hourly", live=Live())  # type: ignore[arg-type]
    calls: list[tuple[str | None, str | None]] = []
    statuses = iter(["ran", "error", "ran"])

    async def fake_run(only, *, reason=None, recovery=None, **kw):
        calls.append((reason, recovery))
        return CycleResult("r", next(statuses))

    loop._run = fake_run  # type: ignore[method-assign]
    h = lambda n: DAY0 + n * HOUR_MS  # noqa: E731
    asyncio.run(loop.run_hour(h(2)))
    assert calls[-1][1] is None
    asyncio.run(loop.run_hour(h(10)))  # 03..09 nunca se vieron; 10 falla
    assert "sin analisis 7 h (02:00-09:00 UTC)" in calls[-1][1]
    assert h(10) not in loop._hours_done and loop.due_hour(h(10) + 400_000) == h(10)  # se reintenta
    asyncio.run(loop.run_hour(h(10)))  # vuelve la red
    rec = calls[-1][1]
    assert "sin analisis 7 h (02:00-09:00 UTC)" in rec and "BTC-USDT -1.00% (max +3.00%, min -2.00%)" in rec
    assert not loop._missed_hours


# ---- estrategias de 2h/4h ------------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(p for d in ("2h", "4h") for p in STRATEGIES.glob(f"*-{d}/*.json")))
def test_tf_strategy_files_are_valid(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    s = StrategyHypothesis(**data)
    assert validate_hourly(s) is None, path.name
    tf = path.name[:2]
    assert s.exit_conditions["timeframe"] == s.entry_conditions["timeframe"] == tf
    assert path.stem == s.description.split(":", 1)[0]


def test_session_setups_cannot_be_registered_in_2h() -> None:
    s = _strategy("2h-x", "2h", htf_setup="apertura_continuacion_a_favor", sessions=["europa"])
    assert "vela de 1h de una sesion" in validate_hourly(s)
