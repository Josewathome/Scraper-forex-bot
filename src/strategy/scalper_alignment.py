"""
scalper_alignment.py — M1/M5 momentum alignment for scalper mode.

Replaces the H1/H4/D1 structure alignment used in intraday mode with a
faster, lower-timeframe momentum consensus suitable for 4–8 pip targets.

Signal logic:
  1. M5 trend direction  — EMA(10) slope over last 10 closed M5 bars.
     Bullish if latest close > EMA and EMA is rising (slope > 0).
     Bearish if latest close < EMA and EMA is falling (slope < 0).

  2. M1 momentum consensus — of the last 5 closed M1 bars, at least
     SCALPER_M1_CONSENSUS_MIN must close in the same direction as M5.
     Direction = bullish if close[i] > close[i-1], else bearish.

  3. Composite score = (M5_weight × m5_score) + (M1_weight × m1_score)
     where m5_score and m1_score are 0.0–1.0.

     M5 score: 0.0 (no trend), 0.5 (EMA aligned only), 1.0 (EMA + slope)
     M1 score: consensus_count / 5 bars (e.g. 4 of 5 → 0.80)

  4. Score threshold: 0.60 (configurable as SCALPER_MIN_ALIGNMENT_SCORE)

No zone dependency. No HTF structure wait. Fires on every M1 close.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

import enum

import src.config as config
from src.domain.entities import Candle, Direction, Timeframe
from src.strategy.structure_state import StructureState, StructureStateManager


class Regime(enum.Enum):
    TRENDING = "TRENDING"
    RANGING  = "RANGING"

logger = logging.getLogger(__name__)


# ── ScalperAlignmentResult ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScalperAlignmentResult:
    """
    Extended result carrying scalper-specific breakdown alongside the
    standard AlignmentResult interface (so entry_gate.py works unchanged).
    """
    score:         float
    direction:     Optional[Direction]
    regime:        Regime
    details:       Dict[str, str]
    m5_score:      float     # M5 EMA momentum score 0.0–1.0
    m1_score:      float     # M1 consensus score 0.0–1.0
    m5_ema:        float     # current EMA value
    m5_slope:      float     # EMA slope (positive = rising)
    m1_consensus:  int       # number of M1 bars agreeing with direction

    @property
    def is_valid(self) -> bool:
        min_score = getattr(config, "SCALPER_MIN_ALIGNMENT_SCORE", 0.60)
        return self.score >= min_score and self.direction is not None


# ── ScalperAlignmentEngine ────────────────────────────────────────────────────

class ScalperAlignmentEngine:
    """
    M1/M5 momentum alignment engine for scalper mode.

    One instance per symbol. Stateless across calls — all inputs come
    from the CandleBuilder cache passed at evaluation time.
    """

    # Weights: M1 structure is primary, M5 provides context filter.
    # Flipped from original (0.65/0.35) — M1-first scalper design.
    # M5 EMA(10) looks back 50 minutes which is intraday context, not
    # scalper context.  M1 structure (BOS/CHoCH) is the actual trigger.
    M1_WEIGHT: float = 0.65
    M5_WEIGHT: float = 0.35

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def get_alignment(
        self,
        m5_candles: List[Candle],
        m1_candles: List[Candle],
        structure:  Optional[StructureStateManager] = None,
    ) -> ScalperAlignmentResult:
        """
        Compute scalper alignment from M1 structure + M5 context.

        Parameters
        ----------
        m5_candles : closed M5 candles, newest last (need at least EMA_PERIOD + 2)
        m1_candles : last 20+ closed M1 candles, newest last
        structure  : StructureStateManager for this symbol — used to read real
                     M1 BOS/CHoCH state and swing points.  If None, falls back
                     to close-delta counting (legacy behaviour).
        """
        ema_period = getattr(config, "SCALPER_M5_EMA_PERIOD", 10)
        min_score = getattr(config, "SCALPER_MIN_ALIGNMENT_SCORE", 0.60)

        # ── M5 EMA analysis (context filter) ─────────────────────────
        m5_score, m5_direction, m5_ema, m5_slope = self._m5_score(
            m5_candles, ema_period
        )

        # ── M1 structure score (primary signal) ───────────────────────
        # Use real BOS/CHoCH from StructureStateManager when available.
        # Falls back to close-delta counting if structure manager not provided.
        m1_score, m1_direction, m1_consensus = self._m1_structure_score(
            m1_candles, structure
        )

        # ── Combine ───────────────────────────────────────────────────
        # M1 is primary (0.65 weight) — direction comes from M1 structure.
        # M5 is context (0.35 weight) — confirms or penalises M1 signal.
        raw_score = self.M1_WEIGHT * m1_score + self.M5_WEIGHT * m5_score

        # Direction: M1 structure drives direction.
        # M5 must not be actively opposing (if M5 is trending opposite, block).
        if m1_direction is not None:
            if m5_direction is not None and m5_direction != m1_direction:
                # M5 actively trending against M1 — block entry
                direction: Optional[Direction] = None
                raw_score = min(raw_score, min_score - 0.01)
            else:
                # M5 agrees or is flat — M1 direction wins
                direction = m1_direction
        elif m5_direction is not None and m5_score >= 0.85:
            # M1 inconclusive but M5 very strongly trending — follow M5
            direction = m5_direction
        else:
            direction = None
            raw_score = min(raw_score, min_score - 0.01)  # force block

        # Regime: use M5 ATR ratio as ranging detector (informational — no penalty)
        regime = self._detect_regime(m5_candles)
        # Ranging regime is logged for analytics; it does not reduce score.
        # The alignment threshold and tick velocity gate already suppress
        # low-momentum entries without an artificial score haircut.

        details = {
            "m1_score":    f"{m1_score:.2f}",
            "m1_dir":      m1_direction.value if m1_direction else "NONE",
            "m5_score":    f"{m5_score:.2f}",
            "m5_dir":      m5_direction.value if m5_direction else "NONE",
            "m5_ema":      f"{m5_ema:.5f}",
            "m5_slope":    f"{m5_slope:+.6f}",
            "m1_detail":   str(m1_consensus),
            "regime":      regime.value,
        }

        logger.info(
            "SCALPER_ALIGN [%s] | M1_BOS/CHoCH: score=%.2f dir=%s | M5_EMA: score=%.2f slope=%+.6f | combined=%.2f → %s | regime=%s",
            self.symbol,
            m1_score,
            m1_direction.value if m1_direction else "NONE",
            m5_score,
            m5_slope,
            raw_score,
            direction.value if direction else "BLOCKED",
            regime.value,
        )

        return ScalperAlignmentResult(
            score=round(raw_score, 3),
            direction=direction,
            regime=regime,
            details=details,
            m5_score=round(m5_score, 3),
            m1_score=round(m1_score, 3),
            m5_ema=round(m5_ema, 6),
            m5_slope=round(m5_slope, 8),
            m1_consensus=m1_consensus,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _m5_score(
        self,
        candles: List[Candle],
        ema_period: int,
    ) -> tuple:
        """
        Returns (score, direction, ema_value, slope).
        score is 0.0 (flat/no trend), 0.5 (EMA aligned only), 1.0 (EMA + slope).
        """
        if len(candles) < ema_period + 2:
            return 0.0, None, 0.0, 0.0

        closes = [c.close for c in candles]

        # Compute EMA
        ema = self._ema(closes, ema_period)
        if len(ema) < 2:
            return 0.0, None, 0.0, 0.0

        current_ema   = ema[-1]
        prev_ema      = ema[-2]
        current_close = closes[-1]
        slope         = current_ema - prev_ema

        # ATR-normalize the slope so near-zero slopes on slow instruments
        # don't masquerade as strong trends (e.g. USDCHF slope +0.000047).
        # Use last 14 bars for ATR; fall back to last close range if too few bars.
        atr = self._candle_atr(candles[-14:] if len(candles) >= 14 else candles)
        if atr > 0:
            # Slope per bar normalized against ATR.
            # |norm_slope| < 0.05 = flat; > 0.20 = strong trend.
            norm_slope = slope / atr
        else:
            norm_slope = 0.0

        # Direction from EMA position
        if current_close > current_ema:
            direction = Direction.BULLISH
            # Continuous score: 0.50 (flat/negligible slope) → 1.0 (strong slope).
            # tanh(norm_slope * 10) saturates at ±1 for |norm_slope| ≥ 0.3.
            if slope > 0:
                score = 0.50 + 0.50 * math.tanh(norm_slope * 10.0)
                score = max(0.50, min(1.0, score))
            else:
                # EMA above but slope falling — trend may be reversing
                score = max(0.20, 0.50 + 0.50 * math.tanh(norm_slope * 10.0))
        elif current_close < current_ema:
            direction = Direction.BEARISH
            if slope < 0:
                score = 0.50 + 0.50 * math.tanh(abs(norm_slope) * 10.0)
                score = max(0.50, min(1.0, score))
            else:
                score = max(0.20, 0.50 + 0.50 * math.tanh(norm_slope * 10.0))
        else:
            direction = None
            score = 0.0

        return score, direction, current_ema, slope

    @staticmethod
    def _candle_atr(candles: List) -> float:
        """Average True Range over a list of candles."""
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            pc = candles[i - 1].close
            c  = candles[i]
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    @staticmethod
    def _ema(values: List[float], period: int) -> List[float]:
        """Compute EMA over a list of floats. Returns list same length as input."""
        if len(values) < period:
            return []
        k = 2.0 / (period + 1)
        ema = [sum(values[:period]) / period]
        for v in values[period:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    @staticmethod
    def _m1_structure_score(
        candles:   List[Candle],
        structure: Optional[StructureStateManager],
    ) -> tuple:
        """
        Returns (score, direction, detail_count).

        Primary path — StructureStateManager available:
          Reads real M1 BOS/CHoCH state.
          BULLISH_TREND  → score 1.0, direction BULLISH
          BEARISH_TREND  → score 1.0, direction BEARISH
          STRUCTURE_BREAK→ score 0.80 in the BOS direction (momentum signal)
          MITIGATION_ZONE→ score 0.65 (fading back to broken level — valid scalp)
          RANGING        → score 0.20, direction None (no clear M1 bias)

          Additionally checks M1 swing proximity: if current price is within
          3 pips of the last M1 swing low (for longs) or swing high (for
          shorts), score gets a 0.10 bonus — price is at a structural level.

        Fallback path — no structure manager:
          Close-delta counting over last 5 M1 bars (original behaviour).
        """
        if structure is None:
            # Legacy fallback: close-delta counting
            if len(candles) < 2:
                return 0.0, None, 0
            recent = candles[-5:]
            up   = sum(1 for i in range(1, len(recent)) if recent[i].close > recent[i-1].close)
            down = sum(1 for i in range(1, len(recent)) if recent[i].close < recent[i-1].close)
            total = len(recent) - 1
            if total == 0:
                return 0.0, None, 0
            if up > down:
                score = up / total
                direction = Direction.BULLISH if up >= getattr(config, "SCALPER_M1_CONSENSUS_MIN", 3) else None
                return score, direction, up
            elif down > up:
                score = down / total
                direction = Direction.BEARISH if down >= getattr(config, "SCALPER_M1_CONSENSUS_MIN", 3) else None
                return score, direction, down
            return 0.5, None, 0

        # Primary path: use real M1 structure state
        from src.strategy.structure_state import StructureState
        m1_state = structure.get_state(Timeframe.M1)
        last_bos = structure._tf.get(Timeframe.M1)

        if m1_state == StructureState.BULLISH_TREND:
            score, direction = 1.0, Direction.BULLISH
        elif m1_state == StructureState.BEARISH_TREND:
            score, direction = 1.0, Direction.BEARISH
        elif m1_state == StructureState.STRUCTURE_BREAK:
            # Use BOS direction stored in the tracker
            bos_dir = last_bos.last_bos_direction if last_bos else None
            if bos_dir == Direction.BULLISH:
                score, direction = 0.80, Direction.BULLISH
            elif bos_dir == Direction.BEARISH:
                score, direction = 0.80, Direction.BEARISH
            else:
                score, direction = 0.50, None
        elif m1_state == StructureState.MITIGATION_ZONE:
            # Price returning to broken level — fade opportunity
            bos_dir = last_bos.last_bos_direction if last_bos else None
            # Fade: if bullish BOS, price retracing = buy the dip
            if bos_dir == Direction.BULLISH:
                score, direction = 0.65, Direction.BULLISH
            elif bos_dir == Direction.BEARISH:
                score, direction = 0.65, Direction.BEARISH
            else:
                score, direction = 0.40, None
        else:
            # RANGING — no M1 structural bias
            score, direction = 0.20, None

        # Swing proximity bonus: +0.10 if price is near a key M1 level
        if direction is not None and candles:
            current_price = candles[-1].close
            # Get pip size approximation from last candle (5-digit = 0.0001 pip)
            _pip = 0.01 if candles[-1].close > 10 else 0.0001  # JPY/Gold vs forex
            _proximity_pips = 3.0
            _buffer = _proximity_pips * _pip

            if direction == Direction.BULLISH:
                swing_low = structure.last_swing_low(Timeframe.M1)
                if swing_low and abs(current_price - swing_low.price) <= _buffer:
                    score = min(score + 0.10, 1.0)
            else:
                swing_high = structure.last_swing_high(Timeframe.M1)
                if swing_high and abs(current_price - swing_high.price) <= _buffer:
                    score = min(score + 0.10, 1.0)

        return round(score, 3), direction, int(score * 10)

    @staticmethod
    def _detect_regime(m5_candles: List[Candle]) -> Regime:
        """
        Quick ranging detector using M5 ATR ratio.
        current_atr (last 5 bars) vs baseline_atr (last 20 bars).
        < 0.50 ratio = ranging.
        """
        if len(m5_candles) < 20:
            return Regime.TRENDING

        def _atr(cs):
            trs = []
            for i in range(1, len(cs)):
                pc = cs[i - 1].close
                c  = cs[i]
                trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
            return sum(trs) / len(trs) if trs else 0.0

        current  = _atr(m5_candles[-5:])
        baseline = _atr(m5_candles[-20:])

        if baseline <= 0:
            return Regime.TRENDING

        ratio = current / baseline
        return Regime.RANGING if ratio < 0.50 else Regime.TRENDING
