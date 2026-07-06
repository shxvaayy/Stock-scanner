"""Option-trade plumbing shared by all options strategies.

- Strike selection (ATM/OTM with VIX-adjusted offset)
- Expiry selection (current week unless near expiry → next week)
- Position sizing for options (risk-based, lot-rounded)
- Quote fetcher with bid/ask + spread tracking
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import dataclass

# Avoid circular imports — pulled lazily inside functions where needed
try:
    from src.expiry import is_nifty_expiry_day, next_expiry_date
except Exception:  # pragma: no cover — fallback when expiry helpers unavailable
    is_nifty_expiry_day = None
    next_expiry_date = None

NIFTY_LOT_SIZE = 65  # Verified live from instrument master, Apr 2026


def select_atm_strike(spot: float, vix: float, is_expiry_day: bool, direction: str) -> int:
    """Pick a strike for a Nifty option trade.

    - Round spot to nearest 50 → ATM
    - If expiry day: ATM (no offset)
    - If VIX ≤ 15 (low vol): 1 strike OTM (50pt) for premium efficiency
    - If VIX 15–18 (medium): 1 strike OTM
    - If VIX > 18 (high): ATM (stay near the money, premium is rich anyway)

    direction: "CE" or "PE"
    """
    direction = direction.upper()
    if direction not in {"CE", "PE"}:
        raise ValueError(f"direction must be CE or PE, got {direction!r}")
    atm = round(spot / 50) * 50
    if is_expiry_day:
        return atm
    if vix > 18:
        return atm
    # 1 strike OTM
    if direction == "CE":
        return atm + 50
    return atm - 50


def select_expiry_date(today: date, prefer_current_week: bool = True) -> date:
    """Pick an expiry: current week unless today IS expiry day or 1 day before.

    Falls back to a simple Tuesday-of-this-week heuristic if expiry helpers
    aren't available.
    """
    if next_expiry_date is None:
        # Heuristic: next Tuesday from today
        days_ahead = (1 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # if today is Tuesday, return next Tuesday
        return today + timedelta(days=days_ahead)

    nearest = next_expiry_date(today)
    days_to_expiry = (nearest - today).days
    if days_to_expiry <= 1 and prefer_current_week:
        # Skip to the week after
        return next_expiry_date(today + timedelta(days=2))
    return nearest


def calculate_option_qty(premium: float, sl_pct: float, capital: float,
                         risk_pct: float, lot_size: int = NIFTY_LOT_SIZE,
                         high_vix_multiplier: float = 1.0) -> int:
    """Size a BUY order based on max-loss risk budget.

    risk_amount = capital * risk_pct
    max_loss_per_unit = premium * sl_pct
    raw_qty = (risk_amount * size_multiplier) / max_loss_per_unit
    Round DOWN to nearest multiple of lot_size. Returns 0 if below 1 lot.
    """
    if premium <= 0 or sl_pct <= 0 or lot_size <= 0:
        return 0
    # risk_pct convention: 1.0 means 1% (matches config.yaml: risk_per_trade_pct)
    risk_amount = capital * (risk_pct / 100.0)
    risk_amount *= high_vix_multiplier
    max_loss_per_unit = premium * sl_pct
    raw_qty = int(risk_amount / max_loss_per_unit)
    lots = raw_qty // lot_size
    return lots * lot_size  # 0 if raw_qty < lot_size


def fetch_option_quote(api, exchange: str, symbol: str, token: str) -> dict:
    """Wrap api.ltpData / quote endpoint into a normalised dict.

    Returns:
        {
            "ltp": float,
            "bid": float | None,
            "ask": float | None,
            "spread_pct": float | None,  # (ask-bid)/mid
            "ts": datetime,
        }
    SmartAPI's ltpData() doesn't expose bid/ask consistently. If unavailable,
    returns bid=ask=None and spread_pct=None — callers must handle that.
    """
    try:
        result = api.ltpData(exchange, symbol, token)
        if not result or not result.get("status"):
            return {"ltp": 0.0, "bid": None, "ask": None,
                    "spread_pct": None, "ts": datetime.now()}
        data = result.get("data", {})
        ltp = float(data.get("ltp", 0))
        # Some endpoints return bid/ask in best5 — try a couple of keys
        bid = data.get("bid") or data.get("buy_price")
        ask = data.get("ask") or data.get("sell_price")
        spread_pct = None
        if bid and ask and ask > 0 and bid > 0:
            mid = (bid + ask) / 2
            if mid > 0:
                spread_pct = (ask - bid) / mid
        return {
            "ltp": ltp, "bid": bid, "ask": ask,
            "spread_pct": spread_pct, "ts": datetime.now(),
        }
    except Exception:
        return {"ltp": 0.0, "bid": None, "ask": None,
                "spread_pct": None, "ts": datetime.now()}
