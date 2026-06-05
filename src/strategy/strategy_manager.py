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

import src.config as config
from src.domain.entities import Candle, Direction, Timeframe
from src.domain.value_objects import PipCalculator
from src.strategy.candle_analyzer import CandleProgressResult, LiveCandleAnalyzer
from src.strategy.scalper_alignment import ScalperAlignmentEngine, ScalperAlignmentResult, Regime
from src.strategy.structure_state import StructureStateManager
from src.strategy.tick_engine import SpreadState, TickAnalysisResult, TickAnalyzer

logger = logging.getLogger(__name__)


# ── Signal evaluation result ──────────────────────────────────────────────────

@dataclass
class StrategySignal:
    symbol:         str
    direction:      Direction
    confidence:     float              # 0.0–1.0 combined score
    alignment:      ScalperAlignmentResult
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

    MIN_ALIGNMENT_SCORE  = getattr(config, "SCALPER_MIN_ALIGNMENT_SCORE", 0.55)
    MIN_TICK_SCORE       = getattr(config, "SCALPER_MIN_TICK_SCORE",      0.55)
    MIN_CANDLE_SCORE_ABS = getattr(config, "SCALPER_MIN_CANDLE_SCORE",   0.30)
    MAX_VOLATILITY_BURST = 2.5

    def __init__(self, symbols: List[str]) -> None:
        self._mtf:       Dict[str, ScalperAlignmentEngine] = {}
        self._tick:      Dict[str, TickAnalyzer]           = {}
        self._structure: Dict[str, StructureStateManager]  = {}
        self._candle     = LiveCandleAnalyzer()

        for sym in symbols:
            self._mtf[sym]       = ScalperAlignmentEngine(sym)
            self._structure[sym] = StructureStateManager(sym)
            self._tick[sym]      = TickAnalyzer(sym)

    # ── Lifecycle methods (called from event loop) ────────────────────

    def seed(self, symbol: str, timeframe: Timeframe, candles: List[Candle]) -> None:
        """Seed historical candles at startup into the structure engine."""
        if symbol in self._mtf:
            mtf = self._mtf[symbol]
            if hasattr(mtf, 'seed'):
                mtf.seed(timeframe, candles)
        # Also seed scalper structure manager (tracks M1 BOS/CHoCH)
        struct = self._structure.get(symbol)
        if struct is not None:
            struct.seed(timeframe, candles)

    def on_candle(self, candle: Candle) -> None:
        """Feed a closed candle to the structure engine."""
        mtf = self._mtf.get(candle.symbol)
        if mtf and hasattr(mtf, 'on_candle'):
            mtf.on_candle(candle)
        # Keep scalper structure manager up to date on every bar close
        struct = self._structure.get(candle.symbol)
        if struct is not None:
            struct.on_candle(candle)

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
        # Scalper mode: needs M5 candles + M1 structure.
        # h1_candles slot carries M5 candles (set by main_stream).
        struct = self._structure.get(symbol)
        alignment = mtf.get_alignment(
            m5_candles=h1_candles,   # M5 candles passed via this slot
            m1_candles=m1_candles,
            structure=struct,
        )

        # Log alignment result every M1 close so you can see M1 BOS/CHoCH + M5 EMA working
        logger.info(
            "ALIGN [%s] m1=%.2f(%s) m5=%.2f(%s) score=%.2f regime=%s ema=%.5f slope=%+.6f",
            symbol,
            alignment.m1_score,
            alignment.details.get("m1_dir", "?"),
            alignment.m5_score,
            alignment.details.get("m5_slope", "?"),
            alignment.score,
            alignment.regime.value,
            alignment.m5_ema,
            alignment.m5_slope,
        )

        min_score = self.MIN_ALIGNMENT_SCORE
        if alignment.score < min_score:
            logger.info(
                "GATE1 BLOCK [%s] align_score=%.2f < threshold=%.2f | dir=%s",
                symbol, alignment.score, min_score,
                alignment.details.get("m1_dir", "NONE"),
            )
            return None

        # ── Gate 2: Consensus direction ───────────────────────────────
        if alignment.direction is None:
            logger.info("GATE2 BLOCK [%s] no consensus direction (M1/M5 split)", symbol)
            return None

        direction = alignment.direction
        dir_int = 1 if direction == Direction.BULLISH else -1

        # ── Gate 3: Spread check ──────────────────────────────────────
        tick_result = tick.analyze(direction=dir_int)

        logger.info(
            "TICK  [%s] score=%.2f vel=%.1f accel=%.2f imbal=%.2f disp=%+.5f spread=%s burst=%.2f",
            symbol,
            tick_result.score,
            tick_result.velocity,
            tick_result.acceleration,
            tick_result.imbalance,
            tick_result.displacement,
            tick_result.spread_state.value,
            tick_result.burst,
        )

        if tick_result.spread_state == SpreadState.DANGEROUS:
            logger.info("GATE3 BLOCK [%s] spread DANGEROUS — spread too wide for scalper", symbol)
            return None

        # ── Gate 4: Volatility burst (potential news) ─────────────────
        if tick_result.burst > self.MAX_VOLATILITY_BURST:
            logger.info(
                "GATE4 BLOCK [%s] volatility burst=%.2f > %.2f — likely news spike",
                symbol, tick_result.burst, self.MAX_VOLATILITY_BURST,
            )
            return None

        # ── Gate 5: Tick composite score ──────────────────────────────
        if tick_result.score < self.MIN_TICK_SCORE:
            logger.info(
                "GATE5 BLOCK [%s] tick_score=%.3f < %.2f — momentum not confirmed",
                symbol, tick_result.score, self.MIN_TICK_SCORE,
            )
            return None

        # ── Gate 6: Candle progress score ─────────────────────────────
        if forming_m1 is None:
            logger.info("GATE6 BLOCK [%s] no forming M1 candle yet", symbol)
            return None

        candle_result = self._candle.candle_progress_score(
            forming_candle=forming_m1,
            tick_imbalance=tick_result.imbalance,
            elapsed_seconds=elapsed_m1_secs,
            bar_seconds=60,   # M1 = 60 seconds
        )

        logger.info(
            "CANDLE[%s] score=%+.3f body=%s wick_pressure=%+.3f tick_bias=%+.3f elapsed=%.0fs",
            symbol,
            candle_result.score,
            "BULL" if candle_result.body_direction > 0 else "BEAR" if candle_result.body_direction < 0 else "DOJI",
            candle_result.wick_pressure,
            candle_result.tick_bias,
            elapsed_m1_secs,
        )

        if abs(candle_result.score) < self.MIN_CANDLE_SCORE_ABS:
            logger.info(
                "GATE6 BLOCK [%s] candle_score=%.3f < %.2f — candle too weak",
                symbol, abs(candle_result.score), self.MIN_CANDLE_SCORE_ABS,
            )
            return None

        # Candle direction must agree with structural direction
        if (direction == Direction.BULLISH and candle_result.score < 0) or \
           (direction == Direction.BEARISH and candle_result.score > 0):
            logger.info(
                "GATE6 BLOCK [%s] candle direction CONFLICTS — structure=%s candle=%s",
                symbol,
                direction.value,
                "BEAR" if candle_result.score < 0 else "BULL",
            )
            return None

        # ── All gates passed — build signal ───────────────────────────
        confidence = (
            alignment.score          * 0.50 +
            tick_result.score        * 0.30 +
            abs(candle_result.score) * 0.20
        )

        # Get swing reference levels for SL guidance from M1 structure manager.
        structure = self._structure.get(symbol)
        swing_tf  = Timeframe.M1

        if structure is not None:
            last_swing_support    = structure.last_swing_low(swing_tf)
            last_swing_resistance = structure.last_swing_high(swing_tf)
        else:
            last_swing_support    = None
            last_swing_resistance = None

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
        struct = self._structure.get(symbol)
        return struct.summary() if struct else {}

    def tick_snapshot(self, symbol: str) -> int:
        """Number of ticks currently in buffer."""
        return len(self._tick.get(symbol) or [])

    def spread_state(self, symbol: str) -> SpreadState:
        tick = self._tick.get(symbol)
        return tick.current_spread_state() if tick is not None else SpreadState.NORMAL
