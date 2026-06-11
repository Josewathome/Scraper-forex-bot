"""
recovery_scorer.py — Autonomous Recovery Conviction Scorer.

Replaces hard-coded exit thresholds with market-physics-based conviction.
Three ATR-normalized dimensions tell us whether a struggling trade deserves
to be held, or whether the market is genuinely moving against it.

Components
----------
1. Momentum trajectory (40%)
   Tick velocity + acceleration toward/away from SL, normalized against
   the instrument's own ATR so a 3-pip USDCHF move ≠ a 3-pip XAUUSD move.

2. Structural barrier vote (35%)
   Is there a confirmed swing level between current price and the SL?
   If yes — the market has to break that structure to hit our stop.
   If no  — price is in free air between entry and SL.

3. Continuation score velocity (25%)
   Rate-of-change of the continuation score over the last N bars.
   A rising cont score = thesis recovering.
   A falling cont score = thesis deteriorating.

Output: RecoveryConviction 0.0–1.0 (no fixed exit thresholds).
The caller decides what to do with the score; this module only computes it.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from src.domain.entities import Direction, Timeframe
from src.domain.value_objects import PipCalculator

if TYPE_CHECKING:
    from src.strategy.structure_state import StructureStateManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryConviction:
    """Full breakdown of the recovery conviction computation."""
    score:              float   # 0.0–1.0 composite
    momentum_score:     float   # component 1 — are ticks moving toward TP?
    structural_score:   float   # component 2 — swing barrier between price & SL?
    velocity_score:     float   # component 3 — is cont score recovering?
    momentum_raw:       float   # raw ATR-normalized tick velocity
    barrier_exists:     bool    # True if swing level found between price and SL
    cont_velocity:      float   # cont score slope (positive = recovering)
    detail:             str     # human-readable summary


def compute_recovery_conviction(
    direction:          Direction,
    entry:              float,
    current_sl:         float,
    price:              float,
    recent_prices:      List[float],    # last 10 tick prices (oldest first)
    cont_score_history: List[float],    # last N continuation scores (oldest first)
    structure:          "StructureStateManager",
    m1_candles:         list,           # for ATR computation
    pip_calc:           PipCalculator,
) -> RecoveryConviction:
    """
    Compute recovery conviction for an open trade.

    Parameters
    ----------
    direction          : trade direction
    entry              : entry price
    current_sl         : current stop-loss level
    price              : latest tick price
    recent_prices      : last 10 tick prices (oldest→newest)
    cont_score_history : last 5 continuation scores (oldest→newest)
    structure          : live StructureStateManager for swing levels
    m1_candles         : recent M1 candles for ATR normalization
    pip_calc           : pip calculator for the instrument
    """

    # ── ATR baseline ──────────────────────────────────────────────────────────
    # Use last 14 M1 bars as ATR denominator so all scores are relative
    # to the instrument's own recent volatility.
    atr = _compute_atr(m1_candles, period=14)
    if atr <= 0:
        # No ATR data yet — use SL distance as proxy
        atr = abs(entry - current_sl) or pip_calc.price_to_pips(1.0)

    # ── Component 1: Momentum trajectory (40%) ───────────────────────────────
    # Measure net tick displacement over the last N ticks, normalized against ATR.
    # Positive = price moving toward TP (good).
    # Negative = price moving toward SL (bad).
    momentum_score, momentum_raw = _momentum_score(
        direction, recent_prices, atr, pip_calc
    )

    # ── Component 2: Structural barrier vote (35%) ────────────────────────────
    # Does a confirmed swing level sit between current price and SL?
    structural_score, barrier_exists = _structural_barrier_score(
        direction, price, current_sl, structure
    )

    # ── Component 3: Continuation score velocity (25%) ───────────────────────
    # Rate-of-change (slope) of cont score over last N bars.
    # Rising cont = recovering thesis; falling cont = deteriorating.
    velocity_score, cont_velocity = _cont_velocity_score(cont_score_history)

    # ── Composite ─────────────────────────────────────────────────────────────
    composite = (
        momentum_score    * 0.40 +
        structural_score  * 0.35 +
        velocity_score    * 0.25
    )
    composite = round(max(0.0, min(1.0, composite)), 3)

    detail = (
        f"mom={momentum_score:.2f}(raw={momentum_raw:+.3f}atr) "
        f"struct={structural_score:.2f}(barrier={barrier_exists}) "
        f"vel={velocity_score:.2f}(slope={cont_velocity:+.3f})"
    )

    logger.debug("RECOVERY_CONVICTION %s | %s | composite=%.3f", direction.value, detail, composite)

    return RecoveryConviction(
        score=composite,
        momentum_score=momentum_score,
        structural_score=structural_score,
        velocity_score=velocity_score,
        momentum_raw=momentum_raw,
        barrier_exists=barrier_exists,
        cont_velocity=cont_velocity,
        detail=detail,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _compute_atr(candles: list, period: int = 14) -> float:
    """Average True Range over last `period` bars."""
    if len(candles) < 2:
        return 0.0
    bars = candles[-period:] if len(candles) >= period else candles
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        c  = bars[i]
        trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _momentum_score(
    direction:     Direction,
    recent_prices: List[float],
    atr:           float,
    pip_calc:      PipCalculator,
) -> tuple[float, float]:
    """
    Net tick displacement normalized against ATR.

    Returns (score 0.0-1.0, raw_normalized_displacement).
    Score 1.0 = price strongly moving toward TP.
    Score 0.0 = price strongly moving toward SL.
    Score 0.5 = flat/neutral.
    """
    if len(recent_prices) < 2:
        return 0.50, 0.0

    # Net move over all recent ticks
    net = recent_prices[-1] - recent_prices[0]
    if direction == Direction.BEARISH:
        net = -net  # positive = toward TP for both directions

    # Normalize against ATR (full ATR of travel = ±1.0 in ATR units)
    if atr > 0:
        normalized = net / atr
    else:
        normalized = 0.0

    # Map to 0-1 score using tanh so extreme values don't dominate
    # tanh(1.0)=0.76, tanh(2.0)=0.96 — provides smooth saturation
    score = 0.5 + 0.5 * math.tanh(normalized * 3.0)

    # Weight by number of ticks in the window (more ticks = more reliable)
    reliability = min(1.0, len(recent_prices) / 8.0)
    score = 0.5 + (score - 0.5) * reliability

    return round(score, 3), round(normalized, 4)


def _structural_barrier_score(
    direction:  Direction,
    price:      float,
    sl:         float,
    structure:  "StructureStateManager",
) -> tuple[float, bool]:
    """
    Check whether a confirmed swing level exists between current price and SL.

    A swing between price and SL = structural barrier the market must break
    before our stop is threatened = higher confidence in holding.

    Returns (score 0.0-1.0, barrier_found).
    """
    try:
        if direction == Direction.BULLISH:
            # For long trades: SL is below price.
            # We want a swing low between price and SL.
            swing_lows = structure.get_swing_lows(Timeframe.M1)
            for sw in swing_lows:
                if sl < sw.price < price:
                    return 0.80, True
            # Check M5 as fallback
            swing_lows_m5 = structure.get_swing_lows(Timeframe.M5)
            for sw in swing_lows_m5:
                if sl < sw.price < price:
                    return 0.65, True
        else:
            # For short trades: SL is above price.
            # We want a swing high between price and SL.
            swing_highs = structure.get_swing_highs(Timeframe.M1)
            for sw in swing_highs:
                if price < sw.price < sl:
                    return 0.80, True
            swing_highs_m5 = structure.get_swing_highs(Timeframe.M5)
            for sw in swing_highs_m5:
                if price < sw.price < sl:
                    return 0.65, True
    except Exception:
        pass

    # No barrier — free air between price and SL
    return 0.20, False


def _cont_velocity_score(cont_history: List[float]) -> tuple[float, float]:
    """
    Compute slope of continuation scores over the last N bars.

    Rising slope = thesis is recovering → high conviction.
    Flat/falling = thesis deteriorating → low conviction.

    Returns (score 0.0-1.0, slope).
    """
    if len(cont_history) < 2:
        return 0.50, 0.0  # not enough data — neutral

    # Simple linear regression slope
    n = len(cont_history)
    x_mean = (n - 1) / 2.0
    y_mean = sum(cont_history) / n

    numerator   = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(cont_history))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    slope = numerator / denominator if denominator > 0 else 0.0

    # Scale slope to score:
    # slope = +0.05/bar → recovering well → score ~0.75
    # slope = 0 → flat → score = 0.50
    # slope = -0.05/bar → deteriorating → score ~0.25
    # tanh(slope * 10) maps ±0.1 slope → ±0.76
    score = 0.5 + 0.5 * math.tanh(slope * 10.0)

    return round(score, 3), round(slope, 4)
