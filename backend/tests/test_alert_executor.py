"""Ejecutor de alertas en la demo de Bybit (fase 4, 2026-11-21) con un exchange falso: candado demo-only, entrada
mandada -> fill, salida automatica de la posicion abierta, rechazos con motivo y conciliacion."""

from __future__ import annotations

from pathlib import Path

from heatsignal.alerts import executor as X
from heatsignal.alerts.engine import alert_id
from heatsignal.alerts.store import AlertStore

MIN = 60_000
T0 = 1_795_000_000_000 - 1_795_000_000_000 % 3_600_000


class AuthenticationError(Exception):
    pass


class FakeDemo:
    urls = {"api": {"private": "https://api-demo.bybit.com"}}

    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self.orders: list[tuple] = []
        self.net: dict[str, float] = {}

    def load_time_difference(self): return 0
    def privateGetV5UserQueryApi(self): return {"result": {"userID": 42, "permissions": {"ContractTrade": ["Order", "Position"]}}}
    def load_markets(self): return {}
    def fetch_ticker(self, m): return {"last": self.price}
    def amount_to_precision(self, m, a): return f"{a:.3f}"

    def create_order(self, m, typ, side, amount, price, params):
        q = amount if side == "buy" else -amount
        self.net[m] = self.net.get(m, 0.0) + q
        self.orders.append((m, side, amount))
        return {"id": str(len(self.orders)), "average": self.price, "filled": amount, "fee": {"cost": amount * self.price * 0.00055}}

    def fetch_positions(self):
        return [{"symbol": m, "contracts": abs(q), "side": "long" if q > 0 else "short"} for m, q in self.net.items() if q]


class FakeReal:
    def __init__(self, accepts: bool = False) -> None:
        self.accepts = accepts

    def load_time_difference(self): return 0

    def privateGetV5UserQueryApi(self):
        if self.accepts:
            return {"result": {}}
        raise AuthenticationError('bybit {"retCode":10003,"retMsg":"API key is invalid."}')


def _alert(sid, kind, t, target, prev=0.0, sym="BTCUSDT"):
    return {"alert_id": alert_id(sid, sym, t, kind), "run_id": "r", "created_ms": t, "decision_ms": t, "strategy_id": sid,
            "symbol": sym, "kind": kind, "direction": "long" if target > 0 else "short" if target < 0 else "flat",
            "target": target, "prev_target": prev, "price": 100.0, "timeframe": "5min", "source": "catalogo",
            "lab_status": "pasa", "lab_sharpe": 1.0, "paper_role": None, "exit_plan": ""}


def _cfg(tmp_path: Path, **kw) -> X.ExecConfig:
    return X.ExecConfig(alerts_db=tmp_path / "a.sqlite", keys_path=tmp_path / "nope.json", **kw)


def test_lock_refuses_a_real_account_key(tmp_path: Path) -> None:
    st = AlertStore(tmp_path / "a.sqlite")
    st.record_run("r", "ok", alerts=[_alert("s1", "entrada", T0, 1.0)])
    st.act(_alert("s1", "entrada", T0, 1.0)["alert_id"], "enviada")
    st.close()
    demo = FakeDemo()
    out = X.run_once(_cfg(tmp_path), exchanges=(demo, FakeReal(accepts=True)), now_ms=T0 + MIN)
    assert out["status"] == "bloqueado" and "REAL" in out["error"] and demo.orders == []
    out = X.run_once(_cfg(tmp_path), now_ms=T0 + MIN)  # sin archivo de claves
    assert out["status"] == "bloqueado"


def test_sent_entry_fills_and_exit_closes_by_itself(tmp_path: Path) -> None:
    st = AlertStore(tmp_path / "a.sqlite")
    st.record_run("r1", "ok", alerts=[_alert("s1", "salida", T0 - 30 * MIN, 0.0, prev=1.0),  # salida VIEJA sin procesar
                                       _alert("s1", "entrada", T0, 1.0)])
    st.act(_alert("s1", "entrada", T0, 1.0)["alert_id"], "enviada")
    demo, real = FakeDemo(100.0), FakeReal()
    out = X.run_once(_cfg(tmp_path), exchanges=(demo, real), now_ms=T0 + MIN)
    assert out["ejecutadas"] == 1 and demo.orders == [("BTC/USDT:USDT", "buy", 10.0)]  # 1.000 USDT / 100
    assert out["conciliacion"]["diferencias"] == {}
    # la salida vieja (antes de la entrada) no cierra la posicion nueva
    assert X.run_once(_cfg(tmp_path), exchanges=(demo, real), now_ms=T0 + 2 * MIN)["ejecutadas"] == 0
    demo.price = 110.0
    st.record_run("r2", "ok", alerts=[_alert("s1", "salida", T0 + 20 * MIN, 0.0, prev=1.0)])  # nadie la manda
    out = X.run_once(_cfg(tmp_path), exchanges=(demo, real), now_ms=T0 + 21 * MIN)
    assert out["ejecutadas"] == 1 and demo.orders[-1] == ("BTC/USDT:USDT", "sell", 10.0)
    pos = X.ExecBook(st).positions()[("s1", "BTCUSDT")]
    assert pos["qty"] == 0 and round(pos["realized"], 6) == 100.0
    states = {a["kind"]: a["state"] for a in st.recent() if a["decision_ms"] >= T0}
    assert states == {"entrada": "ejecutada", "salida": "ejecutada"}


def test_rejections_have_reasons(tmp_path: Path) -> None:
    st = AlertStore(tmp_path / "a.sqlite")
    st.set_auto("s1", True)
    st.record_run("r1", "ok", alerts=[
        _alert("s1", "entrada", T0 - 30 * MIN, 1.0, sym="ETHUSDT"),       # vieja
        _alert("s1", "salida", T0, 0.0, prev=1.0, sym="SOLUSDT"),         # salida sin posicion
        _alert("s1", "entrada", T0, 1.0, sym="BTCUSDT"),
        _alert("s1", "entrada", T0, -1.0, sym="XRPUSDT")])                # pasa el limite de 1 posicion
    out = X.run_once(_cfg(tmp_path, max_open=1), exchanges=(FakeDemo(), FakeReal()), now_ms=T0 + MIN)
    assert out["ejecutadas"] == 1 and out["rechazadas"] == 3
    motivos = {e["symbol"]: e["detail"]["motivo"] for e in X.ExecBook(st).recent() if e["kind"] == "rechazo"}
    assert "vieja" in motivos["ETHUSDT"] and "no hay posicion" in motivos["SOLUSDT"]
    (limited,) = {"BTCUSDT", "XRPUSDT"} & set(motivos)  # misma decision: la que llega segunda choca con el limite
    assert "limite" in motivos[limited]
    assert st.queued() == []  # nada queda colgado en la cola
