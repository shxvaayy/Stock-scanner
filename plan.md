# AutoTheta — Upcoming Strategies (Roadmap)

This document describes the three new trading strategies being added to AutoTheta. All three are currently in development and will be deployed in paper mode first, validated over several weeks, and only then considered for live trading.

The existing strategies (RSI Bounce, RSI 15-Min, Expiry Condor) focus on mean reversion in individual stocks. The new strategies bring three different edges:

- **Price action at key levels** (where stop-loss clusters tend to live)
- **Volume profile structure** (where real institutional activity happened)
- **Multi-day RSI swing patterns** (setups that form over days, not minutes)

All three trade Nifty index options (calls and puts) — not individual stocks.

---

## Shared Infrastructure (Built First)

Before any new strategy goes in, a layer of shared code gets built that all three will use. This is being done once so each strategy doesn't reinvent the same wheel.

**`indicators.py`** — A single file with all the indicator math: RSI, EMA, ATR, VWAP, ADX, MFI, Kaufman Efficiency Ratio, swing highs/lows, and volume profile. Written from scratch in pure Python — no TA-Lib, no external libraries.

**`option_utils.py`** — All the option-specific plumbing:
- Given the spot price and VIX, which strike to buy
- Given a capital allocation and premium, how many lots to buy
- How to look up the option symbol and token from the instruments master

**`precheck.py`** — The pre-trade checklist. Every new strategy runs through this before placing any order:
1. Premium must be at least ₹60 (no micro-premium trades)
2. Bid-ask spread must be below 5% (liquidity check)
3. Live data must be fresh (last tick within 90 seconds)
4. India VIX must be below 22
5. VIX must not have spiked more than 20% in the last hour
6. Risk manager must allow the trade (not hit daily cap)
7. Market regime must match the trade direction (CRASH blocks most entries)
8. Today must not be a major macro event day (RBI, Budget, US Fed)

Any one of these failing kills the trade. The reason is logged every time.

---

## Position Management — Scaling In and Out

A single all-in/all-out trade rarely matches what real traders do. The bot supports two scaling concepts that can be enabled per strategy via config: **profit pyramiding** (the default, safer) and **averaging down** (advanced, opt-in).

### Profit pyramiding — default for all 3 new strategies

Book partial profits as the trade works, then optionally add back on confirmed continuation. This is the *anti-martingale* approach — scale into winners, never into losers.

- **T1** at 1.5× average cost → book 33% of position
- **T2** at 2.0× average cost → book another 33%, trail stop to T1 price
- **T3** at 3.0× average cost → book remaining 34%
- **Pyramid (optional, opt-in per strategy):** after T2 fires, if a fresh entry signal triggers in the same direction (a new sweep, a new VAL bounce), one re-entry at 50% of initial size is allowed with its own fresh stop

### Averaging down — advanced, opt-in only

Add to the position when premium drops, on the assumption that the original setup is still valid. This is the *martingale* approach. It sometimes recovers losers, and it sometimes accelerates blow-ups. The bot exposes it behind strict guardrails:

- Maximum 2 averages per trade, each at 0.5× initial size (total exposure capped at 2.0× initial)
- **Invalidation gate** — before any average, the original setup-defining level must still hold:
  - Liquidity Sweep: the swept level itself must still be respected (sweep direction intact)
  - VP + Trend: price must still be on the entry side of VAL (bullish) or VAH (bearish)
  - RSI Predictor: 15-min RSI(5) must still be on the entry side of 40 (or 60 bearish)
  - If invalidation fires → all averaging is blocked, full exit at market
- **Time gate** — no averaging after 60 minutes from initial entry, and no averaging after 2:00 PM IST regardless
- **Hard stop on the running average cost** — if combined premium drops 45% below average, exit everything. With averaging at full 2.0× exposure, the worst-case loss equals roughly 90% of one full position-size unit. Be aware of this before flipping it on

### Why averaging on options is genuinely riskier than on stocks

Options decay from theta regardless of direction, and IV crush after entry can drop premium 30% even when the underlying moves the right way. Averaging fights both direction and time at once. **Default for all 3 new strategies is `profit_pyramid` only.** Switch to `averaging` or `full` per strategy in config only after dry-run results show a setup hit rate high enough that adding to losers carries positive expected value.

### Configuration per strategy

In `config.yaml` each strategy declares a `scaling_profile`:

| Profile | Behavior |
|---|---|
| `no_scaling` | Single entry, single exit ladder, no adds |
| `profit_pyramid` | **Default.** Profit ladder + optional pyramid on fresh signal |
| `averaging` | Initial + up to 2 averages on loss + profit ladder, no pyramid |
| `full` | All of the above |

All 3 new strategies start at `profit_pyramid`. Flip to `averaging` per strategy only after paper validation.

### State management

A single `position_manager.py` module owns all multi-entry / multi-exit accounting: total qty, weighted average cost, which targets fired, how many averages were added, how many pyramids were used. Strategies don't reimplement any of this — they query the position manager for "should I take profit now?", "should I average here?", "is the position invalidated?". On bot restart, open positions are rebuilt from `trade_journal` rows.

---

## Strategy A — Liquidity Sweep

**One-line idea:** When the market fakes out past a key level, trapping stop-loss orders, and then snaps back, we enter in the reversal direction.

### What's a "liquidity sweep"?

Big institutions know where retail traders put their stop-losses — just below obvious support levels and just above obvious resistance levels. When price briefly dips below a key level, those stops get triggered, creating a burst of selling. Institutions buy into that selling. Price snaps back above the level. Retailers got flushed; institutions got filled cheaply. This is called a sweep.

The pattern looks like a long wick on a candle — the price spiked down, but the candle closed back up above the level.

### Levels the bot watches

- **Prior Day High/Low** — The previous session's highest and lowest points. These are the most popular stop-loss targets.
- **Opening Range High/Low** — The high and low from 9:15–9:45 AM. Locked in at 9:45.
- **Round numbers** — Every 100-point Nifty level near current price (e.g., 24000, 24100, 24200). Natural psychological magnets.
- **Equal Highs/Lows clusters** — Swing points that occurred at nearly the same price (within 0.1%) at least twice in the last 4 hours. These mark areas where stop-loss clusters build up over time.

Only levels with at least 2 prior touches (without breaking) qualify — virgin levels are skipped.

### How it detects a sweep

On each 5-minute Nifty Futures candle, the bot checks every active level:

**Bullish sweep** (price probed below a support level then closed back above):
- Candle's low went below the level AND close came back above
- The downside wick is more than 60% of the total candle range
- Volume was at least 1.5× the 20-candle average (real activity, not slow drift)
- Wick size was more than 0.5× the ATR (meaningful move, not noise)

**Bearish sweep** (price probed above a resistance level then closed back below):
- Mirror of the above — wick above, close below, high volume

Detected sweeps stay active for 10 candles (50 minutes). If nothing happens in that window, the setup expires.

### Entry trigger

The bot watches 1-minute Nifty Futures candles. After a sweep is detected, it waits for price to reclaim the session VWAP (Volume Weighted Average Price from 9:15 AM):

- **Bullish reclaim:** prior 1-min close was below VWAP, current close is above → enter CE (call option)
- **Bearish reclaim:** prior close above VWAP, current close below → enter PE (put option)

Entry happens on the next candle's open (simulated market order).

### Entry gates

All must pass before entering:
- Time window: 9:45–11:00 AM or 1:30–2:45 PM (avoids open chaos and pre-close thin markets)
- Sweep must be fresh (under 10 candles old)
- No existing open position from this strategy
- Pre-trade checklist passes

### Option selection

- Strike: ATM (at-the-money) adjusted for VIX — if VIX is low (below 15), go 1 strike OTM for better premium efficiency; if high, stay ATM
- Expiry: current week, unless today is expiry day or the day before (then use next week)
- Lot size: 25 units per lot (post-SEBI revision)
- Risk: 1% of ₹2.5L = ₹2,500 per trade, with SL at 35% of premium paid

### Position management

Default `scaling_profile`: `profit_pyramid`. Sweeps tend to work fast or fail fast — averaging into a failed sweep historically loses money on this setup, so averaging stays available as an opt-in but defaults OFF. Pyramid IS enabled by default: a continuation sweep + reclaim in the same session is genuinely a fresh signal worth scaling into.

**Invalidation level** (used by both averaging gate and exit logic): the swept level itself. Bullish trade is invalidated if Nifty 5-min closes back below the swept level. Bearish: closes back above.

### Exits (in priority order)

1. **Hard stop:** premium 45% below average cost → full exit at market (managed by position manager)
2. **Invalidation:** Nifty 5-min closes back through the swept level → full exit (reversal thesis broke)
3. **Profit ladder** (managed by position manager):
   - T1 at 1.5× average cost → sell 33%
   - T2 at 2.0× average cost → sell 33%, trail stop to T1
   - T3 at 3.0× average cost → sell remaining 34%
4. **Optional pyramid (after T2):** if a fresh sweep + VWAP reclaim fires in the same direction in the same session, add 0.5× initial size with its own fresh stop
5. **Time stop:** 2:45 PM IST → exit everything

---

## Strategy B — Volume Profile + Trend

**One-line idea:** Yesterday's session created a volume map — a clear picture of where most trading happened. Price gravitates toward those zones. We trade bounces from the edges of that map.

### What's a volume profile?

Instead of plotting price over time (a normal chart), a volume profile plots price vs. how much volume traded at each price level. Think of it as a horizontal histogram rotated 90 degrees.

Three key levels come out of it:

- **POC (Point of Control):** The price where the most volume traded. This acts like a magnet — price tends to return here.
- **VAH (Value Area High):** The upper edge of where 70% of yesterday's volume happened.
- **VAL (Value Area Low):** The lower edge of that same 70% zone.

Price outside the value area (above VAH or below VAL) tends to either return to the value area or continue strongly in that direction. We trade the return.

### Setup A — VAL Bounce (buy calls)

When price dips to yesterday's VAL and then closes back inside the value area, the bot sees this as a failed breakdown and buys calls:
- Prior 5-min close was at or below VAL
- Current 5-min close is above VAL
- The candle closed within 0.5× ATR of VAL (entered right at the zone, not far from it)

### Setup B — VAH Rejection (buy puts)

Mirror of Setup A — price pushed above VAH but couldn't hold, closes back inside:
- Prior 5-min close was at or above VAH
- Current 5-min close is below VAH
- Within 0.5× ATR of VAH

### Additional filters

- **200-day EMA trend filter:** For Setup A (bullish), Nifty must be within 3% of its 200-day EMA or above. For Setup B (bearish), must be within 3% below or above. This avoids fading the trend when we're far extended.
- **POC clearance:** Price must be at least 30 Nifty points away from POC — we don't trade right in the middle of the congestion zone.
- **Time window:** 9:45 AM – 12:30 PM or 2:00 PM – 2:45 PM
- **CVD (Cumulative Volume Delta):** A soft tie-breaker using buy vs. sell volume imbalance. If both setups would trigger at the same time, take the one whose CVD confirms the direction. Otherwise this doesn't block a trade — it's just logged.

### Intraday update

Every 30 minutes after 10:15 AM, the bot recomputes a second volume profile using only today's data (from 9:45 AM onward). It uses whichever profile's VAH/VAL is currently closer to price — the prior day or today's. This makes it adaptive to where volume is actually building.

### Option selection

Always ATM (at-the-money) for this strategy — VP setups are high conviction, so we pay for the maximum delta rather than buying cheaper OTM options.

### Position management

Default `scaling_profile`: `profit_pyramid`. POC magnetism gives clean partial-exit targets. Pyramiding works well on confirmed continuation toward POC. Averaging is opt-in: VP setups have a higher hit rate than sweeps, so averaging at VAL on a defended bounce can make sense — but only after paper validation.

**Invalidation level:** the entry-side VA boundary. Setup A (bullish, entered at VAL) is invalidated if 5-min closes back below VAL. Setup B (bearish, entered at VAH) is invalidated if 5-min closes back above VAH.

### Exits (in priority order)

1. **Hard stop:** premium 45% below average cost → full exit
2. **Invalidation / zone failure:** 5-min close back across the entry-side VA boundary → full exit
3. **POC magnet (intermediate):** when underlying touches POC ± 10 pts, the position manager fires the next pending profit-ladder rung early — POC tends to mark the first real take-profit zone for VP trades
4. **Profit ladder** (managed by position manager):
   - T1 at 1.5× average cost → sell 33%
   - T2 at 2.0× average cost → sell 33%, trail stop to T1
   - T3 at 3.0× average cost → sell remainder (underlying typically reaching opposite VA boundary by this point)
5. **Optional pyramid (after T2):** if a fresh VAL bounce / VAH rejection fires later in the same session, add 0.5× initial
6. **Time stop:** 2:45 PM IST

---

## Strategy C — RSI Predictor (Daily Swing)

**One-line idea:** Specific RSI patterns on the daily chart signal multi-day momentum shifts. The bot detects them at end-of-day and enters an option trade the following morning.

This is split into two parts that work together across days:

### Part 1 — The EOD Scanner (runs at 3:20 PM every day)

Fetches the last 60 daily closes of Nifty Futures and looks for one of two patterns in the RSI(14) series.

**Pattern A — Failed Swing W-Bottom (bullish signal):**

This pattern says: RSI tried to break down but couldn't, which usually means price has more upside.

In plain English:
1. RSI got very oversold (below 30) sometime in the last 15 trading days
2. It recovered above 30
3. It dipped again but did NOT go as low as the first dip (higher low = buyers stepping in earlier)
4. Currently RSI is above 30 and rising

This forms a "W" shape on the RSI chart. The failed second low is the key — it shows sellers are running out of steam.

Additional gate: Market regime must be BULL.

**Pattern B — Failed Swing M-Top (bearish signal):**

Mirror of Pattern A:
1. RSI got overbought (above 70) sometime in the last 15 trading days
2. It pulled back below 70
3. It pushed up again but didn't reach the prior high (lower high = buyers fading)
4. Currently RSI is below 70 and falling

This forms an "M" shape — a failed second attempt at new highs. Bearish.

Additional gate: Regime must be BULL or BEAR (blocked in CRASH).

**Output:** The scanner writes a prediction file (`data/signals/rsi_prediction.json`) with:
- Which pattern was detected (BULLISH_W, BEARISH_M, or NEUTRAL)
- Today's RSI value and the prior extreme (the first peak/trough)
- Current regime and VIX
- The date this prediction is valid for (tomorrow)

If no pattern is found, it writes NEUTRAL and the entry strategy does nothing the next day.

### Part 2 — The Entry Strategy (runs next morning, candle-driven)

Reads the prediction file at startup. If it's for today and not NEUTRAL, it enters monitoring mode.

**Entry window:** 9:45 AM – 11:30 AM only. If no entry by 11:30, the prediction is marked expired for the day.

**Entry trigger for BULLISH_W:**
- Resample 1-min candles to 15-min
- Wait for RSI(5) on 15-min to cross from below 40 to above 40 (momentum resuming)
- Price must be above the session VWAP (buyers are in control)
- Gap from yesterday's close must be between -0.5% and +1.5% (not an extreme gap — we want to catch the move, not chase)

**Entry trigger for BEARISH_M:**
- 15-min RSI(5) crosses from above 60 to below 60
- Price below VWAP
- Gap between -1.5% and +0.5%

If VIX was above 18 when the prediction was generated, position size is cut in half (high-volatility caution).

**Position management:** Default `scaling_profile`: `profit_pyramid` BUT pyramid disabled (`pyramid_after_target_idx: -1`). RSI Predictor is a single-thesis daily-momentum trade — adding back on continuation amplifies whipsaw risk on this setup specifically, and there is rarely a fresh "next signal" within the same session. Averaging is also OFF by default; it can be enabled after paper validation if the W-bottom / M-top hit rate exceeds 60%.

**Invalidation level:** 15-min RSI(5) crossing back through 40 (BULLISH_W) or 60 (BEARISH_M).

**Exits (priority order):**
1. **Hard stop:** premium 45% below average cost → full exit
2. **Invalidation / signal failure:** 15-min RSI(5) crosses back through 40 (or 60) within first 30 minutes after entry → full exit
3. **Profit ladder** (managed by position manager):
   - T1 at 1.5× average cost → sell 33%
   - T2 at 2.0× average cost → sell 33%, trail stop to T1
   - T3 at 3.0× average cost → sell remainder
4. **RSI extreme acceleration:** if 15-min RSI(5) reaches 70 (BULLISH_W) or 30 (BEARISH_M), force the next pending profit-ladder rung early — mean reversion exhausted
5. **Time stop:** 2:45 PM IST

**Cleanup:** When the trade fully closes, the prediction file is deleted — it cannot be re-used if the bot restarts mid-day.

---

## Rollout Plan

All three strategies are deployed with `enabled: false` initially.

**Step 1 — 30-day dry run (no real orders, just logs)**
Run `scripts/dryrun_new_strategies.py` which replays 30 days of historical Nifty Futures data through all three strategies and prints:
- Number of signals generated
- Win rate
- Net P&L
- Sharpe ratio
- Max drawdown

If any strategy shows a win rate below 40% or negative Sharpe in the dry run, investigate before proceeding.

**Step 2 — Enable one strategy at a time in paper mode**
Flip `enabled: true` for one strategy only. Run it in paper mode for 5 trading days. Compare the paper P&L to the dry-run expectation. If divergence is greater than 30% (e.g., dry run said +₹8,000 but paper said +₹2,000), investigate the cause before continuing.

Keep `scaling_profile: profit_pyramid` for the first 5 paper days regardless of dry-run results. Averaging-down is enabled per strategy only after paper validation, never before. The profile change is one config flip — no code changes required.

**Step 3 — Repeat for each strategy**
Never enable all three new strategies simultaneously without checking that total risk exposure across all 6 strategies (3 old + 3 new) stays within the global cap.

**Recommended order to flip live:**
1. RSI Predictor (most straightforward — daily scan + morning entry, easy to review)
2. Volume Profile + Trend (requires prior-session data fetch, test that initialization works correctly)
3. Liquidity Sweep (most real-time complexity — level tracking, sweep detection, 1-min monitoring)

---

## Known Limitations / TODOs

- **Holiday calendar:** The expiry helper currently skips weekends but doesn't have a full NSE holiday list. The next-trading-day calculation emits a TODO for this. Until fixed, check manually around market holidays.
- **Bid-ask data:** Angel One SmartAPI's LTP endpoint doesn't expose bid/ask. The spread check in the pre-trade checklist uses an estimate. This is a known gap — the check won't block trades on this criterion until real bid/ask data becomes available.
- **India VIX 1-hour jump check:** If the bot starts mid-session without a full 60-minute VIX history buffer, the 1-hour jump check is skipped (logged but not blocking). This is safe but means the check isn't always active at market open.
- **Backtesting:** The dry-run script uses historical data but doesn't model slippage or partial fills. Actual paper results will differ slightly.
