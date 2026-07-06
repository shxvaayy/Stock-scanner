"""Cache-only data loaders for the backtest harness.

Reads pickles written by scripts/fetch_history.py (and the legacy
nifty_fut_*_66691.pkl files from the original options backtest). Never
touches the network — a cache miss returns None.
"""

from __future__ import annotations

import pickle
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "backtest_cache"

from config.universe import STOCKS  # noqa: E402


def _load(path: Path):
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


# ── underlying (Nifty 1-min) ──
def load_underlying_day(d: date) -> tuple[list, str] | tuple[None, str]:
    """1-min Nifty candles for a day.

    Prefers the futures file with the highest total volume (front-month beats
    far-month), falls back to the index series (volume = 0).
    Returns (candles, source) where source is 'fut' or 'idx'.
    """
    fut_files = sorted(CACHE_DIR.glob(f"nifty_fut_{d}_*.pkl"))
    best = None
    for p in fut_files:
        candles = _load(p)
        if candles and len(candles) >= 60:
            vol = sum(c.volume for c in candles)
            if best is None or vol > best[1]:
                best = (candles, vol)
    if best:
        return best[0], "fut"
    idx = _load(CACHE_DIR / f"nifty_idx_{d}.pkl")
    if idx and len(idx) >= 60:
        return idx, "idx"
    return None, "none"


def available_underlying_days(start: date, end: date) -> list[date]:
    """Trading days in [start, end] that have any Nifty 1-min cache file."""
    days = set()
    for p in CACHE_DIR.glob("nifty_fut_*.pkl"):
        parts = p.stem.split("_")
        try:
            days.add(date.fromisoformat(parts[2]))
        except (ValueError, IndexError):
            pass
    for p in CACHE_DIR.glob("nifty_idx_*.pkl"):
        try:
            days.add(date.fromisoformat(p.stem.split("_")[2]))
        except (ValueError, IndexError):
            pass
    return sorted(d for d in days if start <= d <= end)


# ── equities ──
def load_equity_day(symbol: str, d: date) -> pd.DataFrame | None:
    df = _load(CACHE_DIR / f"eq_{symbol}_{d}.pkl")
    if df is not None and len(df) > 50:
        return df.copy()
    return None


def load_all_equities_day(d: date, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in (symbols or STOCKS):
        df = load_equity_day(sym, d)
        if df is not None:
            out[sym] = df
    return out


@lru_cache(maxsize=128)
def load_equity_daily(symbol: str) -> pd.DataFrame | None:
    """Full daily history for one symbol (from 2024-01-01)."""
    return _load(CACHE_DIR / f"eq_daily_{symbol}.pkl")


# ── index daily / VIX ──
@lru_cache(maxsize=1)
def load_nifty_daily() -> list | None:
    """Daily Nifty list[Candle] from 2024-01-01."""
    return _load(CACHE_DIR / "nifty_daily_full.pkl")


def load_nifty_daily_df() -> pd.DataFrame | None:
    candles = load_nifty_daily()
    if not candles:
        return None
    return pd.DataFrame([{
        "timestamp": c.timestamp, "open": c.open, "high": c.high,
        "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles])


@lru_cache(maxsize=1)
def load_vix_daily() -> pd.DataFrame | None:
    return _load(CACHE_DIR / "vix_daily.pkl")


def load_vix_1m(d: date) -> pd.DataFrame | None:
    return _load(CACHE_DIR / f"vix_1m_{d}.pkl")


def vix_close_before(d: date) -> float | None:
    """Most recent VIX daily close strictly before day d (what you know at 9:15)."""
    vix = load_vix_daily()
    if vix is None:
        return None
    mask = vix["timestamp"].dt.date < d
    sub = vix[mask]
    if sub.empty:
        return None
    return float(sub["close"].iloc[-1])


def vix_at(d: date, hh: int, mm: int) -> float | None:
    """Intraday VIX at a specific minute (expiry days), falling back to daily close."""
    intraday = load_vix_1m(d)
    if intraday is not None and len(intraday):
        ts_mask = (intraday["timestamp"].dt.hour * 60 + intraday["timestamp"].dt.minute) <= hh * 60 + mm
        sub = intraday[ts_mask]
        if len(sub):
            return float(sub["close"].iloc[-1])
    vix = load_vix_daily()
    if vix is not None:
        mask = vix["timestamp"].dt.date == d
        sub = vix[mask]
        if len(sub):
            return float(sub["close"].iloc[-1])
    return vix_close_before(d)
