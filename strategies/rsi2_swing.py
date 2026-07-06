"""Connors RSI-2 daily swing with Alvarez limit-dip entry.

THE strategy that passed the 12-month train/test validation (Jun 2025 -
Jun 2026): train +Rs 8,503 (86 trades, 55% WR), held-out test +Rs 24,720
(33 trades, 67% WR), fees ~26% of gross.

Rules (documented variants — Connors RSI-2 + Alvarez limit entry):
  Setup (EOD scan, ~15:20):  close > SMA200(daily)  AND  RSI(2) < 10
  Entry (next session):      limit order at signal_close x 0.98 — fills only
                             on a further dip; order lives one session
  Exit (at close):           RSI(2) > 65  OR  close > SMA5  OR  held 10 days
  Stop:                      none (doctrine: tight stops demonstrably hurt
                             mean reversion); risk is bounded by position
                             size and the 200-DMA uptrend gate
  Sizing:                    Rs 83,000 per position, max 5 concurrent,
                             most-oversold candidates first

Holds overnight (CNC delivery — fees include 0.1% STT both sides).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytz

from config.universe import STOCKS, build_token_map
from models.types import Candle, Signal, SignalType
from strategies.base import BaseStrategy, StrategyEngine

log = logging.getLogger("autotheta.rsi2_swing")
trade_log = logging.getLogger("autotheta.trades")
IST = pytz.timezone("Asia/Kolkata")

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_PATH = ROOT / "data" / "signals" / "rsi2_swing_orders.json"


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def scan_eod(daily_by_symbol: dict[str, pd.DataFrame],
             entry_rsi: float = 10.0,
             limit_dip_pct: float = 2.0,
             max_candidates: int = 5) -> list[dict]:
    """Pure EOD scan over per-symbol daily DataFrames
    [timestamp, open, high, low, close, volume].

    Returns next-session limit orders, most-oversold first.
    """
    candidates = []
    for sym, ddf in daily_by_symbol.items():
        if ddf is None or len(ddf) < 210:
            continue
        closes = pd.to_numeric(ddf["close"], errors="coerce")
        sma200 = closes.rolling(200).mean().iloc[-1]
        r2 = rsi(closes, 2).iloc[-1]
        last_close = closes.iloc[-1]
        if (sma200 == sma200 and last_close > sma200
                and r2 == r2 and r2 < entry_rsi):
            candidates.append({
                "symbol": sym,
                "rsi2": round(float(r2), 2),
                "signal_close": float(last_close),
                "limit_price": round(last_close * (1 - limit_dip_pct / 100), 2),
            })
    candidates.sort(key=lambda c: c["rsi2"])
    return candidates[:max_candidates]


def check_exit(ddf: pd.DataFrame, held_sessions: int,
               exit_rsi: float = 65.0, max_hold: int = 10) -> str | None:
    """Exit decision on the latest daily bar. Returns reason or None."""
    closes = pd.to_numeric(ddf["close"], errors="coerce")
    r2 = rsi(closes, 2).iloc[-1]
    sma5 = closes.rolling(5).mean().iloc[-1]
    if r2 == r2 and r2 > exit_rsi:
        return "RSI_EXIT"
    if sma5 == sma5 and closes.iloc[-1] > sma5:
        return "SMA5_EXIT"
    if held_sessions >= max_hold:
        return "MAX_HOLD"
    return None


@StrategyEngine.register("rsi2_swing")
class RSI2SwingStrategy(BaseStrategy):
    """Live wrapper. Time-triggered like expiry_skew, not candle-driven:

    - scheduler calls run_eod_scan() at ~15:20 → writes limit orders JSON
    - scheduler calls place_morning_orders() at 09:15 next session
    - scheduler calls manage_exits() at ~15:20 (before the scan)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.entry_rsi = self.params.get("entry_rsi", 10)
        self.exit_rsi = self.params.get("exit_rsi", 65)
        self.limit_dip_pct = self.params.get("limit_dip_pct", 2.0)
        self.max_hold = self.params.get("max_hold_sessions", 10)
        self.max_positions = self.params.get("max_positions", 5)
        self.position_rs = self.params.get("position_rs", 83000)
        self.api = None          # set by engine
        self.journal = None
        self.risk_manager = None
        self._token_map: dict[str, str] = {}

    async def initialize(self):
        self._token_map = build_token_map(symbols=STOCKS)
        log.info("RSI2 swing initialized: %d symbols", len(self._token_map))

    async def on_candle(self, token: str, candle: Candle) -> Signal | None:
        return None  # time-triggered, not candle-driven

    async def on_tick(self, token: str, price: float) -> Signal | None:
        return None

    def _fetch_daily(self, sym: str, lookback_days: int = 320) -> pd.DataFrame | None:
        token = self._token_map.get(sym)
        if not token:
            return None
        try:
            frm = (datetime.now(IST) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d 09:15")
            to = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
            res = self.api.getCandleData({
                "exchange": "NSE", "symboltoken": token,
                "interval": "ONE_DAY", "fromdate": frm, "todate": to,
            })
            data = (res or {}).get("data") or []
            if len(data) < 210:
                return None
            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df
        except Exception:
            log.exception("daily fetch failed for %s", sym)
            return None

    def run_eod_scan(self) -> list[dict]:
        """~15:20 IST: scan and persist tomorrow's limit orders."""
        daily = {}
        for sym in self._token_map:
            ddf = self._fetch_daily(sym)
            if ddf is not None:
                daily[sym] = ddf
        orders = scan_eod(daily, self.entry_rsi, self.limit_dip_pct,
                          max_candidates=self.max_positions)
        SIGNALS_PATH.parent.mkdir(exist_ok=True)
        with open(SIGNALS_PATH, "w") as f:
            json.dump({"scan_date": str(date.today()), "orders": orders}, f, indent=2)
        log.info("RSI2 EOD scan: %d candidates %s", len(orders),
                 [o["symbol"] for o in orders])
        return orders

    def load_pending_orders(self) -> list[dict]:
        if not SIGNALS_PATH.exists():
            return []
        with open(SIGNALS_PATH) as f:
            payload = json.load(f)
        return payload.get("orders", [])

    async def teardown(self):
        pass
