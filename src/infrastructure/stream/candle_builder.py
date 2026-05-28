"""
candle_builder.py — Tick-to-Candle Aggregator.

Receives raw ticks (bid/ask/time) from the ZMQ feed and builds OHLCV
candles locally for M1, M5, and H1 timeframes.

WHY LOCAL CANDLE BUILDING:
  MT5's get_candles() returns the broker's version of completed candles.
  Building candles from ticks ourselves gives:
    - Exact bar boundaries (we decide when each bar closes)
    - No MT5 round-trip on every loop tick
    - No "forming bar" confusion — we know exactly what state each bar is in
    - Deterministic candle state regardless of MT5 connection quality
    - The forming bar is always available (it's our live state, not a partial fetch)

HOW IT WORKS:
  Each timeframe has a "forming" bar that accumulates ticks.
  When the current tick's epoch crosses a bar boundary:
    1. The forming bar is finalised → emitted as a closed Candle
    2. A new forming bar starts from the current tick

  Bar boundary for timeframe with bar_seconds S:
    bar_open = floor(tick_time / S) * S

THREAD SAFETY:
  This class is not thread-safe. It is designed to be called from a single
  event loop thread (the ZMQ subscriber thread). All consumers of closed
  candles receive them via callback, not shared state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from src.domain.entities import Candle, Timeframe

logger = logging.getLogger(__name__)

# Timeframes we build candles for, and their bar size in seconds.
TRACKED_TIMEFRAMES: Dict[Timeframe, int] = {
    Timeframe.M1:  60,
    Timeframe.M5:  300,
    Timeframe.M30: 1800,
    Timeframe.H1:  3600,
    Timeframe.H4:  14400,
}

# Callback type: called when a candle closes
OnCandleClose = Callable[[Candle], None]


@dataclass
class _FormingBar:
    """A bar that is currently accumulating ticks."""
    symbol:     str
    timeframe:  Timeframe
    bar_open:   int         # epoch-seconds of bar open
    open:       float
    high:       float
    low:        float
    close:      float
    tick_vol:   int = 0     # tick count as proxy for volume


class CandleBuilder:
    """
    Builds OHLCV candles from raw ticks for a single symbol.

    One instance per symbol. Multiple timeframes are tracked simultaneously —
    a single incoming tick advances all timeframe bars at once.

    Usage:
        builder = CandleBuilder("GBPUSD", on_close=my_callback)
        builder.on_tick(bid=1.26490, ask=1.26500, tick_time=1748340063)
        # When a bar closes, my_callback(closed_candle) is called.
    """

    def __init__(
        self,
        symbol:   str,
        on_close: Optional[OnCandleClose] = None,
    ) -> None:
        self._symbol   = symbol
        self._on_close = on_close
        # One forming bar per tracked timeframe
        self._forming: Dict[Timeframe, Optional[_FormingBar]] = {
            tf: None for tf in TRACKED_TIMEFRAMES
        }
        # Closed candle cache: last N closed candles per timeframe
        # Used by the event-driven loop to fetch the recent window without
        # asking MT5.  Max depth = max(H1_CANDLE_COUNT, M5_CANDLE_COUNT, M1_CANDLE_COUNT)
        self._closed: Dict[Timeframe, List[Candle]] = {
            tf: [] for tf in TRACKED_TIMEFRAMES
        }
        self._max_cache: Dict[Timeframe, int] = {
            Timeframe.M1:  120,   # 2 hours of M1 (enough for signal finders + gap)
            Timeframe.M5:  72,    # 6 hours of M5
            Timeframe.M30: 60,    # 30 hours of M30 (M30 story needs 8 bars of context)
            Timeframe.H1:  120,   # 5 days of H1
            Timeframe.H4:  60,    # 10 days of H4
        }

    def seed_from_history(self, timeframe: Timeframe, candles: List[Candle]) -> None:
        """
        Pre-populate the closed-candle cache with historical bars fetched
        at startup.  Called once per timeframe before the live tick stream
        starts so signal finders have a full window from the first tick.

        The most recent candle in `candles` becomes the starting state for
        the forming bar (its open price seeds the next bar's open).
        """
        if not candles:
            return
        max_depth = self._max_cache[timeframe]
        self._closed[timeframe] = list(candles[-max_depth:])
        logger.debug(
            "CandleBuilder[%s] seeded %d %s bars from history",
            self._symbol, len(self._closed[timeframe]), timeframe.value,
        )

    def on_tick(self, bid: float, ask: float, tick_time: int) -> None:
        """
        Process a single tick.

        `tick_time` is epoch-seconds from the broker (IC Markets server time).
        Uses mid-price (bid+ask)/2 as the candle price so bars are not
        distorted by spread direction.
        """
        mid = (bid + ask) / 2.0

        for tf, bar_secs in TRACKED_TIMEFRAMES.items():
            bar_open_epoch = (tick_time // bar_secs) * bar_secs
            forming = self._forming[tf]

            if forming is None:
                # First tick — start the forming bar
                self._forming[tf] = _FormingBar(
                    symbol=self._symbol, timeframe=tf,
                    bar_open=bar_open_epoch,
                    open=mid, high=mid, low=mid, close=mid,
                    tick_vol=1,
                )
                continue

            if bar_open_epoch != forming.bar_open:
                # Bar boundary crossed — finalise and emit the closed candle
                self._finalise_bar(tf, forming)

                # Start new forming bar from this tick
                self._forming[tf] = _FormingBar(
                    symbol=self._symbol, timeframe=tf,
                    bar_open=bar_open_epoch,
                    open=mid, high=mid, low=mid, close=mid,
                    tick_vol=1,
                )
            else:
                # Same bar — update OHLC
                forming.high    = max(forming.high, mid)
                forming.low     = min(forming.low,  mid)
                forming.close   = mid
                forming.tick_vol += 1

    def on_bar_close_from_ea(
        self,
        timeframe: Timeframe,
        bar_time:  int,   # open-time of the closed bar (epoch-seconds)
        open_p:    float,
        high_p:    float,
        low_p:     float,
        close_p:   float,
        volume:    int,
    ) -> None:
        """
        Accept a BAR_CLOSE event pushed directly by the EA.

        The EA uses broker-side OHLCV which includes ticks we may have
        missed (e.g., during a brief connection blip). This method merges
        the EA-reported bar with the locally-built one:
          - If the EA's bar is newer than the last closed bar: accept it.
          - Otherwise: discard (duplicate from reconnect).

        This is the "hybrid trust" approach: tick-built candles are
        preferred for real-time accuracy; EA-pushed candles serve as a
        correction layer for missed ticks.
        """
        closed = self._closed[timeframe]
        if closed and closed[-1].time.timestamp() >= bar_time:
            return  # already have this bar (or a newer one) — discard

        candle = Candle(
            time=      datetime.fromtimestamp(bar_time, tz=timezone.utc),
            open=      open_p,
            high=      high_p,
            low=       low_p,
            close=     close_p,
            volume=    float(volume),
            symbol=    self._symbol,
            timeframe= timeframe,
        )
        self._store_closed(timeframe, candle)
        if self._on_close:
            self._on_close(candle)

    def get_closed_candles(self, timeframe: Timeframe, count: int) -> List[Candle]:
        """
        Return the most recent `count` closed candles for this symbol/timeframe.

        This is the drop-in replacement for MT5MarketDataRepository.get_candles().
        Signal finders call this instead of going to MT5.
        """
        return list(self._closed[timeframe][-count:])

    def get_forming_bar(self, timeframe: Timeframe) -> Optional[Candle]:
        """
        Return the currently-forming (incomplete) bar as a Candle.

        Used by the M1 bar-position gate and the M5 colour gate inside
        find_m1_predictive_signal — they need to see the partially-formed
        current bar without it being mixed into the signal window.
        Returns None if no tick has arrived yet.
        """
        forming = self._forming.get(timeframe)
        if forming is None:
            return None
        return Candle(
            time=      datetime.fromtimestamp(forming.bar_open, tz=timezone.utc),
            open=      forming.open,
            high=      forming.high,
            low=       forming.low,
            close=     forming.close,
            volume=    float(forming.tick_vol),
            symbol=    self._symbol,
            timeframe= timeframe,
        )

    # ── Private ───────────────────────────────────────────────────────

    def _finalise_bar(self, tf: Timeframe, forming: _FormingBar) -> None:
        """Convert a forming bar to a closed Candle, store it, and fire callback."""
        candle = Candle(
            time=      datetime.fromtimestamp(forming.bar_open, tz=timezone.utc),
            open=      forming.open,
            high=      forming.high,
            low=       forming.low,
            close=     forming.close,
            volume=    float(forming.tick_vol),
            symbol=    self._symbol,
            timeframe= tf,
        )
        self._store_closed(tf, candle)
        logger.debug(
            "CandleBuilder[%s/%s] bar closed | O=%.5f H=%.5f L=%.5f C=%.5f ticks=%d",
            self._symbol, tf.value,
            candle.open, candle.high, candle.low, candle.close, forming.tick_vol,
        )
        if self._on_close:
            self._on_close(candle)

    def _store_closed(self, tf: Timeframe, candle: Candle) -> None:
        cache    = self._closed[tf]
        max_size = self._max_cache[tf]
        cache.append(candle)
        if len(cache) > max_size:
            del cache[0]
