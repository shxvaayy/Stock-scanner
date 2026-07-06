"""Precomputed daily regimes for the backtest.

Unlike the live/legacy paths this uses ONLY information available at the
morning of each day: Nifty daily candles through the prior session and the
prior session's VIX close. (The legacy simulate_range fetch included the
target day's own close — lookahead.)
"""

from __future__ import annotations

import pickle
from datetime import date
from pathlib import Path

import pandas as pd

from core.regime import MarketRegime, classify_regime_from_data
from backtest import data_store

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "backtest_cache"


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm.shift(0)) & (minus_dm > 0), 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
    return dx.ewm(alpha=1 / period, min_periods=period).mean()


def build_market_regime_table(days: list[date]) -> dict[date, tuple[MarketRegime, dict]]:
    """Regime per day from Nifty daily candles up to the PRIOR session
    + prior session VIX close."""
    nifty = data_store.load_nifty_daily_df()
    out: dict[date, tuple[MarketRegime, dict]] = {}
    if nifty is None or len(nifty) < 60:
        return {d: (MarketRegime.BULL, {"error": "no_daily_data"}) for d in days}
    nifty = nifty.copy()
    nifty["d"] = pd.to_datetime(nifty["timestamp"]).dt.date
    for d in days:
        sub = nifty[nifty["d"] < d]
        if len(sub) < 60:
            out[d] = (MarketRegime.BULL, {"error": "insufficient"})
            continue
        vix = data_store.vix_close_before(d)
        regime, details = classify_regime_from_data(
            sub[["timestamp", "open", "high", "low", "close", "volume"]].copy(),
            vix_value=vix, current_nifty=None,
        )
        details["vix_used"] = vix
        out[d] = (regime, details)
    return out


def build_stock_daily_regime(days: list[date],
                             symbols: list[str]) -> dict[date, dict[str, bool]]:
    """Per-stock 2-of-3 daily check (price within 8% of EMA200, RSI14 in 30-65,
    ADX14 < 25) using daily data through the PRIOR session.

    Mirrors simulate_range.fetch_daily_regime but vectorized offline and
    without the target-day lookahead.
    """
    per_sym_tables: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        ddf = data_store.load_equity_daily(sym)
        if ddf is None or len(ddf) < 60:
            continue
        ddf = ddf.copy()
        ddf["close"] = pd.to_numeric(ddf["close"], errors="coerce")
        ddf["high"] = pd.to_numeric(ddf["high"], errors="coerce")
        ddf["low"] = pd.to_numeric(ddf["low"], errors="coerce")
        ddf["ema200"] = _ema(ddf["close"], 200)
        ddf["rsi14"] = _rsi(ddf["close"], 14)
        ddf["adx14"] = _adx(ddf["high"], ddf["low"], ddf["close"], 14)
        c1 = (ddf["close"] - ddf["ema200"]).abs() / ddf["ema200"] < 0.08
        c2 = ddf["rsi14"].between(30, 65)
        c3 = ddf["adx14"] < 25
        ddf["pass_2of3"] = (c1.fillna(False).astype(int)
                            + c2.fillna(False).astype(int)
                            + c3.fillna(False).astype(int)) >= 2
        ddf["d"] = pd.to_datetime(ddf["timestamp"]).dt.date
        per_sym_tables[sym] = ddf[["d", "pass_2of3"]]

    out: dict[date, dict[str, bool]] = {}
    for d in days:
        day_map: dict[str, bool] = {}
        for sym, tbl in per_sym_tables.items():
            sub = tbl[tbl["d"] < d]
            # default True (same as legacy when data missing)
            day_map[sym] = bool(sub["pass_2of3"].iloc[-1]) if len(sub) else True
        out[d] = day_map
    return out


def build_all(days: list[date], symbols: list[str],
              cache_name: str = "regime_table.pkl", force: bool = False):
    """Build (or load) both tables, cached as one pickle."""
    path = CACHE_DIR / cache_name
    if path.exists() and not force:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        if set(days).issubset(set(cached["market"].keys())):
            return cached["market"], cached["stocks"]
    market = build_market_regime_table(days)
    stocks = build_stock_daily_regime(days, symbols)
    with open(path, "wb") as f:
        pickle.dump({"market": market, "stocks": stocks}, f)
    return market, stocks
