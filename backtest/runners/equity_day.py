"""Equity day runner for S1 (RSI Bounce) + S3 (RSI 15-min).

Delegates to simulate_range.simulate_one_day (the canonical S1/S3 simulation,
kept in place so the legacy script stays runnable), then converts the result
dicts to TradeRecord rows WITH intraday equity fees + slippage applied —
the legacy path applies none.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from simulate_range import simulate_one_day
from src.fees import calculate_equity_fees, estimate_equity_slippage
from backtest.records import TradeRecord


def _to_record(t: dict, d: date, regime: str, apply_fees: bool = True) -> TradeRecord:
    """Convert one simulate_one_day trade dict to a TradeRecord with fees."""
    qty = int(t.get("initial_qty", t.get("quantity", 0)))
    entry_p = float(t["entry_price"])
    exit_p = float(t.get("exit_price", entry_p))
    gross = float(t["realized_pnl"])
    side = t.get("side", "LONG")

    fees = 0.0
    if apply_fees and qty > 0:
        # entry + exit orders (partial exits are folded into one round trip:
        # the simulator realizes all P&L on the same symbol/day, and brokerage
        # is per executed order — VWAP60 partials add one extra order)
        n_orders = 3 if t.get("vwap_exit_done") else 2
        buy_fees = calculate_equity_fees(entry_p, qty, "BUY")["total"]
        sell_fees = calculate_equity_fees(exit_p, qty, "SELL")["total"]
        extra_brokerage = 20.0 * (n_orders - 2)
        slip = estimate_equity_slippage(entry_p, qty) + estimate_equity_slippage(exit_p, qty)
        if side == "SHORT":
            buy_fees, sell_fees = (calculate_equity_fees(exit_p, qty, "BUY")["total"],
                                   calculate_equity_fees(entry_p, qty, "SELL")["total"])
        fees = buy_fees + sell_fees + extra_brokerage + slip

    return TradeRecord(
        strategy="rsi_bounce" if t["strategy"] == "S1" else "rsi_15min",
        date=str(d),
        direction="bearish" if side == "SHORT" else "bullish",
        entry_time=t.get("entry_time", ""),
        exit_time="",
        entry_underlying=entry_p,
        exit_underlying=exit_p,
        entry_premium=entry_p,
        exit_premium=exit_p,
        qty=qty,
        gross_pnl=gross,
        fees=round(fees, 2),
        net_pnl=round(gross - fees, 2),
        reason=t.get("exit_reason", ""),
        setup=t["strategy"],
        symbol=t.get("symbol", ""),
        instrument="equity",
        regime=regime,
    )


def run_equity_day(data: dict[str, pd.DataFrame], d: date,
                   daily_regime: dict[str, bool], market_regime, regime_details: dict,
                   apply_fees: bool = True,
                   quality_gates: dict | None = None) -> list[TradeRecord]:
    """Run S1+S3 on one day's pre-loaded equity data."""
    result = simulate_one_day(data, daily_regime, market_regime, regime_details,
                              quality_gates=quality_gates)
    if not result:
        return []
    records = []
    regime_name = getattr(market_regime, "name", str(market_regime))
    for t in result.get("s1_details", []) + result.get("s3_details", []):
        records.append(_to_record(t, d, regime_name, apply_fees))
    return records
