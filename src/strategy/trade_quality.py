"""
trade_quality.py — Autonomous Trade Quality Scorer (entry side).

Replaces binary gate logic with a continuous quality score that reads market
physics: where price has come from, how fast it is moving, what structure is
around it, and how well all timeframes agree.

No hard-coded distance thresholds. All components are ATR-normalized so the
same formula works across XAUUSD, USDCHF, AUDUSD at 3am and 2pm.

Components
----------
1. Momentum quality (30%)
   Tick velocity + acceleration, normalized against the instrument's ATR.
   Decisive, directional momentum = high score.
   Choppy / bidirectional ticks = low score.

2. Structural entry quality (30%)
   Is price entering at or near a key structural level (swing high/low,
   BOS retest zone)? Entry at a level = natural stop placement, better R:R.
   Entry mid-range = arbitrary stop, poor R:R.

3. Alignment quality (25%)
   How strongly aligned are M1 structure + M5 context?
   Uses the existing ScalperAlignmentResult score directly.

4. Market condition quality (15%)
   Trending vs ranging regime (from M5 ATR ratio).
   Trending = better momentum follow-through.
   Ranging = higher false breakout risk.

Output: TradeQuality 0.0–1.0. Minimum entry threshold is dynamic:
   min_quality = base_min + (1.0 - signal_confidence) * 0.10
so a weaker signal requires a higher-quality entry to compensate.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from src.domain.entities import Direction, Timeframe

if TYPE_CHECKING:
    from src.strategy.structure_state import StructureStateManager
    from src.strategy.scalper_alignment import ScalperAlignmentResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeQuality:
    """Full breakdown of entry quality computation."""
    score:             float   # 0.0–1.0 composite
    momentum_score:    float   # component 1
    structural_score:  float   # component 2
    alignment_score:   float   # component 3
    condition_score:   float   # component 4
    at_key_level:      bool    # True if price is at/near a swing level
    atr:               float   # ATR used for normalization
    detail:            str     # human-readable summary

    def passes_minimum(self, base_min: float | None = None) -> bool:
        """
        Deterministic quality floor: composite must clear a single static
        threshold. No coupling to other scores — the floor means exactly one
        thing and a rejection is explainable from this number alone.

        (Phase 0.5: removed the dynamic confidence-coupled minimum. It varied
        the floor by only ±0.015 around TQ_BASE_MIN while making "why did GATE7
        reject" depend on a second score. Determinism > negligible adaptation.)
        """
        if base_min is None:
            import src.config as _cfg
            base_min = getattr(_cfg, "TQ_BASE_MIN", 0.60)
        return self.score >= base_min


def compute_trade_quality(
    direction:   Direction,
    price:       float,
    tick_velocity: float,         # from TickAnalyzer.analyze().velocity
    tick_acceleration: float,     # from TickAnalyzer.analyze().acceleration
    tick_imbalance: float,        # from TickAnalyzer.analyze()  (signed, direction-aware)
    alignment:   "ScalperAlignmentResult",
    structure:   Optional["StructureStateManager"],
    m1_candles:  list,
    m5_candles:  list,
) -> TradeQuality:
    """
    Compute entry quality for a candidate trade.

    All inputs come from data already computed during evaluate() — no extra
    market calls needed.
    """

    # ── ATR baseline ──────────────────────────────────────────────────────────
    atr = _compute_atr(m1_candles, period=14)
    m5_atr = _compute_atr(m5_candles, period=14) if m5_candles else 0.0
    if atr <= 0:
        atr = m5_atr if m5_atr > 0 else 0.0001  # fallback so we don't divide by zero

    # ── Component 1: Momentum quality (30%) ──────────────────────────────────
    momentum_score = _momentum_quality(
        direction, tick_velocity, tick_acceleration, tick_imbalance, atr
    )

    # ── Component 2: Structural entry quality (30%) ───────────────────────────
    structural_score, at_key_level = _structural_entry_quality(
        direction, price, structure, atr
    )

    # ── Component 3: Alignment quality (25%) ─────────────────────────────────
    # Use the composite alignment score directly (already 0.0–1.0)
    alignment_score = float(alignment.score)

    # ── Component 4: Market condition quality (15%) ───────────────────────────
    condition_score = _condition_quality(alignment, m5_candles)

    # ── Composite ─────────────────────────────────────────────────────────────
    composite = (
        momentum_score   * 0.30 +
        structural_score * 0.30 +
        alignment_score  * 0.25 +
        condition_score  * 0.15
    )
    composite = round(max(0.0, min(1.0, composite)), 3)

    detail = (
        f"mom={momentum_score:.2f} struct={structural_score:.2f}(key={at_key_level}) "
        f"align={alignment_score:.2f} cond={condition_score:.2f} atr={atr:.5f}"
    )

    logger.debug("TRADE_QUALITY %s | %s | composite=%.3f", direction.value, detail, composite)

    return TradeQuality(
        score=composite,
        momentum_score=momentum_score,
        structural_score=structural_score,
        alignment_score=alignment_score,
        condition_score=condition_score,
        at_key_level=at_key_level,
        atr=atr,
        detail=detail,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _compute_atr(candles: list, period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    bars = candles[-period:] if len(candles) >= period else candles
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        c  = bars[i]
        trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _momentum_quality(
    direction:    Direction,
    velocity:     float,
    acceleration: float,
    imbalance:    float,
    atr:          float,
) -> float:
    """
    Score the quality of the momentum driving this entry.

    High quality: strong velocity + positive acceleration + confirmed imbalance.
    Low quality: weak velocity, choppy, or decelerating.
    """
    # Normalize velocity against ATR/minute
    # ATR per M1 bar ~ atr; typical fast move = 0.5*atr in one minute
    atr_per_min = atr  # M1 ATR already is per-minute
    if atr_per_min > 0:
        norm_vel = abs(velocity) / (atr_per_min * 0.5 + 1e-10)
    else:
        norm_vel = 0.0

    # velocity score: tanh maps to 0.5-1.0 range; 0 velocity → 0.5 (neutral)
    vel_score = 0.5 + 0.5 * math.tanh(norm_vel * 1.5)

    # Acceleration: if accelerating in trade direction, bonus
    # imbalance is already direction-aware (positive = in trade direction)
    accel_bonus = 0.0
    if imbalance > 0.1:    # tick imbalance confirms direction
        accel_bonus = min(0.10, imbalance * 0.15)
    elif imbalance < -0.1: # imbalance opposes — penalty
        accel_bonus = max(-0.10, imbalance * 0.15)

    score = min(1.0, max(0.0, vel_score + accel_bonus))

    # Deceleration penalty: negative acceleration means momentum fading
    if acceleration < -0.3:
        score = max(0.0, score - 0.10)

    return round(score, 3)


def _structural_entry_quality(
    direction: Direction,
    price:     float,
    structure: Optional["StructureStateManager"],
    atr:       float,
) -> tuple[float, bool]:
    """
    How close is the entry to a meaningful structural level?

    Entering at a swing level = tight, logical SL placement = good R:R.
    Entering mid-range = arbitrary stop = poor R:R.

    Returns (score 0.0-1.0, at_key_level bool).
    """
    if structure is None:
        return 0.50, False  # neutral if no structure data

    try:
        # Look for any swing level within 1.5 ATR of current price
        proximity_buffer = atr * 1.5

        if direction == Direction.BULLISH:
            # Best entry: at or just above a swing low
            swing_lows = structure.get_swing_lows(Timeframe.M1)
            for sw in swing_lows:
                dist = abs(price - sw.price)
                if dist <= proximity_buffer:
                    # How close? Closer = better
                    closeness = 1.0 - (dist / proximity_buffer)
                    return round(0.60 + closeness * 0.40, 3), True
            # Check M5 swing lows as secondary reference
            swing_lows_m5 = structure.get_swing_lows(Timeframe.M5)
            for sw in swing_lows_m5:
                dist = abs(price - sw.price)
                if dist <= proximity_buffer * 1.5:
                    closeness = 1.0 - (dist / (proximity_buffer * 1.5))
                    return round(0.50 + closeness * 0.30, 3), True
        else:
            # Best entry: at or just below a swing high
            swing_highs = structure.get_swing_highs(Timeframe.M1)
            for sw in swing_highs:
                dist = abs(price - sw.price)
                if dist <= proximity_buffer:
                    closeness = 1.0 - (dist / proximity_buffer)
                    return round(0.60 + closeness * 0.40, 3), True
            swing_highs_m5 = structure.get_swing_highs(Timeframe.M5)
            for sw in swing_highs_m5:
                dist = abs(price - sw.price)
                if dist <= proximity_buffer * 1.5:
                    closeness = 1.0 - (dist / (proximity_buffer * 1.5))
                    return round(0.50 + closeness * 0.30, 3), True
    except Exception:
        pass

    # Mid-range entry — no structural reference nearby
    return 0.30, False


def _condition_quality(
    alignment:  "ScalperAlignmentResult",
    m5_candles: list,
) -> float:
    """
    Market condition: trending = higher quality, ranging = lower quality.

    Uses the regime detected by ScalperAlignmentEngine (already computed).
    """
    from src.strategy.scalper_alignment import Regime

    if alignment.regime == Regime.TRENDING:
        # In a trend: slope strength matters
        # Strong slope = better continuation probability
        if m5_candles and len(m5_candles) >= 14:
            atr = _compute_atr(m5_candles, 14)
            norm_slope = abs(alignment.m5_slope) / (atr / 5.0 + 1e-10)  # per-bar normalized
            return round(min(1.0, 0.65 + math.tanh(norm_slope * 2.0) * 0.35), 3)
        return 0.75  # trending but no ATR data
    else:
        # Ranging: lower quality but not zero — mean-reversion setups can still work
        return 0.35
