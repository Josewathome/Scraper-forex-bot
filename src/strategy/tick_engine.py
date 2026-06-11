"""
tick_engine.py — Phase 2: Tick Intelligence Engine.

Three components:
  TickBuffer           — Thread-safe sliding window of recent ticks per symbol.
  TickFeatureCalculator — Computes velocity, acceleration, imbalance, etc.
  SpreadMonitor         — Classifies current spread vs rolling baseline.

Key design rule from research:
  Tick features are FILTERS, not TRIGGERS.
  Enter on candle/structure events; use ticks to approve or reject.
  Retail WebSocket latency (50-200ms) makes tick-triggered entries unreliable.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Raw tick data ─────────────────────────────────────────────────────────────

@dataclass
class RawTick:
    price:  float   # mid-price (bid+ask)/2
    bid:    float
    ask:    float
    ts:     float   # epoch seconds (float, from broker)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


# ── Spread state ──────────────────────────────────────────────────────────────

class SpreadState(str, Enum):
    NORMAL    = "NORMAL"     # spread ≤ 1.3× baseline
    ELEVATED  = "ELEVATED"   # spread 1.3×–2.0× baseline — proceed with caution
    DANGEROUS = "DANGEROUS"  # spread > 2.0× baseline — block entries


# ── TickBuffer ────────────────────────────────────────────────────────────────

class TickBuffer:
    """
    Thread-safe sliding window of recent ticks for one symbol.

    Populated by the ZmqFeed thread; consumed by TickFeatureCalculator
    on the main event loop thread.  Thread safety is needed because
    ZmqFeed runs as a daemon thread.
    """

    MAX_TICKS = 500   # enough for all feature windows (largest window = 50 ticks)

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._ticks: Deque[RawTick] = deque(maxlen=self.MAX_TICKS)
        self._lock  = threading.Lock()

    def push(self, bid: float, ask: float, ts: float) -> None:
        tick = RawTick(
            price=(bid + ask) / 2.0,
            bid=bid,
            ask=ask,
            ts=ts,
        )
        with self._lock:
            self._ticks.append(tick)

    def snapshot(self) -> List[RawTick]:
        """Return a copy of the current buffer — safe to read without holding the lock."""
        with self._lock:
            return list(self._ticks)

    def latest(self) -> Optional[RawTick]:
        with self._lock:
            return self._ticks[-1] if self._ticks else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._ticks)


# ── TickFeatureCalculator ─────────────────────────────────────────────────────

class TickFeatureCalculator:
    """
    Stateless feature calculator.  All methods operate on a tick list
    snapshot returned by TickBuffer.snapshot().

    All methods return floats or SpreadState — no exceptions raised.
    """

    # ── Baseline tracking (rolling average spread) ────────────────────
    # These are maintained per-symbol across calls.
    _spread_baselines: Dict[str, float] = {}
    _spread_baseline_counts: Dict[str, int] = {}
    _BASELINE_WINDOW = 50   # ticks to average for baseline

    @classmethod
    def update_spread_baseline(cls, symbol: str, spread: float) -> None:
        count = cls._spread_baseline_counts.get(symbol, 0)
        current = cls._spread_baselines.get(symbol, spread)
        # Exponential moving average
        alpha = 2.0 / (cls._BASELINE_WINDOW + 1)
        cls._spread_baselines[symbol]        = current * (1 - alpha) + spread * alpha
        cls._spread_baseline_counts[symbol]  = min(count + 1, cls._BASELINE_WINDOW)

    @classmethod
    def get_spread_baseline(cls, symbol: str) -> float:
        return cls._spread_baselines.get(symbol, 0.0)

    # ── Feature methods ───────────────────────────────────────────────

    @staticmethod
    def tick_velocity(ticks: List[RawTick], window_seconds: float = 5.0) -> float:
        """Ticks per second in a rolling time window."""
        if not ticks:
            return 0.0
        now = ticks[-1].ts
        cutoff = now - window_seconds
        recent = [t for t in ticks if t.ts >= cutoff]
        return len(recent) / window_seconds

    @classmethod
    def tick_acceleration(
        cls,
        ticks: List[RawTick],
        short_window: float = 3.0,
        long_window:  float = 10.0,
    ) -> float:
        """
        Rate of change of tick velocity.
        Positive = momentum building; negative = momentum decelerating.
        """
        v_short = cls.tick_velocity(ticks, short_window)
        v_long  = cls.tick_velocity(ticks, long_window)
        if v_long <= 0:
            return 0.0
        return (v_short - v_long) / v_long

    @staticmethod
    def directional_imbalance(ticks: List[RawTick], n_ticks: int = 20) -> float:
        """
        Ratio of up-moves to all directional moves over last N ticks.

        Returns 0.0–1.0.
          > 0.65 = strong buying pressure
          < 0.35 = strong selling pressure
          ~0.50  = balanced / ranging
        """
        recent = ticks[-n_ticks:]
        if len(recent) < 2:
            return 0.5
        up   = sum(1 for i in range(1, len(recent)) if recent[i].price > recent[i - 1].price)
        down = sum(1 for i in range(1, len(recent)) if recent[i].price < recent[i - 1].price)
        total = up + down
        return up / total if total > 0 else 0.5

    @staticmethod
    def price_displacement(ticks: List[RawTick], n_ticks: int = 20) -> float:
        """Net price move (not pips — raw price) over last N ticks."""
        recent = ticks[-n_ticks:]
        if len(recent) < 2:
            return 0.0
        return recent[-1].price - recent[0].price

    @staticmethod
    def volatility_burst(ticks: List[RawTick], window: int = 10) -> float:
        """
        Range of last N ticks divided by range of the 50 ticks before that.
        > 2.0 = volatility burst (potential news spike or fake sweep).
        """
        if len(ticks) < window + 10:
            return 1.0
        recent_window  = ticks[-window:]
        baseline_ticks = ticks[-(window + 50):-window]
        if not baseline_ticks:
            return 1.0

        recent_range   = max(t.price for t in recent_window)  - min(t.price for t in recent_window)
        baseline_range = max(t.price for t in baseline_ticks) - min(t.price for t in baseline_ticks)

        if baseline_range <= 0:
            return 1.0
        return recent_range / baseline_range

    @classmethod
    def spread_state(cls, symbol: str, ticks: List[RawTick]) -> SpreadState:
        """
        Compare current spread to the rolling baseline for this symbol.
        Call update_spread_baseline() before calling this method.
        """
        if not ticks:
            return SpreadState.NORMAL
        current  = ticks[-1].spread
        baseline = cls.get_spread_baseline(symbol)
        if baseline <= 0:
            return SpreadState.NORMAL
        ratio = current / baseline
        if ratio < 1.3:
            return SpreadState.NORMAL
        if ratio < 2.0:
            return SpreadState.ELEVATED
        return SpreadState.DANGEROUS

    @classmethod
    def tick_composite_score(
        cls,
        ticks:    List[RawTick],
        symbol:   str,
        direction: int,   # +1 for long, -1 for short
    ) -> float:
        """
        Composite tick signal strength score: 0.0–1.0.

        Weights:
          velocity     (0.25) — is tick activity high?
          acceleration (0.20) — is momentum building?
          imbalance    (0.30) — is direction confirmed?
          displacement (0.25) — is price actually moving in our direction?

        Spread penalty:
          ELEVATED  → score × 0.70
          DANGEROUS → score = 0.0 (block)
        """
        if not ticks:
            return 0.0

        score = 0.0

        # Velocity: cap at 10 ticks/sec as "maximum" (typical: 2-6 during active sessions)
        vel = min(cls.tick_velocity(ticks) / 10.0, 1.0)
        score += vel * 0.25

        # Acceleration: clamp to [0, 1]
        acc = max(min(cls.tick_acceleration(ticks), 1.0), 0.0)
        score += acc * 0.20

        # Directional imbalance in our direction
        imb = cls.directional_imbalance(ticks)
        if direction == 1:
            dir_score = max((imb - 0.5) * 2.0, 0.0)
        else:
            dir_score = max((0.5 - imb) * 2.0, 0.0)
        score += dir_score * 0.30

        # Price displacement in our direction
        disp = cls.price_displacement(ticks)
        if (direction == 1 and disp > 0) or (direction == -1 and disp < 0):
            # Normalise: 2 pips of displacement = full score (reduced from 5 pips —
            # typical M1 window sees 0.3-1.5 pip moves; 5 pips was never reached)
            disp_norm = min(abs(disp) / 0.0002, 1.0)
        else:
            disp_norm = 0.0
        score += disp_norm * 0.25

        # Spread penalty
        state = cls.spread_state(symbol, ticks)
        if state == SpreadState.ELEVATED:
            score *= 0.70
        elif state == SpreadState.DANGEROUS:
            score = 0.0

        return round(min(score, 1.0), 3)


# ── SpreadMonitor ─────────────────────────────────────────────────────────────

class SpreadMonitor:
    """
    Per-symbol spread tracker.

    Maintains a rolling baseline and classifies each incoming tick's
    spread.  Also exposes a delay-entry advisory when spread has just
    widened but is returning to normal.

    Call update() on every TICK event, query state() before entry.
    """

    # If spread > 1.5× baseline, delay entry and re-evaluate after this many ticks
    DELAY_TICKS = 15

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._ticks_since_elevated = 0
        self._was_elevated = False

    def update(self, ticks: List[RawTick]) -> None:
        """Update baseline and track spread elevation history."""
        if not ticks:
            return
        latest_spread = ticks[-1].spread
        TickFeatureCalculator.update_spread_baseline(self.symbol, latest_spread)

        state = TickFeatureCalculator.spread_state(self.symbol, ticks)
        if state in (SpreadState.ELEVATED, SpreadState.DANGEROUS):
            self._was_elevated = True
            self._ticks_since_elevated = 0
        else:
            if self._was_elevated:
                self._ticks_since_elevated += 1
                if self._ticks_since_elevated >= self.DELAY_TICKS:
                    self._was_elevated = False

    def state(self, ticks: List[RawTick]) -> SpreadState:
        return TickFeatureCalculator.spread_state(self.symbol, ticks)

    @property
    def recently_elevated(self) -> bool:
        """True for DELAY_TICKS after spread returns to normal from elevated."""
        return self._was_elevated


# ── TickAnalysisResult ────────────────────────────────────────────────────────

@dataclass
class TickAnalysisResult:
    symbol:        str
    score:         float         # composite tick score 0.0–1.0
    spread_state:  SpreadState
    velocity:      float
    acceleration:  float
    imbalance:     float         # directional imbalance
    displacement:  float         # net price move, last 20 ticks
    burst:         float         # volatility burst ratio

    @property
    def is_valid(self) -> bool:
        """Minimum threshold for tick signal to pass the entry gate."""
        return self.score >= 0.60 and self.spread_state != SpreadState.DANGEROUS


# ── TickAnalyzer — per-symbol facade ─────────────────────────────────────────

class TickAnalyzer:
    """
    Combines TickBuffer, TickFeatureCalculator, and SpreadMonitor for
    one symbol.  Single entry point for the entry gate.

    Usage:
        analyzer = TickAnalyzer("GBPUSD")
        # On every TICK event:
        analyzer.push_tick(bid, ask, ts)
        # Before entry gate:
        result = analyzer.analyze(direction=+1)
        if result.is_valid:
            ...
    """

    def __init__(self, symbol: str) -> None:
        self.symbol  = symbol
        self._buffer = TickBuffer(symbol)
        self._spread = SpreadMonitor(symbol)
        self._calc   = TickFeatureCalculator()

    def push_tick(self, bid: float, ask: float, ts: float) -> None:
        self._buffer.push(bid, ask, ts)
        ticks = self._buffer.snapshot()
        self._spread.update(ticks)

    def analyze(self, direction: int) -> TickAnalysisResult:
        """
        direction: +1 for long, -1 for short.
        Returns a TickAnalysisResult with score and all sub-features.
        """
        ticks = self._buffer.snapshot()

        score        = self._calc.tick_composite_score(ticks, self.symbol, direction)
        spread_state = self._spread.state(ticks)
        velocity     = self._calc.tick_velocity(ticks)
        acceleration = self._calc.tick_acceleration(ticks)
        imbalance    = self._calc.directional_imbalance(ticks)
        displacement = self._calc.price_displacement(ticks)
        burst        = self._calc.volatility_burst(ticks)

        return TickAnalysisResult(
            symbol=self.symbol,
            score=score,
            spread_state=spread_state,
            velocity=velocity,
            acceleration=acceleration,
            imbalance=imbalance,
            displacement=displacement,
            burst=burst,
        )

    def current_spread_state(self) -> SpreadState:
        ticks = self._buffer.snapshot()
        return self._spread.state(ticks)

    @property
    def recently_elevated_spread(self) -> bool:
        return self._spread.recently_elevated

    def __len__(self) -> int:
        return len(self._buffer)
