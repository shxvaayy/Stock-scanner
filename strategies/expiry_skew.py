"""OTM Premium Skew Strategy (Iron Condor) — Plugin version.

Runs on Nifty expiry days (Tuesday) at 2 PM IST.
Compares OTM Put vs Call premiums, enters iron condor if skew >= 2:1.
Monitors with SL at 2x premium, hard exit at 3:15 PM.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime

import pytz

from core.data_feed import DataFeed
from core.risk_manager import GlobalRiskManager
from core.trade_journal import TradeJournal
from models.types import Candle, Signal, SignalType
from src.expiry import is_nifty_expiry_day
from src.fees import calculate_fees
from src.instruments import InstrumentMaster
from strategies.base import BaseStrategy, StrategyEngine

log = logging.getLogger("autotheta.expiry_skew")
trade_log = logging.getLogger("autotheta.trades")
IST = pytz.timezone("Asia/Kolkata")

NIFTY_SPOT_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926017"  # Correct VIX token (99926004 is Nifty 500)


@dataclass
class IronCondorState:
    """Tracks the legs of an active condor / rich-side vertical."""
    sell_put: dict  # {'symbol', 'token', 'strike', 'premium', 'trade_id'}
    sell_call: dict
    buy_put: dict
    buy_call: dict
    sl_put: float
    sl_call: float
    net_credit: float
    put_closed: bool = False
    call_closed: bool = False
    placed_legs: frozenset = frozenset({"sell_put", "sell_call", "buy_put", "buy_call"})


@StrategyEngine.register("expiry_skew")
class ExpirySkewStrategy(BaseStrategy):
    """OTM premium skew iron condor on Nifty expiry days."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.otm_offset = self.params.get("otm_offset", 50)
        self.wing_width = self.params.get("wing_width", 100)
        self.min_skew_ratio = self.params.get("min_skew_ratio", 2.0)
        self.sl_multiplier = self.params.get("sl_multiplier", 2.0)
        # Research-backed restructure (0DTE evidence: credit spreads are the
        # only structure with positive medians; symmetric condors get the
        # lean-side strike run over). "rich_side_vertical" sells only the
        # expensive side. Gates skip trending afternoons (gamma death).
        self.structure = self.params.get("structure", "rich_side_vertical")
        self.max_range_pct_by_entry = self.params.get("max_range_pct_by_entry", 0.8)
        self.max_vwap_dist_pct = self.params.get("max_vwap_dist_pct", 0.3)
        self.entry_hour = self.params.get("entry_hour", 14)
        self.entry_minute = self.params.get("entry_minute", 0)
        self.exit_hour = self.params.get("exit_hour", 15)
        self.exit_minute = self.params.get("exit_minute", 15)
        self.monitor_interval = self.params.get("monitor_interval", 30)
        self.lot_size = self.params.get("lot_size", 65)
        self.vix_min = self.params.get("vix_min", 12)
        self.vix_max = self.params.get("vix_max", 18)

        # External deps (set by engine)
        self.instruments: InstrumentMaster | None = None
        self.risk_manager: GlobalRiskManager | None = None
        self.journal: TradeJournal | None = None
        self.api = None  # SmartConnect instance for LTP
        self.broker = None

        # State
        self._condor: IronCondorState | None = None
        self._executed_today: bool = False

    async def initialize(self):
        log.info("Expiry Skew strategy initialized")

    async def on_candle(self, token: str, candle: Candle) -> Signal | None:
        """Not candle-driven — this strategy is time-triggered."""
        return None

    async def on_tick(self, token: str, price: float) -> Signal | None:
        """Not tick-driven."""
        return None

    async def execute(self) -> bool:
        """Main execution — called by the scheduler at 2 PM on expiry days.

        Returns True if a trade was taken.
        """
        if not is_nifty_expiry_day():
            log.info("Not an expiry day — skipping")
            return False

        if self._executed_today:
            log.info("Already executed today")
            return False

        log.info("=" * 50)
        log.info("Expiry Skew — executing iron condor strategy")

        # 1. VIX check
        try:
            vix = self._fetch_ltp("NSE", "India VIX", INDIA_VIX_TOKEN)
            if vix < self.vix_min or vix > self.vix_max:
                log.warning("VIX %.1f outside [%d, %d] range — skipping", vix, self.vix_min, self.vix_max)
                return False
        except Exception:
            log.warning("VIX fetch failed — proceeding cautiously")

        # 2. Risk check
        ok, reason = self.risk_manager.can_trade(self.name)
        if not ok:
            log.info("Risk blocked: %s", reason)
            return False

        # 2b. Trend-day gates: selling premium into a directional afternoon
        # is how 0DTE sellers die. Skip if the session has already ranged
        # > max_range_pct_by_entry or price is far from session VWAP.
        gate_reason = self._session_gates()
        if gate_reason:
            log.info("Trend-day gate: %s — skipping", gate_reason)
            return False

        # 3. Spot price and strikes
        spot = self._fetch_ltp("NSE", "NIFTY", NIFTY_SPOT_TOKEN)
        atm = round(spot / 50) * 50
        sell_put_strike = atm - self.otm_offset
        sell_call_strike = atm + self.otm_offset
        buy_put_strike = sell_put_strike - self.wing_width
        buy_call_strike = sell_call_strike + self.wing_width

        log.info("Spot: %.2f | ATM: %d | Sells: %dPE/%dCE | Buys: %dPE/%dCE",
                 spot, atm, sell_put_strike, sell_call_strike, buy_put_strike, buy_call_strike)

        # 4. Look up instruments
        expiry = self.instruments.get_nearest_expiry()
        if not expiry:
            log.error("No expiry found")
            return False

        legs = {}
        for label, strike, opt_type in [
            ("sell_put", sell_put_strike, "PE"),
            ("sell_call", sell_call_strike, "CE"),
            ("buy_put", buy_put_strike, "PE"),
            ("buy_call", buy_call_strike, "CE"),
        ]:
            info = self.instruments.lookup(strike, opt_type, expiry)
            if not info:
                log.error("Instrument not found: %s %d%s", label, strike, opt_type)
                return False
            premium = self._fetch_ltp("NFO", info["symbol"], info["token"])
            legs[label] = {**info, "strike": strike, "premium": premium}

        # 5. Skew check
        sp = legs["sell_put"]["premium"]
        sc = legs["sell_call"]["premium"]
        denom = max(min(sp, sc), 0.05)
        ratio = max(sp, sc) / denom
        if ratio < self.min_skew_ratio:
            log.info("Skew ratio %.1f < %.1f threshold — no trade", ratio, self.min_skew_ratio)
            return False

        net_credit = (sp + sc) - (legs["buy_put"]["premium"] + legs["buy_call"]["premium"])
        log.info("Premiums: SellPut=%.2f SellCall=%.2f BuyPut=%.2f BuyCall=%.2f Net=%.2f",
                 sp, sc, legs["buy_put"]["premium"], legs["buy_call"]["premium"], net_credit)

        # 6. Place legs. rich_side_vertical sells ONLY the expensive side
        # (credit spread): half the gamma exposure, half the brokerage, and
        # the structure the 0DTE evidence favors. The cheap side's legs are
        # marked closed so the monitor ignores them.
        if self.structure == "rich_side_vertical":
            rich_is_put = sp >= sc
            if rich_is_put:
                leg_plan = [("sell_put", "SELL"), ("buy_put", "BUY")]
                net_credit = sp - legs["buy_put"]["premium"]
            else:
                leg_plan = [("sell_call", "SELL"), ("buy_call", "BUY")]
                net_credit = sc - legs["buy_call"]["premium"]
            log.info("Structure: rich-side vertical (%s) | net credit %.2f",
                     "PUT" if rich_is_put else "CALL", net_credit)
        else:
            leg_plan = [("sell_put", "SELL"), ("sell_call", "SELL"),
                        ("buy_put", "BUY"), ("buy_call", "BUY")]

        for label, side in leg_plan:
            leg = legs[label]
            tid = TradeJournal.generate_trade_id("IC")
            entry_fees = calculate_fees(leg["premium"], self.lot_size, side)["total"]
            self.journal.record_entry(
                tid, self.name, leg["symbol"], leg["token"], side,
                self.lot_size, leg["premium"],
                fees=entry_fees,
                indicators=f'{{"strike": {leg["strike"]}, "leg": "{label}"}}',
            )
            leg["trade_id"] = tid
            log.info("Placed %s %s %s @ ₹%.2f (fees=₹%.2f)",
                     side, label, leg["symbol"], leg["premium"], entry_fees)

        # 7. Set up monitoring state. For the vertical, the unplaced side is
        # marked closed so _monitor_condor / _close_remaining skip it.
        placed = {label for label, _ in leg_plan}
        self._condor = IronCondorState(
            sell_put=legs["sell_put"], sell_call=legs["sell_call"],
            buy_put=legs["buy_put"], buy_call=legs["buy_call"],
            sl_put=sp * self.sl_multiplier,
            sl_call=sc * self.sl_multiplier,
            net_credit=net_credit,
            put_closed="sell_put" not in placed,
            call_closed="sell_call" not in placed,
            placed_legs=frozenset(placed),
        )

        trade_log.info("IC ENTRY | Spot=%.2f ATM=%d | SP=%d@%.2f SC=%d@%.2f | Net=%.2f/unit",
                       spot, atm, sell_put_strike, sp, sell_call_strike, sc, net_credit)

        # 8. Monitor until exit time
        self._monitor_condor()

        self._executed_today = True
        return True

    def _monitor_condor(self):
        """Blocking monitor loop — checks SL and hard exit time."""
        if not self._condor:
            return

        log.info("Monitoring: SL Put=%.2f | SL Call=%.2f | Exit at %02d:%02d",
                 self._condor.sl_put, self._condor.sl_call, self.exit_hour, self.exit_minute)

        while True:
            now = datetime.now(IST)
            if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                log.info("Hard exit time reached")
                break

            time.sleep(self.monitor_interval)

            # Check sell put SL
            if not self._condor.put_closed:
                try:
                    curr = self._fetch_ltp("NFO", self._condor.sell_put["symbol"],
                                           self._condor.sell_put["token"])
                    if curr >= self._condor.sl_put:
                        # Closing a SOLD put = BUY transaction → BUY-side fees
                        exit_fees = calculate_fees(curr, self.lot_size, "BUY")["total"]
                        pnl = self.journal.record_exit(
                            self._condor.sell_put["trade_id"], curr,
                            self.lot_size, "stop_loss", fees=exit_fees,
                        )
                        log.warning("SL HIT PUT @ ₹%.2f P&L=₹%.2f (fees=₹%.2f)",
                                    curr, pnl, exit_fees)
                        self._condor.put_closed = True
                except Exception:
                    log.exception("Error checking put SL")

            # Check sell call SL
            if not self._condor.call_closed:
                try:
                    curr = self._fetch_ltp("NFO", self._condor.sell_call["symbol"],
                                           self._condor.sell_call["token"])
                    if curr >= self._condor.sl_call:
                        # Closing a SOLD call = BUY transaction → BUY-side fees
                        exit_fees = calculate_fees(curr, self.lot_size, "BUY")["total"]
                        pnl = self.journal.record_exit(
                            self._condor.sell_call["trade_id"], curr,
                            self.lot_size, "stop_loss", fees=exit_fees,
                        )
                        log.warning("SL HIT CALL @ ₹%.2f P&L=₹%.2f (fees=₹%.2f)",
                                    curr, pnl, exit_fees)
                        self._condor.call_closed = True
                except Exception:
                    log.exception("Error checking call SL")

            if self._condor.put_closed and self._condor.call_closed:
                break

        # Hard exit: close remaining legs
        self._close_remaining()

    def _close_remaining(self):
        """Close all remaining open legs."""
        if not self._condor:
            return

        legs_to_close = []
        if not self._condor.put_closed and "sell_put" in self._condor.placed_legs:
            legs_to_close.append(("sell_put", self._condor.sell_put))
        if not self._condor.call_closed and "sell_call" in self._condor.placed_legs:
            legs_to_close.append(("sell_call", self._condor.sell_call))
        if "buy_put" in self._condor.placed_legs:
            legs_to_close.append(("buy_put", self._condor.buy_put))
        if "buy_call" in self._condor.placed_legs:
            legs_to_close.append(("buy_call", self._condor.buy_call))

        total_pnl = 0.0
        for label, leg in legs_to_close:
            try:
                curr = self._fetch_ltp("NFO", leg["symbol"], leg["token"])
                # Sold legs (sell_put, sell_call) close as BUY; bought legs close as SELL
                exit_side = "BUY" if label.startswith("sell_") else "SELL"
                exit_fees = calculate_fees(curr, self.lot_size, exit_side)["total"]
                pnl = self.journal.record_exit(
                    leg["trade_id"], curr, self.lot_size, "hard_exit",
                    fees=exit_fees,
                )
                total_pnl += pnl
                log.info("EXIT %s %s @ ₹%.2f P&L=₹%.2f (fees=₹%.2f)",
                         label, leg["symbol"], curr, pnl, exit_fees)
            except Exception:
                log.exception("Failed to close %s", label)

        self.risk_manager.record_trade_result(self.name, total_pnl)
        trade_log.info("IC EXIT | Total P&L=₹%.2f", total_pnl)
        self._condor = None

    def _session_gates(self) -> str | None:
        """Trend-day filters from today's 1-min Nifty candles.
        Returns a skip reason or None (trade allowed)."""
        try:
            today = datetime.now(IST).strftime("%Y-%m-%d")
            res = self.api.getCandleData({
                "exchange": "NSE", "symboltoken": NIFTY_SPOT_TOKEN,
                "interval": "ONE_MINUTE",
                "fromdate": f"{today} 09:15", "todate": f"{today} 14:00",
            })
            rows = (res or {}).get("data") or []
            if len(rows) < 60:
                return None  # not enough data to judge — allow
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            closes = [float(r[4]) for r in rows]
            spot = closes[-1]
            day_range_pct = (max(highs) - min(lows)) / spot * 100
            if day_range_pct > self.max_range_pct_by_entry:
                return f"range {day_range_pct:.2f}% > {self.max_range_pct_by_entry}%"
            # index candles have no volume — plain TP average as VWAP proxy
            tps = [(float(r[2]) + float(r[3]) + float(r[4])) / 3 for r in rows]
            vwap_proxy = sum(tps) / len(tps)
            vwap_dist_pct = abs(spot - vwap_proxy) / spot * 100
            if vwap_dist_pct > self.max_vwap_dist_pct:
                return f"vwap dist {vwap_dist_pct:.2f}% > {self.max_vwap_dist_pct}%"
        except Exception:
            log.exception("session gate check failed — allowing trade")
        return None

    def _fetch_ltp(self, exchange: str, symbol: str, token: str) -> float:
        """Fetch LTP via REST API."""
        result = self.api.ltpData(exchange, symbol, token)
        if not result or not result.get("status"):
            raise RuntimeError(f"LTP failed: {symbol}")
        return float(result["data"]["ltp"])

    async def teardown(self):
        if self._condor:
            log.warning("Teardown: closing remaining condor legs")
            self._close_remaining()
