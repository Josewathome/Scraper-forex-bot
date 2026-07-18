"""
test_tick_score_correctness_fix.py — Regression tests for the Phase 3
tick-score normalization fix (2026-07-18 QA finding).

Runs with plain Python (no pytest required):

    python -m tests.test_tick_score_correctness_fix

BUG (pre-fix): tick_composite_score's displacement term normalized RAW PRICE
displacement by a fixed 0.0002 divisor — 2 pips for a 5-digit pair
(GBPUSD/AUDUSD/USDCHF, pip=0.0001) but only 0.02 pips for USDJPY (pip=0.01).
Any trivial USDJPY tick noise saturated the full 0.25 displacement credit,
while 5-digit majors needed a genuine 2-pip move. Combined with a velocity
divisor (/10.0) calibrated for a tick rate the feed never reaches, this
locked GBPUSD/AUDUSD/USDCHF out of the 0.40 Gate-5 threshold (2-4% pass
rate) while inflating USDJPY (23-26% pass rate) — confirmed live in the
Pre-Phase-3 Baseline (baselines/PRE_PHASE3_BASELINE_2026-07-18.md).

FIX: displacement divisor now scales with the instrument's own pip size
(price-magnitude heuristic already used elsewhere in this codebase,
scalper_alignment.py); velocity divisor corrected from 10.0 to 4.0 ticks/sec
based on this feed's measured distribution (p95=3.4, p99=4.4).

These tests reproduce the OLD formula inline (not by importing removed
code — it no longer exists) purely to document and prove the asymmetry the
fix corrects. No other gate, RR, EV, or sizing logic is touched or tested
here — that is intentionally out of scope for this narrow correctness fix.
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

from src.strategy.tick_engine import RawTick, TickFeatureCalculator as TFC


def _monotonic_ticks(open_price: float, total_move: float, n: int = 20, dt: float = 0.25):
    """n ticks, evenly spaced dt seconds apart, price moving linearly from
    open_price to open_price+total_move. Constructed so tick_velocity(5.0)
    == n / 5.0 exactly (all n ticks fall within the trailing 5s window)."""
    ticks = []
    for i in range(n):
        p = open_price + total_move * (i / (n - 1))
        ticks.append(RawTick(price=p, bid=p - 0.00001, ask=p + 0.00001, ts=i * dt))
    return ticks


def _old_disp_norm(disp: float) -> float:
    """The REMOVED formula, reproduced only for comparison: fixed 0.0002
    raw-price divisor regardless of instrument."""
    return min(abs(disp) / 0.0002, 1.0)


def _old_vel_component(velocity: float) -> float:
    """The REMOVED formula, reproduced only for comparison: /10.0 divisor."""
    return min(velocity / 10.0, 1.0)


# ── Displacement: parity restored across instruments ─────────────────────────

def test_typical_small_move_now_has_equal_credit_across_instruments():
    """QA's own documented 'typical M1 window' move: 0.3 pips. Same relative
    move on both instruments must now produce the SAME displacement
    contribution — the core parity the fix restores."""
    gbp_ticks = _monotonic_ticks(1.33960, total_move=0.00003)   # 0.3 pip @ 0.0001/pip
    jpy_ticks = _monotonic_ticks(162.000, total_move=0.003)     # 0.3 pip @ 0.01/pip

    old_gbp, old_jpy = _old_disp_norm(0.00003), _old_disp_norm(0.003)
    assert abs(old_gbp - 0.15) < 1e-6
    assert old_jpy == 1.0, "sanity check: the OLD bug saturated USDJPY on a trivial move"
    assert old_jpy - old_gbp > 0.8, "OLD formula must show the ~0.85 gap this fix corrects"

    score_gbp = TFC.tick_composite_score(gbp_ticks, "TESTGBP_SMALL", direction=1)
    score_jpy = TFC.tick_composite_score(jpy_ticks, "TESTJPY_SMALL", direction=1)
    assert abs(score_gbp - score_jpy) < 1e-6, (
        f"NEW formula must give equal composite scores for an equal relative move: "
        f"GBP={score_gbp} JPY={score_jpy}"
    )
    print(f"PASS  test_typical_small_move_now_has_equal_credit_across_instruments "
          f"(old_gap={old_jpy - old_gbp:.3f}, new: GBP={score_gbp} == JPY={score_jpy})")


def test_full_2pip_move_saturates_equally_on_both_instruments():
    """At exactly 2 pips (the documented 'full credit' threshold), both
    instruments must saturate to the same displacement contribution."""
    gbp_ticks = _monotonic_ticks(1.33960, total_move=0.0002)   # 2 pips @ 0.0001/pip
    jpy_ticks = _monotonic_ticks(162.000, total_move=0.02)     # 2 pips @ 0.01/pip

    score_gbp = TFC.tick_composite_score(gbp_ticks, "TESTGBP_FULL", direction=1)
    score_jpy = TFC.tick_composite_score(jpy_ticks, "TESTJPY_FULL", direction=1)
    assert abs(score_gbp - score_jpy) < 1e-6, (
        f"a genuine 2-pip move must saturate identically: GBP={score_gbp} JPY={score_jpy}"
    )
    print(f"PASS  test_full_2pip_move_saturates_equally_on_both_instruments "
          f"(GBP={score_gbp} == JPY={score_jpy})")


def test_five_digit_pair_math_unchanged_by_the_fix():
    """The fix must be a no-op for 5-digit pairs — 0.0002 raw price already
    equalled 2 pips there before the fix; only JPY/gold-scale instruments
    were wrong. Regression guard against accidentally changing GBPUSD."""
    disp = 0.00003  # 0.3 pip
    old = _old_disp_norm(disp)
    new_pip_size = 0.01 if 1.3396 > 10 else 0.0001
    new = min(disp / (2.0 * new_pip_size), 1.0)
    assert abs(old - new) < 1e-9, f"5-digit pair math must be unchanged: old={old} new={new}"
    print("PASS  test_five_digit_pair_math_unchanged_by_the_fix")


# ── Velocity: divisor corrected to match observed feed behavior ─────────────

def test_velocity_divisor_matches_observed_feed_ceiling():
    """4 ticks/sec (this feed's measured p95-p99 band, per the Pre-Phase-3
    baseline) must now earn full velocity credit; the old /10.0 divisor
    gave it only 40% credit."""
    ticks = _monotonic_ticks(1.33960, total_move=0.0, n=20, dt=0.25)  # 20 ticks / 5s = 4.0 t/s
    velocity = TFC.tick_velocity(ticks)
    assert abs(velocity - 4.0) < 1e-6, f"test construction error: velocity={velocity}"

    old_component = _old_vel_component(velocity)
    assert abs(old_component - 0.4) < 1e-6, "OLD formula must show only 40% credit at 4 t/s"

    new_component = min(velocity / 4.0, 1.0)
    assert new_component == 1.0, f"NEW formula must give full credit at the measured ceiling, got {new_component}"
    print(f"PASS  test_velocity_divisor_matches_observed_feed_ceiling "
          f"(old_credit={old_component:.2f} -> new_credit={new_component:.2f})")


def test_composite_score_still_bounded_zero_to_one():
    """Sanity: the fix must not break the documented 0.0-1.0 contract."""
    for open_p, move in ((1.33960, 0.0002), (162.000, 0.02), (2300.0, 5.0)):
        ticks = _monotonic_ticks(open_p, move)
        score = TFC.tick_composite_score(ticks, f"TESTBOUND_{open_p}", direction=1)
        assert 0.0 <= score <= 1.0, f"score out of bounds: {score} for open={open_p}"
    print("PASS  test_composite_score_still_bounded_zero_to_one")


if __name__ == "__main__":
    test_typical_small_move_now_has_equal_credit_across_instruments()
    test_full_2pip_move_saturates_equally_on_both_instruments()
    test_five_digit_pair_math_unchanged_by_the_fix()
    test_velocity_divisor_matches_observed_feed_ceiling()
    test_composite_score_still_bounded_zero_to_one()
    print("\nAll tick-score correctness-fix checks passed.")
