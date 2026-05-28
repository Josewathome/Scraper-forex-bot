"""
strategy_manager.py — Central coordinator for Phase 1 + Phase 2 strategy logic.

StrategyManager owns one MultiTFAnalyzer + one TickAnalyzer per symbol.
It is the single object the event loop interacts with.

Called from main_stream.py on:
  - Every TICK event      → push_tick()
  - Every candle close    → on_candle()
  - Every M1 close        → evaluate_signal() (entry gate)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.value_objects import PipCalculator
from src.strategy.alignment import AlignmentResult, MultiTFAnalyzer, Regime
from src.strategy.candle_analyzer import CandleProgressResult, LiveCandleAnalyzer
from src.strategy.tick_engine import SpreadState, TickAnalysisResult, TickAnalyzer

logger = logging.getLogger(__name__)


# ── Signal evaluation result ──────────────────────────────────────────────────

@dataclass
class StrategySignal:
    symbol:         str
    direction:      Direction
    confidence:     float              # 0.0–1.0 combined score
    alignment:      AlignmentResult
    tick_analysis:  TickAnalysisResult
    candle_score:   CandleProgressResult
    timestamp:      datetime

    # Suggested entry levels (caller sets SL/TP using existing execution_service logic)
    proposed_entry: float              # current mid-price at signal time
    last_swing_support:  Optional[float]   # nearest swing low (for longs)
    last_swing_resistance: Optional[float] # nearest swing high (for shorts)

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.BULLISH

    def log_summary(self) -> None:
        logger.info(
            "STRATEGY SIGNAL | %s %s | conf=%.2f | align=%.2f | tick=%.2f | "
            "candle=%.2f | regime=%s | spread=%s | entry=%.5f",
            self.symbol,
            self.direction.value.upper(),
            self.confidence,
            self.alignment.score,
            self.tick_analysis.score,
            abs(self.candle_score.score),
            self.alignment.regime.value,
            self.tick_analysis.spread_state.value,
            self.proposed_entry,
        )


# ── Gate reasons (for logging blocked signals) ────────────────────────────────

class BlockReason:
    ALIGNMENT_SCORE  = "alignment_score_below_threshold"
    NO_DIRECTION     = "no_consensus_direction"
    SPREAD_DANGEROUS = "spread_dangerous"
    TICK_SCORE       = "tick_score_below_threshold"
    CANDLE_SCORE     = "candle_score_below_threshold"
    VOLATILITY_BURST = "volatility_burst_detected"
    RANGING_REGIME   = "ranging_regime_suppressed"


# ── StrategyManager ───────────────────────────────────────────────────────────

class StrategyManager:
    """
    Central strategy coordinator.  One instance for the whole bot.

    Key thresholds (all tunable — do not optimise on in-sample data):
      MIN_ALIGNMENT_SCORE  = 0.70  (raised to 0.75 in RANGING regime)
      MIN_TICK_SCORE       = 0.60
      MIN_CANDLE_SCORE_ABS = 0.40  (absolute value)
      MAX_VOLATILITY_BURST = 2.5   (tick range explosion — noise/news)
    """

    MIN_ALIGNMENT_SCORE  = 0.70
    RANGING_ALIGNMENT_BOOST = 0.05   # threshold raised by this in RANGING
    MIN_TICK_SCORE       = 0.60
    MIN_CANDLE_SCORE_ABS = 0.40
    MAX_VOLATILITY_BURST = 2.5

    def __init__(self, symbols: List[str]) -> None:
        self._mtf:     Dict[str, MultiTFAnalyzer]  = {}
        self._tick:    Dict[str, TickAnalyzer]      = {}
        self._candle   = LiveCandleAnalyzer()

        for sym in symbols:
            self._mtf[sym]  = MultiTFAnalyzer(sym)
            self._tick[sym] = TickAnalyzer(sym)

    # ── Lifecycle methods (called from event loop) ────────────────────

    def seed(self, symbol: str, timeframe: Timeframe, candles: List[Candle]) -> None:
        """Seed historical candles at startup into the structure engine."""
        if symbol in self._mtf:
            self._mtf[symbol].seed(timeframe, candles)

    def on_candle(self, candle: Candle) -> None:
        """Feed a closed candle to the structure engine."""
        mtf = self._mtf.get(candle.symbol)
        if mtf:
            mtf.on_candle(candle)

    def push_tick(self, symbol: str, bid: float, ask: float, ts: float) -> None:
        """Push a raw tick into the tick analyzer.  Called on every TICK event."""
        analyzer = self._tick.get(symbol)
        if analyzer is not None:
            analyzer.push_tick(bid, ask, ts)

    # ── Signal evaluation (called on every M1 close) ──────────────────

    def evaluate(
        self,
        symbol:         str,
        forming_m1:     Optional[Candle],
        m1_candles:     List[Candle],
        h1_candles:     List[Candle],
        current_price:  float,
        elapsed_m1_secs: float = 30.0,
    ) -> Optional[StrategySignal]:
        """
        Run the full signal evaluation for one symbol.
        Returns StrategySignal if all gates pass, else None.

        Parameters
        ----------
        forming_m1     : the currently-forming M1 bar from CandleBuilder.get_forming_bar()
        m1_candles     : last N closed M1 candles
        h1_candles     : last N closed H1 candles (for regime detection)
        current_price  : current mid-price
        elapsed_m1_secs: seconds elapsed in the forming M1 bar
        """
        mtf  = self._mtf.get(symbol)
        tick = self._tick.get(symbol)
        if mtf is None or tick is None:
            return None

        # ── Gate 1: Alignment score ───────────────────────────────────
        alignment = mtf.get_alignment(h1_candles)

        min_score = self.MIN_ALIGNMENT_SCORE
        if alignment.regime == Regime.RANGING:
            min_score += self.RANGING_ALIGNMENT_BOOST

        if alignment.score < min_score:
            logger.debug(
                "GATE_BLOCK [%s] %s: score=%.2f < %.2f",
                symbol, BlockReason.ALIGNMENT_SCORE, alignment.score, min_score,
            )
            return None

        # ── Gate 2: Consensus direction ───────────────────────────────
        if alignment.direction is None:
            logger.debug("GATE_BLOCK [%s] %s", symbol, BlockReason.NO_DIRECTION)
            return None

        direction = alignment.direction
        dir_int = 1 if direction == Direction.BULLISH else -1

        # ── Gate 3: Spread check ──────────────────────────────────────
        tick_result = tick.analyze(direction=dir_int)

        if tick_result.spread_state == SpreadState.DANGEROUS:
            logger.debug(
                "GATE_BLOCK [%s] %s: spread=%s",
                symbol, BlockReason.SPREAD_DANGEROUS, tick_result.spread_state.value,
            )
            return None

        # ── Gate 4: Volatility burst (potential news) ─────────────────
        if tick_result.burst > self.MAX_VOLATILITY_BURST:
            logger.debug(
                "GATE_BLOCK [%s] %s: burst=%.2f",
                symbol, BlockReason.VOLATILITY_BURST, tick_result.burst,
            )
            return None

        # ── Gate 5: Tick composite score ──────────────────────────────
        if tick_result.score < self.MIN_TICK_SCORE:
            logger.debug(
                "GATE_BLOCK [%s] %s: tick_score=%.3f < %.2f",
                symbol, BlockReason.TICK_SCORE, tick_result.score, self.MIN_TICK_SCORE,
            )
            return None

        # ── Gate 6: Candle progress score ─────────────────────────────
        if forming_m1 is None:
            logger.debug("GATE_BLOCK [%s] no forming M1 candle available", symbol)
            return None

        candle_result = self._candle.candle_progress_score(
            forming_candle=forming_m1,
            tick_imbalance=tick_result.imbalance,
            elapsed_seconds=elapsed_m1_secs,
            bar_seconds=60,   # M1 = 60 seconds
        )

        if abs(candle_result.score) < self.MIN_CANDLE_SCORE_ABS:
            logger.debug(
                "GATE_BLOCK [%s] %s: candle_score=%.3f < %.2f",
                symbol, BlockReason.CANDLE_SCORE,
                abs(candle_result.score), self.MIN_CANDLE_SCORE_ABS,
            )
            return None

        # Candle direction must agree with structural direction
        if (direction == Direction.BULLISH and candle_result.score < 0) or \
           (direction == Direction.BEARISH and candle_result.score > 0):
            logger.debug(
                "GATE_BLOCK [%s] candle direction conflicts with structure direction",
                symbol,
            )
            return None

        # ── All gates passed — build signal ───────────────────────────
        confidence = (
            alignment.score          * 0.50 +
            tick_result.score        * 0.30 +
            abs(candle_result.score) * 0.20
        )

        # Get swing reference levels for SL guidance
        structure = mtf.manager
        if direction == Direction.BULLISH:
            last_swing_support    = structure.last_swing_low(Timeframe.M5)
            last_swing_resistance = structure.last_swing_high(Timeframe.M5)
        else:
            last_swing_support    = structure.last_swing_low(Timeframe.M5)
            last_swing_resistance = structure.last_swing_high(Timeframe.M5)

        signal = StrategySignal(
            symbol=symbol,
            direction=direction,
            confidence=round(confidence, 3),
            alignment=alignment,
            tick_analysis=tick_result,
            candle_score=candle_result,
            timestamp=datetime.now(tz=timezone.utc),
            proposed_entry=current_price,
            last_swing_support=last_swing_support.price if last_swing_support else None,
            last_swing_resistance=last_swing_resistance.price if last_swing_resistance else None,
        )

        signal.log_summary()
        return signal

    # ── Introspection ─────────────────────────────────────────────────

    def structure_summary(self, symbol: str) -> Dict[str, str]:
        mtf = self._mtf.get(symbol)
        return mtf.manager.summary() if mtf else {}

    def tick_snapshot(self, symbol: str) -> int:
        """Number of ticks currently in buffer."""
        return len(self._tick.get(symbol) or [])

    def spread_state(self, symbol: str) -> SpreadState:
        tick = self._tick.get(symbol)
        return tick.current_spread_state() if tick is not None else SpreadState.NORMAL
