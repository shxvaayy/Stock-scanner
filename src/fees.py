"""Trading fee calculator for Angel One options trades.

Rates verified June 2026 against Zerodha/NSE published schedules (the same
schedule applies at Angel One for flat-brokerage F&O):
- STT on options sell side: 0.1% of premium (Finance Act 2024, effective Oct 2024 —
  the often-quoted 0.15% is wrong)
- Brokerage: Flat Rs 20 per executed order
- GST: 18% on brokerage + exchange txn + SEBI fee
- Exchange txn: 0.03553% of premium (NSE, post Oct-2024 revision)
- SEBI turnover fee: 0.0001%
- Stamp duty on buy side: 0.003%
"""


def calculate_fees(premium: float, quantity: int, side: str,
                   is_settlement: bool = False, intrinsic_value: float = 0.0) -> dict:
    """Calculate all trading costs for an options trade.

    Args:
        premium: Price per unit
        quantity: Number of units (lot size)
        side: 'BUY' or 'SELL'
        is_settlement: True if the option is settling at expiry (ITM auto-exercise)
        intrinsic_value: Per-unit intrinsic value at settlement (only used if is_settlement=True)

    Returns:
        dict with individual fee components and 'total'
    """
    turnover = premium * quantity

    brokerage = 20.0  # Flat per order
    exchange_txn = turnover * 0.0003553  # NSE transaction charge (0.03553% of premium)
    sebi_fee = turnover * 0.000001  # SEBI turnover fee
    gst = (brokerage + exchange_txn + sebi_fee) * 0.18
    stt = turnover * 0.001 if side == "SELL" else 0.0  # 0.1% of premium, sell side
    stamp = turnover * 0.00003 if side == "BUY" else 0.0  # Stamp duty on buy side

    # ITM settlement STT — 0.125% on intrinsic value when an option auto-exercises
    # at expiry. This is applied IN ADDITION to any premium-side STT and is the
    # silent-killer fee on iron-condor short legs that go ITM.
    settlement_stt = 0.0
    if is_settlement and intrinsic_value > 0:
        settlement_stt = (intrinsic_value * quantity) * 0.00125

    total = brokerage + gst + stt + exchange_txn + sebi_fee + stamp + settlement_stt

    return {
        "brokerage": round(brokerage, 2),
        "gst": round(gst, 2),
        "stt": round(stt, 2),
        "settlement_stt": round(settlement_stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi_fee": round(sebi_fee, 2),
        "stamp": round(stamp, 2),
        "total": round(total, 2),
    }


def calculate_equity_fees(price: float, quantity: int, side: str) -> dict:
    """Calculate trading costs for an NSE intraday equity trade.

    Used by equity strategies (RSI Bounce, RSI 15-min) that operate on Nifty 50 stocks.

    Args:
        price: Price per share
        quantity: Number of shares
        side: 'BUY' or 'SELL'
    """
    turnover = price * quantity

    brokerage = 20.0  # Flat per order at Angel
    exchange_txn = turnover * 0.0000297  # NSE intraday equity (0.00297%)
    stt = turnover * 0.00025 if side == "SELL" else 0.0  # 0.025% intraday equity sell-side
    sebi_fee = turnover * 0.000001
    stamp = turnover * 0.00003 if side == "BUY" else 0.0  # 0.003% buy side
    gst = (brokerage + exchange_txn + sebi_fee) * 0.18

    total = brokerage + gst + stt + exchange_txn + sebi_fee + stamp

    return {
        "brokerage": round(brokerage, 2),
        "gst": round(gst, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi_fee": round(sebi_fee, 2),
        "stamp": round(stamp, 2),
        "total": round(total, 2),
    }


def calculate_delivery_fees(price: float, quantity: int, side: str) -> dict:
    """NSE equity DELIVERY (CNC) trade costs — used by swing strategies that
    hold overnight. STT is 0.1% on BOTH sides for delivery (vs 0.025%
    sell-only intraday) and stamp duty is 0.015% on buys."""
    turnover = price * quantity

    brokerage = 20.0  # Angel One flat per order (delivery no longer free)
    exchange_txn = turnover * 0.0000297
    stt = turnover * 0.001  # 0.1% both sides on delivery
    sebi_fee = turnover * 0.000001
    stamp = turnover * 0.00015 if side == "BUY" else 0.0
    gst = (brokerage + exchange_txn + sebi_fee) * 0.18

    total = brokerage + gst + stt + exchange_txn + sebi_fee + stamp
    return {
        "brokerage": round(brokerage, 2), "gst": round(gst, 2),
        "stt": round(stt, 2), "exchange_txn": round(exchange_txn, 2),
        "sebi_fee": round(sebi_fee, 2), "stamp": round(stamp, 2),
        "total": round(total, 2),
    }


def estimate_option_slippage(premium: float, quantity: int) -> float:
    """Conservative one-side slippage for Nifty weekly options.

    1 tick (Rs 0.05) plus 0.25% of premium per unit — liquid ATM/near-OTM
    strikes typically show 0.05-0.20 spreads; we assume crossing half of a
    pessimistic spread.
    """
    per_unit = max(0.05, premium * 0.0025)
    return round(per_unit * quantity, 2)


def estimate_equity_slippage(price: float, quantity: int) -> float:
    """Conservative one-side slippage for liquid NSE large/mid caps: 0.02% of price."""
    return round(price * 0.0002 * quantity, 2)


def _is_option_symbol(symbol: str) -> bool:
    """Detect Nifty option symbol pattern (e.g. NIFTY28APR2624000CE)."""
    if not symbol or len(symbol) < 8:
        return False
    if not (symbol.endswith("CE") or symbol.endswith("PE")):
        return False
    # Must contain digits in body (the strike)
    return any(c.isdigit() for c in symbol[:-2])


def calculate_trade_fees(symbol: str, price: float, quantity: int, side: str) -> float:
    """Convenience wrapper that picks options vs equity fee structure based on symbol.

    Returns just the total fee in rupees. For full breakdown call calculate_fees() or
    calculate_equity_fees() directly.
    """
    if _is_option_symbol(symbol):
        return calculate_fees(price, quantity, side)["total"]
    return calculate_equity_fees(price, quantity, side)["total"]
