"""Strategy A — Liquidity Sweep + Anchored VWAP.

Detects a sweep candle (wick past a known level + close-back) on Nifty Futures,
then enters an option in the reversal direction once price reclaims session AVWAP.

Designed to be both:
- live-tradable via BaseStrategy interface
- backtestable via the standalone evaluate() entry point
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

import pytz

from models.types import Candle, Signal, SignalType
from strategies.base import BaseStrategy, StrategyEngine
from strategies.indicators import (
    atr, vwap, swing_highs_lows, resample,
)
from strategies.position_manager import ScaledPosition, ScalingConfig

log = logging.getLogger("autotheta.liquidity_sweep")
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class DetectedSweep:
    candle_idx: int           # index of sweep candle in 5-min series
    timestamp: datetime
    level: float
    direction: str            # "bullish" | "bearish"
    age_candles: int = 0


@dataclass
class StrategyState:
    """Per-day state needed for sweep + reclaim logic."""
    pdh: float = 0.0
    pdl: float = 0.0
    pd_open: float = 0.0   # prior session open (for 80-20 precondition)
    pd_close: float = 0.0  # prior session close
    or_high: float = 0.0
    or_low: float = 0.0
    or_locked: bool = False
    eq_levels: list[float] = field(default_factory=list)
    detected_sweeps: list[DetectedSweep] = field(default_factory=list)
    avwap_anchor_idx: int = 0


def detect_sweep(candles_5m: list[Candle], level: float, direction: str,
                  vol_avg: float, atr_5m_val: float,
                  vol_mult: float = 1.5,
                  wick_atr_mult: float = 0.5,
                  wick_body_ratio: float = 0.6,
                  lookback: int = 3) -> Optional[int]:
    """Check if the last `lookback` 5-min candles contain a sweep of `level`.

    direction:
      "bullish" — sweep BELOW the level then close back above
      "bearish" — sweep ABOVE the level then close back below

    Returns the index (in candles_5m) of the sweep candle, or None.
    """
    n = len(candles_5m)
    if n == 0 or vol_avg <= 0 or atr_5m_val <= 0:
        return None
    start = max(0, n - lookback)
    for i in range(start, n):
        c = candles_5m[i]
        full_range = c.high - c.low
        if full_range <= 0:
            continue
        body = abs(c.close - c.open)
        if direction == "bullish":
            wick_size = level - c.low if c.low < level else 0
            if c.low < level and c.close > level:
                if (wick_size / full_range >= wick_body_ratio
                        and c.volume >= vol_avg * vol_mult
                        and wick_size >= atr_5m_val * wick_atr_mult):
                    return i
        elif direction == "bearish":
            wick_size = c.high - level if c.high > level else 0
            if c.high > level and c.close < level:
                if (wick_size / full_range >= wick_body_ratio
                        and c.volume >= vol_avg * vol_mult
                        and wick_size >= atr_5m_val * wick_atr_mult):
                    return i
    return None


def evaluate_sweep_entry(candles_1m: list[Candle], candles_5m: list[Candle],
                         state: StrategyState, params: dict) -> dict | None:
    """Pure function used by both live strategy and backtest.

    Inputs:
      candles_1m: full session 1-min Nifty Futures candles (chronological)
      candles_5m: same data resampled to 5-min
      state: StrategyState (PDH/PDL, OR levels, detected sweeps)
      params: strategy params dict

    Returns entry signal dict or None.
    """
    if len(candles_5m) < 25 or len(candles_1m) < 5:
        return None

    last_5m = candles_5m[-1]
    now = last_5m.timestamp
    now_t = now.time() if isinstance(now.time(), time) else now.time()

    # Time window
    in_window = (
        time(9, 50) <= now_t <= time(11, 0) or
        time(13, 30) <= now_t <= time(14, 45)
    )
    if not in_window:
        return None

    # Compute 5-min vol/ATR for sweep detection
    recent_vols = [c.volume for c in candles_5m[-21:-1]]  # exclude current
    vol_avg = sum(recent_vols) / max(len(recent_vols), 1)
    atr_5m = atr(candles_5m, 14)
    atr_val = atr_5m[-1] if atr_5m and atr_5m[-1] == atr_5m[-1] else 0.0

    # Build candidate levels: PDH/PDL plus clustered equal highs/lows (2+
    # swings within tolerance = a real resting-liquidity pool, per the
    # smart-money-concepts definition). Round numbers are gone — they carry
    # no liquidity story and flip direction as spot drifts; they generated
    # most of the garbage sweeps in the original backtest.
    levels = []
    if state.pdh > 0:
        levels.append((state.pdh, "bearish", "PDH"))
    if state.pdl > 0:
        levels.append((state.pdl, "bullish", "PDL"))
    if params.get("use_or_levels") and state.or_locked:
        levels.append((state.or_high, "bearish", "OR_HIGH"))
        levels.append((state.or_low, "bullish", "OR_LOW"))
    spot = last_5m.close
    # EQH/EQL clusters (intraday swing pools — disable via levels="pd_only";
    # they sit close to price and their breaks proved mostly noise)
    swings = [] if params.get("levels") == "pd_only" else swing_highs_lows(candles_5m, swing_length=5)
    if swings:
        # Group within 0.1% tolerance
        tolerance = spot * 0.001
        for i, (idx_a, price_a, kind_a) in enumerate(swings):
            cluster = [(idx_a, price_a)]
            for j in range(i + 1, len(swings)):
                idx_b, price_b, kind_b = swings[j]
                if kind_b == kind_a and abs(price_b - price_a) < tolerance:
                    cluster.append((idx_b, price_b))
            if len(cluster) >= 2:
                avg = sum(p for _, p in cluster) / len(cluster)
                direction = "bearish" if kind_a == "high" else "bullish"
                levels.append((avg, direction, f"EQ_{kind_a}"))

    # Detect new sweeps on the most recent few 5-min candles
    sweep_lookback = params.get("sweep_lookback", 3)
    for level, direction, label in levels:
        idx = detect_sweep(
            candles_5m, level, direction, vol_avg, atr_val,
            vol_mult=params.get("sweep_volume_mult", 1.5),
            wick_atr_mult=params.get("sweep_wick_atr_mult", 0.5),
            wick_body_ratio=params.get("sweep_wick_body_ratio", 0.6),
            lookback=sweep_lookback,
        )
        if idx is not None:
            # Avoid duplicates
            if not any(s.candle_idx == idx and s.level == level
                       for s in state.detected_sweeps):
                state.detected_sweeps.append(DetectedSweep(
                    candle_idx=idx,
                    timestamp=candles_5m[idx].timestamp,
                    level=level, direction=direction,
                ))

    # Expire stale sweeps — a reclaim 50 minutes after the sweep is noise;
    # genuine stop-run reversals snap back fast
    max_age = params.get("max_sweep_age_candles", 4)
    state.detected_sweeps = [
        s for s in state.detected_sweeps
        if (len(candles_5m) - 1 - s.candle_idx) <= max_age
    ]

    mode = params.get("mode", "reversal")

    # ── Raschke/Connors 80-20 (Street Smarts, verified rules): prior session
    # opened in the top 20% of its range and closed in the bottom 20% (a
    # full trend-down day), today undercuts yesterday's low, entry is a
    # buy-stop back AT yesterday's low (the failed-breakdown snap-back).
    # Stop = today's low (structural). Mirror for shorts. Day trade only.
    if mode == "8020" and state.pdh > 0 and state.pdl > 0 and state.pd_open > 0:
        pd_range = state.pdh - state.pdl
        if pd_range > 0:
            open_pos = (state.pd_open - state.pdl) / pd_range
            close_pos = (state.pd_close - state.pdl) / pd_range
            buf = max(spot * 0.0004, 8.0)  # undercut must be meaningful
            today_low = min(c.low for c in candles_1m)
            today_high = max(c.high for c in candles_1m)
            last_1m_bar = candles_1m[-1]
            prev_1m_bar = candles_1m[-2]
            # Long: trend-down prior day, undercut, reclaim of PDL
            if (open_pos >= 0.8 and close_pos <= 0.2
                    and today_low <= state.pdl - buf
                    and prev_1m_bar.close <= state.pdl < last_1m_bar.close):
                return {
                    "direction": "bullish",
                    "setup": "8020_long",
                    "entry_price": last_1m_bar.close,
                    "underlying": last_1m_bar.close,
                    "sweep_level": state.pdl,
                    "structural_stop": today_low,
                    "atr": atr_val,
                    "candle_ts": last_1m_bar.timestamp,
                }
            # Short: trend-up prior day, overshoot of PDH, failure back under
            if (open_pos <= 0.2 and close_pos >= 0.8
                    and today_high >= state.pdh + buf
                    and prev_1m_bar.close >= state.pdh > last_1m_bar.close):
                return {
                    "direction": "bearish",
                    "setup": "8020_short",
                    "entry_price": last_1m_bar.close,
                    "underlying": last_1m_bar.close,
                    "sweep_level": state.pdh,
                    "structural_stop": today_high,
                    "atr": atr_val,
                    "candle_ts": last_1m_bar.timestamp,
                }
        return None

    # ── Continuation setup: high-volume 5-min close THROUGH a level that
    # then holds (acceptance) is traded WITH the break, not faded. 5/7 of
    # the original sweep-reversal trades continued through the level.
    if mode in ("continuation", "both") and len(candles_5m) >= 3:
        break_5m = candles_5m[-2]
        hold_5m = candles_5m[-1]
        for level, lvl_dir, label in levels:
            if lvl_dir == "bearish":  # resistance level → upside break
                broke = (candles_5m[-3].close <= level < break_5m.close
                         and break_5m.volume >= vol_avg * params.get("sweep_volume_mult", 1.5))
                holds = hold_5m.low > level and hold_5m.close > level
                if broke and holds:
                    return {
                        "direction": "bullish",
                        "setup": "break_continuation",
                        "entry_price": hold_5m.close,
                        "underlying": hold_5m.close,
                        "sweep_level": level,
                        "level_label": label,
                        "atr": atr_val,
                        "candle_ts": hold_5m.timestamp,
                    }
            else:  # support level → downside break
                broke = (candles_5m[-3].close >= level > break_5m.close
                         and break_5m.volume >= vol_avg * params.get("sweep_volume_mult", 1.5))
                holds = hold_5m.high < level and hold_5m.close < level
                if broke and holds:
                    return {
                        "direction": "bearish",
                        "setup": "break_continuation",
                        "entry_price": hold_5m.close,
                        "underlying": hold_5m.close,
                        "sweep_level": level,
                        "level_label": label,
                        "atr": atr_val,
                        "candle_ts": hold_5m.timestamp,
                    }

    if mode not in ("reversal", "both"):
        return None

    # ── Reversal setup: AVWAP reclaim after a fresh sweep. Requires TWO
    # consecutive 1-min closes beyond AVWAP (hold, not a single noise cross)
    # and the entry close on the correct side of the swept level — the
    # original code never checked the level side and could enter already
    # past its own invalidation.
    avwap_series = vwap(candles_1m, anchor_idx=state.avwap_anchor_idx)
    if len(avwap_series) < 3:
        return None
    last_1m = candles_1m[-1]
    prev_1m = candles_1m[-2]
    prev2_1m = candles_1m[-3]
    avwap_now = avwap_series[-1]
    avwap_prev = avwap_series[-2]
    avwap_prev2 = avwap_series[-3]
    if avwap_now != avwap_now or avwap_prev != avwap_prev or avwap_prev2 != avwap_prev2:
        return None

    for sweep in state.detected_sweeps:
        if sweep.direction == "bullish":
            crossed = prev2_1m.close <= avwap_prev2 and prev_1m.close > avwap_prev
            held = last_1m.close > avwap_now
            right_side = last_1m.close > sweep.level
            if crossed and held and right_side:
                return {
                    "direction": "bullish",
                    "setup": "sweep_reversal",
                    "entry_price": last_1m.close,
                    "underlying": last_1m.close,
                    "sweep_level": sweep.level,
                    "avwap": avwap_now,
                    "atr": atr_val,
                    "candle_ts": last_1m.timestamp,
                }
        elif sweep.direction == "bearish":
            crossed = prev2_1m.close >= avwap_prev2 and prev_1m.close < avwap_prev
            held = last_1m.close < avwap_now
            right_side = last_1m.close < sweep.level
            if crossed and held and right_side:
                return {
                    "direction": "bearish",
                    "setup": "sweep_reversal",
                    "entry_price": last_1m.close,
                    "underlying": last_1m.close,
                    "sweep_level": sweep.level,
                    "avwap": avwap_now,
                    "atr": atr_val,
                    "candle_ts": last_1m.timestamp,
                }
    return None


def update_or_levels(candles_1m: list[Candle], state: StrategyState):
    """Lock OR high/low at 9:45 IST."""
    if state.or_locked:
        return
    or_window = [c for c in candles_1m
                 if time(9, 15) <= c.timestamp.time() <= time(9, 44)]
    if or_window and any(c.timestamp.time() >= time(9, 44) for c in candles_1m):
        state.or_high = max(c.high for c in or_window)
        state.or_low = min(c.low for c in or_window)
        state.or_locked = True


# ─────────────────────────────────────────────────────────────────────
# Live-tradable strategy class
# ─────────────────────────────────────────────────────────────────────
@StrategyEngine.register("liquidity_sweep")
class LiquiditySweepStrategy(BaseStrategy):
    """Live-trading wrapper. The pure logic lives in evaluate_sweep_entry()."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.data_feed = None
        self.risk_manager = None
        self.journal = None
        self.api = None
        self.instruments = None
        self._state = StrategyState()
        self._open_positions: dict[str, ScaledPosition] = {}
        self._underlying_token = self.params.get("underlying_token", "26000")
        self._underlying_exchange = self.params.get("underlying_exchange", "NFO")

    async def initialize(self):
        log.info("LiquiditySweep initializing")
        # PDH/PDL fetched at session start; backtest harness sets these directly.

    async def on_candle(self, token: str, candle: Candle) -> Signal | None:
        return None  # backtest harness drives evaluation, not the live engine

    async def on_tick(self, token: str, price: float) -> Signal | None:
        return None

    async def teardown(self):
        log.info("LiquiditySweep teardown — open positions: %d",
                 len(self._open_positions))
