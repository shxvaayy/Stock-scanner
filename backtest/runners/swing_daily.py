"""Daily-bar swing runners.

run_rsi2_swing — Connors RSI-2 pullback system (the documented variant with
published evidence on equities): long-only pullbacks within an uptrend, daily
bars, multi-day holds, no tight stop. Replaces the intraday RSI scalp whose
12-month backtest showed fees consuming any edge.

run_rsi_predictor_universe — the W-bottom failed-swing scan run across the
full equity universe (instead of Nifty alone), trading the STOCK intraday the
next day. Long-only, BULL regime + above 200-DMA names.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from src.fees import calculate_delivery_fees, calculate_equity_fees, estimate_equity_slippage
from strategies.rsi_predictor import classify_eod_pattern
from backtest.records import TradeRecord
from backtest import data_store

POSITION_RS = 83_000
MAX_CONCURRENT = 5


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def run_rsi2_swing(start: date, end: date, symbols: list[str],
                   entry_rsi: float = 10.0, exit_rsi: float = 65.0,
                   exit_on_sma5: bool = True, max_hold: int = 10,
                   limit_dip_pct: float = 0.0) -> list[TradeRecord]:
    """Connors RSI-2: close > SMA200, RSI(2) < entry_rsi → buy at close.
    Exit at close when RSI(2) > exit_rsi or close > SMA5; max_hold sessions.
    No tight stop (doctrine: stops hurt mean reversion).

    limit_dip_pct > 0 switches to the Alvarez limit-entry variant: the signal
    arms at the close, and the buy is a NEXT-session limit order at
    signal_close x (1 - limit_dip_pct/100), filled only if the next session's
    low reaches it. Deeper entries, fewer fills, more edge per trade."""
    # Pre-compute per-symbol tables
    tables: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        ddf = data_store.load_equity_daily(sym)
        if ddf is None or len(ddf) < 210:
            continue
        ddf = ddf.copy()
        ddf["d"] = pd.to_datetime(ddf["timestamp"]).dt.date
        ddf["sma200"] = ddf["close"].rolling(200).mean()
        ddf["sma5"] = ddf["close"].rolling(5).mean()
        ddf["rsi2"] = _rsi(ddf["close"], 2)
        tables[sym] = ddf.reset_index(drop=True)

    # Collect all trading days in range from the union of tables
    all_days = sorted({d for t in tables.values() for d in t["d"] if start <= d <= end})

    open_pos: dict[str, dict] = {}
    pending_limits: dict[str, dict] = {}  # symbol -> {limit, armed_on}
    records: list[TradeRecord] = []

    for day in all_days:
        # limit fills first (Alvarez variant): one session lifetime
        for sym in list(pending_limits):
            order = pending_limits.pop(sym)
            if sym in open_pos or len(open_pos) >= MAX_CONCURRENT:
                continue
            t = tables[sym]
            row = t[t["d"] == day]
            if row.empty:
                continue
            row = row.iloc[0]
            if float(row["low"]) <= order["limit"]:
                px = order["limit"]
                qty = int(POSITION_RS / px)
                if qty > 0:
                    open_pos[sym] = {"entry": px, "qty": qty, "held": 0,
                                     "entry_date": day}

        # exits
        for sym in list(open_pos):
            t = tables[sym]
            row = t[t["d"] == day]
            if row.empty:
                continue
            row = row.iloc[0]
            pos = open_pos[sym]
            pos["held"] += 1
            do_exit = (
                (row["rsi2"] == row["rsi2"] and row["rsi2"] > exit_rsi)
                or (exit_on_sma5 and row["sma5"] == row["sma5"] and row["close"] > row["sma5"])
                or pos["held"] >= max_hold
            )
            if do_exit:
                exit_p = float(row["close"])
                qty = pos["qty"]
                gross = (exit_p - pos["entry"]) * qty
                fees = (calculate_delivery_fees(pos["entry"], qty, "BUY")["total"]
                        + calculate_delivery_fees(exit_p, qty, "SELL")["total"]
                        + estimate_equity_slippage(pos["entry"], qty)
                        + estimate_equity_slippage(exit_p, qty))
                reason = ("RSI_EXIT" if row["rsi2"] > exit_rsi
                          else ("SMA5_EXIT" if exit_on_sma5 and row["close"] > row["sma5"]
                                else "MAX_HOLD"))
                records.append(TradeRecord(
                    strategy="rsi2_swing", date=str(day), direction="bullish",
                    entry_time=str(pos["entry_date"]), exit_time=str(day),
                    entry_underlying=pos["entry"], exit_underlying=exit_p,
                    entry_premium=pos["entry"], exit_premium=exit_p, qty=qty,
                    gross_pnl=round(gross, 2), fees=round(fees, 2),
                    net_pnl=round(gross - fees, 2), reason=reason,
                    setup=f"rsi2<{entry_rsi:g}", symbol=sym, instrument="equity",
                ))
                del open_pos[sym]

        # entries at close
        if len(open_pos) >= MAX_CONCURRENT:
            continue
        candidates = []
        for sym, t in tables.items():
            if sym in open_pos:
                continue
            row = t[t["d"] == day]
            if row.empty:
                continue
            row = row.iloc[0]
            if (row["sma200"] == row["sma200"] and row["close"] > row["sma200"]
                    and row["rsi2"] == row["rsi2"] and row["rsi2"] < entry_rsi):
                candidates.append((row["rsi2"], sym, float(row["close"])))
        # most oversold first
        for rsi2_val, sym, px in sorted(candidates):
            if limit_dip_pct > 0:
                pending_limits[sym] = {"limit": px * (1 - limit_dip_pct / 100),
                                       "armed_on": day}
                continue
            if len(open_pos) >= MAX_CONCURRENT:
                break
            qty = int(POSITION_RS / px)
            if qty <= 0:
                continue
            open_pos[sym] = {"entry": px, "qty": qty, "held": 0, "entry_date": day}

    return records


def run_rsi_predictor_universe(start: date, end: date, symbols: list[str],
                               market_regimes: dict) -> list[TradeRecord]:
    """W-bottom failed-swing scan per stock at EOD; next session, buy the
    stock on the intraday trigger (15-min RSI(5) cross above 40 + above VWAP)
    and exit at VWAP-extension / RSI extreme / 14:45. Long-only, BULL regime,
    stock above its 200-DMA."""
    tables: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        ddf = data_store.load_equity_daily(sym)
        if ddf is None or len(ddf) < 210:
            continue
        ddf = ddf.copy()
        ddf["d"] = pd.to_datetime(ddf["timestamp"]).dt.date
        ddf["sma200"] = ddf["close"].rolling(200).mean()
        tables[sym] = ddf.reset_index(drop=True)

    all_days = sorted({d for t in tables.values() for d in t["d"] if start <= d <= end})
    records: list[TradeRecord] = []
    pending: dict[str, str] = {}  # symbol -> signal for next session

    for day in all_days:
        # 1. trade today's pending signals
        for sym, signal in list(pending.items()):
            if signal != "BULLISH_W":
                continue
            df = data_store.load_equity_day(sym, day)
            if df is None or len(df) < 60:
                continue
            df = df.copy()
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            tp = (df["high"] + df["low"] + df["close"]) / 3
            df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, 1)
            df_15 = df.set_index("timestamp").resample("15min").agg(
                {"open": "first", "high": "max", "low": "min",
                 "close": "last", "volume": "sum"}).dropna().reset_index()
            df_15["rsi5"] = _rsi(df_15["close"], 5)

            entry = None
            for j in range(2, len(df_15)):
                bar = df_15.iloc[j]
                prev = df_15.iloc[j - 1]
                t_ = bar["timestamp"].time()
                if t_ < pd.Timestamp("09:45").time() or t_ > pd.Timestamp("11:30").time():
                    continue
                # 1-min row at this bar end for VWAP check
                mrows = df[df["timestamp"] <= bar["timestamp"] + pd.Timedelta(minutes=14)]
                if mrows.empty:
                    continue
                vwap_now = mrows["vwap"].iloc[-1]
                if (prev["rsi5"] == prev["rsi5"] and bar["rsi5"] == bar["rsi5"]
                        and prev["rsi5"] < 40 <= bar["rsi5"] and bar["close"] > vwap_now):
                    entry = {"px": float(bar["close"]), "ts": bar["timestamp"]}
                    break
            if entry is None:
                continue
            qty = int(POSITION_RS / entry["px"])
            if qty <= 0:
                continue
            # manage: exit on 15m RSI(5) > 70, RSI cross back < 40 (failure),
            # or 14:45 time stop
            exit_px, exit_ts, reason = None, None, "TIME_1445"
            after = df_15[df_15["timestamp"] > entry["ts"]]
            for _, bar in after.iterrows():
                if bar["timestamp"].time() >= pd.Timestamp("14:45").time():
                    exit_px, exit_ts, reason = float(bar["close"]), bar["timestamp"], "TIME_1445"
                    break
                if bar["rsi5"] == bar["rsi5"] and bar["rsi5"] >= 70:
                    exit_px, exit_ts, reason = float(bar["close"]), bar["timestamp"], "RSI_EXTREME"
                    break
                if bar["rsi5"] == bar["rsi5"] and bar["rsi5"] < 40:
                    exit_px, exit_ts, reason = float(bar["close"]), bar["timestamp"], "SIGNAL_FAIL"
                    break
            if exit_px is None:
                exit_px = float(df["close"].iloc[-1])
                exit_ts = df["timestamp"].iloc[-1]
            gross = (exit_px - entry["px"]) * qty
            fees = (calculate_equity_fees(entry["px"], qty, "BUY")["total"]
                    + calculate_equity_fees(exit_px, qty, "SELL")["total"]
                    + estimate_equity_slippage(entry["px"], qty)
                    + estimate_equity_slippage(exit_px, qty))
            records.append(TradeRecord(
                strategy="rsi_predictor", date=str(day), direction="bullish",
                entry_time=str(entry["ts"].time())[:5], exit_time=str(exit_ts.time())[:5],
                entry_underlying=entry["px"], exit_underlying=exit_px,
                entry_premium=entry["px"], exit_premium=exit_px, qty=qty,
                gross_pnl=round(gross, 2), fees=round(fees, 2),
                net_pnl=round(gross - fees, 2), reason=reason,
                setup="W_BOTTOM", symbol=sym, instrument="equity",
            ))
        pending.clear()

        # 2. EOD scan for tomorrow
        regime = market_regimes.get(day)
        regime_name = getattr(regime[0], "name", "BULL") if regime else "BULL"
        if regime_name != "BULL":
            continue
        for sym, t in tables.items():
            hist = t[t["d"] <= day]
            if len(hist) < 210:
                continue
            last = hist.iloc[-1]
            if not (last["sma200"] == last["sma200"] and last["close"] > last["sma200"]):
                continue
            closes = hist["close"].tolist()
            if classify_eod_pattern(closes, regime="BULL") == "BULLISH_W":
                pending[sym] = "BULLISH_W"

    return records
