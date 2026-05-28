"""
alignment.py — Phase 1: Multi-Timeframe Alignment Score & Regime Detector.

AlignmentScoreCalculator:
    Takes the structure states across all timeframes and produces a
    0.0–1.0 confidence score.  Requires ≥0.70 to allow entry evaluation.

RegimeDetector:
    Classifies the market as TRENDING or RANGING by comparing the current
    H1 ATR to its rolling baseline.  Ranging markets suppress position
    sizing and tighten entry requirements.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from src.domain.entities import Candle, Direction, Timeframe
from src.strategy.structure_state import StructureState, StructureStateManager

logger = logging.getLogger(__name__)


# ── Regime ────────────────────────────────────────────────────────────────────

class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING  = "RANGING"


# ── Alignment result ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AlignmentResult:
    score:     float              # 0.0 – 1.0
    direction: Optional[Direction]  # consensus direction, or None if split
    regime:    Regime
    details:   Dict[str, str]     # tf → state string for logging

    @property
    def is_valid(self) -> bool:
        """True when score meets the minimum threshold for entry consideration."""
        return self.score >= AlignmentScoreCalculator.MIN_SCORE and self.direction is not None


# ── AlignmentScoreCalculator ──────────────────────────────────────────────────

class AlignmentScoreCalculator:
    """
    Weights each timeframe's structural bias and returns a composite score.

    Weights chosen to reflect institutional importance:
      H1  — global context, heaviest weight
      M30 — intermediate swing
      M15 — intraday structure
      M5  — local entry context
      M1  — execution only, minimal weight

    A state of BULLISH_TREND contributes its full weight toward a bullish
    score; BEARISH_TREND contributes toward bearish; anything else (RANGING,
    STRUCTURE_BREAK, MITIGATION_ZONE) contributes nothing.

    The final score is the maximum of (bullish_sum, bearish_sum).
    Direction is only reported when one side clearly wins.
    """

    MIN_SCORE = 0.70

    _WEIGHTS: Dict[Timeframe, float] = {
        Timeframe.H1:  0.35,
        Timeframe.M30: 0.25,
        Timeframe.M15: 0.20,
        Timeframe.M5:  0.15,
        Timeframe.M1:  0.05,
    }

    def calculate(
        self,
        manager: StructureStateManager,
        regime:  Regime,
    ) -> AlignmentResult:
        bull_score = 0.0
        bear_score = 0.0
        details: Dict[str, str] = {}

        for tf, weight in self._WEIGHTS.items():
            state = manager.get_state(tf)
            details[tf.value] = state.value
            if state == StructureState.BULLISH_TREND:
                bull_score += weight
            elif state == StructureState.BEARISH_TREND:
                bear_score += weight

        score = max(bull_score, bear_score)

        # Determine consensus direction — only if one side clearly dominates
        if bull_score > bear_score and bull_score >= self.MIN_SCORE:
            direction: Optional[Direction] = Direction.BULLISH
        elif bear_score > bull_score and bear_score >= self.MIN_SCORE:
            direction = Direction.BEARISH
        else:
            direction = None

        # Critical rule: never allow a trade against H1 bias
        h1_state = manager.get_state(Timeframe.H1)
        if direction == Direction.BULLISH and h1_state == StructureState.BEARISH_TREND:
            logger.debug(
                "AlignmentScore[%s]: direction BULLISH blocked — H1 is BEARISH",
                manager.symbol,
            )
            direction = None
            score = 0.0
        elif direction == Direction.BEARISH and h1_state == StructureState.BULLISH_TREND:
            logger.debug(
                "AlignmentScore[%s]: direction BEARISH blocked — H1 is BULLISH",
                manager.symbol,
            )
            direction = None
            score = 0.0

        result = AlignmentResult(
            score=score,
            direction=direction,
            regime=regime,
            details=details,
        )

        logger.debug(
            "AlignmentScore[%s] bull=%.2f bear=%.2f final=%.2f dir=%s regime=%s",
            manager.symbol, bull_score, bear_score, score,
            direction.value if direction else "NONE",
            regime.value,
        )
        return result


# ── RegimeDetector ────────────────────────────────────────────────────────────

class RegimeDetector:
    """
    Classifies the current market regime using H1 ATR.

    Logic:
        current_atr  = average true range of last ATR_PERIOD H1 candles
        baseline_atr = average true range of last BASELINE_PERIOD H1 candles

        If current_atr < baseline_atr * RANGING_THRESHOLD → RANGING
        Else → TRENDING

    In RANGING regime:
        - Position sizes are halved
        - Minimum alignment score is raised by 0.05

    This prevents the strategy from over-trading choppy markets where
    tick signals and structure states produce a lot of noise.
    """

    ATR_PERIOD:        int   = 14
    BASELINE_PERIOD:   int   = 50
    RANGING_THRESHOLD: float = 0.50   # current ATR < 50% of baseline = ranging

    def detect(self, h1_candles: List[Candle]) -> Regime:
        if len(h1_candles) < self.BASELINE_PERIOD:
            return Regime.TRENDING   # not enough data — assume trending

        current_slice  = h1_candles[-self.ATR_PERIOD:]
        baseline_slice = h1_candles[-self.BASELINE_PERIOD:]

        current_atr  = self._atr(current_slice)
        baseline_atr = self._atr(baseline_slice)

        if baseline_atr <= 0:
            return Regime.TRENDING

        ratio = current_atr / baseline_atr
        regime = Regime.RANGING if ratio < self.RANGING_THRESHOLD else Regime.TRENDING

        logger.debug(
            "RegimeDetector: current_atr=%.5f baseline_atr=%.5f ratio=%.2f → %s",
            current_atr, baseline_atr, ratio, regime.value,
        )
        return regime

    @staticmethod
    def _atr(candles: List[Candle]) -> float:
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            prev_close = candles[i - 1].close
            c = candles[i]
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0


# ── Convenience facade ────────────────────────────────────────────────────────

class MultiTFAnalyzer:
    """
    Single entry point combining StructureStateManager, AlignmentScoreCalculator,
    and RegimeDetector for one symbol.

    Instantiate once per symbol, feed it candles, query for alignment.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol  = symbol
        self.manager = StructureStateManager(symbol)
        self._scorer  = AlignmentScoreCalculator()
        self._regime  = RegimeDetector()

    def on_candle(self, candle: Candle) -> None:
        self.manager.on_candle(candle)

    def seed(self, timeframe: Timeframe, candles: List[Candle]) -> None:
        self.manager.seed(timeframe, candles)

    def get_alignment(self, h1_candles: List[Candle]) -> AlignmentResult:
        regime = self._regime.detect(h1_candles)
        return self._scorer.calculate(self.manager, regime)

    def get_regime(self, h1_candles: List[Candle]) -> Regime:
        return self._regime.detect(h1_candles)
