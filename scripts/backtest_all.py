"""Unified backtest CLI — all 6 strategies, cache-only, train/test split.

Usage:
  python scripts/backtest_all.py --start 2025-06-02 --end 2026-06-09 \
      --split 2026-02-01 --strategies all
  --strategies: comma list of s1,s3,s2,ls,vp,rp or 'all'
  --no-fees: disable equity fees (parity mode vs legacy simulate_range)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from models.types import Candle  # noqa: E402
from strategies.indicators import ema  # noqa: E402
from strategies.rsi_predictor import classify_eod_pattern  # noqa: E402
from src.expiry import is_nifty_expiry_day  # noqa: E402

from backtest import data_store, regime_cache  # noqa: E402
from backtest.records import TradeRecord  # noqa: E402
from backtest.report import to_dataframe, print_report, save  # noqa: E402
from backtest.runners.options_day import simulate_strategy_day  # noqa: E402
from backtest.runners.equity_day import run_equity_day  # noqa: E402
from backtest.runners.condor_day import run_condor_day, CondorParams  # noqa: E402

from config.universe import STOCKS  # noqa: E402

OPTIONS_STRATS = {"ls": "liquidity_sweep", "vp": "vp_trend", "rp": "rsi_predictor"}


def run(start: date, end: date, split: date | None, strats: set[str],
        apply_equity_fees: bool = True, condor_params: CondorParams | None = None,
        vp_params: dict | None = None, ls_params: dict | None = None,
        eq_quality_gates: dict | None = None,
        quiet: bool = False) -> pd.DataFrame:
    days = data_store.available_underlying_days(start, end)
    if not days:
        print("No cached underlying days in range — run scripts/fetch_history.py first.")
        sys.exit(1)
    if not quiet:
        print(f"{len(days)} cached trading days {days[0]} .. {days[-1]}")

    # daily context
    daily_candles = data_store.load_nifty_daily() or []
    daily_closes = [c.close for c in daily_candles]
    daily_ema_series = ema(daily_closes, 200) if daily_closes else []
    daily_dates = {c.timestamp.date(): i for i, c in enumerate(daily_candles)}

    need_equity = strats & {"s1", "s3"}
    market_regimes, stock_regimes = ({}, {})
    if need_equity:
        if not quiet:
            print("building regime tables...")
        market_regimes, stock_regimes = regime_cache.build_all(days, STOCKS)

    all_trades: list[TradeRecord] = []
    prior_session: list[Candle] | None = None
    prior_close = 0.0
    pending_rsi_signal = None

    for d in days:
        candles_1m, source = data_store.load_underlying_day(d)
        if not candles_1m:
            continue

        idx_today = daily_dates.get(d)
        if idx_today is None or idx_today < 200:
            ema200 = daily_closes[idx_today] if idx_today is not None else 0
        else:
            ema200 = daily_ema_series[idx_today - 1]

        day_trades: list[TradeRecord] = []

        for code, name in OPTIONS_STRATS.items():
            if code not in strats:
                continue
            sig = pending_rsi_signal if code == "rp" else None
            extra = vp_params if code == "vp" else (ls_params if code == "ls" else None)
            day_trades += simulate_strategy_day(
                name, candles_1m, prior_close, prior_session,
                daily_closes, sig, ema200, d, extra_params=extra,
            )

        if "s2" in strats and is_nifty_expiry_day(d):
            vix = data_store.vix_at(d, 14, 0)
            condor_trades, skip = run_condor_day(candles_1m, d, vix, condor_params)
            day_trades += condor_trades

        if need_equity:
            eq_data = data_store.load_all_equities_day(d)
            if eq_data:
                regime, details = market_regimes.get(d, (None, {}))
                day_trades += run_equity_day(
                    eq_data, d, stock_regimes.get(d, {}), regime, details,
                    apply_fees=apply_equity_fees,
                    quality_gates=eq_quality_gates,
                )

        all_trades += day_trades

        # EOD pattern scan for tomorrow's RSI predictor
        if "rp" in strats and idx_today is not None and idx_today >= 30:
            closes_up_to_today = daily_closes[:idx_today + 1]
            pattern = classify_eod_pattern(closes_up_to_today, regime="BULL")
            pending_rsi_signal = {"signal": pattern, "vix": 14}
        else:
            pending_rsi_signal = None

        prior_session = candles_1m
        prior_close = candles_1m[-1].close

        if not quiet and day_trades:
            pnl = sum(t.net_pnl for t in day_trades)
            print(f"  {d} [{source}] {len(day_trades)}t net ₹{pnl:+,.0f}")

    return to_dataframe(all_trades, split)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--split", default=None)
    ap.add_argument("--strategies", default="all")
    ap.add_argument("--no-fees", action="store_true",
                    help="disable equity fees (parity mode)")
    ap.add_argument("--vp-mode", default="fade", choices=["fade", "breakout", "both"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    split = date.fromisoformat(args.split) if args.split else None
    strats = ({"s1", "s2", "s3", "ls", "vp", "rp"} if args.strategies == "all"
              else set(args.strategies.split(",")))

    df = run(start, end, split, strats, apply_equity_fees=not args.no_fees,
             vp_params={"mode": args.vp_mode})
    print_report(df, f"{start} → {end} (split {split})")
    name = args.out or f"trades_{start}_{end}.csv"
    if not df.empty:
        path = save(df, name)
        print(f"\n  saved: {path}")


if __name__ == "__main__":
    main()
