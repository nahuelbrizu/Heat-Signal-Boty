"""Estrategias de dias laterales (decisions.md 2026-10-19): niveles htf-levels-v1, entrada por codigo, tope de una por
moneda y sesion, JSON pre-registrables y tamano del contexto. Velas sinteticas, sin red ni LLM."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _agent_fakes import ScriptedChatModel, make_feature_set, prompt_text

from heatsignal.agents._common import htf_digest_line, render_htf
from heatsignal.agents.hypothesis_store import InMemoryPendingHypothesisStore
from heatsignal.agents.market_context_analyst import _SYSTEM_PROMPT as CTX_SYSTEM
from heatsignal.agents.cio import CioBatch, CioSymbolVerdict, make_cio_node
from heatsignal.agents.cro import make_cro_node
from heatsignal.agents.synthesizer import StrategyProposal, apply_proposals, make_registrar_node
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.register_strategy import main as register_main
from heatsignal.evaluation.register_strategy import validate_hourly
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.evaluation.timeframes import check_htf_entry
from heatsignal.features import htf as htf_mod
from heatsignal.features.htf import DAY_MS, HOUR_MS, build_htf_context, wilder_adx
from heatsignal.history.hourly import aggregate_minutes
from heatsignal.schema.features import Candle1h

SYM = "BTC-USDT"
DAY0 = 1_792_195_200_000  # 2026-10-18 00:00 UTC
NOW = DAY0 + 10 * HOUR_MS + 2 * 60_000  # 10:02 UTC: la ultima vela cerrada es la de 09:00
T = datetime.fromtimestamp(NOW / 1000, tz=timezone.utc)
DIR = Path(__file__).parents[1] / "strategies" / "lateralidad-1h"
FILES = {p.stem: p for p in DIR.glob("*.json")}
EUROPA = {"name": "europa", "label": "Europa (Londres)", "day": "2026-10-18"}


def _c(open_ms: int, o: float, h: float, low: float, c: float, *, vol: float = 60.0) -> Candle1h:
    return Candle1h(open_ms=open_ms, open=o, high=h, low=low, close=c, source="klines",
                    vwap_pv=(h + low + c) / 3 * vol, vwap_vol=vol)


def _candles(trigger: tuple[float, float, float, float] | None = None, *, days: int = 30,
             extra: list[Candle1h] | None = None, spike: tuple[int, float] | None = None) -> list[Candle1h]:
    """Serie plana (lateral): cada vela abre 100.0/100.2 y cierra 100.2/100.0, max 100.6, min 99.6 (ATR = 1).
    `trigger` (o, h, l, c) reemplaza la vela de 09:00 del dia DAY0 (la ultima cerrada a las 10:02).
    `spike` (hora, precio): escenarios de VWAP. Maximos/minimos alternados (+DM y -DM parejos: ADX bajo, no 0) y una
    vela de mucho volumen a ese precio a esa hora del dia, que corre el VWAP sin cambiar la apertura (dia lateral)."""
    out = []
    start = DAY0 - days * DAY_MS
    for i in range((NOW - start) // HOUR_MS):
        t = start + i * HOUR_MS
        base = 100.0
        o, c = (base, base + 0.2) if i % 2 == 0 else (base + 0.2, base)
        hi, lo = max(o, c) + 0.4, min(o, c) - 0.4
        if spike is not None:
            hi, lo = (hi + 0.1, lo) if i % 2 == 0 else (hi, lo - 0.1)
            if t == DAY0 + spike[0] * HOUR_MS:
                out.append(_c(t, o, max(o, spike[1]) + 0.3, min(o, spike[1]) - 0.1, spike[1], vol=3000.0))
                continue
        out.append(_c(t, o, hi, lo, c))
    if trigger is not None:
        out[-1] = _c(out[-1].open_ms, *trigger)
    assert out[-1].open_ms == DAY0 + 9 * HOUR_MS
    return out + (extra or [])


def _ctx(trigger=None, **kw):
    if trigger is VWAP_LONG:
        kw.setdefault("spike", (3, 101.9))
    if trigger is VWAP_SHORT:
        kw.setdefault("spike", (2, 98.0))
    return build_htf_context(SYM, _candles(trigger, **kw), NOW)


FALSA_SHORT = (100.1, 101.5, 100.0, 100.3)   # rompe max Asia 100.6 + 0.5 ATR y cierra adentro (tambien rechaza ayer)
FALSA_LONG = (100.1, 100.2, 98.5, 99.8)      # rompe min Asia 99.6 - 0.5 ATR y cierra adentro
RECHAZO_SHORT = (100.1, 100.9, 100.0, 100.2)  # toca max de ayer, mecha 78 %, sin superar max Asia + 0.5 ATR
RECHAZO_LONG = (100.0, 100.1, 99.3, 99.9)     # toca min de ayer, mecha inferior 75 %
VWAP_LONG = (100.0, 100.1, 98.4, 98.5)        # VWAP ~101.1 (pico de volumen a 101.9): cierre a -2.05 ATR, ADX ~13
VWAP_SHORT = (100.0, 101.7, 99.9, 101.6)      # VWAP ~99.0 (pico a 98.0): cierre a +2.07 ATR, ADX ~17


# ---- features ------------------------------------------------------------------------------------------

def test_levels_from_closed_candles() -> None:
    h = _ctx()
    lv = h.levels
    assert h.daily_trend == "lateral" and lv.levels_model_id == "htf-levels-v1"
    assert lv.asia_hours == 8 and lv.asia_high == pytest.approx(100.6) and lv.asia_low == pytest.approx(99.6)
    assert lv.prev_day_high == pytest.approx(100.6) and lv.prev_day_low == pytest.approx(99.6)
    assert lv.vwap == pytest.approx((100.6 + 99.6 + 100.1) / 3, abs=0.02) and lv.vwap_minutes == 600
    assert lv.adx14_1h == pytest.approx(0.0, abs=1e-9)
    assert lv.last_range == pytest.approx(1.0) and lv.upper_wick_frac == pytest.approx(0.4)
    assert lv.lower_wick_frac == pytest.approx(0.4)
    assert not (lv.falsa_ruptura_asia or lv.rechazo_dia_anterior or lv.extremo_vwap)
    assert h.snapshot()["levels"]["asia_high"] == pytest.approx(100.6)
    assert h.htf_model_id == "htf-trend-v1"  # la regla de tendencia no cambia (las 5 estrategias siguen validas)


def test_missing_data_is_declared_not_invented() -> None:
    cs = [c for c in _candles() if c.open_ms != DAY0 + 3 * HOUR_MS]  # falta una hora de Asia
    cs = [c.model_copy(update={"vwap_pv": None, "vwap_vol": None}) if c.open_ms == DAY0 + 5 * HOUR_MS else c for c in cs]
    h = build_htf_context(SYM, cs, NOW)
    lv = h.levels
    assert lv.asia_high is None and lv.asia_hours == 7 and lv.vwap is None
    assert any("rango de Asia" in m for m in lv.sin_datos) and any("VWAP" in m for m in lv.sin_datos)
    assert any(g.startswith("htf-levels: VWAP") for g in h.data_gaps)
    rest = [c.model_copy(update={"vwap_pv": None, "vwap_vol": None, "source": "rest"}) for c in _candles()]
    lv = build_htf_context(SYM, rest, NOW).levels
    assert lv.vwap is None and any("sin velas 1m" in m for m in lv.sin_datos)
    early = build_htf_context(SYM, [c for c in _candles() if c.open_ms < DAY0 + 4 * HOUR_MS], DAY0 + 4 * HOUR_MS + 60_000)
    assert early.levels.asia_high is None and any("en curso" in m for m in early.levels.sin_datos)
    assert not any("en curso" in g for g in early.data_gaps)  # en curso no es un hueco


def test_no_lookahead_open_candle_is_ignored() -> None:
    spike = _c(DAY0 + 10 * HOUR_MS, 100.0, 150.0, 50.0, 140.0, vol=10_000)  # vela de 10:00, todavia abierta
    a, b = _ctx(), _ctx(extra=[spike])
    assert b.levels.model_dump() == a.levels.model_dump()
    assert b.last_closed_open_ms == DAY0 + 9 * HOUR_MS
    assert any("abiertas o futuras" in g for g in b.data_gaps)


def test_adx_wilder_trend_vs_range() -> None:
    flat = _candles(days=2)
    assert wilder_adx(flat) == pytest.approx(0.0, abs=1e-9)
    up = [_c(i * HOUR_MS, 100 + i, 100.6 + i, 99.8 + i, 100.5 + i) for i in range(40)]
    assert wilder_adx(up) > 90
    assert wilder_adx(up[:28]) is None  # necesita 2*14 + 1 velas


def test_vwap_parts_from_1m_rows() -> None:
    m = 60_000
    kl = [(i * m, 10.0, 12.0, 9.0, 11.0 if i else 9.0, 2.0) for i in range(60)]  # volumen base
    c = aggregate_minutes(kl, "klines", 60)[0]
    tp0, tp = (12 + 9 + 9) / 3, (12 + 9 + 11) / 3
    assert c.vwap_vol == pytest.approx(120.0) and c.vwap_pv == pytest.approx(2 * tp0 + 59 * 2 * tp)
    bars = [(i * m, 10.0, 12.0, 9.0, 11.0, 32.0) for i in range(55)]  # USD -> base = USD / precio tipico
    b = aggregate_minutes(bars, "bars_1m", 50, quote_volume=True)[0]
    assert b.vwap_pv / b.vwap_vol == pytest.approx(tp) and b.minutes == 55
    assert aggregate_minutes([r[:5] for r in kl], "klines", 60)[0].vwap_pv is None  # sin volumen: sin VWAP


# ---- reglas (disparan cuando deben y no cuando no) -------------------------------------------------------

@pytest.mark.parametrize("trigger, flag, want", [
    (FALSA_SHORT, "falsa_ruptura_asia", "SHORT"),
    (FALSA_LONG, "falsa_ruptura_asia", "LONG"),
    ((100.1, 101.0, 100.0, 100.3), "falsa_ruptura_asia", None),   # no supera max Asia + 0.5 ATR
    ((100.1, 101.5, 100.0, 100.9), "falsa_ruptura_asia", None),   # cierra afuera del rango (ruptura real)
    (RECHAZO_SHORT, "rechazo_dia_anterior", "SHORT"),
    (RECHAZO_LONG, "rechazo_dia_anterior", "LONG"),
    ((100.0, 100.9, 99.95, 100.5), "rechazo_dia_anterior", None),  # mecha superior 42 % < 50 %
    ((100.1, 100.3, 100.0, 100.2), "rechazo_dia_anterior", None),  # no llega a max ayer - 0.25 ATR
    ((100.1, 101.0, 100.0, 100.8), "rechazo_dia_anterior", None),  # cierra sobre el max de ayer
    (RECHAZO_SHORT, "falsa_ruptura_asia", None),
    (VWAP_LONG, "extremo_vwap", "LONG"),
    (VWAP_SHORT, "extremo_vwap", "SHORT"),
    ((100.0, 100.1, 99.2, 99.3), "extremo_vwap", None),            # a < 2 ATR del VWAP (con el mismo pico)
    (VWAP_LONG, "rechazo_dia_anterior", None),
    (VWAP_LONG, "falsa_ruptura_asia", None),
])
def test_rules_fire_only_when_they_should(trigger, flag, want) -> None:
    h = _ctx(trigger)
    assert h.daily_trend == "lateral", h.trend_reason
    assert getattr(h.levels, flag) == want, h.levels


def test_wick_threshold_is_50_percent() -> None:
    # high 101.0, low 100.0 (rango 1.0): close 100.5 -> mecha 50 % exacta (dispara); 100.51 -> 49 % (no)
    assert _ctx((100.0, 101.0, 100.0, 100.5)).levels.rechazo_dia_anterior == "SHORT"
    assert _ctx((100.0, 101.0, 100.0, 100.51)).levels.rechazo_dia_anterior is None


def test_vwap_rule_needs_adx_below_20(monkeypatch) -> None:
    lv = _ctx(VWAP_LONG).levels
    assert lv.extremo_vwap == "LONG" and lv.adx14_1h < 20 and lv.dist_vwap_atr <= -2
    lv = _ctx(VWAP_SHORT).levels
    assert lv.extremo_vwap == "SHORT" and lv.adx14_1h < 20 and lv.dist_vwap_atr >= 2
    monkeypatch.setattr(htf_mod, "wilder_adx", lambda cs, period=14: 20.0)  # mismas velas, ADX en el umbral
    lv = _ctx(VWAP_LONG).levels
    assert lv.adx14_1h == 20.0 and lv.extremo_vwap is None and _ctx(VWAP_SHORT).levels.extremo_vwap is None
    monkeypatch.setattr(htf_mod, "wilder_adx", lambda cs, period=14: 19.99)
    assert _ctx(VWAP_LONG).levels.extremo_vwap == "LONG" and _ctx(VWAP_SHORT).levels.extremo_vwap == "SHORT"


def _not_lateral(monkeypatch) -> None:
    """Mismas velas, pero la regla de tendencia dice alcista: aisla la condicion 'solo dia lateral'."""
    monkeypatch.setattr(htf_mod, "classify_trend", lambda *a: ("alcista", "test: dia con tendencia"))


def test_flags_only_on_lateral_days(monkeypatch) -> None:
    assert _ctx(FALSA_SHORT).levels.falsa_ruptura_asia == "SHORT"
    _not_lateral(monkeypatch)
    h = _ctx(FALSA_SHORT)
    assert h.daily_trend == "alcista", h.trend_reason
    assert h.levels.asia_high is not None  # los niveles se calculan igual (panel)
    assert not (h.levels.falsa_ruptura_asia or h.levels.rechazo_dia_anterior or h.levels.extremo_vwap)


# ---- verificacion por codigo (timeframes + sintetizador) ---------------------------------------------------

def _strat(stem: str) -> StrategyHypothesis:
    return StrategyHypothesis(**json.loads(FILES[stem].read_text()))


def _prop(s: StrategyHypothesis, direction: str, symbol: str = SYM) -> StrategyProposal:
    return StrategyProposal(symbol=symbol, decision="hypothesis", strategy_id=str(s.strategy_id), direction=direction,
                            confidence=0.55, rationale="en dias laterales similares...")


def _fs(trigger, symbol: str = SYM, **kw):
    return make_feature_set(symbol).model_copy(update={"htf": _ctx(trigger, **kw).model_copy(update={"symbol": symbol})})


@pytest.mark.parametrize("stem, trigger, direction", [
    ("1h-lateral-falsa-ruptura-asia-v1", FALSA_SHORT, "SHORT"),
    ("1h-lateral-falsa-ruptura-asia-v1", FALSA_LONG, "LONG"),
    ("1h-lateral-rechazo-dia-anterior-v1", RECHAZO_SHORT, "SHORT"),
    ("1h-lateral-rechazo-dia-anterior-v1", RECHAZO_LONG, "LONG"),
    ("1h-lateral-extremo-vwap-v1", VWAP_LONG, "LONG"),
])
def test_valid_proposal_is_accepted_with_context(stem, trigger, direction) -> None:
    s = _strat(stem)
    hyps, dec = apply_proposals([_prop(s, direction)], {SYM: _fs(trigger)}, [s], run_id="r", now=T, session=EUROPA)
    assert dec[0].outcome == "hypothesis", dec[0].reason
    [h] = hyps
    assert h.daily_trend == "lateral" and h.trend_alignment == "lateral" and h.session == "europa"
    assert h.htf_snapshot["levels"]["levels_model_id"] == "htf-levels-v1"
    assert h.horizon_bars == (3 if "falsa" in stem else 4)


@pytest.mark.parametrize("stem, trigger, direction, needle", [
    ("1h-lateral-falsa-ruptura-asia-v1", FALSA_SHORT, "LONG", "da SHORT"),            # direccion contraria
    ("1h-lateral-falsa-ruptura-asia-v1", RECHAZO_SHORT, "SHORT", "no se cumple"),     # sin falsa ruptura
    ("1h-lateral-rechazo-dia-anterior-v1", (100.0, 100.9, 99.95, 100.5), "SHORT", "no se cumple"),  # mecha < 50 %
    ("1h-lateral-extremo-vwap-v1", (100.0, 100.1, 99.2, 99.3), "LONG", "no se cumple"),  # a < 2 ATR
])
def test_llm_proposal_that_breaks_the_rule_is_rejected(stem, trigger, direction, needle) -> None:
    s = _strat(stem)
    hyps, dec = apply_proposals([_prop(s, direction)], {SYM: _fs(trigger)}, [s], run_id="r", now=T, session=EUROPA)
    assert hyps == [] and dec[0].outcome == "rejected" and needle in dec[0].reason, dec[0].reason


@pytest.mark.parametrize("stem", sorted(FILES))
def test_only_lateral_days(stem, monkeypatch) -> None:
    s = _strat(stem)
    _not_lateral(monkeypatch)
    fs = _fs(FALSA_SHORT)
    _, dec = apply_proposals([_prop(s, "SHORT")], {SYM: fs}, [s], run_id="r", now=T, session=EUROPA)
    assert dec[0].outcome == "rejected" and "daily_trend=alcista" in dec[0].reason
    # aunque el JSON no lo exigiera, el codigo lo exige igual
    loose = {k: v for k, v in s.entry_conditions.items() if k not in ("daily_trend_in", "alignment")}
    assert "solo aplican con daily_trend=lateral" in check_htf_entry(loose, fs.htf, "SHORT", None, NOW, "europa")


@pytest.mark.parametrize("session, ok", [("europa", True), ("eeuu", True), ("asia", False), ("cierre", False), (None, False)])
def test_false_break_only_in_europe_and_us(session, ok) -> None:
    s = _strat("1h-lateral-falsa-ruptura-asia-v1")
    sess = None if session is None else {"name": session, "day": "2026-10-18"}
    _, dec = apply_proposals([_prop(s, "SHORT")], {SYM: _fs(FALSA_SHORT)}, [s], run_id="r", now=T, session=sess)
    assert (dec[0].outcome == "hypothesis") is ok, dec[0].reason
    if not ok:
        assert "la estrategia aplica a ['europa', 'eeuu']" in dec[0].reason


def test_stale_context_is_rejected() -> None:
    s = _strat("1h-lateral-rechazo-dia-anterior-v1")
    later = datetime.fromtimestamp((NOW + HOUR_MS) / 1000, tz=timezone.utc)  # el contexto quedo una vela atrasado
    _, dec = apply_proposals([_prop(s, "SHORT")], {SYM: _fs(RECHAZO_SHORT)}, [s], run_id="r", now=later, session=EUROPA)
    assert dec[0].outcome == "rejected" and "ultima vela cerrada" in dec[0].reason


# ---- una por moneda y por sesion ---------------------------------------------------------------------------

def test_one_per_symbol_and_session_within_a_run() -> None:
    falsa, rech = _strat("1h-lateral-falsa-ruptura-asia-v1"), _strat("1h-lateral-rechazo-dia-anterior-v1")
    fs = {SYM: _fs(FALSA_SHORT), "ETH-USDT": _fs(FALSA_SHORT, "ETH-USDT")}  # FALSA_SHORT tambien rechaza ayer
    props = [_prop(falsa, "SHORT"), _prop(rech, "SHORT"), _prop(rech, "SHORT", "ETH-USDT")]
    hyps, dec = apply_proposals(props, fs, [falsa, rech], run_id="r", now=T, session=EUROPA)
    assert [(h.symbol, str(h.strategy_id)) for h in hyps] == [(SYM, str(falsa.strategy_id)), ("ETH-USDT", str(rech.strategy_id))]
    assert dec[1].outcome == "no_hypothesis" and "tope por moneda y sesion" in dec[1].reason


def test_one_per_symbol_and_session_uses_the_database(tmp_path) -> None:
    reg = StrategyRegistry(tmp_path / "eval.db")
    strats = [_strat(k) for k in sorted(FILES)]
    for s in strats:
        reg.register(s)
    store = PendingHypothesisStore(tmp_path / "eval.db", reg)
    falsa = next(s for s in strats if "falsa" in s.description)
    rech = next(s for s in strats if "rechazo" in s.description)
    hyps, _ = apply_proposals([_prop(falsa, "SHORT")], {SYM: _fs(FALSA_SHORT)}, strats, run_id="r1", now=T, session=EUROPA)
    store.add(hyps[0])
    taken = store.session_hypotheses("2026-10-18", "europa")
    assert taken == {(str(falsa.strategy_id).lower(), SYM)}
    assert store.session_hypotheses("2026-10-18", "eeuu") == set()
    # otra corrida (p.ej. tras un reinicio) en la misma sesion: la base la frena, aunque sea otra estrategia
    _, dec = apply_proposals([_prop(rech, "SHORT")], {SYM: _fs(FALSA_SHORT)}, strats, run_id="r2", now=T,
                             session=EUROPA, session_taken=taken)
    assert dec[0].outcome == "no_hypothesis" and "tope" in dec[0].reason
    # en la sesion de EE.UU. vuelve a poder (otro dia/sesion = otra clave)
    eeuu = {"name": "eeuu", "day": "2026-10-18"}
    hyps, _ = apply_proposals([_prop(rech, "SHORT")], {SYM: _fs(FALSA_SHORT)}, strats, run_id="r3", now=T,
                              session=eeuu, session_taken=store.session_hypotheses("2026-10-18", "eeuu"))
    assert len(hyps) == 1
    # el filtro por familia del panel
    assert store.query(strategy_ids=[s.strategy_id for s in strats])[0] == 1 and store.query(strategy_ids=[])[0] == 0
    store.close()
    reg.close()


def test_cap_needs_a_session() -> None:
    s = _strat("1h-lateral-extremo-vwap-v1")
    _, dec = apply_proposals([_prop(s, "LONG")], {SYM: _fs(VWAP_LONG)}, [s], run_id="r", now=T, session=None)
    assert dec[0].outcome == "rejected" and "sin sesion" in dec[0].reason


async def test_cio_chain_reads_session_cap_from_store_and_prompt_lists_lateral_candidate() -> None:
    s = _strat("1h-lateral-extremo-vwap-v1")
    store = InMemoryPendingHypothesisStore()
    fs = _fs(VWAP_LONG)
    take = CioBatch(verdicts=[CioSymbolVerdict(symbol=SYM, postura_ganadora="alcista", peso=0.55, decision="tomar",
                                               strategy_id=str(s.strategy_id), direction="LONG",
                                               razones=["en dias laterales similares..."])])
    model = ScriptedChatModel({CioBatch: [take, take]})
    clock = lambda: NOW  # noqa: E731 - el CIO verifica la entrada con el reloj: 10:02 UTC
    cio, cro, reg = (make_cio_node(model, hypothesis_store=store, clock_ms=clock),
                     make_cro_node(hypothesis_store=store, clock_ms=clock), make_registrar_node(hypothesis_store=store))

    async def run(state):
        for node in (cio, cro, reg):
            state = {**state, **await node(state)}
        return state

    state = {"run_id": "r1", "features_by_symbol": {SYM: fs}, "allowed_strategies": [s], "session": EUROPA}
    out = await run(state)
    assert len(out["pending_hypotheses"]) == 1 and len(store) == 1
    prompt = prompt_text(model.calls_for(CioBatch)[0])
    assert f"strategy_id={str(s.strategy_id).lower()} direction=LONG setup=extremo_vwap" in prompt
    assert "familia=1h-lateralidad" in prompt and "lateral (codigo): extremo_vwap=LONG" in prompt
    # la hipotesis anterior ya se resolvio (sin candado de abiertas): el tope por sesion lo veta el CRO
    store.open_positions = lambda: set()  # type: ignore[method-assign]
    out = await run({**state, "run_id": "r2"})
    assert out["pending_hypotheses"] == [] and "tope por sesion" in out["synthesis_decisions"][0].reason
    assert out["cio_verdicts"][0]["cro_veto"].startswith("tope por sesion")
    assert len(model.calls) == 2  # una llamada por corrida: nada extra


# ---- JSON, prompts y tamano del contexto --------------------------------------------------------------------

def test_strategy_json_files_validate_and_dry_run(capsys) -> None:
    assert sorted(FILES) == ["1h-lateral-extremo-vwap-v1", "1h-lateral-falsa-ruptura-asia-v1",
                             "1h-lateral-rechazo-dia-anterior-v1"]
    for stem in FILES:
        s = _strat(stem)
        assert s.description.startswith(stem + ":")
        assert validate_hourly(s) is None
        ec = s.entry_conditions
        assert ec["daily_trend_in"] == ["lateral"] and ec["max_per_symbol_session"] == 1 and ec["family"] == "1h-lateralidad"
        assert s.exit_conditions["timeframe"] == "1h" and "exit_rule" in s.exit_conditions
    assert register_main(["--dry-run", *map(str, sorted(FILES.values()))]) == 0
    assert "3 estrategia(s) validas, no se registro nada" in capsys.readouterr().err


@pytest.mark.parametrize("patch, needle", [
    ({"sessions": ["asia", "europa"]}, "europa"),
    ({"daily_trend_in": ["alcista", "lateral"]}, "lateral"),
    ({"levels_model_id": "htf-levels-v0"}, "levels_model_id"),
    ({"max_per_symbol_session": 0}, "max_per_symbol_session"),
])
def test_register_rejects_bad_lateral_json(patch, needle) -> None:
    s = _strat("1h-lateral-falsa-ruptura-asia-v1")
    bad = s.model_copy(update={"entry_conditions": {**s.entry_conditions, **patch}})
    assert needle in (validate_hourly(bad) or "")


def test_context_size_and_content() -> None:
    fs = _fs(VWAP_LONG)
    block = render_htf(fs)
    [line] = [x for x in block.splitlines() if "niveles (codigo" in x]
    assert "Asia 00-08 UTC" in line and "ayer min-max" in line and "VWAP dia" in line and "ADX14 1h" in line
    assert "lateral (codigo): extremo_vwap=LONG" in line
    assert len(line) <= 330, len(line)  # ~100 tokens por simbolo (3,2 car./token), solo en el Analista de Contexto
    digest = htf_digest_line(fs)
    assert digest.endswith("lateral (codigo): extremo_vwap=LONG")
    assert len("; lateral (codigo): extremo_vwap=LONG") <= 40
    missing = render_htf(make_feature_set(SYM).model_copy(update={"htf": _ctx(days=0)}))
    assert "sin_datos" in missing
    assert "rango de Asia" in CTX_SYSTEM and "VWAP" in CTX_SYSTEM
