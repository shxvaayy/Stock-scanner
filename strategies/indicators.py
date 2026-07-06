"""Technical indicators — pure Python/NumPy, no TA-Lib, no numba.

Used by all new strategies (liquidity sweep, vp_trend, rsi predictor) and
intended for the existing strategies once they're refactored.

All functions accept either lists of floats or lists of Candle objects and
return Python lists (NaN for indices where the indicator isn't yet defined).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable

from models.types import Candle

NaN = float("nan")


# ─────────────────────────────────────────────────────────────────────
# Basic indicators
# ─────────────────────────────────────────────────────────────────────
def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average. Returns NaN for the first (period-1) values."""
    if not values or period <= 0:
        return [NaN] * len(values)
    out = [NaN] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    # Seed with simple mean of first `period` values
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, len(values)):
        prev = out[i - 1]
        out[i] = alpha * values[i] + (1 - alpha) * prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns NaN for indices < period."""
    if len(closes) < period + 1:
        return [NaN] * len(closes)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    out = [NaN] * len(closes)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    out[period] = 100 - (100 / (1 + rs)) if avg_loss > 0 else 100.0
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        out[i] = 100 - (100 / (1 + rs)) if avg_loss > 0 else 100.0
    return out


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    """Average True Range using Wilder smoothing."""
    if len(candles) < period + 1:
        return [NaN] * len(candles)
    tr = [NaN]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr.append(max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        ))
    out = [NaN] * len(candles)
    seed = sum(tr[1:period + 1]) / period
    out[period] = seed
    for i in range(period + 1, len(candles)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def vwap(candles: list[Candle], anchor_idx: int = 0) -> list[float]:
    """Anchored VWAP. Returns NaN for indices before anchor_idx.

    anchor_idx=0 means full session/series VWAP.
    """
    if not candles or anchor_idx >= len(candles):
        return [NaN] * len(candles)
    out = [NaN] * len(candles)
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(anchor_idx, len(candles)):
        c = candles[i]
        tp = (c.high + c.low + c.close) / 3
        cum_pv += tp * c.volume
        cum_v += c.volume
        out[i] = cum_pv / cum_v if cum_v > 0 else c.close
    return out


def adx(candles: list[Candle], period: int = 14) -> tuple[list[float], list[float], list[float]]:
    """Average Directional Index — returns (adx, plus_di, minus_di)."""
    n = len(candles)
    if n < period + 1:
        return [NaN] * n, [NaN] * n, [NaN] * n
    tr = [0.0]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        up_move = c.high - p.high
        down_move = p.low - c.low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))

    # Wilder smoothed
    sm_tr = [NaN] * n
    sm_plus = [NaN] * n
    sm_minus = [NaN] * n
    sm_tr[period] = sum(tr[1:period + 1])
    sm_plus[period] = sum(plus_dm[1:period + 1])
    sm_minus[period] = sum(minus_dm[1:period + 1])
    for i in range(period + 1, n):
        sm_tr[i] = sm_tr[i - 1] - sm_tr[i - 1] / period + tr[i]
        sm_plus[i] = sm_plus[i - 1] - sm_plus[i - 1] / period + plus_dm[i]
        sm_minus[i] = sm_minus[i - 1] - sm_minus[i - 1] / period + minus_dm[i]

    plus_di = [NaN] * n
    minus_di = [NaN] * n
    dx = [NaN] * n
    for i in range(period, n):
        if sm_tr[i] and sm_tr[i] > 0:
            plus_di[i] = 100 * sm_plus[i] / sm_tr[i]
            minus_di[i] = 100 * sm_minus[i] / sm_tr[i]
            denom = plus_di[i] + minus_di[i]
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom if denom > 0 else 0.0

    adx_out = [NaN] * n
    if n > 2 * period:
        adx_out[2 * period] = sum(dx[period + 1:2 * period + 1]) / period
        for i in range(2 * period + 1, n):
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, plus_di, minus_di


def mfi(candles: list[Candle], period: int = 14) -> list[float]:
    """Money Flow Index — RSI weighted by volume."""
    n = len(candles)
    if n < period + 1:
        return [NaN] * n
    out = [NaN] * n
    tp = [(c.high + c.low + c.close) / 3 for c in candles]
    pos_flow = [0.0]
    neg_flow = [0.0]
    for i in range(1, n):
        flow = tp[i] * candles[i].volume
        if tp[i] > tp[i - 1]:
            pos_flow.append(flow); neg_flow.append(0.0)
        elif tp[i] < tp[i - 1]:
            pos_flow.append(0.0); neg_flow.append(flow)
        else:
            pos_flow.append(0.0); neg_flow.append(0.0)
    for i in range(period, n):
        pf = sum(pos_flow[i - period + 1:i + 1])
        nf = sum(neg_flow[i - period + 1:i + 1])
        if nf == 0:
            out[i] = 100.0
        else:
            mr = pf / nf
            out[i] = 100 - (100 / (1 + mr))
    return out


def kaufman_efficiency_ratio(closes: list[float], period: int = 10) -> list[float]:
    """KER = |net move| / sum of absolute moves. Range: 0 (chop) to 1 (trending)."""
    n = len(closes)
    out = [NaN] * n
    if n < period + 1:
        return out
    for i in range(period, n):
        net_change = abs(closes[i] - closes[i - period])
        vol_sum = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
        out[i] = net_change / vol_sum if vol_sum > 0 else 0.0
    return out


# ─────────────────────────────────────────────────────────────────────
# Swing detection — used by liquidity sweep
# ─────────────────────────────────────────────────────────────────────
def swing_highs_lows(candles: list[Candle], swing_length: int = 5) -> list[tuple[int, float, str]]:
    """Detect swing highs and swing lows.

    A candle at index i is a swing high if its high is the strict maximum
    in the window [i - swing_length, i + swing_length]. Mirror for swing low.
    Returns a list of (index, price, "high" | "low") in chronological order.
    """
    out: list[tuple[int, float, str]] = []
    n = len(candles)
    if n < 2 * swing_length + 1:
        return out
    for i in range(swing_length, n - swing_length):
        window = candles[i - swing_length:i + swing_length + 1]
        center_high = candles[i].high
        center_low = candles[i].low
        if center_high == max(c.high for c in window) and \
                sum(1 for c in window if c.high == center_high) == 1:
            out.append((i, center_high, "high"))
        elif center_low == min(c.low for c in window) and \
                sum(1 for c in window if c.low == center_low) == 1:
            out.append((i, center_low, "low"))
    return out


# ─────────────────────────────────────────────────────────────────────
# Resampling
# ─────────────────────────────────────────────────────────────────────
def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    """Aggregate 1-min candles into N-min candles aligned to minute boundaries."""
    if minutes <= 1 or not candles:
        return list(candles)
    out: list[Candle] = []
    bucket: list[Candle] = []
    bucket_key = None
    for c in candles:
        # Floor timestamp to nearest `minutes` boundary
        m = c.timestamp.minute - (c.timestamp.minute % minutes)
        key = c.timestamp.replace(minute=m, second=0, microsecond=0)
        if bucket_key is None:
            bucket_key = key
        if key != bucket_key and bucket:
            out.append(_aggregate(bucket, bucket_key))
            bucket = []
            bucket_key = key
        bucket.append(c)
    if bucket:
        out.append(_aggregate(bucket, bucket_key))
    return out


def _aggregate(bucket: list[Candle], ts) -> Candle:
    return Candle(
        timestamp=ts,
        open=bucket[0].open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=bucket[-1].close,
        volume=sum(c.volume for c in bucket),
        token=bucket[0].token,
        symbol=bucket[0].symbol,
    )


# ─────────────────────────────────────────────────────────────────────
# Volume Profile — used by VP+Trend strategy
# ─────────────────────────────────────────────────────────────────────
def compute_volume_profile(candles: list[Candle], n_bins: int = 150,
                           value_area_pct: float = 0.70) -> dict:
    """Compute Volume Profile from a list of OHLCV candles.

    Returns dict with:
        POC : float (point of control — price midpoint of highest-volume bin)
        VAH : float (value area high)
        VAL : float (value area low)
        bin_volumes : list[float]
        bin_edges : list[float]
    """
    if not candles or n_bins <= 0:
        return {"POC": NaN, "VAH": NaN, "VAL": NaN, "bin_volumes": [], "bin_edges": []}
    price_min = min(c.low for c in candles)
    price_max = max(c.high for c in candles)
    if price_max == price_min:
        return {"POC": price_min, "VAH": price_min, "VAL": price_min,
                "bin_volumes": [sum(c.volume for c in candles)],
                "bin_edges": [price_min, price_max]}

    bin_size = (price_max - price_min) / n_bins
    edges = [price_min + i * bin_size for i in range(n_bins + 1)]
    volumes = [0.0] * n_bins

    for c in candles:
        if c.high == c.low:
            idx = min(max(int((c.low - price_min) / bin_size), 0), n_bins - 1)
            volumes[idx] += c.volume
            continue
        for i in range(n_bins):
            b_lo = edges[i]
            b_hi = edges[i + 1]
            overlap = min(c.high, b_hi) - max(c.low, b_lo)
            if overlap > 0:
                volumes[i] += c.volume * (overlap / (c.high - c.low))

    # POC
    poc_idx = max(range(n_bins), key=lambda i: volumes[i])
    poc = (edges[poc_idx] + edges[poc_idx + 1]) / 2

    # Value area: expand from POC outward until 70% of total volume covered
    total = sum(volumes)
    target = total * value_area_pct
    accum = volumes[poc_idx]
    above = poc_idx + 1
    below = poc_idx - 1
    vah_idx = poc_idx
    val_idx = poc_idx
    while accum < target and (above < n_bins or below >= 0):
        vol_above = volumes[above] if above < n_bins else -1
        vol_below = volumes[below] if below >= 0 else -1
        # Tie-break: prefer below (more conservative VAL)
        if vol_above > vol_below:
            accum += vol_above
            vah_idx = above
            above += 1
        else:
            if below >= 0:
                accum += vol_below
                val_idx = below
                below -= 1
            else:
                accum += vol_above
                vah_idx = above
                above += 1

    vah = edges[vah_idx + 1]  # upper edge of highest VA bin
    val = edges[val_idx]       # lower edge of lowest VA bin
    return {"POC": poc, "VAH": vah, "VAL": val,
            "bin_volumes": volumes, "bin_edges": edges}


def approximate_delta(candle: Candle) -> float:
    """OHLCV-only approximation of order-flow delta.

    Returns volume * (2*bull_frac - 1):
      bull_frac = (close - low) / (high - low)
      +volume = 100% bullish, 0 = doji, -volume = 100% bearish.
    """
    if candle.high == candle.low:
        return 0.0
    bull_frac = (candle.close - candle.low) / (candle.high - candle.low)
    return candle.volume * (2 * bull_frac - 1)


def compute_cvd(candles: list[Candle]) -> list[float]:
    """Cumulative Volume Delta from approximate_delta()."""
    out = []
    cum = 0.0
    for c in candles:
        cum += approximate_delta(c)
        out.append(cum)
    return out
