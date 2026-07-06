"""Bulk historical data fetcher for the 12-month backtest.

Fetches and caches (under data/backtest_cache/):
  eq_{SYMBOL}_{date}.pkl     per-day 1-min DataFrame [timestamp,open,high,low,close,volume]
  eq_daily_{SYMBOL}.pkl      daily DataFrame from 2024-01-01 (for 200-DMA / regime)
  nifty_idx_{date}.pkl       per-day 1-min list[Candle], Nifty index 99926000
  nifty_fut_{date}_{tok}.pkl per-day 1-min list[Candle], Nifty futures (longest-history token)
  nifty_daily_full.pkl       daily list[Candle] from 2024-01-01
  vix_daily.pkl              India VIX daily DataFrame
  vix_1m_{date}.pkl          India VIX 1-min DataFrame, expiry days only

Resumable: data/backtest_cache/fetch_manifest.json records completed chunks.
The Angel One historical API silently truncates responses to ~8000 rows, so
1-min data is fetched in 18-calendar-day chunks (~13 trading days x 375 rows)
with a truncation guard.

Usage: python scripts/fetch_history.py 2025-06-02 2026-06-09
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyotp
from dotenv import load_dotenv
from SmartApi import SmartConnect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from config.universe import STOCKS, INDEX_TOKENS, build_token_map
from models.types import Candle
from src.expiry import is_nifty_expiry_day

CACHE_DIR = ROOT / "data" / "backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)
MANIFEST_PATH = CACHE_DIR / "fetch_manifest.json"

THROTTLE = 0.4
CHUNK_DAYS = 18
TRUNCATION_ROWS = 7900  # responses at/above this are suspect (API cap ~8000)


# ── manifest ──
def load_manifest() -> set[str]:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return set(json.load(f))
    return set()


def save_manifest(done: set[str]) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump(sorted(done), f)


# ── api ──
def login() -> SmartConnect:
    api = SmartConnect(os.getenv("ANGEL_API_KEY"))
    totp = pyotp.TOTP(os.getenv("ANGEL_TOTP_SECRET")).now()
    sess = api.generateSession(os.getenv("ANGEL_CLIENT_ID"),
                               os.getenv("ANGEL_PASSWORD"), totp)
    if not sess or not sess.get("status"):
        raise RuntimeError(f"login failed: {sess}")
    return api


class Fetcher:
    def __init__(self):
        self.api = login()
        self.calls = 0

    def candles(self, exchange: str, token: str, interval: str,
                frm: str, to: str) -> list | None:
        """getCandleData with throttle, retries, and one re-login."""
        for attempt in range(4):
            time.sleep(THROTTLE * (1 + attempt))
            try:
                res = self.api.getCandleData({
                    "exchange": exchange, "symboltoken": str(token),
                    "interval": interval, "fromdate": frm, "todate": to,
                })
                self.calls += 1
                if res and res.get("status") is False and "rate" in str(res.get("message", "")).lower():
                    time.sleep(2 * (attempt + 1))
                    continue
                return (res or {}).get("data") or []
            except Exception as e:
                msg = str(e)
                if attempt == 1 and ("session" in msg.lower() or "token" in msg.lower()):
                    print("    re-login...")
                    self.api = login()
                elif attempt == 3:
                    print(f"    FAILED {exchange}/{token} {frm}: {e}")
                    return None
                time.sleep(2 * (attempt + 1))
        return None


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        out.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return out


def rows_to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    df["volume"] = df["volume"].astype(int)
    return df


def rows_to_candles(rows: list, token: str, symbol: str) -> list[Candle]:
    candles = []
    for row in rows:
        ts = datetime.fromisoformat(row[0])
        if ts.tzinfo:
            ts = ts.replace(tzinfo=None)
        candles.append(Candle(
            timestamp=ts, open=float(row[1]), high=float(row[2]),
            low=float(row[3]), close=float(row[4]), volume=int(row[5]),
            token=token, symbol=symbol,
        ))
    return candles


def split_days_df(df: pd.DataFrame) -> dict[date, pd.DataFrame]:
    return {d: g.reset_index(drop=True)
            for d, g in df.groupby(df["timestamp"].dt.date)}


def split_days_candles(candles: list[Candle]) -> dict[date, list[Candle]]:
    out: dict[date, list[Candle]] = {}
    for c in candles:
        out.setdefault(c.timestamp.date(), []).append(c)
    return out


def fetch_1m_series(fetcher: Fetcher, done: set[str], key: str, exchange: str,
                    token: str, start: date, end: date,
                    write_day) -> None:
    """Fetch a 1-min series in chunks; write per-day files via write_day(day, rows)."""
    for c0, c1 in month_chunks(start, end):
        chunk_key = f"{key}:{c0}:{c1}"
        if chunk_key in done:
            continue
        rows = fetcher.candles(exchange, token, "ONE_MINUTE",
                               f"{c0} 09:15", f"{c1} 15:30")
        if not rows:
            # None (error) or [] (rate-limit storms return SUCCESS with no
            # data) — leave un-manifested so the next run retries
            continue
        if len(rows) >= TRUNCATION_ROWS:
            # bisect: refetch in two halves
            mid = c0 + (c1 - c0) / 2
            half1 = fetcher.candles(exchange, token, "ONE_MINUTE",
                                    f"{c0} 09:15", f"{mid} 15:30") or []
            half2 = fetcher.candles(exchange, token, "ONE_MINUTE",
                                    f"{mid + timedelta(days=1)} 09:15", f"{c1} 15:30") or []
            rows = half1 + half2
        for d, day_rows in _group_rows_by_day(rows).items():
            write_day(d, day_rows)
        done.add(chunk_key)
        save_manifest(done)


def _group_rows_by_day(rows: list) -> dict[date, list]:
    out: dict[date, list] = {}
    for row in rows:
        d = datetime.fromisoformat(row[0]).date()
        out.setdefault(d, []).append(row)
    return out


def main():
    if len(sys.argv) < 3:
        print("Usage: fetch_history.py YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])

    fetcher = Fetcher()
    done = load_manifest()
    token_map = build_token_map(symbols=STOCKS)
    print(f"fetching {start} -> {end} | {len(token_map)} equities | manifest: {len(done)} chunks done")

    t0 = time.time()

    # 1. Nifty daily (regime + 200-DMA + RSI predictor)
    if "nifty_daily_full" not in done:
        rows = fetcher.candles("NSE", INDEX_TOKENS["NIFTY"], "ONE_DAY",
                               "2024-01-01 09:15", f"{end} 15:30")
        if rows:
            with open(CACHE_DIR / "nifty_daily_full.pkl", "wb") as f:
                pickle.dump(rows_to_candles(rows, INDEX_TOKENS["NIFTY"], "NIFTY"), f)
            done.add("nifty_daily_full"); save_manifest(done)
            print(f"  nifty daily: {len(rows)} rows")

    # 2. VIX daily
    if "vix_daily" not in done:
        rows = fetcher.candles("NSE", INDEX_TOKENS["INDIA_VIX"], "ONE_DAY",
                               "2024-01-01 09:15", f"{end} 15:30")
        if rows:
            with open(CACHE_DIR / "vix_daily.pkl", "wb") as f:
                pickle.dump(rows_to_df(rows), f)
            done.add("vix_daily"); save_manifest(done)
            print(f"  vix daily: {len(rows)} rows")

    # 3. Equity daily per symbol
    for sym, tok in token_map.items():
        key = f"eq_daily:{sym}"
        if key in done:
            continue
        rows = fetcher.candles("NSE", tok, "ONE_DAY", "2024-01-01 09:15", f"{end} 15:30")
        if rows:
            with open(CACHE_DIR / f"eq_daily_{sym}.pkl", "wb") as f:
                pickle.dump(rows_to_df(rows), f)
            done.add(key); save_manifest(done)
    print(f"  equity dailies done ({fetcher.calls} calls, {time.time()-t0:.0f}s)")

    # 4. Nifty index 1-min
    def write_idx_day(d, day_rows):
        with open(CACHE_DIR / f"nifty_idx_{d}.pkl", "wb") as f:
            pickle.dump(rows_to_candles(day_rows, INDEX_TOKENS["NIFTY"], "NIFTY"), f)
    fetch_1m_series(fetcher, done, "nifty_idx", "NSE", INDEX_TOKENS["NIFTY"],
                    start, end, write_idx_day)
    print(f"  nifty index 1-min done ({fetcher.calls} calls)")

    # 5. Nifty futures 1-min — pick the listed FUTIDX token with the deepest history
    with open(ROOT / "data" / "instruments.json") as f:
        inst = pd.DataFrame(json.load(f))
    fut = inst[(inst["name"] == "NIFTY") & (inst["instrumenttype"] == "FUTIDX")
               & (inst["exch_seg"] == "NFO")].copy()
    fut["expiry_dt"] = pd.to_datetime(fut["expiry"], format="mixed", dayfirst=True,
                                      errors="coerce")
    fut_token = str(fut.sort_values("expiry_dt").iloc[0]["token"])

    def write_fut_day(d, day_rows):
        path = CACHE_DIR / f"nifty_fut_{d}_{fut_token}.pkl"
        with open(path, "wb") as f:
            pickle.dump(rows_to_candles(day_rows, fut_token, "NIFTY_FUT"), f)
    fetch_1m_series(fetcher, done, f"nifty_fut_{fut_token}", "NFO", fut_token,
                    start, end, write_fut_day)
    print(f"  nifty futures 1-min done ({fetcher.calls} calls)")

    # 6. VIX 1-min on expiry days (for the S2 2PM gate)
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and is_nifty_expiry_day(cur):
            key = f"vix_1m:{cur}"
            if key not in done:
                rows = fetcher.candles("NSE", INDEX_TOKENS["INDIA_VIX"], "ONE_MINUTE",
                                       f"{cur} 09:15", f"{cur} 15:30")
                if rows:
                    with open(CACHE_DIR / f"vix_1m_{cur}.pkl", "wb") as f:
                        pickle.dump(rows_to_df(rows), f)
                done.add(key); save_manifest(done)
        cur += timedelta(days=1)
    print(f"  vix expiry-day 1-min done ({fetcher.calls} calls)")

    # 7. Equity 1-min per symbol (the bulk of the calls)
    for i, (sym, tok) in enumerate(token_map.items(), 1):
        def write_eq_day(d, day_rows, sym=sym):
            df = pd.DataFrame(day_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            with open(CACHE_DIR / f"eq_{sym}_{d}.pkl", "wb") as f:
                pickle.dump(df, f)
        fetch_1m_series(fetcher, done, f"eq_{sym}", "NSE", tok, start, end, write_eq_day)
        elapsed = time.time() - t0
        print(f"  [{i}/{len(token_map)}] {sym} done | {fetcher.calls} calls | {elapsed:.0f}s")

    print(f"\nfetch complete: {fetcher.calls} API calls, {time.time()-t0:.0f}s")
    day_files = len(list(CACHE_DIR.glob("eq_*_2*.pkl")))
    print(f"equity day-files in cache: {day_files}")


if __name__ == "__main__":
    main()
