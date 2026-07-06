"""S2 expiry-day backtest runner.

Mirrors strategies/expiry_skew.py step for step (2PM entry, VIX 12-18 gate,
skew >= 2 gate, per-short-leg SL at 2x entry premium, 15:15 hard exit), with
leg premiums synthesized via Black-Scholes on the Nifty 1-min path.

Premium model: base IV = VIX/100 with a red-day put-skew adjustment — puts
richen when the session is down. The skew parameter s0 controls how often
the >=2:1 premium-skew gate fires; it is reported, not silently assumed.
Entry IVs are held fixed through the monitoring window (no intraday IV path).
Results are directional evidence, not exact P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime

from models.types import Candle
from src.fees import calculate_fees, estimate_option_slippage
from backtest.records import TradeRecord
from backtest.option_synth import black_scholes_price

EXPIRY_CLOSE = dtime(15, 30)


@dataclass
class CondorParams:
    otm_offset: int = 50
    wing_width: int = 100
    min_skew_ratio: float = 2.0
    sl_multiplier: float = 2.0
    entry_hh: int = 14
    entry_mm: int = 0
    exit_hh: int = 15
    exit_mm: int = 15
    lot_size: int = 65
    vix_min: float = 12.0
    vix_max: float = 18.0
    # premium model
    skew_s0: float = 0.10
    skew_k: float = 0.5
    skew_max: float = 0.35
    # restructure (Phase 4c): "condor" or "rich_side_vertical"
    structure: str = "condor"
    # strike selection by target premium (research: ~Rs 20-25 ~ 15-20 delta
    # at 2PM) instead of fixed +-50 points; None keeps the fixed offset
    target_premium: float | None = None
    # conditional gates (Phase 4c); None disables
    max_range_pct_by_entry: float | None = None
    max_vwap_dist_pct: float | None = None
    underlying_touch_stop: bool = False


def _years_to_close(ts: datetime) -> float:
    mins = (EXPIRY_CLOSE.hour * 60 + EXPIRY_CLOSE.minute) - (ts.hour * 60 + ts.minute)
    return max(mins, 1) / (365.0 * 24 * 60)


def _leg_ivs(base_iv: float, day_ret: float, p: CondorParams) -> tuple[float, float]:
    """(put_iv, call_iv). Puts richen on red sessions."""
    sigma_daily = max(base_iv / (252 ** 0.5), 1e-4)
    s = p.skew_s0 + p.skew_k * (-day_ret / sigma_daily) * 0.01
    s = min(max(s, 0.0), p.skew_max)
    return base_iv * (1 + s), base_iv * (1 - 0.5 * s)


def _price_leg(spot: float, strike: float, ts: datetime, iv: float, opt: str) -> float:
    return black_scholes_price(spot, strike, _years_to_close(ts), iv, opt)


def run_condor_day(candles_1m: list[Candle], d: date, vix: float | None,
                   params: CondorParams | None = None) -> tuple[list[TradeRecord], str]:
    """Run S2 on one expiry day. Returns (leg trade records, skip_reason)."""
    p = params or CondorParams()
    if not candles_1m or len(candles_1m) < 120:
        return [], "no_data"
    if vix is None:
        return [], "no_vix"
    if vix > 100:
        vix = vix / 100
    if vix < p.vix_min or vix > p.vix_max:
        return [], f"vix_gate({vix:.1f})"

    entry_t = dtime(p.entry_hh, p.entry_mm)
    exit_t = dtime(p.exit_hh, p.exit_mm)
    entry_idx = next((i for i, c in enumerate(candles_1m)
                      if c.timestamp.time() >= entry_t), None)
    if entry_idx is None or entry_idx < 30:
        return [], "no_entry_bar"

    entry_bar = candles_1m[entry_idx]
    spot = entry_bar.close
    session_open = candles_1m[0].open
    day_ret = (spot - session_open) / session_open * 100  # percent

    # conditional gates (off by default = legacy behaviour)
    if p.max_range_pct_by_entry is not None:
        hi = max(c.high for c in candles_1m[:entry_idx + 1])
        lo = min(c.low for c in candles_1m[:entry_idx + 1])
        if (hi - lo) / spot * 100 > p.max_range_pct_by_entry:
            return [], "range_gate"
    if p.max_vwap_dist_pct is not None:
        num = sum(((c.high + c.low + c.close) / 3) * max(c.volume, 1)
                  for c in candles_1m[:entry_idx + 1])
        den = sum(max(c.volume, 1) for c in candles_1m[:entry_idx + 1])
        sess_vwap = num / den
        if abs(spot - sess_vwap) / spot * 100 > p.max_vwap_dist_pct:
            return [], "vwap_gate"

    base_iv = vix / 100.0
    put_iv, call_iv = _leg_ivs(base_iv, day_ret, p)

    atm = round(spot / 50) * 50
    sell_put_k = atm - p.otm_offset
    sell_call_k = atm + p.otm_offset
    if p.target_premium:
        # walk OTM in 50-pt steps to the farthest strike still collecting
        # >= target premium
        k = sell_put_k
        while k > atm - 500:
            nxt = k - 50
            if _price_leg(spot, nxt, entry_bar.timestamp, put_iv, "PE") < p.target_premium:
                break
            k = nxt
        sell_put_k = k
        k = sell_call_k
        while k < atm + 500:
            nxt = k + 50
            if _price_leg(spot, nxt, entry_bar.timestamp, call_iv, "CE") < p.target_premium:
                break
            k = nxt
        sell_call_k = k
    buy_put_k = sell_put_k - p.wing_width
    buy_call_k = sell_call_k + p.wing_width

    sp = _price_leg(spot, sell_put_k, entry_bar.timestamp, put_iv, "PE")
    sc = _price_leg(spot, sell_call_k, entry_bar.timestamp, call_iv, "CE")
    bp = _price_leg(spot, buy_put_k, entry_bar.timestamp, put_iv, "PE")
    bc = _price_leg(spot, buy_call_k, entry_bar.timestamp, call_iv, "CE")

    ratio = max(sp, sc) / max(min(sp, sc), 0.05)
    if ratio < p.min_skew_ratio:
        return [], f"skew_gate({ratio:.2f})"

    rich_is_put = sp >= sc
    legs: dict[str, dict] = {}
    if p.structure == "rich_side_vertical":
        if rich_is_put:
            legs["sell_put"] = {"strike": sell_put_k, "opt": "PE", "iv": put_iv, "entry": sp, "side": "SELL"}
            legs["buy_put"] = {"strike": buy_put_k, "opt": "PE", "iv": put_iv, "entry": bp, "side": "BUY"}
        else:
            legs["sell_call"] = {"strike": sell_call_k, "opt": "CE", "iv": call_iv, "entry": sc, "side": "SELL"}
            legs["buy_call"] = {"strike": buy_call_k, "opt": "CE", "iv": call_iv, "entry": bc, "side": "BUY"}
    else:
        legs["sell_put"] = {"strike": sell_put_k, "opt": "PE", "iv": put_iv, "entry": sp, "side": "SELL"}
        legs["sell_call"] = {"strike": sell_call_k, "opt": "CE", "iv": call_iv, "entry": sc, "side": "SELL"}
        legs["buy_put"] = {"strike": buy_put_k, "opt": "PE", "iv": put_iv, "entry": bp, "side": "BUY"}
        legs["buy_call"] = {"strike": buy_call_k, "opt": "CE", "iv": call_iv, "entry": bc, "side": "BUY"}

    for leg in legs.values():
        leg["closed"] = False
        leg["exit"] = None
        leg["exit_ts"] = None
        leg["reason"] = ""

    sl_levels = {name: leg["entry"] * p.sl_multiplier
                 for name, leg in legs.items() if leg["side"] == "SELL"}

    # minute-by-minute monitor
    for c in candles_1m[entry_idx + 1:]:
        ts = c.timestamp
        if ts.time() >= exit_t:
            break
        for name, leg in legs.items():
            if leg["side"] != "SELL" or leg["closed"]:
                continue
            curr = _price_leg(c.close, leg["strike"], ts, leg["iv"], leg["opt"])
            touched = (p.underlying_touch_stop and
                       ((leg["opt"] == "PE" and c.close <= leg["strike"]) or
                        (leg["opt"] == "CE" and c.close >= leg["strike"])))
            if curr >= sl_levels[name] or touched:
                leg["closed"] = True
                leg["exit"] = curr
                leg["exit_ts"] = ts
                leg["reason"] = "touch_stop" if touched else "stop_loss"
        if all(leg["closed"] for leg in legs.values() if leg["side"] == "SELL"):
            break

    # hard exit for everything still open
    exit_idx = next((i for i, c in enumerate(candles_1m)
                     if c.timestamp.time() >= exit_t), len(candles_1m) - 1)
    exit_bar = candles_1m[min(exit_idx, len(candles_1m) - 1)]
    for leg in legs.values():
        if not leg["closed"]:
            leg["closed"] = True
            leg["exit"] = _price_leg(exit_bar.close, leg["strike"], exit_bar.timestamp,
                                     leg["iv"], leg["opt"])
            leg["exit_ts"] = exit_bar.timestamp
            leg["reason"] = "hard_exit"

    # records with fees + slippage per leg
    records = []
    for name, leg in legs.items():
        qty = p.lot_size
        if leg["side"] == "SELL":
            gross = (leg["entry"] - leg["exit"]) * qty
            f_in = calculate_fees(leg["entry"], qty, "SELL")["total"]
            f_out = calculate_fees(leg["exit"], qty, "BUY")["total"]
        else:
            gross = (leg["exit"] - leg["entry"]) * qty
            f_in = calculate_fees(leg["entry"], qty, "BUY")["total"]
            f_out = calculate_fees(leg["exit"], qty, "SELL")["total"]
        slip = (estimate_option_slippage(leg["entry"], qty)
                + estimate_option_slippage(leg["exit"], qty))
        fees = f_in + f_out + slip
        records.append(TradeRecord(
            strategy="expiry_skew",
            date=str(d),
            direction=name,
            entry_time=entry_bar.timestamp.strftime("%H:%M"),
            exit_time=leg["exit_ts"].strftime("%H:%M"),
            entry_underlying=spot,
            exit_underlying=exit_bar.close,
            entry_premium=round(leg["entry"], 2),
            exit_premium=round(leg["exit"], 2),
            qty=qty,
            gross_pnl=round(gross, 2),
            fees=round(fees, 2),
            net_pnl=round(gross - fees, 2),
            reason=leg["reason"],
            setup=p.structure,
            instrument="condor_leg",
        ))
    return records, ""
