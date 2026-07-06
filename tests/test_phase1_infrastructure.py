"""Phase 1 shared infrastructure tests — all synthetic, no live API."""

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from models.types import Candle


def _ts(year=2026, month=1, day=1, hour=9, minute=15, offset_min=0):
    """Build datetime safely even when minute offset crosses hour boundaries."""
    base = datetime(year, month, day, hour, minute)
    return base + timedelta(minutes=offset_min)


def _candle(ts, o, h, l, c, v=1000):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v,
                  token="T", symbol="TEST")


# ─────────────────────────────────────────────────────────────────────
def test_indicators_rsi():
    from strategies.indicators import rsi
    closes = list(range(50))  # monotonic up
    r = rsi(closes, 14)
    assert all(x != x for x in r[:14])  # NaN
    assert r[-1] == 100.0  # all gains, no losses
    closes2 = list(range(50, 0, -1))
    r2 = rsi(closes2, 14)
    assert r2[-1] < 5  # all losses
    print("  ✓ rsi monotonic up→100, monotonic down→~0")


def test_indicators_ema():
    from strategies.indicators import ema
    vals = [10] * 30
    e = ema(vals, 10)
    # After convergence, EMA of constant = constant
    assert abs(e[-1] - 10) < 0.01
    print("  ✓ ema converges to constant input")


def test_indicators_atr():
    from strategies.indicators import atr
    candles = [_candle(_ts(offset_min=i), 100, 102, 98, 100, 1000) for i in range(30)]
    a = atr(candles, 14)
    # Each candle has range 4 → ATR should converge to 4
    assert abs(a[-1] - 4.0) < 0.01
    print("  ✓ atr converges to candle range")


def test_indicators_vwap_anchor():
    from strategies.indicators import vwap
    candles = [_candle(_ts(offset_min=i), 100, 102, 98, 100, 1000) for i in range(10)]
    v = vwap(candles, anchor_idx=5)
    # Values before anchor should be NaN
    for x in v[:5]:
        assert x != x
    # Values from anchor onward should be valid
    assert v[5] == 100  # typical price = (102+98+100)/3 = 100
    print("  ✓ vwap with anchor_idx=5 → NaN before 5, valid from 5")


def test_indicators_ker():
    from strategies.indicators import kaufman_efficiency_ratio as ker
    # Trending series
    trending = [100 + i for i in range(20)]
    k_t = ker(trending, 10)
    # Choppy series
    choppy = [100 + (i % 2) for i in range(20)]
    k_c = ker(choppy, 10)
    assert k_t[-1] > 0.9  # near-perfect trend
    assert k_c[-1] < 0.2  # high chop
    print(f"  ✓ ker(trend)={k_t[-1]:.2f}, ker(chop)={k_c[-1]:.2f}")


def test_indicators_volume_profile():
    from strategies.indicators import compute_volume_profile
    # All volume in one bin
    candles = [_candle(_ts(offset_min=i), 100, 100, 100, 100, 1000) for i in range(10)]
    vp = compute_volume_profile(candles, n_bins=10)
    assert vp["POC"] == 100
    assert vp["VAH"] == 100
    assert vp["VAL"] == 100

    # Uniform price distribution
    candles2 = []
    for i in range(100):
        p = 100 + (i % 10)
        candles2.append(_candle(_ts(offset_min=i), p, p, p, p, 1000))
    vp2 = compute_volume_profile(candles2, n_bins=20)
    assert vp2["VAL"] <= vp2["POC"] <= vp2["VAH"]
    assert vp2["VAH"] - vp2["VAL"] > 0
    print(f"  ✓ vp single-bin POC=VAL=VAH=100; uniform vp range > 0")


def test_indicators_swing_detection():
    from strategies.indicators import swing_highs_lows
    # Create candles with clear pivot at index 5
    candles = []
    for i in range(11):
        h = 100 + (5 if i == 5 else 0)
        candles.append(_candle(_ts(offset_min=i), 100, h, 99, 100, 1000))
    swings = swing_highs_lows(candles, swing_length=3)
    assert any(s[0] == 5 and s[2] == "high" for s in swings), \
        f"expected swing high at 5, got {swings}"
    print(f"  ✓ swing high detected at expected index")


def test_indicators_resample():
    from strategies.indicators import resample
    # 10 1-min candles → 2 5-min candles
    candles = [_candle(_ts(offset_min=i), 100+i, 101+i, 99+i, 100+i, 100) for i in range(10)]
    out = resample(candles, 5)
    assert len(out) == 2
    # First 5-min candle: open of first 1-min, close of 5th, sum volume
    assert out[0].open == 100
    assert out[0].close == 104  # 5th candle close
    assert out[0].volume == 500
    print(f"  ✓ resample 1-min × 10 → 5-min × 2")


def test_option_utils_strikes():
    from strategies.option_utils import select_atm_strike
    # Spot 24038, low VIX → 1 strike OTM
    s_low_ce = select_atm_strike(24038, vix=14, is_expiry_day=False, direction="CE")
    s_low_pe = select_atm_strike(24038, vix=14, is_expiry_day=False, direction="PE")
    # ATM is round(24038/50)*50 = 24050, OTM CE = 24100, OTM PE = 24000
    assert s_low_ce == 24100, f"got {s_low_ce}"
    assert s_low_pe == 24000, f"got {s_low_pe}"
    # High VIX → ATM
    s_hi_ce = select_atm_strike(24038, vix=20, is_expiry_day=False, direction="CE")
    assert s_hi_ce == 24050
    # Expiry day → ATM regardless
    s_exp = select_atm_strike(24038, vix=14, is_expiry_day=True, direction="CE")
    assert s_exp == 24050
    print("  ✓ ATM rounding, VIX-based OTM offset, expiry-day override")


def test_option_utils_qty():
    from strategies.option_utils import calculate_option_qty
    # ₹250K capital, 1% risk, 35% SL on ₹120 premium, lot 65
    # risk = 2500, max_loss_per_unit = 42, raw_qty = 59 → 0 lots → 0
    qty = calculate_option_qty(120, 0.35, 250000, 1, lot_size=65)
    assert qty == 0  # below 1 lot
    # ₹500K capital, 1% risk → 5000/42 = 119 → 1 lot of 65
    qty2 = calculate_option_qty(120, 0.35, 500000, 1, lot_size=65)
    assert qty2 == 65, f"got {qty2}"
    # High VIX multiplier 0.5 → halves
    qty3 = calculate_option_qty(120, 0.35, 500000, 1, lot_size=65, high_vix_multiplier=0.5)
    assert qty3 == 0, f"got {qty3} (should round down to 0 lots after 0.5x)"
    print(f"  ✓ qty rounds to lots, returns 0 when below 1 lot")


def test_option_position_size_in_risk_manager():
    from core.risk_manager import GlobalRiskManager
    rm = GlobalRiskManager({"max_daily_loss": 15000, "max_total_exposure": 500000})
    rm.register_strategy("test", {
        "capital_allocation": 500000, "risk_per_trade_pct": 1.0,
        "daily_loss_cap": 7500, "max_positions": 1,
    })
    # Premium 120, SL 35% → max loss/unit = 42 → 5000/42 = 119 raw → 1 lot of 65
    qty = rm.calculate_option_position_size("test", 120, 0.35, lot_size=65)
    assert qty == 65, f"got {qty}"
    print("  ✓ risk_manager.calculate_option_position_size returns lot multiple")


def test_position_manager_avg_cost():
    from strategies.position_manager import ScaledPosition, ScalingConfig
    pos = ScaledPosition(
        strategy_name="test", trade_id="T-1", direction="bullish",
        initial_premium=100, initial_qty=10,
        invalidation_level=24000, invalidation_direction="below",
        scaling_config=ScalingConfig(profile="averaging"),
    )
    now = datetime.now()
    pos.add_entry(10, 100, now, kind="initial")
    pos.add_entry(5, 80, now + timedelta(minutes=5), kind="average")
    pos.add_entry(5, 65, now + timedelta(minutes=10), kind="average")
    # Avg = (10*100 + 5*80 + 5*65) / 20 = (1000 + 400 + 325) / 20 = 86.25
    assert abs(pos.avg_cost - 86.25) < 0.01, f"got {pos.avg_cost}"
    assert pos.net_qty == 20
    assert pos.total_size_mult == 2.0
    print(f"  ✓ avg cost 3 entries: ₹{pos.avg_cost:.2f}")


def test_position_manager_profit_ladder():
    from strategies.position_manager import ScaledPosition, ScalingConfig
    cfg = ScalingConfig(profile="profit_pyramid")
    pos = ScaledPosition("test", "T-1", "bullish", 100, 100,
                         24000, "below", cfg)
    pos.add_entry(100, 100, datetime.now(), kind="initial")
    # avg = 100. T1 at 1.5x = 150
    qty, idx = pos.should_take_profit(150)
    assert idx == 0, f"expected T1 (idx=0), got {idx}"
    assert qty == 33, f"expected 33% of 100, got {qty}"
    pos.targets_hit.add(0)
    pos.add_exit(33, 150, datetime.now(), "target_0")
    # T2 at 2.0x = 200, on remaining 67
    qty2, idx2 = pos.should_take_profit(200)
    assert idx2 == 1
    assert qty2 == int(round(67 * 0.33))
    print(f"  ✓ profit ladder: T1={qty}@idx0, T2={qty2}@idx1")


def test_position_manager_hard_stop():
    from strategies.position_manager import ScaledPosition, ScalingConfig
    pos = ScaledPosition("test", "T-1", "bullish", 100, 100,
                         24000, "below", ScalingConfig())
    pos.add_entry(100, 100, datetime.now())
    # hard stop = 45% below 100 = 55
    assert not pos.hit_hard_stop(60)
    assert pos.hit_hard_stop(54)
    assert pos.hit_hard_stop(50)
    print("  ✓ hard stop fires at 45% below avg cost")


def test_position_manager_invalidation():
    from strategies.position_manager import ScaledPosition, ScalingConfig
    pos = ScaledPosition("test", "T-1", "bullish", 100, 100,
                         24000, "below", ScalingConfig())
    pos.add_entry(100, 100, datetime.now())
    assert pos.is_invalidated(23990)  # below
    assert not pos.is_invalidated(24010)  # above

    pos2 = ScaledPosition("test", "T-2", "bearish", 100, 100,
                          24000, "above", ScalingConfig())
    pos2.add_entry(100, 100, datetime.now())
    assert pos2.is_invalidated(24010)
    assert not pos2.is_invalidated(23990)
    print("  ✓ invalidation fires correctly for bullish (below) and bearish (above)")


def test_position_manager_averaging_gates():
    from strategies.position_manager import ScaledPosition, ScalingConfig
    cfg = ScalingConfig(profile="averaging",
                        no_averaging_cutoff_time=time(14, 0),
                        no_averaging_after_minutes=60)
    initial_ts = datetime(2026, 4, 28, 10, 0)  # 10 AM
    pos = ScaledPosition("test", "T-1", "bullish", 100, 100,
                         24000, "below", cfg, initial_entry_ts=initial_ts)
    pos.add_entry(100, 100, initial_ts)

    # profile=profit_pyramid → never averages
    cfg_pp = ScalingConfig(profile="profit_pyramid")
    pos_pp = ScaledPosition("test", "T-2", "bullish", 100, 100,
                            24000, "below", cfg_pp, initial_entry_ts=initial_ts)
    pos_pp.add_entry(100, 100, initial_ts)
    assert pos_pp.should_average(75, 24050, initial_ts + timedelta(minutes=5)) == 0

    # averaging profile, premium below 80% trigger, all gates pass
    qty = pos.should_average(75, 24050, initial_ts + timedelta(minutes=5))
    assert qty == 50, f"got {qty}"  # 100 × 0.5

    # Time gate: > 60 min from initial → no add
    qty2 = pos.should_average(75, 24050, initial_ts + timedelta(minutes=70))
    assert qty2 == 0

    # Cutoff time gate: at 14:30 → no add
    qty3 = pos.should_average(75, 24050, initial_ts.replace(hour=14, minute=30))
    assert qty3 == 0

    # Invalidation gate: underlying below 24000 → no add
    qty4 = pos.should_average(75, 23900, initial_ts + timedelta(minutes=5))
    assert qty4 == 0

    print("  ✓ averaging gates: profile, premium, time, cutoff, invalidation")


def test_position_manager_journal_round_trip():
    from strategies.position_manager import ScaledPosition, ScalingConfig, reconstruct_from_journal
    from core.trade_journal import TradeJournal
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db = tf.name
    try:
        j = TradeJournal(db)
        tid = TradeJournal.generate_trade_id("RC")
        j.record_entry(tid, "test", "NIFTY28APR2624000CE", "12345",
                       "BUY", 65, 120.0, fees=10)
        j.record_exit(tid, 180.0, 22, "target_0", fees=8)

        pos = reconstruct_from_journal("test", tid, j, ScalingConfig())
        assert pos is not None
        assert pos.initial_qty == 65
        assert pos.initial_premium == 120
        # 1 entry replayed + 1 exit replayed
        assert len(pos.entries) == 1
        assert len(pos.exits) == 1
        assert pos.net_qty == 43
        j.close()
        print("  ✓ journal round-trip preserves state (qty, premium, exits)")
    finally:
        os.unlink(db)


# ─── Runner ───
TESTS = [
    ("indicators.rsi", test_indicators_rsi),
    ("indicators.ema", test_indicators_ema),
    ("indicators.atr", test_indicators_atr),
    ("indicators.vwap (anchored)", test_indicators_vwap_anchor),
    ("indicators.kaufman_efficiency_ratio", test_indicators_ker),
    ("indicators.compute_volume_profile", test_indicators_volume_profile),
    ("indicators.swing_highs_lows", test_indicators_swing_detection),
    ("indicators.resample", test_indicators_resample),
    ("option_utils.select_atm_strike", test_option_utils_strikes),
    ("option_utils.calculate_option_qty", test_option_utils_qty),
    ("risk_manager.calculate_option_position_size", test_option_position_size_in_risk_manager),
    ("position_manager.avg_cost", test_position_manager_avg_cost),
    ("position_manager.should_take_profit (ladder)", test_position_manager_profit_ladder),
    ("position_manager.hit_hard_stop", test_position_manager_hard_stop),
    ("position_manager.is_invalidated", test_position_manager_invalidation),
    ("position_manager.should_average gates", test_position_manager_averaging_gates),
    ("position_manager.reconstruct_from_journal", test_position_manager_journal_round_trip),
]


def main():
    print("=" * 60)
    print("Phase 1 infrastructure tests")
    print("=" * 60)
    failed = []
    for name, fn in TESTS:
        try:
            print(f"\n[{name}]")
            fn()
        except AssertionError as e:
            print(f"  ✗ FAIL: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed.append(name)
    print("\n" + "=" * 60)
    if failed:
        print(f"FAILED: {len(failed)}/{len(TESTS)} — {failed}")
        sys.exit(1)
    else:
        print(f"PASSED: {len(TESTS)}/{len(TESTS)} ✅")


if __name__ == "__main__":
    main()
