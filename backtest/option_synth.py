"""Option premium synthesis.

estimate_atm_premium / synthesize_option_pnl are moved verbatim from
scripts/backtest_new_strategies.py (delta-0.5 + linear theta model) so the
options runners reproduce the original backtest exactly.

black_scholes_price is for the S2 condor runner, whose OTM legs are nowhere
near delta 0.5.
"""

from __future__ import annotations

import math


def estimate_atm_premium(spot: float, days_to_expiry: int = 3,
                         implied_vol: float = 0.14) -> float:
    """Rough ATM premium for a Nifty weekly option.

    Uses simplified ATM premium = 0.4 × spot × IV × √(T/365)
    """
    if days_to_expiry <= 0:
        days_to_expiry = 1
    return round(0.4 * spot * implied_vol * math.sqrt(days_to_expiry / 365), 1)


def synthesize_option_pnl(direction: str,
                          entry_underlying: float, exit_underlying: float,
                          entry_premium: float, hours_held: float,
                          delta: float = 0.5,
                          theta_pct_per_day: float = 0.08) -> tuple[float, float]:
    """Approx P&L per unit on an option trade. Returns (exit_premium, change)."""
    move = exit_underlying - entry_underlying
    if direction == "bearish":
        move = -move  # PE profits when underlying falls
    delta_pnl_per_unit = delta * move
    days = hours_held / 6.25  # ~6.25 trading hours in NSE day
    theta_loss_per_unit = entry_premium * theta_pct_per_day * days
    exit_premium = max(entry_premium + delta_pnl_per_unit - theta_loss_per_unit, 0.5)
    return exit_premium, exit_premium - entry_premium


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(spot: float, strike: float, t_years: float,
                        iv: float, opt_type: str, r: float = 0.065) -> float:
    """European option price. opt_type: 'CE' | 'PE'. Floors at 0.05 (1 tick)."""
    if t_years <= 0 or iv <= 0:
        intrinsic = max(spot - strike, 0.0) if opt_type == "CE" else max(strike - spot, 0.0)
        return max(intrinsic, 0.05)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    if opt_type == "CE":
        price = spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    else:
        price = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(price, 0.05)
