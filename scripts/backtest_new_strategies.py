"""4-month backtest of the 3 new strategies.

Approach (pragmatic, not perfect):
- Underlying: Nifty Index (token 99926000) for daily RSI Predictor signal,
  and Nifty Futures front-month (per-month token) for intraday strategies.
- Option P&L synthesis:
    Initial premium estimated as 0.012 × spot for ATM weekly with ~3 days to expiry.
    Exit P&L via approx delta:
        delta_CE ≈ 0.5 (ATM), so option_change = 0.5 × underlying_change
    Theta: linearly decays 8% of premium per trading day held.
    This is approximate — real options can deviate ±20% on the same underlying move.

- Each strategy uses ScaledPosition for ladder/SL accounting.
- Fees applied via src/fees.calculate_fees.

Outputs a per-strategy summary plus a combined total.

Usage:
  python scripts/backtest_new_strategies.py 2025-12-01 2026-04-25
"""

from __future__ import annotations

import os
import sys
import json
import time
import pickle
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path

import pandas as pd
import pyotp
import pytz
from dotenv import load_dotenv
from SmartApi import SmartConnect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from models.types import Candle
from strategies.indicators import (
    rsi, ema, atr, vwap, resample, compute_volume_profile,
)
from strategies.position_manager import ScaledPosition, ScalingConfig
from strategies.liquidity_sweep import (
    StrategyState as SweepState, evaluate_sweep_entry, update_or_levels,
)
from strategies.volume_profile_trend import (
    VPState, VPLevels, evaluate_vp_entry,
)
from strategies.rsi_predictor import (
    classify_eod_pattern, evaluate_rsi_entry,
)
from src.fees import calculate_fees

IST = pytz.timezone("Asia/Kolkata")
LOT_SIZE = 65
CAPITAL_PER_STRATEGY = 250_000

# Cache directory for fetched 1-min Nifty Futures data
CACHE_DIR = ROOT / "data" / "backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────
# Synthesis: option premium model
# ─────────────────────────────────────────────────────────────────────
def estimate_atm_premium(spot: float, days_to_expiry: int = 3,
                          implied_vol: float = 0.14) -> float:
    """Rough ATM premium for a Nifty weekly option.

    Uses simplified ATM premium = 0.4 × spot × IV × √(T/365)
    For Nifty at 24000, IV 14%, 3 days: 0.4 × 24000 × 0.14 × √(3/365) ≈ ₹122
    """
    import math
    if days_to_expiry <= 0:
        days_to_expiry = 1
    return round(0.4 * spot * implied_vol * math.sqrt(days_to_expiry / 365), 1)


def synthesize_option_pnl(direction: str,
                          entry_underlying: float, exit_underlying: float,
                          entry_premium: float, hours_held: float,
                          delta: float = 0.5,
                          theta_pct_per_day: float = 0.08) -> tuple[float, float]:
    """Approx P&L per unit on an option trade.

    Returns (exit_premium, premium_change_per_unit).
    """
    move = exit_underlying - entry_underlying
    if direction == "bearish":
        move = -move  # PE profits when underlying falls
    delta_pnl_per_unit = delta * move  # how the option premium moves with underlying
    days = hours_held / 6.25  # ~6.25 trading hours in NSE day
    theta_loss_per_unit = entry_premium * theta_pct_per_day * days
    exit_premium = max(entry_premium + delta_pnl_per_unit - theta_loss_per_unit, 0.5)
    return exit_premium, exit_premium - entry_premium


# ─────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────
def get_front_month_token(api, target_date: date, instruments_df: pd.DataFrame) -> str | None:
    """Find the Nifty futures front-month contract whose expiry is on/after target_date."""
    fut = instruments_df[
        (instruments_df["name"] == "NIFTY")
        & (instruments_df["instrumenttype"] == "FUTIDX")
        & (instruments_df["exch_seg"] == "NFO")
    ].copy()
    fut["expiry_dt"] = pd.to_datetime(fut["expiry"], format="mixed", dayfirst=True,
                                       errors="coerce").dt.date
    fut = fut.sort_values("expiry_dt")
    eligible = fut[fut["expiry_dt"] >= target_date]
    if eligible.empty:
        return None
    return str(eligible.iloc[0]["token"])


def fetch_1min_futures(api, token: str, target_date: date) -> list[Candle] | None:
    """Fetch 1-min Nifty Futures candles for a single trading day."""
    cache_path = CACHE_DIR / f"nifty_fut_{target_date}_{token}.pkl"
    if cache_path.exists():
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    try:
        time.sleep(0.4)
        res = api.getCandleData({
            "exchange": "NFO", "symboltoken": token, "interval": "ONE_MINUTE",
            "fromdate": f"{target_date} 09:15", "todate": f"{target_date} 15:30",
        })
        if not res or not res.get("data"):
            return None
        candles = []
        for row in res["data"]:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo:
                ts = ts.astimezone(IST).replace(tzinfo=None)
            candles.append(Candle(
                timestamp=ts, open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=int(row[5]),
                token=token, symbol=f"NIFTY_FUT",
            ))
        with open(cache_path, "wb") as f:
            pickle.dump(candles, f)
        return candles
    except Exception as e:
        print(f"  fetch error {target_date}: {e}")
        return None


def fetch_nifty_daily(api, end_date: date) -> list[Candle] | None:
    """Fetch Nifty index daily candles up to end_date."""
    cache_path = CACHE_DIR / f"nifty_daily_{end_date}.pkl"
    if cache_path.exists():
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    try:
        res = api.getCandleData({
            "exchange": "NSE", "symboltoken": "99926000", "interval": "ONE_DAY",
            "fromdate": "2024-01-01 09:15", "todate": f"{end_date} 15:30",
        })
        if not res or not res.get("data"):
            return None
        candles = []
        for row in res["data"]:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo:
                ts = ts.astimezone(IST).replace(tzinfo=None)
            candles.append(Candle(
                timestamp=ts, open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=int(row[5]),
                token="99926000", symbol="NIFTY",
            ))
        with open(cache_path, "wb") as f:
            pickle.dump(candles, f)
        return candles
    except Exception as e:
        print(f"  daily fetch error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
# Single-day per-strategy simulation
# ─────────────────────────────────────────────────────────────────────
@dataclass
class TradeRecord:
    strategy: str
    date: str
    direction: str
    entry_time: str
    exit_time: str
    entry_underlying: float
    exit_underlying: float
    entry_premium: float
    exit_premium: float
    qty: int
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str
    setup: str


def simulate_strategy_day(strategy_name: str, candles_1m: list[Candle],
                           prior_close: float, prior_session_candles: list[Candle] | None,
                           daily_closes: list[float], pending_rsi_signal: dict | None,
                           daily_ema200: float,
                           target_date: date) -> list[TradeRecord]:
    """Run ONE strategy on ONE day. Returns trades closed."""
    if not candles_1m or len(candles_1m) < 60:
        return []

    candles_5m = resample(candles_1m, 5)
    if len(candles_5m) < 25:
        return []

    trades: list[TradeRecord] = []

    if strategy_name == "liquidity_sweep":
        state = SweepState()
        if prior_session_candles:
            state.pdh = max(c.high for c in prior_session_candles)
            state.pdl = min(c.low for c in prior_session_candles)
        # Anchored at session open
        state.avwap_anchor_idx = 0

        position: ScaledPosition | None = None
        params = {
            "sweep_lookback": 3, "sweep_volume_mult": 1.5,
            "sweep_wick_atr_mult": 0.5, "sweep_wick_body_ratio": 0.6,
            "max_sweep_age_candles": 10,
        }
        for i in range(60, len(candles_1m)):
            sub_1m = candles_1m[:i + 1]
            sub_5m = resample(sub_1m, 5)
            update_or_levels(sub_1m, state)

            if position is None:
                sig = evaluate_sweep_entry(sub_1m, sub_5m, state, params)
                if sig is not None:
                    spot = sig["entry_price"]
                    days_to_expiry = max(1, 4 - target_date.weekday())  # crude
                    premium = estimate_atm_premium(spot, days_to_expiry=days_to_expiry)
                    if premium < 60:
                        continue
                    cfg = ScalingConfig(profile="profit_pyramid")
                    risk_amt = CAPITAL_PER_STRATEGY * 0.01
                    qty = int(risk_amt / (premium * 0.45))
                    qty = (qty // LOT_SIZE) * LOT_SIZE
                    if qty < LOT_SIZE:
                        continue
                    inv_level = sig["sweep_level"]
                    inv_dir = "below" if sig["direction"] == "bullish" else "above"
                    position = ScaledPosition(
                        strategy_name="liquidity_sweep",
                        trade_id=f"LS-{target_date}-{i}",
                        direction=sig["direction"],
                        initial_premium=premium, initial_qty=qty,
                        invalidation_level=inv_level, invalidation_direction=inv_dir,
                        scaling_config=cfg, initial_entry_ts=sub_1m[-1].timestamp,
                    )
                    position.add_entry(qty, premium, sub_1m[-1].timestamp, "initial")
                    entry_underlying = spot
                    entry_idx = i
                    entry_time = sub_1m[-1].timestamp
                continue

            # Position open — check exits
            current_under = sub_1m[-1].close
            now = sub_1m[-1].timestamp
            hours_held = (now - entry_time).total_seconds() / 3600
            current_premium, _ = synthesize_option_pnl(
                position.direction, entry_underlying, current_under,
                position.initial_premium, hours_held,
            )

            exit_reason = None
            exit_qty = 0

            # Hard stop
            if position.hit_hard_stop(current_premium):
                exit_qty = position.net_qty
                exit_reason = "hard_stop"
            elif position.is_invalidated(current_under):
                exit_qty = position.net_qty
                exit_reason = "invalidation"
            elif now.time() >= dtime(14, 45):
                exit_qty = position.net_qty
                exit_reason = "time_stop"
            else:
                qty_t, idx_t = position.should_take_profit(current_premium)
                if qty_t > 0:
                    exit_qty = qty_t
                    exit_reason = f"target_{idx_t}"
                    position.targets_hit.add(idx_t)

            if exit_qty > 0 and exit_reason:
                position.add_exit(exit_qty, current_premium, now, exit_reason)
                # Per-trade fees: BUY entry side + SELL exit side, both options
                entry_fees = calculate_fees(position.initial_premium, exit_qty, "BUY")["total"]
                exit_fees = calculate_fees(current_premium, exit_qty, "SELL")["total"]
                gross = (current_premium - position.avg_cost) * exit_qty
                if position.direction == "bearish":
                    pass  # already accounted via synthesize_option_pnl flipping
                fees = entry_fees + exit_fees
                trades.append(TradeRecord(
                    strategy="liquidity_sweep",
                    date=str(target_date),
                    direction=position.direction,
                    entry_time=entry_time.strftime("%H:%M"),
                    exit_time=now.strftime("%H:%M"),
                    entry_underlying=entry_underlying,
                    exit_underlying=current_under,
                    entry_premium=position.initial_premium,
                    exit_premium=current_premium,
                    qty=exit_qty,
                    gross_pnl=gross, fees=fees, net_pnl=gross - fees,
                    reason=exit_reason, setup="sweep",
                ))
                if position.is_fully_closed:
                    position = None

    elif strategy_name == "vp_trend":
        if not prior_session_candles:
            return []
        state = VPState()
        prior_vp = compute_volume_profile(prior_session_candles, n_bins=150)
        state.prior_vp = VPLevels(
            POC=prior_vp["POC"], VAH=prior_vp["VAH"], VAL=prior_vp["VAL"], source="prior"
        )
        state.daily_ema200 = daily_ema200

        position: ScaledPosition | None = None
        params = {"poc_proximity_points": 30}

        for i in range(60, len(candles_1m), 5):  # check on 5-min boundaries
            sub_1m = candles_1m[:i + 1]
            sub_5m = resample(sub_1m, 5)
            if len(sub_5m) < 25:
                continue

            # Intraday VP refresh after 11:45 (120 candles)
            now = sub_1m[-1].timestamp
            if now.time() >= dtime(11, 45) and len(sub_1m) >= 150:
                # candles since 9:45
                start_idx = next((j for j, c in enumerate(sub_1m)
                                   if c.timestamp.time() >= dtime(9, 45)), 0)
                intraday = sub_1m[start_idx:]
                if len(intraday) >= 120:
                    vp = compute_volume_profile(intraday, n_bins=150)
                    state.intraday_vp = VPLevels(
                        POC=vp["POC"], VAH=vp["VAH"], VAL=vp["VAL"], source="intraday"
                    )

            if position is None:
                sig = evaluate_vp_entry(sub_1m, sub_5m, state, params)
                if sig:
                    spot = sig["entry_price"]
                    days_to_expiry = max(1, 4 - target_date.weekday())
                    premium = estimate_atm_premium(spot, days_to_expiry=days_to_expiry)
                    if premium < 60:
                        continue
                    cfg = ScalingConfig(profile="profit_pyramid")
                    risk_amt = CAPITAL_PER_STRATEGY * 0.01
                    qty = int(risk_amt / (premium * 0.45))
                    qty = (qty // LOT_SIZE) * LOT_SIZE
                    if qty < LOT_SIZE:
                        continue
                    inv_level = sig["vp"].VAL if sig["direction"] == "bullish" else sig["vp"].VAH
                    inv_dir = "below" if sig["direction"] == "bullish" else "above"
                    position = ScaledPosition(
                        strategy_name="vp_trend",
                        trade_id=f"VP-{target_date}-{i}",
                        direction=sig["direction"],
                        initial_premium=premium, initial_qty=qty,
                        invalidation_level=inv_level, invalidation_direction=inv_dir,
                        scaling_config=cfg, initial_entry_ts=sub_1m[-1].timestamp,
                    )
                    position.add_entry(qty, premium, sub_1m[-1].timestamp, "initial")
                    entry_underlying = spot
                    entry_time = sub_1m[-1].timestamp
                    poc_target = state.intraday_vp.POC if state.intraday_vp else state.prior_vp.POC
                continue

            current_under = sub_1m[-1].close
            now = sub_1m[-1].timestamp
            hours_held = (now - entry_time).total_seconds() / 3600
            current_premium, _ = synthesize_option_pnl(
                position.direction, entry_underlying, current_under,
                position.initial_premium, hours_held,
            )

            exit_reason = None
            exit_qty = 0

            if position.hit_hard_stop(current_premium):
                exit_qty = position.net_qty; exit_reason = "hard_stop"
            elif position.is_invalidated(current_under):
                exit_qty = position.net_qty; exit_reason = "zone_failure"
            elif now.time() >= dtime(14, 45):
                exit_qty = position.net_qty; exit_reason = "time_stop"
            elif abs(current_under - poc_target) <= 10:
                # POC magnet — force-fire next pending rung
                qty_t, idx_t = position.force_next_target()
                if qty_t > 0:
                    exit_qty = qty_t
                    exit_reason = f"poc_magnet_{idx_t}"
                    position.targets_hit.add(idx_t)
            else:
                qty_t, idx_t = position.should_take_profit(current_premium)
                if qty_t > 0:
                    exit_qty = qty_t
                    exit_reason = f"target_{idx_t}"
                    position.targets_hit.add(idx_t)

            if exit_qty > 0 and exit_reason:
                position.add_exit(exit_qty, current_premium, now, exit_reason)
                entry_fees = calculate_fees(position.initial_premium, exit_qty, "BUY")["total"]
                exit_fees = calculate_fees(current_premium, exit_qty, "SELL")["total"]
                gross = (current_premium - position.avg_cost) * exit_qty
                fees = entry_fees + exit_fees
                trades.append(TradeRecord(
                    strategy="vp_trend",
                    date=str(target_date),
                    direction=position.direction,
                    entry_time=entry_time.strftime("%H:%M"),
                    exit_time=now.strftime("%H:%M"),
                    entry_underlying=entry_underlying,
                    exit_underlying=current_under,
                    entry_premium=position.initial_premium,
                    exit_premium=current_premium,
                    qty=exit_qty,
                    gross_pnl=gross, fees=fees, net_pnl=gross - fees,
                    reason=exit_reason, setup="vp",
                ))
                if position.is_fully_closed:
                    position = None

    elif strategy_name == "rsi_predictor":
        if not pending_rsi_signal or pending_rsi_signal.get("signal") == "NEUTRAL":
            return []
        position: ScaledPosition | None = None
        for i in range(60, len(candles_1m)):
            sub_1m = candles_1m[:i + 1]
            now = sub_1m[-1].timestamp
            if position is None:
                if now.time() < dtime(10, 0) or now.time() > dtime(11, 30):
                    continue
                sig = evaluate_rsi_entry(sub_1m, pending_rsi_signal, prior_close, {})
                if sig:
                    spot = sig["entry_price"]
                    days_to_expiry = max(1, 4 - target_date.weekday())
                    premium = estimate_atm_premium(spot, days_to_expiry=days_to_expiry)
                    if premium < 60:
                        continue
                    cfg = ScalingConfig(
                        profile="profit_pyramid",
                        pyramid_after_target_idx=-1,  # disable pyramid for this strategy
                    )
                    vix_proxy = pending_rsi_signal.get("vix", 14)
                    size_mult = 0.5 if vix_proxy > 18 else 1.0
                    risk_amt = CAPITAL_PER_STRATEGY * 0.01 * size_mult
                    qty = int(risk_amt / (premium * 0.45))
                    qty = (qty // LOT_SIZE) * LOT_SIZE
                    if qty < LOT_SIZE:
                        continue
                    position = ScaledPosition(
                        strategy_name="rsi_predictor",
                        trade_id=f"RP-{target_date}-{i}",
                        direction=sig["direction"],
                        initial_premium=premium, initial_qty=qty,
                        invalidation_level=None, invalidation_direction="below",
                        scaling_config=cfg, initial_entry_ts=now,
                    )
                    position.add_entry(qty, premium, now, "initial")
                    entry_underlying = spot
                    entry_time = now
                continue

            current_under = sub_1m[-1].close
            hours_held = (now - entry_time).total_seconds() / 3600
            current_premium, _ = synthesize_option_pnl(
                position.direction, entry_underlying, current_under,
                position.initial_premium, hours_held,
            )

            exit_reason = None
            exit_qty = 0
            if position.hit_hard_stop(current_premium):
                exit_qty = position.net_qty; exit_reason = "hard_stop"
            elif now.time() >= dtime(14, 45):
                exit_qty = position.net_qty; exit_reason = "time_stop"
            else:
                qty_t, idx_t = position.should_take_profit(current_premium)
                if qty_t > 0:
                    exit_qty = qty_t
                    exit_reason = f"target_{idx_t}"
                    position.targets_hit.add(idx_t)

            if exit_qty > 0 and exit_reason:
                position.add_exit(exit_qty, current_premium, now, exit_reason)
                entry_fees = calculate_fees(position.initial_premium, exit_qty, "BUY")["total"]
                exit_fees = calculate_fees(current_premium, exit_qty, "SELL")["total"]
                gross = (current_premium - position.avg_cost) * exit_qty
                fees = entry_fees + exit_fees
                trades.append(TradeRecord(
                    strategy="rsi_predictor",
                    date=str(target_date),
                    direction=position.direction,
                    entry_time=entry_time.strftime("%H:%M"),
                    exit_time=now.strftime("%H:%M"),
                    entry_underlying=entry_underlying,
                    exit_underlying=current_under,
                    entry_premium=position.initial_premium,
                    exit_premium=current_premium,
                    qty=exit_qty,
                    gross_pnl=gross, fees=fees, net_pnl=gross - fees,
                    reason=exit_reason, setup=pending_rsi_signal.get("signal", ""),
                ))
                if position.is_fully_closed:
                    position = None

    return trades


# ─────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────
def get_trading_days(start: date, end: date) -> list[date]:
    cur = start
    out = []
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            out.append(cur)
        cur += timedelta(days=1)
    return out


def main():
    if len(sys.argv) < 3:
        print("Usage: backtest_new_strategies.py YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])

    print(f"\n{'='*78}")
    print(f"  Backtesting 3 new strategies: {start} to {end}")
    print(f"{'='*78}\n")

    api = SmartConnect(os.getenv("ANGEL_API_KEY"))
    totp = pyotp.TOTP(os.getenv("ANGEL_TOTP_SECRET")).now()
    api.generateSession(os.getenv("ANGEL_CLIENT_ID"),
                         os.getenv("ANGEL_PASSWORD"), totp)

    with open(ROOT / "data" / "instruments.json") as f:
        instruments_df = pd.DataFrame(json.load(f))

    print("  Fetching Nifty daily index data (for RSI Predictor)...")
    daily_candles = fetch_nifty_daily(api, end)
    if not daily_candles:
        print("  failed to fetch daily — RSI predictor disabled")
        daily_closes = []
        daily_ema_series = []
    else:
        daily_closes = [c.close for c in daily_candles]
        daily_ema_series = ema(daily_closes, 200)
    daily_dates = {c.timestamp.date(): i for i, c in enumerate(daily_candles or [])}

    days = get_trading_days(start, end)
    print(f"  {len(days)} weekdays to scan\n")

    all_trades: list[TradeRecord] = []
    prior_session_candles: list[Candle] | None = None
    prior_close = 0.0

    pending_rsi_signal = None  # set after each EOD scan

    for d in days:
        token = get_front_month_token(api, d, instruments_df)
        if not token:
            continue
        candles_1m = fetch_1min_futures(api, token, d)
        if not candles_1m or len(candles_1m) < 60:
            sys.stdout.write(f"  {d} (skip)\n"); sys.stdout.flush()
            continue

        # daily_ema200 as of yesterday
        idx_today = daily_dates.get(d)
        if idx_today is None or idx_today < 200:
            ema200 = daily_closes[idx_today] if idx_today is not None else 0
        else:
            ema200 = daily_ema_series[idx_today - 1]

        # Run all 3 strategies
        ls_trades = simulate_strategy_day(
            "liquidity_sweep", candles_1m, prior_close, prior_session_candles,
            daily_closes, None, ema200, d,
        )
        vp_trades = simulate_strategy_day(
            "vp_trend", candles_1m, prior_close, prior_session_candles,
            daily_closes, None, ema200, d,
        )
        rp_trades = simulate_strategy_day(
            "rsi_predictor", candles_1m, prior_close, prior_session_candles,
            daily_closes, pending_rsi_signal, ema200, d,
        )
        all_trades.extend(ls_trades + vp_trades + rp_trades)

        # EOD: classify pattern for tomorrow
        if idx_today is not None and idx_today >= 30:
            closes_up_to_today = daily_closes[:idx_today + 1]
            pattern = classify_eod_pattern(closes_up_to_today, regime="BULL")
            pending_rsi_signal = {"signal": pattern, "vix": 14}
        else:
            pending_rsi_signal = None

        # Update prior for next day
        prior_session_candles = candles_1m
        prior_close = candles_1m[-1].close

        ls_pnl = sum(t.net_pnl for t in ls_trades)
        vp_pnl = sum(t.net_pnl for t in vp_trades)
        rp_pnl = sum(t.net_pnl for t in rp_trades)
        sys.stdout.write(f"  {d} {d.strftime('%a')[:3]} | LS:{len(ls_trades)}t ₹{ls_pnl:+.0f} | "
                         f"VP:{len(vp_trades)}t ₹{vp_pnl:+.0f} | RP:{len(rp_trades)}t ₹{rp_pnl:+.0f}\n")
        sys.stdout.flush()

    # ── Summary ──
    print(f"\n{'='*78}")
    print(f"  RESULTS — 3 new strategies")
    print(f"{'='*78}\n")

    by_strat = {"liquidity_sweep": [], "vp_trend": [], "rsi_predictor": []}
    for t in all_trades:
        by_strat[t.strategy].append(t)

    grand_gross = 0.0
    grand_fees = 0.0
    grand_net = 0.0

    for strat, trades in by_strat.items():
        if not trades:
            print(f"  {strat:20s}  no trades")
            continue
        gross = sum(t.gross_pnl for t in trades)
        fees = sum(t.fees for t in trades)
        net = sum(t.net_pnl for t in trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        grand_gross += gross; grand_fees += fees; grand_net += net
        print(f"  {strat:20s}  trades={len(trades):3d}  wins={wins:3d}  "
              f"gross=₹{gross:+10,.0f}  fees=₹{fees:8,.0f}  net=₹{net:+10,.0f}")

    print()
    print(f"  {'COMBINED':20s}  trades={len(all_trades):3d}  "
          f"gross=₹{grand_gross:+10,.0f}  fees=₹{grand_fees:8,.0f}  "
          f"net=₹{grand_net:+10,.0f}")

    # User-facing money summary
    capital_x = CAPITAL_PER_STRATEGY * 3  # one allocation per strategy
    pct = grand_net / capital_x * 100
    print(f"\n{'─'*78}")
    print(f"  CAPITAL PUT IN (X):       ₹{capital_x:,}  (₹{CAPITAL_PER_STRATEGY:,} × 3 strategies)")
    print(f"  PROFIT/LOSS (Y):          ₹{grand_net:+,.2f}")
    print(f"  RETURN ON CAPITAL:        {pct:+.2f}%")
    print(f"{'─'*78}")

    print(f"\n  NOTES:")
    print(f"  • Option P&L synthesised via delta=0.5 + linear theta — actual fills may differ ±20%")
    print(f"  • Premium estimated from ATM IV ~14% (typical Nifty regime)")
    print(f"  • Fees: full Indian fee stack via src/fees.py:calculate_fees")
    print(f"  • Slippage NOT modeled (real fills will be slightly worse)")
    print(f"\n{'='*78}\n")

    # Persist trades to CSV for inspection
    out_csv = ROOT / "data" / f"backtest_trades_{start}_{end}.csv"
    df = pd.DataFrame([t.__dict__ for t in all_trades])
    df.to_csv(out_csv, index=False)
    print(f"  Trade log: {out_csv}")


if __name__ == "__main__":
    main()
