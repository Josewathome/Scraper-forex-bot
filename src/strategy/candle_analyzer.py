"""
candle_analyzer.py — Phase 2: Live Candle Analysis.

LiveCandleAnalyzer:
    Evaluates the in-progress (forming) candle in real time to infer the
    probable close direction, and reconstructs higher-timeframe in-progress
    candle states from completed M1 candles.

Key rule: candle_progress_score returns a value in [-1, +1].
  abs(score) >= 0.40 is required before the candle layer passes the entry gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.domain.entities import Candle, Timeframe
from src.strategy.tick_engine import TickFeatureCalculator

logger = logging.getLogger(__name__)


# ── Candle progress result ────────────────────────────────────────────────────

@dataclass
class CandleProgressResult:
    score:          float   # -1.0 (strong bear) to +1.0 (strong bull)
    elapsed_ratio:  float   # 0.0 to 1.0 — how far through the bar period
    wick_pressure:  float   # positive = bullish, negative = bearish
    body_direction: int     # +1 bullish, -1 bearish, 0 doji
    tick_bias:      float   # tick directional imbalance mapped to -1..+1

    @property
    def is_valid(self) -> bool:
        """True when candle signal is strong enough to pass the entry gate."""
        return abs(self.score) >= 0.40


# ── Higher-timeframe synthetic candle ─────────────────────────────────────────

@dataclass
class SyntheticCandle:
    """
    Reconstructed in-progress state of a higher timeframe candle,
    inferred from the batch of completed M1 candles that form it.
    """
    open:      float
    high:      float
    low:       float
    close:     float
    progress:  float   # 0.0 to 1.0 — what fraction of the bar has elapsed
    is_bullish: bool

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low


# ── LiveCandleAnalyzer ────────────────────────────────────────────────────────

class LiveCandleAnalyzer:
    """
    Analyzes the currently-forming candle to produce a directional
    probability score combining:
      - Candle body direction and size
      - Wick pressure (upper/lower wick asymmetry)
      - Tick-level directional imbalance

    Time-weighted: early in the bar, tick momentum dominates.
    Late in the bar, candle body dominates.
    """

    _CANDLES_PER_TF: Dict[str, int] = {
        "M5":  5,
        "M15": 15,
        "M30": 30,
        "H1":  60,
    }

    def candle_progress_score(
        self,
        forming_candle:  Candle,
        tick_imbalance:  float,    # from TickFeatureCalculator.directional_imbalance()
        elapsed_seconds: float,    # seconds elapsed in the current bar period
        bar_seconds:     int,      # total bar period in seconds
    ) -> CandleProgressResult:
        """
        Score the in-progress candle for probable close direction.

        elapsed_seconds and bar_seconds are used to derive elapsed_ratio.
        tick_imbalance should come from TickFeatureCalculator.directional_imbalance().
        """
        elapsed_ratio = min(elapsed_seconds / bar_seconds, 1.0) if bar_seconds > 0 else 0.5

        # Body direction: +1 if bullish body, -1 if bearish
        if forming_candle.close > forming_candle.open:
            body_direction = 1
        elif forming_candle.close < forming_candle.open:
            body_direction = -1
        else:
            body_direction = 0

        # Wick pressure: positive = bullish (long lower wick), negative = bearish
        wick_pressure = self._wick_analysis(forming_candle)

        # Tick bias: map imbalance (0.0–1.0) to (-1.0 to +1.0)
        tick_bias = (tick_imbalance - 0.5) * 2.0

        # Time-weighted blend of candle body vs tick momentum
        if elapsed_ratio < 0.30:
            tick_weight   = 0.70
            candle_weight = 0.30
        elif elapsed_ratio < 0.70:
            tick_weight   = 0.50
            candle_weight = 0.50
        else:
            tick_weight   = 0.25
            candle_weight = 0.75

        # Body signal: direction × body dominance (large body = stronger signal)
        total_range = forming_candle.total_range or 0.00001
        body_ratio  = forming_candle.body_size / total_range
        body_signal = body_direction * body_ratio  # -1.0 to +1.0

        # Wick adds to body signal (wick pressure is already -1 to +1)
        candle_signal = body_signal * 0.70 + wick_pressure * 0.30

        combined = candle_weight * candle_signal + tick_weight * tick_bias

        return CandleProgressResult(
            score=round(max(-1.0, min(1.0, combined)), 3),
            elapsed_ratio=elapsed_ratio,
            wick_pressure=round(wick_pressure, 3),
            body_direction=body_direction,
            tick_bias=round(tick_bias, 3),
        )

    def higher_candle_state(
        self,
        m1_candles:  List[Candle],
        target_tf:   str,            # "M5", "M15", "M30", "H1"
    ) -> Optional[SyntheticCandle]:
        """
        Reconstruct the in-progress state of a higher timeframe candle
        from the current batch of M1 candles.

        Example: 3 out of 5 M1 candles have formed → progress = 0.60,
        and we can infer the current M5 open/high/low/close.
        """
        n = self._CANDLES_PER_TF.get(target_tf)
        if n is None or len(m1_candles) < 1:
            return None

        # Take the last n M1 candles (the ones forming the current higher TF bar)
        current_batch = m1_candles[-n:]
        if not current_batch:
            return None

        progress = len(current_batch) / n

        return SyntheticCandle(
            open=      current_batch[0].open,
            high=      max(c.high  for c in current_batch),
            low=       min(c.low   for c in current_batch),
            close=     current_batch[-1].close,
            progress=  progress,
            is_bullish=current_batch[-1].close >= current_batch[0].open,
        )

    @staticmethod
    def _wick_analysis(candle: Candle) -> float:
        """
        Returns wick pressure in [-1, +1].
        Positive = long lower wick = bullish rejection.
        Negative = long upper wick = bearish rejection.
        """
        total_range = candle.total_range or 0.00001
        upper = candle.upper_wick
        lower = candle.lower_wick
        return (lower - upper) / total_range
