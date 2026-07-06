"""Single source of truth for the equity trading universe.

Nifty 50 core (from the original strategy maps) plus the liquid names from the
user's watchlist. Illiquid small-caps from the watchlist are deliberately
excluded — intraday strategies need volume to fill near fair price.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECTOR_MAP: dict[str, str] = {
    # Banking
    "HDFCBANK-EQ": "Banking", "ICICIBANK-EQ": "Banking", "KOTAKBANK-EQ": "Banking",
    "SBIN-EQ": "Banking", "AXISBANK-EQ": "Banking", "INDUSINDBK-EQ": "Banking",
    "PNB-EQ": "Banking", "BANKBARODA-EQ": "Banking",
    # Finance
    "BAJFINANCE-EQ": "Finance", "BAJAJFINSV-EQ": "Finance", "HDFCLIFE-EQ": "Finance",
    "SBILIFE-EQ": "Finance", "JIOFIN-EQ": "Finance", "SHRIRAMFIN-EQ": "Finance",
    # Energy
    "RELIANCE-EQ": "Energy", "ONGC-EQ": "Energy", "NTPC-EQ": "Energy",
    "POWERGRID-EQ": "Energy", "ADANIENT-EQ": "Energy", "JSWENERGY-EQ": "Energy",
    "ADANIPOWER-EQ": "Energy", "NAVA-EQ": "Energy",
    # IT
    "TCS-EQ": "IT", "INFY-EQ": "IT", "WIPRO-EQ": "IT", "HCLTECH-EQ": "IT",
    "TECHM-EQ": "IT", "LTM-EQ": "IT",  # LTM = LTIMindtree (renamed from LTIM)
    # FMCG
    "HINDUNILVR-EQ": "FMCG", "ITC-EQ": "FMCG", "NESTLEIND-EQ": "FMCG",
    "BRITANNIA-EQ": "FMCG", "TATACONSUM-EQ": "FMCG",
    # Pharma
    "SUNPHARMA-EQ": "Pharma", "DRREDDY-EQ": "Pharma", "CIPLA-EQ": "Pharma",
    "APOLLOHOSP-EQ": "Pharma",
    # Auto
    "M&M-EQ": "Auto", "MARUTI-EQ": "Auto",
    "BAJAJ-AUTO-EQ": "Auto", "HEROMOTOCO-EQ": "Auto", "EICHERMOT-EQ": "Auto",
    # Metals
    "TATASTEEL-EQ": "Metals", "JSWSTEEL-EQ": "Metals", "HINDALCO-EQ": "Metals",
    "COALINDIA-EQ": "Metals", "SAIL-EQ": "Metals",
    # Infra
    "LT-EQ": "Infra", "ULTRACEMCO-EQ": "Infra", "GRASIM-EQ": "Infra",
    "ADANIPORTS-EQ": "Infra", "IRCON-EQ": "Infra",
    # Telecom
    "BHARTIARTL-EQ": "Telecom",
    # Other
    "ASIANPAINT-EQ": "Other", "TITAN-EQ": "Other", "DIVISLAB-EQ": "Other",
    "BEL-EQ": "Other", "TRENT-EQ": "Other", "ETERNAL-EQ": "Other",
}

STOCKS: list[str] = list(SECTOR_MAP.keys())

INDEX_TOKENS = {
    "NIFTY": "99926000",
    "INDIA_VIX": "99926017",
}


def build_token_map(instruments: list[dict] | None = None,
                    symbols: list[str] | None = None) -> dict[str, str]:
    """Map universe symbols to NSE tokens from the instrument master."""
    if instruments is None:
        with open(ROOT / "data" / "instruments.json") as f:
            instruments = json.load(f)
    wanted = set(symbols if symbols is not None else STOCKS)
    token_map: dict[str, str] = {}
    for row in instruments:
        sym = row.get("symbol")
        if sym in wanted and row.get("exch_seg") == "NSE":
            token_map.setdefault(sym, str(row["token"]))
    return token_map
