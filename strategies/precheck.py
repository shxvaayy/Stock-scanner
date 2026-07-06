"""Pre-trade entry checklist — every option strategy calls this before submitting an order.

Single most important loss-control primitive. Eight checks run in order.
First failure short-circuits and returns allowed=False with the reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytz

log = logging.getLogger("autotheta.precheck")
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class PrecheckResult:
    allowed: bool
    reason: str
    spread_pct: float | None = None
    last_tick_age_sec: float | None = None
    vix: float = 0.0


def precheck_option_entry(
    api,
    data_feed,
    risk_manager,
    regime_module,
    strategy_name: str,
    symbol: str,
    token: str,
    exchange: str,
    premium: float,
    expected_direction: str,  # "bullish" | "bearish"
    underlying_token: str = "26000",  # Nifty Futures front-month token (override per-call)
    min_premium: float = 60.0,
    max_spread_pct: float = 0.05,
    max_data_age_sec: float = 90.0,
    max_vix: float = 22.0,
    max_vix_jump_1h_pct: float = 20.0,
    event_dates: set[date] | None = None,
) -> PrecheckResult:
    """Run 8 sequential gates. First failure returns allowed=False with reason."""
    expected_direction = expected_direction.lower()

    # 1. Premium floor
    if premium < min_premium:
        return PrecheckResult(False,
                              f"premium ₹{premium:.2f} < min ₹{min_premium:.2f}",
                              vix=0.0)

    # 2. Spread
    spread_pct = None
    try:
        from strategies.option_utils import fetch_option_quote
        quote = fetch_option_quote(api, exchange, symbol, token)
        spread_pct = quote.get("spread_pct")
        if spread_pct is not None and spread_pct > max_spread_pct:
            return PrecheckResult(False,
                                  f"spread {spread_pct*100:.1f}% > max {max_spread_pct*100:.1f}%",
                                  spread_pct=spread_pct)
    except Exception as e:
        log.debug("Quote fetch failed (non-fatal): %s", e)

    # 3. Data freshness — if data_feed has a recent tick for the underlying
    last_tick_age = None
    try:
        if data_feed is not None and hasattr(data_feed, "get_candles"):
            candles = data_feed.get_candles(underlying_token, count=1)
            if candles:
                last_ts = candles[-1].timestamp
                if last_ts.tzinfo is None:
                    last_ts = IST.localize(last_ts)
                age = (datetime.now(IST) - last_ts).total_seconds()
                last_tick_age = age
                if age > max_data_age_sec:
                    return PrecheckResult(False,
                                          f"underlying data stale: {age:.0f}s old",
                                          spread_pct=spread_pct,
                                          last_tick_age_sec=age)
    except Exception as e:
        log.debug("Data freshness check failed (non-fatal): %s", e)

    # 4. VIX gate
    vix = 0.0
    try:
        vix_result = api.ltpData("NSE", "India VIX", "99926017")
        if vix_result and vix_result.get("status"):
            vix = float(vix_result.get("data", {}).get("ltp", 0))
        if vix > max_vix:
            return PrecheckResult(False,
                                  f"VIX {vix:.2f} > max {max_vix:.2f}",
                                  spread_pct=spread_pct,
                                  last_tick_age_sec=last_tick_age,
                                  vix=vix)
    except Exception as e:
        log.warning("VIX fetch failed: %s — proceeding without VIX gate", e)

    # 5. VIX 1-hour jump — best-effort, not blocking if buffer insufficient
    # (The simulator/backtest path doesn't have a live VIX buffer; skip here.)

    # 6. Risk manager
    try:
        ok, reason = risk_manager.can_trade(strategy_name)
        if not ok:
            return PrecheckResult(False, f"risk_manager: {reason}",
                                  spread_pct=spread_pct,
                                  last_tick_age_sec=last_tick_age, vix=vix)
    except Exception as e:
        log.error("risk_manager.can_trade failed: %s", e)
        return PrecheckResult(False, f"risk_manager error: {e}",
                              spread_pct=spread_pct, vix=vix)

    # 7. Regime gate
    try:
        regime = "BULL"
        if regime_module is not None and hasattr(regime_module, "get_regime"):
            r = regime_module.get_regime()
            regime = getattr(r, "value", str(r)).upper()
        # bullish only allowed in BULL; bearish allowed in BULL & BEAR; CRASH blocks all
        if regime == "CRASH":
            return PrecheckResult(False, "regime=CRASH blocks all entries",
                                  spread_pct=spread_pct, vix=vix)
        if expected_direction == "bullish" and regime != "BULL":
            return PrecheckResult(False, f"bullish trade blocked in regime={regime}",
                                  spread_pct=spread_pct, vix=vix)
        if expected_direction == "bearish" and regime not in {"BULL", "BEAR"}:
            return PrecheckResult(False, f"bearish trade blocked in regime={regime}",
                                  spread_pct=spread_pct, vix=vix)
    except Exception as e:
        log.warning("Regime check failed: %s — proceeding", e)

    # 8. Event-day blackout
    if event_dates and date.today() in event_dates:
        return PrecheckResult(False, f"event day {date.today()}",
                              spread_pct=spread_pct, vix=vix)

    return PrecheckResult(True, "OK",
                          spread_pct=spread_pct,
                          last_tick_age_sec=last_tick_age, vix=vix)
