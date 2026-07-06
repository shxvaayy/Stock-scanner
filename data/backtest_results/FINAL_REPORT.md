# AutoTheta Strategy Overhaul — Final Validation Report
**Date:** 2026-06-12
**Data:** 12 months 1-min (Jun 2025 – Jun 2026), 59 equities (Nifty 50 + 9 liquid watchlist names), Nifty index/futures, India VIX (incl. intraday). All results net of full Indian fee stack (verified rates: Rs 20/order, NSE options 0.03553% premium, STT 0.1% options sell / 0.1% delivery both sides / 0.025% intraday sell) + conservative slippage.
**Method:** train (through Jan 2026) / held-out test (Feb – Jun 2026, evaluated once). Pass = net-positive on test with ≥10 trades.

## Verdicts

| Slot | Strategy | Train | Test | Verdict |
|---|---|---|---|---|
| S1→new | **RSI-2 Swing** (Connors + Alvarez limit-dip) | **+₹8,503** (86t, 55% WR) | **+₹24,720** (33t, 67% WR) | ✅ **PASS — enabled (paper)** |
| S2 | Expiry Skew → rich-side credit vertical + gates | +₹273 (6 expiries) | -₹432 (2 expiries) | ⚠️ Insufficient evidence — restructured, paper-gate 8 expiries |
| S5 | VP Trend → Dalton breakout + CVD gate | +₹2,148 (4t, good-volume window) | +₹116 (3t) | ⚠️ Insufficient evidence — disabled, needs front-month volume data |
| S1 old | RSI Bounce intraday | **-₹255,200** (1,835t, 18% WR, gross-negative) | not run | ❌ Disabled |
| S3 | RSI 15-min intraday | **-₹53,166** (392t, 26% WR, gross-negative) | not run | ❌ Disabled |
| S4 | Liquidity Sweep (all variants: reversal / continuation / 80-20) | -₹2,342 … -₹16,166 | not run | ❌ Disabled |
| S6 | RSI Predictor (universe-expanded) | -₹2,214 (15t) | not run | ❌ Disabled |

## The winner: RSI-2 Swing

Rules (all from documented sources — Connors RSI-2, Alvarez limit-entry):
- **Setup (EOD):** daily close > SMA200 AND RSI(2) < 10
- **Entry:** next-session **limit order at signal close × 0.98** (fills only on a further dip; order lives one session)
- **Exit (at close):** RSI(2) > 65 OR close > SMA5 OR 10 sessions held
- **No tight stop** (doctrine + evidence: stops hurt mean reversion)
- **Sizing:** ₹83k/position, max 5 concurrent, most-oversold first
- Robustness: dip 2%/3%/4% all positive on train (+8.5k/+6.7k/+7.8k) — a mechanism, not a curve-fit point. Fee share of gross: 26% (vs 245% for the intraday RSI it replaces).
- Test monthly: Feb +14.5k, Mar -18.4k, Apr +2.4k, May +20.5k, Jun +5.8k — expect losing months; edge is multi-month.

## Why the old strategies lost (verified, not speculation)

1. **Transaction costs.** SEBI's own studies: index options have 21% cost-to-profit; loss-makers pay 28% of net losses as costs. Our backtests reproduced it: the intraday RSI strategies paid ₹245k fees on ₹2.5L capital in 8 months.
2. **Structural bugs.** rsi_bounce entered on RSI crossing *below* oversold (falling knife — fixed); liquidity_sweep could enter past its own invalidation level (fixed); vp_trend checked a 5-min entry against 1-min noise at the exact level (fixed: buffered, 5-min close basis); rsi_15min computed "daily" regime from 1-min candles (documented).
3. **Wrong side of the auction.** Dalton: fade only in balance, go with high-volume breaks. Every fade variant lost; the volume-gated breakout variant is the only VP config with positive expectancy.
4. **Buying short-dated ATM premium for small moves.** The 0DTE literature (SSRN 4404704/4692190): single-leg long options have negative medians; only defined-risk credit structures have positive medians. Our condor → rich-side vertical restructure follows this.

## Caveats
- Options P&L is synthesized (delta≈0.5 + linear theta; BS for condor legs). Directional evidence only — paper-trade confirmation required before live size.
- Jun–Nov 2025 futures volume is far-month (thin) — volume-dependent strategies (VP, sweep) could only be judged on Dec 2025+ data.
- RSI-2 swing holds overnight: gap risk is real, bounded by the 200-DMA gate, 5-position cap and ₹83k sizing.
- One losing month (-₹18.4k Mar) inside a passing test window: size expectations accordingly.

## Reproduce
```bash
python scripts/backtest_all.py --start 2025-06-02 --end 2026-06-09 --split 2026-02-01 --strategies all
# RSI-2 swing specifically:
python -c "
from datetime import date
from config.universe import STOCKS
from backtest.runners.swing_daily import run_rsi2_swing
import pandas as pd
recs = run_rsi2_swing(date(2026,2,1), date(2026,6,9), STOCKS, entry_rsi=10, exit_rsi=65, exit_on_sma5=True, max_hold=10, limit_dip_pct=2.0)
df = pd.DataFrame([r.__dict__ for r in recs]); print(df.net_pnl.sum())"
```
