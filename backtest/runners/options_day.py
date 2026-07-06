"""Single-day runners for the 3 Nifty-options strategies.

simulate_strategy_day is moved VERBATIM from
scripts/backtest_new_strategies.py (only imports and the TradeRecord class
location changed) so the unified harness reproduces the original backtest
exactly. Strategy fixes land in the strategy modules / here in later phases.
"""

from __future__ import annotations

from datetime import date, time as dtime

from models.types import Candle
from strategies.indicators import resample, compute_volume_profile
from strategies.position_manager import ScaledPosition, ScalingConfig
from strategies.liquidity_sweep import (
    StrategyState as SweepState, evaluate_sweep_entry, update_or_levels,
)
from strategies.volume_profile_trend import VPState, VPLevels, evaluate_vp_entry
from strategies.rsi_predictor import evaluate_rsi_entry
from src.fees import calculate_fees

from backtest.records import TradeRecord
from backtest.option_synth import estimate_atm_premium, synthesize_option_pnl

LOT_SIZE = 65
CAPITAL_PER_STRATEGY = 250_000


def simulate_strategy_day(strategy_name: str, candles_1m: list[Candle],
                          prior_close: float, prior_session_candles: list[Candle] | None,
                          daily_closes: list[float], pending_rsi_signal: dict | None,
                          daily_ema200: float,
                          target_date: date,
                          extra_params: dict | None = None) -> list[TradeRecord]:
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
            state.pd_open = prior_session_candles[0].open
            state.pd_close = prior_session_candles[-1].close
        # Anchored at session open
        state.avwap_anchor_idx = 0

        position: ScaledPosition | None = None
        params = {
            "sweep_lookback": 3, "sweep_volume_mult": 1.5,
            "sweep_wick_atr_mult": 0.5, "sweep_wick_body_ratio": 0.6,
            "max_sweep_age_candles": 4,
        }
        if extra_params:
            params.update(extra_params)
        for i in range(60, len(candles_1m)):
            sub_1m = candles_1m[:i + 1]
            sub_5m = resample(sub_1m, 5)
            update_or_levels(sub_1m, state)

            if position is None:
                sig = evaluate_sweep_entry(sub_1m, sub_5m, state, params)
                if sig is not None and params.get("max_trades_per_direction"):
                    n_dir = sum(1 for t in trades if t.direction == sig["direction"])
                    if n_dir >= params["max_trades_per_direction"]:
                        sig = None
                if sig is not None:
                    spot = sig["entry_price"]
                    days_to_expiry = max(1, 4 - target_date.weekday())  # crude
                    premium = estimate_atm_premium(spot, days_to_expiry=days_to_expiry)
                    if premium < 60:
                        continue
                    atr_val = sig.get("atr", 0.0) or (spot * 0.001)
                    buffer = max(0.5 * atr_val, spot * 0.0008)
                    if sig.get("structural_stop"):
                        # 80-20: stop at today's extreme (the sweep low/high)
                        inv_level = sig["structural_stop"]
                        inv_dir = "below" if sig["direction"] == "bullish" else "above"
                    elif sig["direction"] == "bullish":
                        inv_level = sig["sweep_level"] - buffer
                        inv_dir = "below"
                    else:
                        inv_level = sig["sweep_level"] + buffer
                        inv_dir = "above"
                    inv_distance = abs(spot - inv_level)
                    # ATR-ladder structure targets (delta ~0.5)
                    t1 = premium + 0.5 * 1.5 * atr_val
                    t2 = premium + 0.5 * 3.0 * atr_val
                    stop_pct = min(0.35, max(0.18, (0.5 * inv_distance) / premium + 0.10))
                    cfg = ScalingConfig(
                        profile="profit_pyramid",
                        target_premiums=[(t1, 0.5), (t2, 1.0)],
                        hard_stop_pct_from_avg=stop_pct,
                    )
                    risk_amt = CAPITAL_PER_STRATEGY * 0.01
                    qty = int(risk_amt / (premium * stop_pct))
                    qty = (qty // LOT_SIZE) * LOT_SIZE
                    if qty < LOT_SIZE:
                        continue
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
                    entry_setup = sig.get("setup", "sweep")
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
                    reason=exit_reason, setup=entry_setup,
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
        if extra_params:
            params.update(extra_params)

        # Entries evaluated on 5-min boundaries; exits checked EVERY minute
        # while in a position (a fast move can blow far past the premium stop
        # between 5-min checks)
        for i in range(60, len(candles_1m)):
            on_5m_boundary = (i - 60) % 5 == 0
            if position is None and not on_5m_boundary:
                continue
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
                if sig and params.get("max_trades_per_direction"):
                    # a level/direction that already traded today is a
                    # lower-quality retest — skip
                    n_dir = sum(1 for t in trades if t.direction == sig["direction"])
                    if n_dir >= params["max_trades_per_direction"]:
                        sig = None
                if sig:
                    spot = sig["entry_price"]
                    days_to_expiry = max(1, 4 - target_date.weekday())
                    premium = estimate_atm_premium(spot, days_to_expiry=days_to_expiry)
                    if premium < 60:
                        continue
                    vp = sig["vp"]
                    atr_val = sig.get("atr", 0.0) or 0.0
                    is_breakout = sig["setup"] in ("VAH_BREAKOUT", "VAL_BREAKDOWN")
                    # Buffered structural invalidation: the level itself plus
                    # max(0.5 x ATR_5m, 0.08% of spot) of room, so 1-min noise
                    # through the exact VAL/VAH no longer shakes the trade out
                    buffer = max(0.5 * atr_val, spot * 0.0008)
                    va_width = max(vp.VAH - vp.VAL, 10.0)
                    if sig["direction"] == "bullish":
                        if is_breakout:
                            # broke above VAH: invalid if back inside VA;
                            # targets project the VA geometry upward
                            inv_level = vp.VAH - buffer
                            d_poc = max(vp.VAH - vp.POC, 10.0)
                            d_opp = va_width
                        else:
                            inv_level = vp.VAL - buffer
                            d_poc = abs(vp.POC - spot)
                            d_opp = abs(vp.VAH - spot)
                        inv_dir = "below"
                    else:
                        if is_breakout:
                            inv_level = vp.VAL + buffer
                            d_poc = max(vp.POC - vp.VAL, 10.0)
                            d_opp = va_width
                        else:
                            inv_level = vp.VAH + buffer
                            d_poc = abs(spot - vp.POC)
                            d_opp = abs(spot - vp.VAL)
                        inv_dir = "above"
                    inv_distance = abs(spot - inv_level)
                    # Structure-derived ladder: T1 at POC, T2 at the opposite
                    # VA boundary (delta ~0.5), instead of unreachable 1.5x/3x
                    # premium multiples
                    t1 = premium + 0.5 * d_poc
                    t2 = premium + 0.5 * max(d_opp, d_poc + 10)
                    # premium stop just beyond the structural stop; floored so
                    # qty sizing can't explode on tight structures, capped
                    # below the legacy 45%
                    stop_pct = min(0.35, max(0.18, (0.5 * inv_distance) / premium + 0.10))
                    cfg = ScalingConfig(
                        profile="profit_pyramid",
                        target_premiums=[(t1, 0.5), (t2, 1.0)],
                        hard_stop_pct_from_avg=stop_pct,
                    )
                    risk_amt = CAPITAL_PER_STRATEGY * 0.01
                    qty = int(risk_amt / (premium * cfg.hard_stop_pct_from_avg))
                    qty = (qty // LOT_SIZE) * LOT_SIZE
                    if qty < LOT_SIZE:
                        continue
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
                    if is_breakout:
                        # measured-move magnet at the projected extension,
                        # not at POC (which is behind a breakout trade)
                        poc_target = (spot + d_opp) if sig["direction"] == "bullish" else (spot - d_opp)
                    else:
                        poc_target = state.intraday_vp.POC if state.intraday_vp else state.prior_vp.POC
                continue

            current_under = sub_1m[-1].close
            now = sub_1m[-1].timestamp
            hours_held = (now - entry_time).total_seconds() / 3600
            current_premium, _ = synthesize_option_pnl(
                position.direction, entry_underlying, current_under,
                position.initial_premium, hours_held,
            )
            # Zone failure is judged on the last COMPLETED 5-min close, the
            # same bar basis the entry signal used (sub_5m[-1] is partial
            # because the loop steps on 5-min boundaries)
            completed_5m_close = sub_5m[-2].close if len(sub_5m) >= 2 else current_under

            exit_reason = None
            exit_qty = 0

            if position.hit_hard_stop(current_premium):
                exit_qty = position.net_qty; exit_reason = "hard_stop"
            elif position.is_invalidated(completed_5m_close):
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
