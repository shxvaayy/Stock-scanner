"""Trade record shared by every runner."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeRecord:
    strategy: str
    date: str
    direction: str
    entry_time: str
    exit_time: str
    entry_underlying: float
    exit_underlying: float
    entry_premium: float       # for equities: entry price
    exit_premium: float        # for equities: exit price
    qty: int
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str
    setup: str
    symbol: str = "NIFTY"
    instrument: str = "option"  # option | equity | condor_leg
    regime: str = ""
    segment: str = ""           # train | test (stamped by the report)
