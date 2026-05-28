"""
market_data_repo.py — Windows-native MT5 Market Data.
Implements IMarketDataRepository by calling MT5Gateway directly.
No ZMQ bridge. Runs on Windows where MetaTrader5 is installed.
"""
from __future__ import annotations
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from src.domain.entities import Candle, Timeframe
from src.domain.repositories import IMarketDataRepository
from src.domain.value_objects import PipCalculator
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway

logger = logging.getLogger(__name__)


class BrokerClock:
    """
    Single source of truth for "what time is it now?" in the bot.

    All time-sensitive decisions (session filter, kill zone, news block,
    bar-close detection, checkpoint timestamps) must use broker time —
    the UTC timestamp embedded in the latest IC Markets tick — NOT the
    Linux server's local clock, which may drift or be set to the wrong
    timezone.

    How it works
    ────────────
    • On first call (or when cache is stale), fetches the latest EURUSD
      tick time from MT5.  This is an epoch-second value coming directly
      from IC Markets' infrastructure, already in UTC.
    • The result is cached for `cache_ttl_secs` (default 2 s) so the
      rest of the loop can call `now()` freely without hammering MT5.
    • If MT5 is unavailable or the tick is too old (>120 s, i.e. market
      is closed / weekend), falls back to: last_known_broker_time +
      elapsed wall-clock seconds.  This keeps time advancing correctly
      even when the market is closed, without ever trusting the raw
      server clock for trading decisions.

    Usage
    ─────
        clock = BrokerClock(gateway)
        now   = clock.now()   # datetime, UTC, from IC Markets

    One instance is created in main.py and passed to every component
    that previously called datetime.now(tz=timezone.utc) for decisions.
    """

    # How long (seconds) to cache the broker timestamp before re-fetching.
    # 2 s is safe — the main loop interval is ≥10 s so we never over-fetch.
    _CACHE_TTL: float = 2.0

    # If the latest tick is older than this (seconds), the market is closed /
    # weekend.  We stop trusting it as "current" and use dead-reckoning instead.
    _MAX_TICK_AGE: float = 120.0

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw                = gateway
        self._cached_broker_ts: Optional[float]  = None   # epoch seconds, from IC Markets
        self._cached_at:        float             = 0.0    # monotonic time of last fetch
        # Last broker time we successfully obtained — used as dead-reckoning base.
        self._last_good_broker: Optional[datetime] = None
        self._last_good_wall:   float              = 0.0   # monotonic when we got it

    def now(self) -> datetime:
        """
        Return the current UTC datetime according to IC Markets' server.

        Never raises.  Falls back gracefully when MT5 is unavailable.
        """
        mono = _time.monotonic()

        # Return cached value if still fresh.
        if self._cached_broker_ts is not None and (mono - self._cached_at) < self._CACHE_TTL:
            return datetime.fromtimestamp(self._cached_broker_ts, tz=timezone.utc)

        # Ask MT5 for the latest tick timestamp.
        try:
            ts = self._gw.get_server_time()   # epoch int from IC Markets tick
        except Exception:
            ts = None

        if ts is not None:
            wall_now = _time.time()
            tick_age = wall_now - ts
            if tick_age <= self._MAX_TICK_AGE:
                # Fresh tick — trust it fully.
                self._cached_broker_ts = float(ts)
                self._cached_at        = mono
                broker_dt              = datetime.fromtimestamp(ts, tz=timezone.utc)
                self._last_good_broker = broker_dt
                self._last_good_wall   = mono
                return broker_dt
            else:
                # Stale tick (market closed / weekend).
                # Use dead-reckoning: last good broker time + elapsed wall-clock.
                # Wall-clock is only used here as a *delta* (how many seconds passed),
                # never as an absolute time reference — so server timezone is irrelevant.
                if self._last_good_broker is not None:
                    elapsed = mono - self._last_good_wall
                    estimated = self._last_good_broker + timedelta(seconds=elapsed)
                    # Cache the estimate so we don't re-fetch every call.
                    self._cached_broker_ts = estimated.timestamp()
                    self._cached_at        = mono
                    return estimated
                # No prior good reading — use the stale tick directly.
                # Still better than the server's local clock.
                self._cached_broker_ts = float(ts)
                self._cached_at        = mono
                return datetime.fromtimestamp(ts, tz=timezone.utc)

        # MT5 completely unavailable — dead-reckon from last good reading.
        if self._last_good_broker is not None:
            elapsed = mono - self._last_good_wall
            estimated = self._last_good_broker + timedelta(seconds=elapsed)
            self._cached_broker_ts = estimated.timestamp()
            self._cached_at        = mono
            logger.debug("BrokerClock: MT5 unavailable — dead-reckoning broker time.")
            return estimated

        # Absolute last resort: nothing to go on.
        # Log once so the operator knows the clock is unanchored.
        logger.warning(
            "BrokerClock: no broker time available and no prior reading — "
            "falling back to server local clock.  Ensure MT5 is connected."
        )
        return datetime.now(tz=timezone.utc)

    def invalidate(self) -> None:
        """Force the next call to re-fetch from MT5 (used at loop start)."""
        self._cached_broker_ts = None
        self._cached_at        = 0.0


class MT5MarketDataRepository(IMarketDataRepository):
    """
    Fetches OHLCV data and symbol metadata directly from MT5Gateway.
    All timestamps are converted to UTC on the way in.
    """

    _SYMBOL_CACHE_TTL = 60  # seconds; symbol info refreshed at most once per minute

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw = gateway
        self._symbol_cache: dict[str, Tuple[dict, float]] = {}  # symbol → (info, fetched_at)

    # ── IMarketDataRepository ─────────────────

    def get_candles(
        self,
        symbol:    str,
        timeframe: Timeframe,
        count:     int,
    ) -> List[Candle]:
        raw = self._gw.get_candles(
            symbol=    symbol,
            timeframe= timeframe.value,
            count=     count + 1,   # fetch one extra — last bar may still be forming
        )
        if not raw:
            logger.error("get_candles returned nothing for %s/%s", symbol, timeframe)
            return []

        candles = []
        for r in raw[:-1]:   # drop the last (still-forming) candle
            ts = datetime.fromtimestamp(r["time"], tz=timezone.utc)
            candles.append(Candle(
                time=      ts,
                open=      r["open"],
                high=      r["high"],
                low=       r["low"],
                close=     r["close"],
                volume=    r["volume"],
                symbol=    symbol,
                timeframe= timeframe,
            ))
        return candles

    def get_current_price(self, symbol: str) -> float:
        tick = self._gw.get_tick(symbol)
        return tick["ask"] if tick else 0.0

    def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        tick = self._gw.get_tick(symbol)
        if tick is None:
            return 0.0, 0.0
        return tick["bid"], tick["ask"]

    def get_spread_pips(self, symbol: str) -> float:
        tick = self._gw.get_tick(symbol)
        info = self._get_symbol_info_cached(symbol)
        if tick is None or info is None:
            return 0.0
        raw_spread = tick["ask"] - tick["bid"]
        pc = PipCalculator(digits=info["digits"])
        return pc.price_to_pips(raw_spread)

    def get_tick_value(self, symbol: str) -> float:
        info = self._get_symbol_info_cached(symbol)
        return info["trade_tick_value"] if info else 0.0

    def get_symbol_digits(self, symbol: str) -> int:
        info = self._get_symbol_info_cached(symbol)
        return info["digits"] if info else 5

    def get_server_time_utc(self) -> datetime:
        """Return broker server time (IC Markets UTC).  Never uses local clock."""
        ts = self._gw.get_server_time()
        if ts is None:
            # MT5 unavailable — callers should use BrokerClock.now() instead,
            # which has dead-reckoning.  Return a safe non-local fallback.
            logger.warning("get_server_time_utc: MT5 unavailable — returning epoch 0 as sentinel.")
            return datetime.fromtimestamp(0, tz=timezone.utc)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # ── Helpers ───────────────────────────────

    def _get_symbol_info_cached(self, symbol: str) -> Optional[dict]:
        now = _time.monotonic()
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            info, fetched_at = cached
            if now - fetched_at < self._SYMBOL_CACHE_TTL:
                return info
        info = self._gw.get_symbol_info(symbol)
        if info:
            self._symbol_cache[symbol] = (info, now)
        return info
    
    def get_candles_range(
        self,
        symbol:    str,
        timeframe: Timeframe,
        start_dt:  datetime,
        end_dt:    datetime,
    ) -> List[Candle]:
        """Fetch all candles in a date range. Use this for backtesting."""
        raw = self._gw.get_candles_range(
            symbol=    symbol,
            timeframe= timeframe.value,
            start_dt=  start_dt,
            end_dt=    end_dt,
        )
        if not raw:
            return []
        candles = []
        for r in raw:
            ts = datetime.fromtimestamp(r["time"], tz=timezone.utc)
            candles.append(Candle(
                time=ts, open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                symbol=symbol, timeframe=timeframe,
            ))
        return candles