# AutoTheta — Nifty Options Trading Bot

AutoTheta is a paper-trading bot that trades Nifty 50 stocks and Nifty index options on the NSE through the Angel One broker API. "Paper trading" means it runs against real market data and real prices, but no actual money moves — it's a full simulation to test and validate strategies before going live.

The bot runs three independent strategies simultaneously during market hours, each hunting for a different type of edge. Every strategy follows strict risk rules, and the system shuts itself down if it hits loss limits.

---

## How the Bot Reads the Market

Before placing any trade, the bot checks what kind of market day it is. This is called **regime detection** and it happens once at 9:15 AM.

The bot classifies each day into one of three regimes:

**BULL** — Normal healthy market. Price is near its long-term average, volatility is moderate. This is when the bot trades most aggressively.

**BEAR** — Market is trending down. Two or more of these conditions are true:
- Nifty is more than 3% below its 200-day average
- India VIX (the fear index) is above 16
- Downward momentum is stronger than upward momentum (ADX with Minus DI dominating)

In BEAR regime, the bot switches to bearish setups or reduces size.

**CRASH** — Any one of these triggers a crash alert:
- India VIX is above 22 (extreme fear)
- Nifty is more than 8% below its 200-day average
- Market opened with a gap down of more than 3%

In CRASH regime, no new positions are opened. Existing ones are exited cleanly.

---

## Risk Rules (Apply to All Strategies)

The bot never bets more than it can afford to lose. These rules are hard-coded and cannot be overridden by any strategy:

- **Max risk per trade:** 1% of the strategy's allocated capital (usually ₹2,500 on ₹2.5L)
- **Per-strategy daily loss cap:** ₹7,500 — if one strategy loses this much, it stops for the day
- **Global daily loss cap:** ₹15,000 across all strategies combined
- **VIX-based sizing:**
  - VIX below 18 → full position size
  - VIX between 18–22 → half position size
  - VIX above 22 → no new trades at all
- **Cooldown rule:** After 3 consecutive losses, a strategy pauses for 30 minutes before trying again
- **Blackout days:** Known high-risk events (RBI MPC meetings, Union Budget day, US Fed decisions) are pre-programmed as no-trade days

---

## Strategy 1 — RSI Bounce

**What it does:** Catches quick recovery bounces in individual Nifty 50 stocks after a sharp dip.

**The idea behind it:** Stocks in an uptrend occasionally get oversold — someone panic-sells, the price drops fast, and then it snaps back. The RSI (Relative Strength Index) measures how oversold a stock is on a 0–100 scale. Below 20 on a 5-minute chart is genuinely panicked. The bot buys into that panic and sells into the recovery.

**How it finds setups:**

1. Watches 5-minute candles for all Nifty 50 stocks
2. Waits for RSI(5) to drop below 20 (deeply oversold)
3. Confirms RSI is starting to tick back up (not still falling)
4. Checks all these filters — every single one must pass:
   - Price is above its 20-period EMA (still in an uptrend context)
   - Price is at or above VWAP (the stock hasn't broken its "fair value" for the day)
   - Today's volume is at least 1.5x the average (real selling, not noise)
   - ADX (trend strength) is above 20 (market has momentum, not just drifting)
   - It's past 10:15 AM (avoids the chaotic first hour)
   - No more than 1 position open in the same sector (diversification guard)

**Exits:**
- Sell 50% when RSI crosses above 50 (half-recovered)
- Sell the remaining 50% when RSI crosses above 50 again (full mean reversion)
- Stop loss at 1.5× ATR below entry (ATR = average daily range, so this is a volatility-adjusted stop)
- Time stop: if RSI doesn't recover within 15 candles (~75 minutes), exit anyway

**Active hours:** 10:15 AM – 2:00 PM IST

**Capital allocated:** ₹2.5 lakh | Max 3 positions at once | Daily loss cap: ₹7,500

---

## Strategy 2 — RSI 15-Minute Multi-Timeframe

**What it does:** Same mean-reversion idea as Strategy 1, but more selective. It requires alignment across two timeframes before entering, which filters out weaker setups.

**The idea behind it:** A dip on the 5-minute chart is more reliable if the 15-minute chart also looks oversold. And neither matters if the daily market structure is wrong. This strategy uses three "screens" — each must agree before a trade happens.

**Screen 1 — Daily check (gate-keeper):**
The bot looks at the daily chart and requires 2 out of 3 conditions to be true:
- Price is within 8% of the 200-day EMA (in an established trading range, not breaking down)
- RSI(14) is between 30–65 (healthy bull market territory)
- ADX(14) is below 25 (market is NOT strongly trending — ideal for mean reversion)

If the daily check fails, no trades happen for the day from this strategy.

**Screen 2 — 15-minute setup:**
When the daily check passes, the bot watches 15-minute candles for:
- RSI(9) below 40 (oversold on the intermediate timeframe)
- KER (Kaufman Efficiency Ratio) below 0.30 — this measures whether price is moving efficiently in a direction, or randomly. Below 0.30 means choppy/mean-reverting market, which is exactly when you want to fade moves
- Price below VWAP (dropped below "fair value")

**Screen 3 — 5-minute entry trigger:**
When both screens above are active, the bot waits for one more confirmation on the 5-minute chart:
- RSI(9) crosses back above 25 (bounce starting)
- The candle is green (close above open)
- Price is still below VWAP (we're entering cheap, before full recovery)
- MFI (Money Flow Index) below 30 (volume confirms weak buyers — good entry point)

**Exits:**
- 5-min RSI crosses above 50 → exit
- Price touches VWAP from below → exit
- 75 minutes pass without either → exit
- 3× ATR stop-loss as disaster protection
- Hard close at 2:30 PM

**Active hours:** 10:15 AM – 12:00 PM (full size), 1:30 PM – 2:30 PM (half size)

**Capital allocated:** ₹2.5 lakh | Max 3 positions at once | Daily loss cap: ₹7,500

---

## Strategy 3 — Expiry Day Iron Condor

**What it does:** Sells Nifty options on the weekly expiry day and collects the time decay (theta). This runs just once per week.

**The idea behind it:** Options lose value as expiry approaches — this is called theta decay. On expiry day, this decay is at its fastest. If Nifty stays within a certain range, the sold options expire worthless and you keep the premium you collected. An iron condor defines that range with four option legs.

**What an iron condor is:**

Imagine Nifty is at 24,000. The bot sets up a "safe zone" around that price:

```
BUY  Put  @ 23,800   (protects against catastrophic crash)
SELL Put  @ 23,950   (collecting premium, betting Nifty stays above here)
                                        ←  SAFE ZONE  →
SELL Call @ 24,050   (collecting premium, betting Nifty stays below here)
BUY  Call @ 24,200   (protects against catastrophic rally)
```

Net result: You collect premium from the two sold legs, pay a smaller premium for the bought wings. You profit if Nifty stays between ~23,950 and ~24,050 until expiry. The bought wings cap your maximum loss if Nifty blows past either boundary.

**Entry conditions (all must be true):**
- It's a Nifty expiry day (Thursday — post-SEBI revision)
- India VIX is between 12 and 18 (not too calm, not panicking)
- The put-side premium is at least 2× the call-side premium — this "skew" check confirms the market is showing normal downside fear, which means option prices are behaving predictably
- It's 2:00 PM IST (late enough that there are only 75 minutes left to expiry — maximum theta burn rate)

**Strike selection:**
- Sell strikes: 50 Nifty points OTM on each side
- Buy strikes (protection): 100 points further OTM (wings)
- Total width of each spread: 150 Nifty points

**Monitoring and exits:**
- Checks position every 30 seconds from 2:00 PM to 3:15 PM
- Stop-loss: if either sold leg's premium reaches 2× what was collected, exit that spread immediately
- Hard close: 3:15 PM IST — all legs closed before market end, no exceptions

**Capital allocated:** ₹2.5 lakh | Max 1 iron condor at a time | Daily loss cap: ₹7,500

---

## What the Logs Show

The bot writes two things after every trading day:

**Trade Journal (SQLite database):** Every entry and exit is recorded with the exact price, quantity, indicators at time of entry, and realized P&L. Partial exits are tracked separately — if a strategy exits 50% of a position and holds the rest, that's two separate journal rows linked to one trade.

**Daily summary:** Win rate, total trades, net P&L, and fees paid — one row per strategy per day.

To pull a quick performance check, run:
```
python -c "from core.trade_journal import TradeJournal; j = TradeJournal(); print(j.get_performance(days=30))"
```

---

## Project Structure (Quick Reference)

```
strategies/         ← The three strategies live here
core/               ← Engine, risk manager, data feed, trade journal, regime detection
src/                ← Angel One API auth, instruments lookup, expiry helpers, broker wrapper
config/config.yaml  ← All parameters in one place — edit here to tune strategies
paper_live.py       ← Active paper trading runner with WebSocket real-time data
data/               ← Cached historical data (pickle files by date)
logs/               ← Daily log files per strategy
```

All strategy parameters (RSI thresholds, stop-loss percentages, time windows, capital allocation) live in `config/config.yaml`. No digging through code to tune anything.
