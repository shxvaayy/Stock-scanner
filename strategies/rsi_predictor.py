"""Strategy C — RSI Failed-Swing Predictor.

EOD scanner detects W-bottom (bullish) or M-top (bearish) on Nifty daily RSI(14).
Next morning, intraday entry triggers when 15-min RSI(5) confirms direction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

import pytz

from models.types import Candle, Signal, SignalType
from strategies.base import BaseStrategy, StrategyEngine
from strategies.indicators import rsi, vwap, resample

log = logging.getLogger("autotheta.rsi_predictor")
IST = pytz.timezone("Asia/Kolkata")


def detect_w_bottom(daily_rsi: list[float], lookback: int = 15) -> bool:
    """Bullish: prior trough <30, recovery >30, second dip higher than first,
    currently rising above 30."""
    n = len(daily_rsi)
    if n < lookback + 3:
        return False
    rsi_clean = [r for r in daily_rsi if r == r]
    if len(rsi_clean) < lookback + 3:
        return False
    series = rsi_clean[-lookback - 3:]
    # Must end rising above 30
    if not (series[-1] > 30 and series[-1] > series[-2]):
        return False
    # Find prior trough below 30 in [-15..-3]
    prior_lows = [(i, v) for i, v in enumerate(series[:-3]) if v < 30]
    if not prior_lows:
        return False
    prior_idx, prior_val = min(prior_lows, key=lambda x: x[1])
    # Recovery above 30 between prior_idx and now
    if not any(v > 30 for v in series[prior_idx + 1:-3]):
        return False
    # Second dip in last 5 candles, NOT lower than prior trough
    recent = series[-5:-1]
    second_low = min(recent)
    if second_low < prior_val:
        return False
    return True


def detect_m_top(daily_rsi: list[float], lookback: int = 15) -> bool:
    """Bearish: mirror of W-bottom."""
    n = len(daily_rsi)
    if n < lookback + 3:
        return False
    rsi_clean = [r for r in daily_rsi if r == r]
    if len(rsi_clean) < lookback + 3:
        return False
    series = rsi_clean[-lookback - 3:]
    if not (series[-1] < 70 and series[-1] < series[-2]):
        return False
    prior_highs = [(i, v) for i, v in enumerate(series[:-3]) if v > 70]
    if not prior_highs:
        return False
    prior_idx, prior_val = max(prior_highs, key=lambda x: x[1])
    if not any(v < 70 for v in series[prior_idx + 1:-3]):
        return False
    recent = series[-5:-1]
    second_high = max(recent)
    if second_high > prior_val:
        return False
    return True


def classify_eod_pattern(daily_closes: list[float], regime: str = "BULL") -> str:
    """Run both patterns. Returns BULLISH_W | BEARISH_M | NEUTRAL.

    Regime gate:
      BULLISH_W requires regime == BULL
      BEARISH_M requires regime in {BULL, BEAR} (blocks CRASH)
    """
    if len(daily_closes) < 30:
        return "NEUTRAL"
    daily_rsi = rsi(daily_closes, 14)
    if detect_w_bottom(daily_rsi):
        if regime.upper() == "BULL":
            return "BULLISH_W"
        return "NEUTRAL"
    if detect_m_top(daily_rsi):
        if regime.upper() in {"BULL", "BEAR"}:
            return "BEARISH_M"
        return "NEUTRAL"
    return "NEUTRAL"


def evaluate_rsi_entry(candles_1m: list[Candle], pending_signal: dict,
                       prior_close: float, params: dict) -> dict | None:
    """Pure entry function for the next-day intraday trigger."""
    if not candles_1m or not pending_signal:
        return None
    sig_type = pending_signal.get("signal", "NEUTRAL")
    if sig_type == "NEUTRAL":
        return None
    last = candles_1m[-1]
    now_t = last.timestamp.time()
    if now_t < time(10, 0) or now_t > time(11, 30):
        return None

    # Resample to 15-min for RSI(5)
    candles_15m = resample(candles_1m, 15)
    if len(candles_15m) < 8:
        return None
    closes_15m = [c.close for c in candles_15m]
    r5 = rsi(closes_15m, 5)
    if len(r5) < 2 or r5[-1] != r5[-1] or r5[-2] != r5[-2]:
        return None

    # Session VWAP
    avwap = vwap(candles_1m, anchor_idx=0)[-1]
    if avwap != avwap:
        return None
    spot = last.close
    session_open = candles_1m[0].open
    gap_pct = ((session_open - prior_close) / prior_close) * 100 if prior_close else 0.0

    if sig_type == "BULLISH_W":
        # 15-min RSI(5) crosses above 40, price above VWAP, sane gap
        if r5[-2] < 40 and r5[-1] >= 40 and spot > avwap and -0.5 <= gap_pct <= 1.5:
            return {"direction": "bullish", "entry_price": spot,
                    "underlying": spot, "rsi5_15m": r5[-1], "gap_pct": gap_pct}
    elif sig_type == "BEARISH_M":
        if r5[-2] > 60 and r5[-1] <= 60 and spot < avwap and -1.5 <= gap_pct <= 0.5:
            return {"direction": "bearish", "entry_price": spot,
                    "underlying": spot, "rsi5_15m": r5[-1], "gap_pct": gap_pct}
    return None


@StrategyEngine.register("rsi_predictor")
class RSIPredictorStrategy(BaseStrategy):
    """Single-class implementation; EOD scan + next-day entry are both methods."""

    def __init__(self, config: dict):
        super().__init__(config)
        self._signal_path = Path(self.params.get("signal_file_path",
                                                 "data/signals/rsi_prediction.json"))

    async def initialize(self):
        log.info("RSI Predictor initializing")

    async def on_candle(self, token: str, candle: Candle) -> Signal | None:
        return None

    async def on_tick(self, token: str, price: float) -> Signal | None:
        return None

    async def teardown(self):
        pass
