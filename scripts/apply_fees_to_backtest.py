"""Parse simulate_range.py output and recompute net P&L with fees.

The simulator does NOT model brokerage/STT. This post-processes its trade list
and applies the same fees module the live bot uses (calculate_equity_fees).

Usage: python scripts/apply_fees_to_backtest.py /tmp/feb2026_backtest.log
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fees import calculate_equity_fees

# Trade line format from simulate_range.py:
# [W|L] YYYY-MM-DD HH:MM S1|S3 SYMBOL    Rs<entry> → <exit> | <reason> | Rs<pnl>
TRADE_RE = re.compile(
    r"\[(W|L)\]\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(S1|S3)\s+(\S+)\s+"
    r"Rs([\d,.]+)\s*→\s*([\d,.]+)\s*\|\s*\S+\s*\|\s*Rs([+-]?[\d,.]+)"
)


def parse_trades(log_path):
    trades = []
    with open(log_path) as f:
        for line in f:
            m = TRADE_RE.search(line)
            if not m:
                continue
            outcome, date_str, time_str, strat, symbol, entry, exit_, pnl = m.groups()
            entry = float(entry.replace(",", ""))
            exit_ = float(exit_.replace(",", ""))
            pnl = float(pnl.replace(",", ""))
            # Recover qty from gross P&L (simulator P&L = (exit - entry) * qty for longs)
            diff = exit_ - entry
            if abs(diff) < 1e-9:
                qty = 1  # zero-move trade — single share assumption
            else:
                qty = round(pnl / diff)
                qty = abs(qty) if qty != 0 else 1
            trades.append({
                "date": date_str, "time": time_str, "strategy": strat,
                "symbol": symbol, "outcome": outcome,
                "entry": entry, "exit": exit_,
                "qty": qty, "gross_pnl": pnl,
            })
    return trades


def main():
    if len(sys.argv) < 2:
        print("Usage: apply_fees_to_backtest.py <backtest.log>")
        sys.exit(1)

    trades = parse_trades(sys.argv[1])
    if not trades:
        print("No trades parsed from log.")
        sys.exit(1)

    capital = 250_000  # matches simulate_range.py CAPITAL
    total_gross = 0.0
    total_net = 0.0
    total_fees = 0.0
    s1_net = 0.0
    s3_net = 0.0
    wins_net = 0
    losses_net = 0
    max_position_value = 0.0  # largest single position deployed

    for t in trades:
        # Fees: BUY-side at entry + SELL-side at exit (long equity)
        entry_fees = calculate_equity_fees(t["entry"], t["qty"], "BUY")["total"]
        exit_fees = calculate_equity_fees(t["exit"], t["qty"], "SELL")["total"]
        total_trade_fees = entry_fees + exit_fees
        net_pnl = t["gross_pnl"] - total_trade_fees

        t["entry_fees"] = entry_fees
        t["exit_fees"] = exit_fees
        t["total_fees"] = total_trade_fees
        t["net_pnl"] = net_pnl

        position_value = t["entry"] * t["qty"]
        max_position_value = max(max_position_value, position_value)

        total_gross += t["gross_pnl"]
        total_net += net_pnl
        total_fees += total_trade_fees
        if t["strategy"] == "S1":
            s1_net += net_pnl
        else:
            s3_net += net_pnl
        if net_pnl > 0:
            wins_net += 1
        else:
            losses_net += 1

    n = len(trades)

    print("=" * 76)
    print("  Net P&L after applying real Indian equity brokerage + STT")
    print("  (using src/fees.py:calculate_equity_fees — same module the live bot uses)")
    print("=" * 76)
    print()
    print(f"  Period:           Feb 2026 (20 trading days)")
    print(f"  Strategies run:   S1 (RSI Bounce, 1-min) + S3 (RSI 15-min)")
    print(f"  Total trades:     {n}")
    print(f"  Wins (net):       {wins_net}")
    print(f"  Losses (net):     {losses_net}")
    print(f"  Win rate (net):   {wins_net/n*100:.1f}%")
    print()
    print(f"  Gross P&L:        ₹{total_gross:+,.2f}  (what simulator reports)")
    print(f"  Total fees paid:  ₹{total_fees:,.2f}")
    print(f"  Net P&L:          ₹{total_net:+,.2f}  (what actually lands in your account)")
    print()
    print(f"  S1 net P&L:       ₹{s1_net:+,.2f}")
    print(f"  S3 net P&L:       ₹{s3_net:+,.2f}")
    print()
    print(f"  Avg net per trade: ₹{total_net/n:+,.2f}")
    print(f"  Avg fees per trade: ₹{total_fees/n:.2f}  ({total_fees/(abs(total_gross)+0.01)*100:.1f}% of gross movement)")
    print()
    print("─" * 76)
    print("  CAPITAL DEPLOYMENT")
    print("─" * 76)
    print(f"  Strategy capital allocation (configured):  ₹{capital:,}")
    print(f"  Largest single position taken:              ₹{max_position_value:,.0f}")
    print(f"  Final balance:                              ₹{capital + total_net:,.2f}")
    print()
    print(f"  YOUR MONEY PUT IN (X):    ₹{capital:,}")
    print(f"  PROFIT/LOSS (Y):          ₹{total_net:+,.2f}")
    print(f"  RETURN ON CAPITAL:        {total_net/capital*100:+.2f}%")
    print()
    print("=" * 76)
    print("  NOTES — read this before drawing any conclusion")
    print("=" * 76)
    print()
    print("  1. This is ONE month of backtest data, not a real trading record.")
    print("     20 days is a TINY sample — month-to-month variance can easily")
    print("     swing a strategy from -3% to +5% just from luck of the draw.")
    print()
    print("  2. Strategies tested:  ONLY 2 of the 6 you'll eventually have")
    print("     - S1 (RSI Bounce)            ✓ tested")
    print("     - S3 (RSI 15-min)            ✓ tested")
    print("     - S2 (Expiry Skew options)   ✗ NOT in this simulator")
    print("     - 3 new options strategies   ✗ NOT IMPLEMENTED YET")
    print()
    print("  3. Fees applied: brokerage ₹20/order, STT 0.025% sell-side,")
    print("     stamp 0.003% buy, GST 18% on brokerage+exchange charges,")
    print("     SEBI fee, exchange transaction charge — full Indian fee stack.")
    print()
    print("  4. NOT modeled: slippage, partial fills, stop-loss order rejections.")
    print("     Real fills will be slightly worse than these numbers suggest.")
    print()


if __name__ == "__main__":
    main()
