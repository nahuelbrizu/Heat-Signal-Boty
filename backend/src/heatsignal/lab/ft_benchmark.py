"""
Benchmark de "analisis tecnico comun": las 7 estrategias de futuros de freqtrade-strategies, TAL CUAL.

    PYTHONPATH=src .venv/bin/python -m heatsignal.lab.ft_benchmark --start 2021-03-13 --end 2026-07-15

Que hace (research/freqtrade-strategies.md, plan-laboratorio.md):
  1. Verifica que las estrategias copiadas en `freqtrade/user_data/strategies/benchmark/` sigan identicas a las
     del repo de origen (sha256). Si alguna cambio, aborta: el benchmark es "sin modificar sus parametros".
  2. Verifica que haya velas de futuros para cada timeframe que usan (1h y 5m) + mark y funding 1h, cubriendo
     [start - colchon, end). Si faltan, las baja con `freqtrade download-data` (Binance futuros).
  3. Corre `freqtrade backtesting` una vez por estrategia (un fallo no tapa a las demas: queda en `error`).
  4. Resume cada corrida con metricas comparables con el laboratorio: retorno total sobre el capital inicial
     (sin reinversion: stake fijo = wallet / n_pares), Sharpe diario anualizado (365) y max DD sobre el PnL
     diario, cantidad de trades. El PnL diario de Freqtrade es REALIZADO (por dia de cierre del trade), no
     mark-to-market por minuto como el simulador del laboratorio: el Sharpe puede diferir un poco por eso.
     Tambien se guardan las metricas nativas de Freqtrade.
  5. Escribe `summary_<start>_<end>.json` en ~/heatsignal-lab/results/_benchmark_ta/ (los zip crudos en raw/).

Costos: comision 5 bps por lado (`--fee 0.0005`, igual que el laboratorio), sin slippage (Freqtrade no lo
modela), funding real (Freqtrade lo cobra con los datos `funding_rate` + `mark`). Leverage: el que fija cada
estrategia (1 salvo VolatilitySystem, que fija 2). No se re-optimiza nada: cada estrategia cuenta como un
intento mas para el Deflated Sharpe del programa.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from . import stats
from .strategies import SEVEN

log = logging.getLogger("heatsignal.lab.ft_benchmark")

REPO_ROOT = Path(__file__).resolve().parents[4]
BENCH_DIR = REPO_ROOT / "freqtrade" / "user_data" / "strategies" / "benchmark"
SOURCE = {"repo": "https://github.com/freqtrade/freqtrade-strategies",
          "commit": "f3340ce11f5bdf62f598522e64d1f5638eaa13f5", "path": "user_data/strategies/futures",
          "license": "GPL-3.0"}

# nombre -> (archivo, timeframe, sha256 del archivo original). Ver benchmark/README.md.
STRATEGIES: dict[str, tuple[str, str, str]] = {
    "FAdxSmaStrategy": ("FAdxSmaStrategy.py", "1h",
                        "43ae02ecca4553b8a609d4533cb4d5b6517292d35b41fc4c77f54f8e959ced89"),
    "FOttStrategy": ("FOttStrategy.py", "1h",
                     "6e7dd4802915f6952c9f7be6b5fe9840d1c894a89476e1b274fb163b096d930e"),
    "FReinforcedStrategy": ("FReinforcedStrategy.py", "5m",
                            "4148d3d2d736a454a470649646a59b77a62f7d4ccb7f7bc6d2ecc5ad0ec01152"),
    "FSampleStrategy": ("FSampleStrategy.py", "1h",
                        "3fcbe2ed16dd097fd6e4548172acbde871bdfe64c5a7341f337f19caf3daa225"),
    "FSupertrendStrategy": ("FSupertrendStrategy.py", "1h",
                            "93f31258c4b6c05cf2380fc81f235f37355d97a3e4d2ebdf075e948e07978e19"),
    "TrendFollowingStrategy": ("TrendFollowingStrategy.py", "5m",
                               "6b8f3e272acfd4923539120364b9ddceccc17a385117d12a249dc1460ea53c4f"),
    "VolatilitySystem": ("VolatilitySystem.py", "1h",
                         "10a32ea131fb8f4684741b48cd3eae70aa4d4990319b49a7acb7a2f9c7c8a672"),
}

# Compatibilidad con Freqtrade 2026.9 / numpy 2 SIN tocar los originales: subclases en benchmark/compat/ que
# heredan todo del archivo original (ver los comentarios de cada una). No cambian parametros ni reglas.
COMPAT_DIR = BENCH_DIR / "compat"
COMPAT: dict[str, tuple[str, str]] = {
    "FOttStrategy": ("FOttStrategyCompat", "3 parches en memoria: numpy.lib.math -> math (no se usa) y la "
                                           "asignacion encadenada de Var (no-op con pandas 3: nunca operaba) "
                                           "+ shortstop creado como float (pandas 3 no upcastea)"),
    "FReinforcedStrategy": ("FReinforcedStrategyCompat", "IntParameter sin space: se re-declaran con el mismo "
                                                         "rango y default y space='buy'"),
}

FUNDING_TF = "1h"            # Binance: funding_rate y mark se guardan en 1h (como los que ya hay)
STARTUP_BUFFER_DAYS = 30     # velas previas para el warm-up de los indicadores (startup_candle_count)
FEE = 0.0005                 # 5 bps por lado, igual que el laboratorio


def ft_dir() -> Path:
    return Path(os.environ.get("HEATSIGNAL_FT_DIR", Path.home() / "heatsignal-lab" / "ft"))


def results_dir() -> Path:
    return Path(os.environ.get("HEATSIGNAL_LAB_RESULTS", Path.home() / "heatsignal-lab" / "results")) / "_benchmark_ta"


def ft_pair(symbol: str) -> str:
    """BTCUSDT -> BTC/USDT:USDT (perpetuo USDT-M en ccxt)."""
    assert symbol.endswith("USDT"), symbol
    return f"{symbol[:-4]}/USDT:USDT"


def _file_base(pair: str) -> str:
    return pair.replace("/", "_").replace(":", "_")  # BTC/USDT:USDT -> BTC_USDT_USDT


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_strategies(names: Iterable[str], bench_dir: Path = BENCH_DIR) -> None:
    bad = []
    for name in names:
        fname, _, digest = STRATEGIES[name]
        path = bench_dir / fname
        if not path.exists() or _sha256(path) != digest:
            bad.append(fname)
    if bad:
        raise RuntimeError(f"estrategias de benchmark ausentes o modificadas: {bad} (tienen que ser identicas al "
                           f"commit {SOURCE['commit']})")


# ---- datos ---------------------------------------------------------------------------------------------


def data_coverage(path: Path) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if not path.exists():
        return None
    dates = pd.read_feather(path, columns=["date"])["date"]
    if dates.empty:
        return None
    return pd.Timestamp(dates.min()).tz_convert("UTC"), pd.Timestamp(dates.max()).tz_convert("UTC")


def _needed_files(pair: str, tf: str, datadir: Path) -> list[Path]:
    base = datadir / "futures" / _file_base(pair)
    return [Path(f"{base}-{tf}-futures.feather"), Path(f"{base}-{FUNDING_TF}-mark.feather"),
            Path(f"{base}-{FUNDING_TF}-funding_rate.feather")]


def coverage_gaps(pairs: Iterable[str], tf: str, start: pd.Timestamp, end: pd.Timestamp,
                  datadir: Path) -> dict[str, dict[str, bool]]:
    """Por par: {'missing': no hay archivo, 'head': empieza despues de start, 'tail': termina antes de end}."""
    out = {}
    last_needed = end - pd.Timedelta(tf)
    for pair in pairs:
        flags = {"missing": False, "head": False, "tail": False}
        for f in _needed_files(pair, tf, datadir):
            cov = data_coverage(f)
            if cov is None:
                flags["missing"] = True
                continue
            first, last = cov
            # mark y funding son 1h: alcanza con que cubran hasta la ultima hora
            flags["head"] |= first > start
            flags["tail"] |= last < (last_needed if f.name.endswith("-futures.feather") else end - pd.Timedelta(FUNDING_TF))
        if any(flags.values()):
            out[pair] = flags
    return out


def _timerange(a: pd.Timestamp, b: pd.Timestamp) -> str:
    return f"{a:%Y%m%d}-{b:%Y%m%d}"


def ensure_data(pairs: list[str], tf: str, start: pd.Timestamp, end: pd.Timestamp, datadir: Path,
                freqtrade: Path, userdir: Path, run=subprocess.run) -> dict[str, Any]:
    """Baja con `freqtrade download-data` lo que falte; devuelve los huecos que quedan (declarados, no silenciosos)."""
    want_start = start - pd.Timedelta(days=STARTUP_BUFFER_DAYS)
    gaps = coverage_gaps(pairs, tf, want_start, end, datadir)
    downloads = []
    if gaps:
        base = [str(freqtrade), "download-data", "--exchange", "binance", "--trading-mode", "futures",
                "-t", tf, "--datadir", str(datadir), "--userdir", str(userdir), "--timerange", _timerange(want_start, end + pd.Timedelta(days=1))]
        append = [p for p, g in gaps.items() if g["missing"] or g["tail"]]
        prepend = [p for p, g in gaps.items() if g["head"] and not g["missing"]]
        for extra, ps in (([], append), (["--prepend"], prepend)):
            if not ps:
                continue
            cmd = base + extra + ["-p", *ps]
            log.info("descargando %s %s: %s", tf, "prepend" if extra else "append", ps)
            r = run(cmd, capture_output=True, text=True, cwd=str(datadir.parent))
            downloads.append({"cmd": cmd, "rc": r.returncode, "stderr_tail": (r.stderr or "")[-2000:]})
            if r.returncode != 0:
                raise RuntimeError(f"download-data fallo (rc={r.returncode}): {(r.stderr or '')[-500:]}")
    remaining = coverage_gaps(pairs, tf, want_start, end, datadir)
    # un 'head' que sigue despues de bajar es la fecha de listado del perpetuo: se declara, no se oculta
    return {"timeframe": tf, "downloads": downloads, "remaining_gaps": remaining}


# ---- config y corrida --------------------------------------------------------------------------------------


def build_config(pairs: list[str], wallet: float) -> dict[str, Any]:
    return {
        "$comment": "Generado por heatsignal.lab.ft_benchmark: benchmark de estrategias de terceros, sin modificar.",
        "dry_run": True,
        "dry_run_wallet": wallet,
        "stake_currency": "USDT",
        "stake_amount": wallet / len(pairs),   # fijo: sin reinversion, como el simulador del laboratorio
        "tradable_balance_ratio": 0.99,
        "max_open_trades": len(pairs),
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "cancel_open_orders_on_exit": False,
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "entry_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "other", "use_order_book": True, "order_book_top": 1},
        "order_types": {"entry": "market", "exit": "market", "stoploss": "market", "stoploss_on_exchange": False},
        "exchange": {"name": "binance", "key": "", "secret": "", "pair_whitelist": pairs},
        "pairlists": [{"method": "StaticPairList"}],
        "bot_name": "heatsignal-benchmark-ta",
        "internals": {"process_throttle_secs": 5},
    }


def run_backtest(name: str, cfg_path: Path, start: pd.Timestamp, end: pd.Timestamp, datadir: Path, userdir: Path,
                 raw_dir: Path, freqtrade: Path, wallet: float, run=subprocess.run) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    cls, path = (COMPAT[name][0], COMPAT_DIR) if name in COMPAT else (name, BENCH_DIR)
    cmd = [str(freqtrade), "backtesting", "-c", str(cfg_path), "--strategy", cls, "--strategy-path", str(path),
           "--datadir", str(datadir), "--userdir", str(userdir), "--timerange", _timerange(start, end),
           "--export", "trades", "--backtest-directory", str(raw_dir), "--dry-run-wallet", str(wallet),
           "--fee", str(FEE), "--cache", "none", "--notes", f"heatsignal benchmark TA {name}"]
    log.info("backtesting %s", name)
    r = run(cmd, capture_output=True, text=True, cwd=str(userdir.parent))
    if r.returncode != 0:
        raise RuntimeError(f"backtesting fallo (rc={r.returncode}): {((r.stderr or '') + (r.stdout or ''))[-1500:]}")
    last = json.loads((raw_dir / ".last_result.json").read_text())["latest_backtest"]
    return raw_dir / last


def load_result(zip_path: Path, name: str) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as z:
        main = [n for n in z.namelist() if n.endswith(".json") and not n.endswith("_config.json")]
        data = json.loads(z.read(main[0]))
    return data["strategy"][name]


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def summarize(res: dict[str, Any], start: pd.Timestamp, end: pd.Timestamp, wallet: float) -> dict[str, Any]:
    """Metricas comparables con el laboratorio desde los trades de Freqtrade."""
    trades = pd.DataFrame(res.get("trades", []))
    days = pd.date_range(start.normalize(), end.normalize() - pd.Timedelta(days=1), freq="1D", tz="UTC")
    if len(trades):
        close = pd.to_datetime(trades["close_timestamp"], unit="ms", utc=True).dt.floor("1D")
        daily_pnl = trades.groupby(close)["profit_abs"].sum()
        pnl_total = float(trades["profit_abs"].sum())
        funding = float(trades["funding_fees"].fillna(0).sum()) if "funding_fees" in trades else 0.0
        win = float((trades["profit_abs"] > 0).mean())
        n_short = int(trades["is_short"].sum()) if "is_short" in trades else 0
    else:
        daily_pnl, pnl_total, funding, win, n_short = pd.Series(dtype=float), 0.0, 0.0, None, 0
    daily = (daily_pnl.reindex(days, fill_value=0.0) / wallet).astype(float)
    return {
        "trades": int(len(trades)),
        "trades_long": int(len(trades) - n_short),
        "trades_short": n_short,
        "return_total": pnl_total / wallet,
        "sharpe_daily": _f(stats.sharpe_ratio(daily)),
        "max_dd": _f(stats.max_drawdown(daily)),
        "win_rate": win,
        "funding_total": funding / wallet,
        "days": int(len(days)),
        "freqtrade_native": {k: _f(res.get(k)) for k in ("profit_total", "cagr", "sharpe", "sortino", "calmar",
                                                          "max_drawdown_account", "final_balance", "market_change")},
    }


def benchmark(start: pd.Timestamp, end: pd.Timestamp, names: list[str], symbols: list[str], wallet: float,
              download: bool = True, out_dir: Path | None = None) -> dict[str, Any]:
    out_dir = out_dir or results_dir()
    raw_dir = out_dir / "raw"
    ft = ft_dir()
    freqtrade = ft / ".venv" / "bin" / "freqtrade"
    datadir = ft / "data"
    userdir = ft / "user_data_benchmark"
    verify_strategies(names)
    if not userdir.exists():
        subprocess.run([str(freqtrade), "create-userdir", "--userdir", str(userdir)], check=True, capture_output=True)
    pairs = [ft_pair(s) for s in symbols]
    tag = _timerange(start, end)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = out_dir / f"config_{tag}.json"
    cfg_path.write_text(json.dumps(build_config(pairs, wallet), indent=2))

    data_report = []
    for tf in sorted({STRATEGIES[n][1] for n in names}):
        if download:
            data_report.append(ensure_data(pairs, tf, start, end, datadir, freqtrade, userdir))
        else:
            data_report.append({"timeframe": tf, "downloads": [], "remaining_gaps": coverage_gaps(
                pairs, tf, start - pd.Timedelta(days=STARTUP_BUFFER_DAYS), end, datadir)})

    version = subprocess.run([str(freqtrade), "--version"], capture_output=True, text=True).stdout
    results = {}
    for name in names:
        entry: dict[str, Any] = {"timeframe": STRATEGIES[name][1], "ok": False, "error": None,
                                 "run_as": COMPAT[name][0] if name in COMPAT else name,
                                 "compat": COMPAT[name][1] if name in COMPAT else None}
        try:
            zip_path = run_backtest(name, cfg_path, start, end, datadir, userdir, raw_dir, freqtrade, wallet)
            res = load_result(zip_path, COMPAT[name][0] if name in COMPAT else name)
            entry.update(summarize(res, start, end, wallet))
            entry.update({"ok": True, "result_file": str(zip_path), "leverage_max": _f(max(
                (t.get("leverage", 1.0) for t in res.get("trades", [])), default=None))})
        except Exception as e:  # sin fallos silenciosos: queda registrado por estrategia
            log.exception("fallo %s", name)
            entry["error"] = f"{type(e).__name__}: {e}"
        results[name] = entry

    summary = {
        "kind": "benchmark-ta-freqtrade",
        "start": str(start), "end": str(end), "symbols": symbols, "pairs": pairs,
        "source": SOURCE,
        "freqtrade_version": next((ln.split(":", 1)[1].strip() for ln in version.splitlines()
                                   if ln.startswith("Freqtrade Version")), version.strip()),
        "settings": {"wallet": wallet, "stake_amount": wallet / len(pairs), "max_open_trades": len(pairs),
                     "fee_per_side": FEE, "slippage": 0.0, "funding": "real (funding_rate + mark de Binance)",
                     "margin_mode": "isolated", "reinvest": False},
        "notes": [
            "Estrategias de terceros sin modificar (sha256 verificado). No se re-optimizan.",
            "FOttStrategy, FSupertrendStrategy y TrendFollowingStrategy no declaran can_short: Freqtrade ignora "
            "sus senales short (corren solo long, tal como vienen).",
            "FOttStrategy y FReinforcedStrategy corren via subclases de compatibilidad (benchmark/compat/) que "
            "heredan del original sin cambiar parametros: ver 'compat' en cada resultado.",
            "VolatilitySystem fija leverage 2.0 y stoploss -1 en su propio codigo.",
            "Sharpe diario (365) y max DD sobre PnL REALIZADO por dia de cierre / wallet inicial (sin reinversion).",
            "Cada estrategia cuenta como un intento para el Deflated Sharpe del programa.",
        ],
        "data": data_report,
        "results": results,
    }
    path = out_dir / f"summary_{tag}.json"
    path.write_text(json.dumps(summary, indent=2, default=str))
    summary["summary_file"] = str(path)
    return summary


def _ts(s: str) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", required=True, help="inicio (UTC, inclusive), ej. 2021-03-13")
    p.add_argument("--end", required=True, help="fin (UTC, exclusivo), ej. 2026-07-15")
    p.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    p.add_argument("--symbols", nargs="+", default=list(SEVEN))
    p.add_argument("--wallet", type=float, default=10_000.0)
    p.add_argument("--no-download", action="store_true", help="no bajar datos faltantes (solo declararlos)")
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start, end = _ts(a.start), _ts(a.end)
    if end <= start:
        p.error("--end tiene que ser posterior a --start")
    s = benchmark(start, end, a.strategies, a.symbols, a.wallet, download=not a.no_download, out_dir=a.out)
    rows = [(n, r.get("trades"), r.get("return_total"), r.get("sharpe_daily"), r.get("max_dd"), r.get("error"))
            for n, r in s["results"].items()]
    for n, t, ret, sh, dd, err in rows:
        print(f"{n:24s} trades={t} ret={ret} sharpe={sh} dd={dd}" + (f" ERROR {err}" if err else ""))
    print(f"resumen: {s['summary_file']}")
    return 0 if all(r["ok"] for r in s["results"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
