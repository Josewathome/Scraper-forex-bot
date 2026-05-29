"""
trade_monitor.py — Open trade monitoring and exit signals.

TradeMonitor evaluates whether an open trade should be:
  - Trailed (SL moved to protect profit)
  - Closed early (reverse ChoCh detected, MFE stall)
  - Left alone

Called by ExecutionService._monitor_open_trades() on every M1 close.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import src.config as config
from src.domain.entities import Candle, Direction, Timeframe, TradeSignal, Zone
from src.domain.value_objects import PipCalculator
from src.application.analysis import (
    atr, _is_swing_high, _is_swing_low, find_m5_choch_signal,
    evaluate_zone_health,
)

logger = logging.getLogger(__name__)


@dataclass
class MonitorEvent:
    """A signal fired by TradeMonitor for a single open trade."""
    signal_type:      str          # "reverse_choch", "mfe_stall", "trailing_sl", "runner_protection"
    timeframe_source: str          # "M1", "M5", "H1"
    mfe_pips_at_signal: float      # max favourable excursion at time of signal
    pips_to_tp:       float        # remaining distance to TP
    detail:           str = ""     # human-readable reason


class TradeMonitor:
    """
    Monitors a single open trade for exit and trailing conditions.

    Instantiated per trade per monitoring cycle by ExecutionService.
    """

    def __init__(
        self,
        direction:    Direction,
        entry:        float,
        stop_loss:    float,
        take_profit:  float,
        pip_calc:     PipCalculator,
        h1_atr:       float,
    ) -> None:
        self.direction   = direction
        self.entry       = entry
        self.stop_loss   = stop_loss
        self.take_profit = take_profit
        self.pip_calc    = pip_calc
        self.h1_atr      = h1_atr
        self._events: List[MonitorEvent] = []

    def scan_opposing_zones(
        self,
        current_price: float,
        zones:         List[Zone],
        h1_atr:        float,
    ) -> List[Zone]:
        """Return any active zones in the opposite direction within 1x ATR of current price."""
        threshold = h1_atr * getattr(config, 'OPPOSING_ZONE_WARN_ATR', 1.0)
        opposing_dir = Direction.BEARISH if self.direction == Direction.BULLISH else Direction.BULLISH
        result = []
        for z in zones:
            if not z.active or z.direction != opposing_dir:
                continue
            dist = abs(current_price - (z.high + z.low) / 2)
            if dist <= threshold:
                result.append(z)
        return result

    def update_dual(
        self,
        m1_candles: List[Candle],
        m5_candles: List[Candle],
        current_price: float,
        mfe_price: float,
    ) -> List[MonitorEvent]:
        """Run both M1 and M5 checks and return all fired events."""
        events: List[MonitorEvent] = []
        events.extend(self._check_reverse_choch(m1_candles, current_price, mfe_price, "M1"))
        events.extend(self._check_reverse_choch(m5_candles, current_price, mfe_price, "M5"))
        return events

    def update(
        self,
        m5_candles:    List[Candle],
        current_price: float,
        mfe_price:     float,
        zones:         List[Zone],
    ) -> List[MonitorEvent]:
        """Primary update — check M5 reverse ChoCh."""
        return self._check_reverse_choch(m5_candles, current_price, mfe_price, "M5")

    def check_live_dual(
        self,
        m1_candles: List[Candle],
        m5_candles: List[Candle],
        current_price: float,
        mfe_price: float,
    ) -> List[MonitorEvent]:
        """Alias for update_dual — used by ExecutionService for live dual-TF monitoring."""
        return self.update_dual(m1_candles, m5_candles, current_price, mfe_price)

    def _check_zone(self, zone: Zone, current_price: float) -> bool:
        """Return True if current_price is inside or has breached the zone."""
        return zone.low <= current_price <= zone.high

    def _check_reverse_choch(
        self,
        candles:       List[Candle],
        current_price: float,
        mfe_price:     float,
        tf_label:      str,
    ) -> List[MonitorEvent]:
        """Detect a Change of Character against the trade direction."""
        events: List[MonitorEvent] = []
        if len(candles) < 5:
            return events
        mfe_pips = self.pip_calc.price_to_pips(abs(mfe_price - self.entry))
        pips_to_tp = self.pip_calc.price_to_pips(abs(self.take_profit - current_price))
        min_mfe_r  = getattr(config, 'REVERSE_CHOCH_MIN_MFE_R', 0.15)
        sl_dist    = abs(self.entry - self.stop_loss)
        mfe_r      = abs(mfe_price - self.entry) / sl_dist if sl_dist > 0 else 0
        if mfe_r < min_mfe_r:
            return events
        # Detect reversal: last 3 candles closing against trade direction
        recent = candles[-3:]
        if self.direction == Direction.BULLISH:
            reversing = sum(1 for c in recent if c.is_bearish and c.body_size > 0)
        else:
            reversing = sum(1 for c in recent if c.is_bullish and c.body_size > 0)
        if reversing >= 2:
            events.append(MonitorEvent(
                signal_type="reverse_choch",
                timeframe_source=tf_label,
                mfe_pips_at_signal=mfe_pips,
                pips_to_tp=pips_to_tp,
                detail=f"{reversing}/3 candles reversing on {tf_label}",
            ))
        return events


def find_swing_trail_sl(
    candles:    List[Candle],
    direction:  Direction,
    pip_calc:   PipCalculator,
    atr_buffer: float,
) -> Optional[float]:
    """
    Find a new trailing SL based on the most recent swing pivot.
    Returns None if no suitable swing is found.
    """
    if len(candles) < 10:
        return None
    window = candles[-20:]
    if direction == Direction.BULLISH:
        for i in range(len(window) - 4, 3, -1):
            if _is_swing_low(window, i):
                return window[i].low - atr_buffer
    else:
        for i in range(len(window) - 4, 3, -1):
            if _is_swing_high(window, i):
                return window[i].high + atr_buffer
    return None


def find_runner_protection_sl(
    candles:    List[Candle],
    direction:  Direction,
    entry:      float,
    pip_calc:   PipCalculator,
    atr_buffer: float,
) -> Optional[float]:
    """
    Find a conservative SL for the post-TP1 runner.
    Stays above/below entry to protect locked profit.
    """
    swing_sl = find_swing_trail_sl(candles, direction, pip_calc, atr_buffer)
    if swing_sl is None:
        return None
    if direction == Direction.BULLISH:
        return max(swing_sl, entry + atr_buffer * 0.5)
    else:
        return min(swing_sl, entry - atr_buffer * 0.5)
