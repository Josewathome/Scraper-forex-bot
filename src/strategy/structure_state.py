"""
structure_state.py — Phase 1: Multi-Timeframe Market Structure Engine.

Tracks swing highs/lows, structure state, BOS, and CHoCH across all
timeframes for a single symbol.  Fed by CandleBuilder bar-close callbacks.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from src.domain.entities import Candle, Direction, Timeframe

logger = logging.getLogger(__name__)


# ── Structure state enum ──────────────────────────────────────────────────────

class StructureState(str, Enum):
    BULLISH_TREND    = "BULLISH_TREND"     # HH + HL sequence confirmed
    BEARISH_TREND    = "BEARISH_TREND"     # LH + LL sequence confirmed
    RANGING          = "RANGING"           # No clear sequence
    STRUCTURE_BREAK  = "STRUCTURE_BREAK"   # BOS detected — transitioning
    MITIGATION_ZONE  = "MITIGATION_ZONE"   # Price returning to broken structure level


# ── Swing point data ──────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    price:     float
    time:      datetime
    is_high:   bool        # True = swing high, False = swing low
    candle_idx: int        # index in the buffer when this was detected

    @property
    def is_low(self) -> bool:
        return not self.is_high


# ── Per-timeframe structure tracker ──────────────────────────────────────────

class _TimeframeStructure:
    """
    Tracks market structure for a single timeframe.

    Swing detection: a swing high is a candle whose high is higher than
    SWING_CONFIRM_BARS candles on each side.  Same for swing lows.
    We use a rolling buffer and confirm swings when enough bars have
    closed after the candidate.
    """

    SWING_CONFIRM_BARS = 2   # bars needed on each side to confirm a swing

    def __init__(self, timeframe: Timeframe) -> None:
        self.timeframe = timeframe
        self._candles:  Deque[Candle]      = deque(maxlen=100)
        self._swing_highs: List[SwingPoint] = []  # confirmed, newest last
        self._swing_lows:  List[SwingPoint] = []
        self.state:        StructureState   = StructureState.RANGING
        self.last_bos_level: Optional[float] = None
        self.last_bos_direction: Optional[Direction] = None
        self._candle_idx = 0  # monotonic counter

    def on_candle(self, candle: Candle) -> None:
        self._candles.append(candle)
        self._candle_idx += 1

        # Need at least 2*SWING_CONFIRM_BARS + 1 candles before we can detect
        min_candles = self.SWING_CONFIRM_BARS * 2 + 1
        if len(self._candles) < min_candles:
            return

        # Check the candle at position SWING_CONFIRM_BARS from the right
        candidate_idx = len(self._candles) - 1 - self.SWING_CONFIRM_BARS
        self._check_swing(candidate_idx)
        self._update_state()

    def _check_swing(self, idx: int) -> None:
        candles = list(self._candles)
        candidate = candles[idx]
        n = self.SWING_CONFIRM_BARS

        left  = candles[idx - n : idx]
        right = candles[idx + 1 : idx + n + 1]

        if len(left) < n or len(right) < n:
            return

        # Swing high: candidate high >= right side (ties allowed) and strictly > left
        if (candidate.high > max(c.high for c in left) and
                candidate.high >= max(c.high for c in right)):
            sp = SwingPoint(
                price=candidate.high,
                time=candidate.time,
                is_high=True,
                candle_idx=self._candle_idx - n,
            )
            # Only add if it's a new / higher high than the last swing high
            if not self._swing_highs or sp.price != self._swing_highs[-1].price:
                self._swing_highs.append(sp)
                if len(self._swing_highs) > 10:
                    self._swing_highs.pop(0)
                logger.debug(
                    "SWING HIGH [%s] %.5f @ %s",
                    self.timeframe.value, sp.price, sp.time,
                )

        # Swing low: candidate low <= right side (ties allowed) and strictly < left
        if (candidate.low < min(c.low for c in left) and
                candidate.low <= min(c.low for c in right)):
            sp = SwingPoint(
                price=candidate.low,
                time=candidate.time,
                is_high=False,
                candle_idx=self._candle_idx - n,
            )
            if not self._swing_lows or sp.price != self._swing_lows[-1].price:
                self._swing_lows.append(sp)
                if len(self._swing_lows) > 10:
                    self._swing_lows.pop(0)
                logger.debug(
                    "SWING LOW  [%s] %.5f @ %s",
                    self.timeframe.value, sp.price, sp.time,
                )

    def _update_state(self) -> None:
        highs = self._swing_highs
        lows  = self._swing_lows

        if len(highs) < 2 or len(lows) < 2:
            return   # not enough data yet

        last_hh = highs[-1].price
        prev_hh = highs[-2].price
        last_ll = lows[-1].price
        prev_ll = lows[-2].price

        current_price = list(self._candles)[-1].close

        # BOS detection — did price close beyond the last swing?
        if self.state == StructureState.BULLISH_TREND:
            if current_price < last_ll:
                self.state = StructureState.STRUCTURE_BREAK
                self.last_bos_level = last_ll
                self.last_bos_direction = Direction.BEARISH
                logger.info(
                    "BOS [%s] BEARISH — close %.5f broke swing low %.5f",
                    self.timeframe.value, current_price, last_ll,
                )
                return

        elif self.state == StructureState.BEARISH_TREND:
            if current_price > last_hh:
                self.state = StructureState.STRUCTURE_BREAK
                self.last_bos_level = last_hh
                self.last_bos_direction = Direction.BULLISH
                logger.info(
                    "BOS [%s] BULLISH — close %.5f broke swing high %.5f",
                    self.timeframe.value, current_price, last_hh,
                )
                return

        # CHoCH / structure classification
        hh = last_hh > prev_hh  # higher high
        hl = last_ll > prev_ll  # higher low
        lh = last_hh < prev_hh  # lower high
        ll = last_ll < prev_ll  # lower low

        if hh and hl:
            new_state = StructureState.BULLISH_TREND
        elif lh and ll:
            new_state = StructureState.BEARISH_TREND
        else:
            new_state = StructureState.RANGING

        if new_state != self.state:
            logger.info(
                "STRUCTURE [%s] %s → %s",
                self.timeframe.value, self.state.value, new_state.value,
            )
            self.state = new_state

        # Mitigation zone: price returns to the BOS level from a STRUCTURE_BREAK
        if (self.state == StructureState.STRUCTURE_BREAK and
                self.last_bos_level is not None):
            tolerance = abs(current_price - self.last_bos_level)
            atr_proxy = self._atr_proxy()
            if atr_proxy > 0 and tolerance < atr_proxy * 0.3:
                self.state = StructureState.MITIGATION_ZONE

    def _atr_proxy(self) -> float:
        candles = list(self._candles)[-14:]
        if not candles:
            return 0.0
        return sum(c.high - c.low for c in candles) / len(candles)

    @property
    def last_swing_high(self) -> Optional[SwingPoint]:
        return self._swing_highs[-1] if self._swing_highs else None

    @property
    def last_swing_low(self) -> Optional[SwingPoint]:
        return self._swing_lows[-1] if self._swing_lows else None

    @property
    def prev_swing_high(self) -> Optional[SwingPoint]:
        return self._swing_highs[-2] if len(self._swing_highs) >= 2 else None

    @property
    def prev_swing_low(self) -> Optional[SwingPoint]:
        return self._swing_lows[-2] if len(self._swing_lows) >= 2 else None

    def recent_candles(self, n: int) -> List[Candle]:
        return list(self._candles)[-n:]


# ── Public API: StructureStateManager ────────────────────────────────────────

class StructureStateManager:
    """
    Manages market structure state across all tracked timeframes for one symbol.

    Usage:
        mgr = StructureStateManager("GBPUSD")
        # Feed candle bar-close events:
        mgr.on_candle(candle)
        # Query state:
        state = mgr.get_state(Timeframe.H1)
        highs = mgr.get_swing_highs(Timeframe.M5)
    """

    TRACKED = [Timeframe.H1, Timeframe.M30, Timeframe.M15, Timeframe.M5, Timeframe.M1]

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._tf: Dict[Timeframe, _TimeframeStructure] = {
            tf: _TimeframeStructure(tf) for tf in self.TRACKED
        }

    def on_candle(self, candle: Candle) -> None:
        """Feed a closed candle. Only processes timeframes we track."""
        tracker = self._tf.get(candle.timeframe)
        if tracker:
            tracker.on_candle(candle)

    def seed(self, timeframe: Timeframe, candles: List[Candle]) -> None:
        """Bulk-seed historical candles at startup."""
        tracker = self._tf.get(timeframe)
        if tracker is None:
            return
        for c in candles:
            tracker.on_candle(c)
        logger.info(
            "StructureStateManager[%s/%s] seeded %d candles → state=%s",
            self.symbol, timeframe.value, len(candles),
            tracker.state.value,
        )

    def get_state(self, timeframe: Timeframe) -> StructureState:
        tracker = self._tf.get(timeframe)
        return tracker.state if tracker else StructureState.RANGING

    def get_swing_highs(self, timeframe: Timeframe) -> List[SwingPoint]:
        tracker = self._tf.get(timeframe)
        return list(tracker._swing_highs) if tracker else []

    def get_swing_lows(self, timeframe: Timeframe) -> List[SwingPoint]:
        tracker = self._tf.get(timeframe)
        return list(tracker._swing_lows) if tracker else []

    def last_swing_high(self, timeframe: Timeframe) -> Optional[SwingPoint]:
        tracker = self._tf.get(timeframe)
        return tracker.last_swing_high if tracker else None

    def last_swing_low(self, timeframe: Timeframe) -> Optional[SwingPoint]:
        tracker = self._tf.get(timeframe)
        return tracker.last_swing_low if tracker else None

    def recent_candles(self, timeframe: Timeframe, n: int) -> List[Candle]:
        tracker = self._tf.get(timeframe)
        return tracker.recent_candles(n) if tracker else []

    def get_last_bos_direction(self, timeframe: Timeframe) -> Optional[Direction]:
        """Return the direction of the most recent BOS event on this timeframe."""
        tracker = self._tf.get(timeframe)
        return tracker.last_bos_direction if tracker else None

    def get_direction(self, timeframe: Timeframe) -> Optional[Direction]:
        """Return the directional bias for this timeframe, or None if ranging."""
        state = self.get_state(timeframe)
        if state == StructureState.BULLISH_TREND:
            return Direction.BULLISH
        if state == StructureState.BEARISH_TREND:
            return Direction.BEARISH
        return None

    def summary(self) -> Dict[str, str]:
        return {tf.value: self.get_state(tf).value for tf in self.TRACKED}
