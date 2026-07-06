"""Strategy B — Volume Profile + Trend.

Computes prior-session VP (POC, VAH, VAL) on Nifty Futures.
Trade VAL bounce → CE, VAH rejection → PE.
200-EMA on daily closes is the trend filter. CVD slope is a soft tie-breaker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

import pytz

from models.types import Candle, Signal, SignalType
from strategies.base import BaseStrategy, StrategyEngine
from strategies.indicators import (
    atr, ema, vwap, compute_volume_profile, compute_cvd, resample,
)
from strategies.position_manager import ScaledPosition, ScalingConfig

log = logging.getLogger("autotheta.vp_trend")
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class VPLevels:
    POC: float = 0.0
    VAH: float = 0.0
    VAL: float = 0.0
    source: str = "prior"  # "prior" | "intraday"


@dataclass
class VPState:
    prior_vp: VPLevels = field(default_factory=VPLevels)
    intraday_vp: VPLevels | None = None
    daily_ema200: float = 0.0


def evaluate_vp_entry(candles_1m: list[Candle], candles_5m: list[Candle],
                      state: VPState, params: dict) -> dict | None:
    """Pure function, used by both live and backtest paths."""
    if len(candles_5m) < 25 or len(candles_1m) < 120:
        return None

    last_5m = candles_5m[-1]
    prev_5m = candles_5m[-2]
    now_t = last_5m.timestamp.time()

    # Time window — ends 12:00; the 12:00-12:30 lunch chop produced
    # persistently failing fades
    in_window = (
        time(9, 50) <= now_t <= time(12, 0) or
        time(14, 0) <= now_t <= time(14, 45)
    )
    if not in_window:
        return None

    mode = params.get("mode", "fade")
    do_fade = mode in ("fade", "both")
    do_breakout = mode in ("breakout", "both")

    # Dalton: fade VA-edge failures only in BALANCE. On trend days
    # (open-drive, large directional session) the fade gets run over.
    # Breakout setups trade WITH the trend, so the gate applies to fades only.
    in_balance = True
    session_open = candles_1m[0].open
    session_high = max(c.high for c in candles_1m)
    session_low = min(c.low for c in candles_1m)
    spot_now = candles_1m[-1].close
    if session_open > 0:
        day_range_pct = (session_high - session_low) / session_open * 100
        drive_pct = abs(spot_now - session_open) / session_open * 100
        in_balance = (day_range_pct <= params.get("max_balance_range_pct", 0.8)
                      and drive_pct <= params.get("max_open_drive_pct", 0.4))
    if do_fade and not do_breakout and not in_balance:
        return None

    # Pick active VP — whichever VAH/VAL is closer to current price
    spot = last_5m.close
    candidates = [state.prior_vp]
    if state.intraday_vp:
        candidates.append(state.intraday_vp)
    active = min(
        candidates,
        key=lambda vp: min(abs(spot - vp.VAL), abs(spot - vp.VAH))
        if (vp.VAL > 0 and vp.VAH > 0) else 1e9,
    )
    if active.VAL <= 0 or active.VAH <= 0:
        return None

    # Adaptive precision band — 1.0% of spot, NOT 0.5×ATR
    band = spot * 0.01

    # Trend filter. Bearish requires price strictly below the 200-EMA —
    # the old ema200*1.03 allowance shorted into uptrends (0/3 winners).
    ema200 = state.daily_ema200
    if ema200 <= 0:
        return None
    bullish_ok = spot > ema200 * 0.97
    bearish_ok = spot < ema200
    # Breakdowns trade WITH momentum, so they only need the loose gate
    bearish_ok_loose = spot < ema200 * 1.03

    # POC clearance
    poc_clearance = abs(spot - active.POC) > params.get("poc_proximity_points", 30)

    atr_5m = atr(candles_5m, 14)
    atr_val = atr_5m[-1] if atr_5m and atr_5m[-1] == atr_5m[-1] else 0.0

    # Dalton: fade a low-volume failure at the VA edge; never fade a
    # high-volume (accepted) break. Volume on the excursion bar >= mult x
    # 20-bar average means acceptance — skip the fade.
    break_5m = candles_5m[-3]
    vols = [c.volume for c in candles_5m[-23:-3]]
    vol_avg = sum(vols) / len(vols) if vols else 0.0
    vol_mult = params.get("acceptance_volume_mult", 1.5)
    low_vol_break = vol_avg <= 0 or break_5m.volume < vol_mult * vol_avg
    high_vol_break = vol_avg > 0 and break_5m.volume >= vol_mult * vol_avg

    # Order-flow confirmation: CVD slope over the last hour of 5-min bars
    # must agree with trade direction. Neutral (no volume data) passes.
    cvd_slope = 0.0
    if params.get("cvd_gate") and len(candles_5m) >= 13:
        cvd = compute_cvd(candles_5m[-12:])
        cvd_slope = cvd[-1] - cvd[0]

    def cvd_ok(direction: str) -> bool:
        if not params.get("cvd_gate") or cvd_slope == 0.0:
            return True
        return cvd_slope > 0 if direction == "bullish" else cvd_slope < 0

    if do_breakout:
        # "Go with breakouts from balance" — high-volume break of a VA
        # boundary, held by the next completed bar (acceptance), traded in
        # the break direction. Targets project the VA geometry beyond the
        # broken boundary.
        if bullish_ok and high_vol_break and cvd_ok("bullish"):
            broke_up = (break_5m.close <= active.VAH < prev_5m.close
                        and abs(prev_5m.close - active.VAH) < band)
            holds = last_5m.low > active.VAH and last_5m.close > active.VAH
            if broke_up and holds:
                return {
                    "direction": "bullish",
                    "setup": "VAH_BREAKOUT",
                    "entry_price": last_5m.close,
                    "underlying": last_5m.close,
                    "vp": active,
                    "atr": atr_val,
                }
        if bearish_ok_loose and high_vol_break and cvd_ok("bearish"):
            broke_down = (break_5m.close >= active.VAL > prev_5m.close
                          and abs(prev_5m.close - active.VAL) < band)
            holds = last_5m.high < active.VAL and last_5m.close < active.VAL
            if broke_down and holds:
                return {
                    "direction": "bearish",
                    "setup": "VAL_BREAKDOWN",
                    "entry_price": last_5m.close,
                    "underlying": last_5m.close,
                    "vp": active,
                    "atr": atr_val,
                }
        if not do_fade:
            return None

    if not do_fade or not in_balance:
        return None

    # Setup A — VAL failure fade (bullish):
    # bar -3 closed below VAL (the excursion), bar -2 reclaimed (closed back
    # above), and the CURRENT completed bar holds the reclaim (low and close
    # above VAL). Entering on the hold bar is Dalton's rejection signature —
    # the one-bar delay removes the first-touch noise reclaims.
    if bullish_ok and poc_clearance and low_vol_break:
        reclaimed = (break_5m.close <= active.VAL and prev_5m.close > active.VAL
                     and abs(prev_5m.close - active.VAL) < band)
        holds = last_5m.low > active.VAL and last_5m.close > active.VAL
        if reclaimed and holds:
            return {
                "direction": "bullish",
                "setup": "VAL_BOUNCE",
                "entry_price": last_5m.close,
                "underlying": last_5m.close,
                "vp": active,
                "atr": atr_val,
            }

    # Setup B — VAH failure fade (bearish), mirror logic
    if bearish_ok and poc_clearance and low_vol_break:
        rejected = (break_5m.close >= active.VAH and prev_5m.close < active.VAH
                    and abs(prev_5m.close - active.VAH) < band)
        holds = last_5m.high < active.VAH and last_5m.close < active.VAH
        if rejected and holds:
            return {
                "direction": "bearish",
                "setup": "VAH_REJECTION",
                "entry_price": last_5m.close,
                "underlying": last_5m.close,
                "vp": active,
                "atr": atr_val,
            }

    return None


@StrategyEngine.register("vp_trend")
class VolumeProfileTrendStrategy(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self._state = VPState()

    async def initialize(self):
        log.info("VP+Trend initializing")

    async def on_candle(self, token: str, candle: Candle) -> Signal | None:
        return None

    async def on_tick(self, token: str, price: float) -> Signal | None:
        return None

    async def teardown(self):
        pass
