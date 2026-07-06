# AutoTheta — Implementation Plan (3 New Strategies)

This document describes the technical approach for implementing the three new strategies. Work is split into 5 phases. Each phase ends with a checkpoint — review and approval before the next phase starts.

---

## Phase 0 — Codebase Audit (Before Writing a Single Line)

Before implementation begins, verify 8 core assumptions about the codebase and broker API. The point is to catch surprises early rather than mid-build.

**What gets verified:**
- Nifty Futures token in NFO segment (expected: 26000)
- Nifty index token in NSE segment (expected: 99926000), India VIX token (expected: 99926017)
- Nifty options lot size (expected: 25, post-Oct 2024 SEBI revision)
- Weekly expiry day (expected: Thursday, post-Oct 2024 SEBI revision from prior Tuesday)
- Whether `data_feed.fetch_historical()` accepts both NSE and NFO segments
- Whether `risk_manager.calculate_position_size()` is built for equity (price points) or can handle options
- Whether the engine has both candle-driven and schedule-driven loops
- Whether `paper_live.py` is still the active runner or a legacy file

**Deliverable:** One-page report with VERIFIED / CORRECTED for each assumption. No code written yet.

---

## Phase 1 — Shared Infrastructure

Goal: Build the shared modules that all 3 strategies will call. No strategy code in this phase.

### File 1: `strategies/indicators.py` (new)

All indicator math in one place, pure Python, no TA-Lib. Functions:

| Function | Notes |
|---|---|
| `rsi(closes, period)` | Wilder's smoothing, returns NaN for indices < period |
| `ema(values, period)` | Standard exponential moving average |
| `atr(candles, period=14)` | Average True Range using Candle objects |
| `vwap(candles, anchor_idx=0)` | Anchored VWAP; anchor_idx=0 means full session |
| `adx(candles, period=14)` | Returns tuple: (adx, plus_di, minus_di) |
| `mfi(candles, period=14)` | Money Flow Index |
| `kaufman_efficiency_ratio(closes, period=10)` | KER — measures trending vs. choppy |
| `swing_highs_lows(candles, swing_length=5)` | Returns list of (index, price, "high"\|"low") |
| `resample(candles, minutes)` | Aggregate 1-min candles into N-min candles (move from rsi_15min.py) |
| `compute_volume_profile(candles, n_bins=150, value_area_pct=0.70)` | Returns dict with POC, VAH, VAL, and profile list |

The existing 3 strategies keep their inline indicators for now — refactor is a separate task.

### File 2: `strategies/option_utils.py` (new)

Option-trade plumbing used by all 3 new strategies. Functions:

- **`select_atm_strike(spot, vix, is_expiry_day, direction)`** — Rounds spot to nearest 50 to get ATM. Applies a VIX-based OTM offset for CE/PE selection (e.g., 1 strike OTM when VIX is 15–18, ATM otherwise).
- **`select_expiry_date(today, prefer_current_week=True)`** — Uses `src/expiry.py`. If today is expiry day or the day before, returns next week's expiry to avoid extreme theta risk.
- **`calculate_option_qty(premium, sl_pct, capital, risk_pct, lot_size, high_vix_multiplier=1.0)`** — Sizes a BUY order based on how much of capital can be risked given the stop-loss percentage. Returns 0 if the result is below 1 lot (caller skips the trade).
- **`fetch_option_quote(api, exchange, symbol, token)`** — Wraps `api.ltpData()` to return a standard dict with LTP, bid, ask, spread_pct, and timestamp.

### File 3: `strategies/precheck.py` (new)

The pre-trade checklist. Every new strategy calls `precheck_option_entry()` before any order. Returns a `PrecheckResult` dataclass with `allowed: bool` and `reason: str`.

Eight checks run in order — first failure short-circuits:

1. Premium ≥ minimum (default ₹60)
2. Bid-ask spread ≤ 5%
3. Last tick age for Nifty Futures ≤ 90 seconds
4. India VIX is fetchable AND ≤ 22
5. VIX 1-hour change ≤ 20% (computed from buffer; if buffer is too small, skip and log)
6. `risk_manager.can_trade(strategy_name)` is True
7. Regime matches trade direction (CRASH blocks most entries; BULL required for bullish, BEAR/BULL allowed for bearish)
8. Today is not a known macro event day

Every rejection logs at INFO level with the reason and current values. Every pass logs at DEBUG.

### File 4: `core/risk_manager.py` (modify — add one method)

Add `calculate_option_position_size(strategy_name, premium, sl_pct, lot_size=25, size_multiplier=1.0)` that reads the strategy's capital and risk config and calls `option_utils.calculate_option_qty()`. Existing methods unchanged.

### File 5: `data/signals/` (new directory)

Empty directory with `.gitkeep`. Used by Strategy C to persist EOD predictions across process restarts.

### File 6: `strategies/position_manager.py` (new)

Centralized position state and scaling logic. Every new strategy uses this — no strategy reimplements partial exits, averaging-down decisions, pyramid logic, or hard-stop tracking.

**Core dataclasses:**

```python
@dataclass
class ScalingConfig:
    profile: str  # "no_scaling" | "profit_pyramid" | "averaging" | "full"
    profit_targets: list[tuple[float, float]] = (
        (1.5, 0.33), (2.0, 0.33), (3.0, 1.0)
    )  # (premium_mult_vs_avg_cost, exit_fraction_of_remaining)
    avg_triggers: list[float] = (0.80, 0.65)
        # premium drop fractions vs INITIAL entry premium
    avg_size_mults: list[float] = (0.5, 0.5)
        # qty multipliers vs initial qty
    max_total_size_mult: float = 2.0
    pyramid_after_target_idx: int = 1  # add after T2; -1 disables pyramiding
    pyramid_size_mult: float = 0.5
    pyramid_requires_fresh_signal: bool = True
    max_pyramids: int = 1
    hard_stop_pct_from_avg: float = 0.45
    no_averaging_after_minutes: int = 60
    no_averaging_cutoff_time: time = time(14, 0)


@dataclass
class ScaledPosition:
    strategy_name: str
    trade_id: str
    direction: str  # "bullish" | "bearish"
    initial_premium: float
    initial_qty: int
    invalidation_level: float | None
    invalidation_direction: str  # "below" | "above"
    scaling_config: ScalingConfig
    entries: list[Entry]   # Entry(ts, qty, premium, kind)
    exits: list[Exit]      # Exit(ts, qty, premium, reason)
    targets_hit: set[int]
    averages_done: int
    pyramids_done: int
    initial_entry_ts: datetime
```

**Methods (pure, no I/O):**

| Method | Returns | Purpose |
|---|---|---|
| `add_entry(qty, premium, ts, kind)` | None | Record entry; updates avg_cost. `kind` ∈ {"initial","average","pyramid"} |
| `add_exit(qty, premium, ts, reason)` | None | Record exit; reduces net qty |
| `avg_cost` | float | Weighted avg premium of remaining qty |
| `net_qty` | int | sum(entry qtys) − sum(exit qtys) |
| `total_size_mult` | float | sum(entry qtys) / initial_qty |
| `should_take_profit(current_premium)` | (qty, target_idx) or (0, None) | Next pending profit-ladder rung |
| `should_average(current_premium, current_underlying, now)` | qty or 0 | Average-down decision (returns 0 if any guardrail fails) |
| `should_pyramid(current_premium, has_fresh_signal)` | qty or 0 | Pyramid-up decision |
| `is_invalidated(current_underlying)` | bool | Compares against `invalidation_level` and direction |
| `hit_hard_stop(current_premium)` | bool | True if mid ≤ avg_cost × (1 − hard_stop_pct_from_avg) |
| `is_fully_closed` | bool | net_qty == 0 |

**`should_average` decision logic (the dangerous one — get this right):**
```
if profile not in {"averaging", "full"}: return 0
if averages_done >= len(avg_triggers): return 0
if total_size_mult >= max_total_size_mult: return 0
if (now - initial_entry_ts).minutes > no_averaging_after_minutes: return 0
if now.time() >= no_averaging_cutoff_time: return 0
if is_invalidated(current_underlying): return 0
trigger = avg_triggers[averages_done]
if current_premium > initial_premium * trigger: return 0
return initial_qty * avg_size_mults[averages_done]
```

**Profit ladder rung firing:**
A profit target fires once. After firing, target index is added to `targets_hit`. The next rung becomes the active threshold. The position manager also exposes `force_next_target()` for strategy-specific accelerators (POC magnet, RSI extreme).

**Restart safety:**
Add a free function:
```
position_manager.reconstruct_from_journal(
    strategy_name: str, trade_id: str, journal: TradeJournal
) -> ScaledPosition
```
Walks the journal's entry/exit rows for that trade_id and replays them into a fresh ScaledPosition. Strategies call this in `initialize()` if they discover an open position on startup.

### File 7: `tests/test_phase1_infrastructure.py` (new)

Synthetic-data unit tests, no live API calls:
- RSI output matches a hand-computed reference on a 20-element input
- VWAP with `anchor_idx=5` returns NaN for indices 0–4
- `select_atm_strike` returns correct strike for known inputs
- `calculate_option_qty` returns a multiple of lot_size
- `precheck_option_entry` rejects when premium is below minimum, with reason containing "premium"

Position manager tests:
- 3 entries [(10, 100), (5, 80), (5, 65)] → `avg_cost` ≈ 86.25
- At premium 1.5 × 86.25, `should_take_profit` returns (qty for 33%, target_idx=0)
- At premium 86.25 × 0.55, `hit_hard_stop` is True
- Bullish position with `invalidation_level=24000` direction "below"; underlying 23990 → `is_invalidated` True
- Profile=`profit_pyramid`: `should_average` always returns 0 regardless of conditions
- Profile=`averaging`, time=14:30 IST: `should_average` returns 0 (cutoff gate)
- Profile=`averaging`, all gates pass: returns expected qty (= initial_qty × 0.5)
- After 2 averages, `total_size_mult` = 2.0; further `should_average` returns 0
- `reconstruct_from_journal` round-trip: write entries+exits to journal, reconstruct, assert state identical

**Checkpoint 1:** Diff of all new/changed files + pytest results. Stop and review.

---

## Phase 2 — Strategy A: Liquidity Sweep

**File:** `strategies/liquidity_sweep.py`  
**Class:** `LiquiditySweepStrategy`, registered as `"liquidity_sweep"`  
**Pattern:** Follows `rsi_bounce.py` class structure exactly — `__init__`, `initialize`, `on_candle`, `on_tick`, `teardown`  
**Data source:** Nifty Futures (token 26000, NFO) — NOT the index (index has no volume)

### Implementation breakdown

**`initialize()`**
- Fetch prior-session Nifty Futures OHLCV to compute Prior Day High/Low
- Compute round-number levels within ±300 of current spot
- Set up buffers for 5-min candles, volume averages, ATR, and swing detection

**Level tracking (updated during session):**
- PDH/PDL: static for the day
- Opening Range: locked at 9:45 AM from 9:15–9:44 candles
- Round numbers: recomputed when spot drifts more than 150 points
- EQH/EQL clusters: from `indicators.swing_highs_lows()` on rolling 100-candle buffer, grouped within 0.1% tolerance, cluster of 2+ = valid level
- All levels require at least 2 prior touches to qualify

**`on_candle()` (5-min candles) — sweep detection:**
- For each active level, check the last 3 candles for the sweep pattern
- Bullish: low < level, close > level, wick > 60% of range, volume > 1.5× avg, wick > 0.5× ATR
- Bearish: mirror
- Store detected sweeps with timestamp and direction; expire after 10 candles

**`on_candle()` (1-min candles) — entry trigger:**
- Compute anchored VWAP from 9:15 AM
- For each active sweep, check if price just reclaimed VWAP
- If reclaim detected: run all entry gates → run precheck → select option → size → emit Signal

**Position manager wiring:**
- On entry signal, create `ScaledPosition` with `direction`, `initial_premium`, `initial_qty`, `invalidation_level = swept_level`, `invalidation_direction` ("below" for bullish trade, "above" for bearish).
- After successful entry order, call `position.add_entry(qty, fill_premium, now, kind="initial")`.
- All exit logic queries the position manager (see below) — no inline ladder math.

**`on_tick()` — exit monitoring (priority order, first match wins):**
1. `position.hit_hard_stop(current_premium)` → emit full exit, reason `"hard_stop"`
2. Strategy-specific invalidation: 5-min close back through swept level → emit full exit, reason `"sweep_invalidated"`
3. `position.should_take_profit(current_premium)` → emit partial exit for that qty, reason `"target_{idx}"`
4. Time stop at 14:45 IST → full exit, reason `"time_stop"`

**`on_candle()` — averaging and pyramid checks (run after entry detection):**
- If position open and not closed:
  - `qty = position.should_average(current_premium, candle.close, now)` → if > 0, emit averaging entry order; on fill, `position.add_entry(qty, fill_premium, now, kind="average")`
  - If a fresh sweep + reclaim fires in same direction: `qty = position.should_pyramid(current_premium, has_fresh_signal=True)` → emit pyramid order; on fill, `position.add_entry(qty, fill_premium, now, kind="pyramid")`

**Config entry (default scaling profile shown):**
```yaml
- strategy:
    name: "LiquiditySweep_AVWAP"
    type: "liquidity_sweep"
    enabled: false
  parameters:
    # ... (existing params)
    scaling_profile: "profit_pyramid"
    profit_targets: [[1.5, 0.33], [2.0, 0.33], [3.0, 1.0]]
    pyramid_after_target_idx: 1
    pyramid_size_mult: 0.5
    # averaging defaults are present but inactive under profit_pyramid:
    avg_triggers: [0.80, 0.65]
    avg_size_mults: [0.5, 0.5]
    max_total_size_mult: 2.0
    hard_stop_pct_from_avg: 0.45
    no_averaging_after_minutes: 60
    no_averaging_cutoff_time: "14:00"
```

**Tests:** `tests/test_liquidity_sweep.py` — sweep detection with synthetic candles, volume gate, VWAP reclaim logic, time window blocking.

**Checkpoint 2:** Diff + test results + one-day dry run on Nifty Futures historical data showing what trades would have been placed. Stop and review.

---

## Phase 3 — Strategy B: Volume Profile + Trend

**File:** `strategies/volume_profile_trend.py`  
**Class:** `VolumeProfileTrendStrategy`, registered as `"vp_trend"`  
**Data source:** Nifty Futures 1-min candles (1-min for VP resolution) and 5-min candles (entry timeframe)

### Implementation breakdown

**`initialize()`**
- Fetch prior trading session's 1-min OHLCV (9:15–15:30) via `data_feed.fetch_historical()`
- Compute prior-session VP using `indicators.compute_volume_profile(candles, n_bins=150, value_area_pct=0.70)`
- Store POC, VAH, VAL as `self._vp_prior`
- Fetch last 250 daily Nifty Futures closes, compute 200-EMA, store as `self._daily_ema200`

**`on_candle()` — 5-min candles:**
- Every 30 min after 10:15 AM, recompute intraday VP from 9:45 onward, store as `self._vp_intraday`
- Select active VP: whichever VAH/VAL is closer to current price
- Check Setup A (VAL bounce) or Setup B (VAH rejection) triggers
- Apply 200-EMA gate and POC clearance gate
- If both setups would fire: use CVD slope as tie-breaker (log CVD either way)
- On valid setup: run precheck → select ATM option → size → emit Signal

**Position manager wiring:**
- On entry, create `ScaledPosition` with `invalidation_level` = VAL (Setup A) or VAH (Setup B), direction set accordingly.
- Same `add_entry` / query pattern as Strategy A.

**`on_tick()` — exit monitoring (priority order):**
1. `position.hit_hard_stop` → full exit, reason `"hard_stop"`
2. Zone failure: 5-min close back across entry-side VA boundary → full exit, reason `"zone_failure"`
3. POC magnet: when underlying ∈ [POC ± 10 pts] AND `position.targets_hit` doesn't already include current rung → call `position.force_next_target()` and emit partial exit, reason `"poc_magnet"`
4. `position.should_take_profit` → partial exit for that qty, reason `"target_{idx}"`
5. Time stop at 14:45 IST → full exit

**`on_candle()` — averaging and pyramid checks:**
- Same pattern as Strategy A. Pyramid fresh signal = a new VAL bounce (Setup A) or VAH rejection (Setup B) firing while position is open.

**Config entry (defaults shown):**
```yaml
- strategy:
    name: "VolumeProfile_Trend"
    type: "vp_trend"
    enabled: false
  parameters:
    # ... (existing params)
    scaling_profile: "profit_pyramid"
    profit_targets: [[1.5, 0.33], [2.0, 0.33], [3.0, 1.0]]
    pyramid_after_target_idx: 1
    pyramid_size_mult: 0.5
    avg_triggers: [0.80, 0.65]
    avg_size_mults: [0.5, 0.5]
    max_total_size_mult: 2.0
    hard_stop_pct_from_avg: 0.45
    no_averaging_after_minutes: 60
    no_averaging_cutoff_time: "14:00"
```

**Tests:** `tests/test_vp_trend.py` — compute_volume_profile with all volume in one bin (POC = that bin), uniform distribution (VA covers ~70% of bins), VAL bounce trigger logic.

**Checkpoint 3:** Diff + test results + one-day dry run. Stop and review.

---

## Phase 4 — Strategy C: RSI Predictor

Two registered strategies, not one. They share the same logical intent but run at different times via the engine's two existing loops.

### Part 1: `strategies/rsi_predictor_eod.py`
**Class:** `RSIPredictorEOD`, registered as `"rsi_predictor_eod"`  
**Type:** Schedule-driven, triggers at 15:20 IST

**`execute()` logic:**
1. Fetch last 60 daily Nifty Futures closes
2. Compute RSI(14) on daily closes
3. Scan for Pattern A (W-bottom: prior RSI trough below 30, second dip didn't break prior low, RSI now rising above 30) or Pattern B (M-top: mirror with RSI above 70)
4. Check regime gate (BULL required for A, BULL/BEAR for B)
5. If pattern found: write `data/signals/rsi_prediction.json` with signal, RSI values, regime, VIX, spot, and `for_date = next_trading_day(today)`
6. If no pattern: write NEUTRAL

**Note:** If `src/expiry.py` doesn't have `next_trading_day()`, add it — skips weekends, emits a TODO for full holiday calendar.

### Part 2: `strategies/rsi_predictor_entry.py`
**Class:** `RSIPredictorEntry`, registered as `"rsi_predictor_entry"`  
**Type:** Candle-driven on Nifty Futures 1-min candles

**`initialize()` logic:**
- Read `data/signals/rsi_prediction.json`
- If file missing or `for_date != today` → `self._signal = None` (expired)
- Otherwise load signal

**`on_candle()` logic:**
- Skip if signal is None, NEUTRAL, `_entry_done` is True, or time is outside 9:45–11:30 AM
- Resample 1-min buffer to 15-min using `indicators.resample()`
- Compute RSI(5) on 15-min closes
- BULLISH_W entry: RSI(5) crosses 40 from below + price above VWAP + gap between -0.5% and +1.5%
- BEARISH_M entry: RSI(5) crosses 60 from above + price below VWAP + gap between -1.5% and +0.5%
- On trigger: adjust size for high-VIX (0.5× if VIX > 18) → precheck → select ATM option → emit Signal → `self._entry_done = True`

**Position manager wiring:**
- Invalidation level for this strategy is RSI-based, not price-based, so the price-only `is_invalidated` check is bypassed. Track invalidation in the strategy's `on_candle` directly: 15-min RSI(5) crossing back through 40 (BULLISH_W) or 60 (BEARISH_M) within first 30 min after entry.
- Pyramid disabled by default (`pyramid_after_target_idx: -1`).

**`on_tick()` exit monitoring (priority order):**
1. `position.hit_hard_stop` → full exit, reason `"hard_stop"`
2. RSI signal failure (checked in on_candle, not on_tick — 15-min cadence) → full exit, reason `"signal_failure"`
3. RSI extreme acceleration: 15-min RSI(5) ≥ 70 (BULLISH_W) or ≤ 30 (BEARISH_M) → call `position.force_next_target()` and emit partial exit, reason `"rsi_extreme"`
4. `position.should_take_profit` → partial exit, reason `"target_{idx}"`
5. Time stop at 14:45 IST → full exit

**`on_candle()` — averaging check (no pyramid):**
- `qty = position.should_average(...)` → if > 0, emit averaging order. Note the dual gate: position manager's invalidation check is empty here (RSI-based invalidation lives in strategy code), so the strategy MUST also check RSI invalidation before submitting an averaging order. Implement as: if RSI back through 40/60, set `position.invalidation_override = True` and the position manager treats it as invalidated for averaging purposes.

**`teardown()` / full exit:** Deletes `data/signals/rsi_prediction.json` to prevent re-use on restart.

**Config:** Two entries — EOD strategy has `capital_allocation: 0` (it doesn't trade). Entry strategy carries full allocation. Default `scaling_profile: "profit_pyramid"` with `pyramid_after_target_idx: -1` (no pyramid).

**Tests:** `tests/test_rsi_predictor.py` — W-bottom series → BULLISH_W, M-top → BEARISH_M, flat RSI → NEUTRAL, stale prediction file → no entry, 15-min RSI(5) crossing 40 triggers entry.

**Checkpoint 4:** Diff + test results + dry run on a week containing a W-bottom pattern. Stop and review.

---

## Phase 5 — Integration & Observability

Final wiring before any strategy goes live (even in paper mode):

- **Config registration:** All 3 strategies added to `config/config.yaml` with `enabled: false`
- **VIX kill-switch in engine.py:** Before each candle dispatch, if VIX 1-hour change > 25% OR VIX > 30, set a session-wide pause flag. Strategies still get `on_candle` calls but precheck blocks entries. Existing positions follow normal exits.
- **Trade journal compatibility:** Verify `record_entry()` and `record_exit()` accept the indicator dicts each strategy produces. If schema is rigid, add a `meta` JSON column rather than forcing individual columns.
- **Per-strategy log files:** Each strategy logs to `logs/{date}/{strategy_name}.log` with three required line types per trade: `SIGNAL_DETECTED`, `ENTRY_SUBMITTED`, `EXIT` with reason code.
- **`paper_live.py` note:** If confirmed legacy in Phase 0, add a banner comment at the top pointing to `core/engine.py`. If still active, add minimal hooks for the 3 new strategies.
- **Dry-run script:** `scripts/dryrun_new_strategies.py` — loads 5 days of historical data, replays through all 3 strategies, prints a summary table: strategy | signals | entries | wins | losses | net_pnl | avg_hold_min. Run this before flipping any strategy to `enabled: true`.

**Checkpoint 5 (final):** All tests green, 30-day dry-run summary table, list of remaining TODOs, recommended order to flip strategies live.

---

## File Summary

| File | Status | Phase |
|---|---|---|
| `strategies/indicators.py` | New | 1 |
| `strategies/option_utils.py` | New | 1 |
| `strategies/precheck.py` | New | 1 |
| `strategies/position_manager.py` | New | 1 |
| `core/risk_manager.py` | Modified (1 method added) | 1 |
| `data/signals/.gitkeep` | New | 1 |
| `tests/test_phase1_infrastructure.py` | New | 1 |
| `strategies/liquidity_sweep.py` | New | 2 |
| `tests/test_liquidity_sweep.py` | New | 2 |
| `strategies/volume_profile_trend.py` | New | 3 |
| `tests/test_vp_trend.py` | New | 3 |
| `strategies/rsi_predictor_eod.py` | New | 4 |
| `strategies/rsi_predictor_entry.py` | New | 4 |
| `tests/test_rsi_predictor.py` | New | 4 |
| `src/expiry.py` | Modified (add next_trading_day if missing) | 4 |
| `config/config.yaml` | Modified (3 new strategy entries) | 5 |
| `core/engine.py` | Modified (VIX kill-switch) | 5 |
| `core/trade_journal.py` | Modified if needed (meta column) | 5 |
| `scripts/dryrun_new_strategies.py` | New | 5 |

**Hard constraints throughout:**
- No new pip dependencies — pure Python only
- Every entry calls `precheck_option_entry()` — no exceptions
- Every new strategy follows the `rsi_bounce.py` class pattern
- `enabled: false` in all initial config entries — user flips manually
- Options position sizing uses `calculate_option_position_size()`, never the equity sizer
- Volume-based features use Nifty Futures (token 26000), never the index
- All entries and exits route through `ScaledPosition.add_entry()` / `add_exit()` — strategies do not maintain their own qty/avg-cost state
- Default `scaling_profile` for every new strategy is `"profit_pyramid"` — averaging-down is opt-in only and never enabled in initial config
- A position can never exceed `max_total_size_mult` × initial qty (default 2.0×) — enforced in `should_average` and `should_pyramid`
- Hard stop is computed against running average cost, not initial entry — re-evaluated on every tick after an averaging fill
