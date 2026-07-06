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
from src.domain.value_objects import PipCalculator, pip_value_per_lot
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway
from src.infrastructure.mt5_time import mt5_epoch_to_datetime

logger = logging.getLogger(__name__)


class BrokerClock:
    """
    Single source of truth for "what time is it now?" in the bot.

    Derived directly from MT5's own server time (the latest tick timestamp),
    via mt5_epoch_to_datetime() — never from the host machine's clock, and
    never from NTP. The host clock is read only as an absolute last resort
    (see `now()`), and that fallback is logged loudly since it means MT5
    has never once returned a usable tick since the process started.

    Cached briefly to avoid hammering MT5 on every call; while MT5 is
    briefly unreachable (or the market is closed and the last tick is
    stale), time is advanced by dead-reckoning: last known-good MT5 time +
    elapsed wall-clock seconds since that reading. The wall-clock delta is
    used only as a *duration* (how many seconds passed), never as an
    absolute time reference, so host-clock timezone/offset never matters.

    Usage
    ─────
        clock = BrokerClock(gateway)
        now   = clock.now()   # datetime, MT5 server time, offset-corrected
    """

    _CACHE_TTL:    float = 2.0     # seconds to cache a fresh MT5 reading
    _MAX_TICK_AGE: float = 120.0   # seconds — beyond this, dead-reckon instead

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw = gateway
        self._cached_broker_ts: Optional[float]  = None   # raw MT5 epoch seconds
        self._cached_at:        float             = 0.0    # monotonic time of last fetch
        self._last_good_broker: Optional[datetime] = None  # last good bot-time reading
        self._last_good_wall:   float              = 0.0   # monotonic when we got it

    def now(self) -> datetime:
        """Return the current bot time (MT5 server time, offset-corrected). Never raises."""
        mono = _time.monotonic()

        if self._cached_broker_ts is not None and (mono - self._cached_at) < self._CACHE_TTL:
            return mt5_epoch_to_datetime(self._cached_broker_ts)

        try:
            ts = self._gw.get_server_time()
        except Exception:
            ts = None

        if ts is not None:
            # Compare like-for-like: broker_dt is already offset-corrected
            # (true UTC), so diff it against wall_now (also true UTC) —
            # never diff the raw uncorrected `ts` against wall_now directly.
            # That mismatch is the exact "two different time bases" bug
            # candle_builder.py's on_bar_close_from_ea() had: with a nonzero
            # BROKER_UTC_OFFSET_HOURS, comparing raw ts to true-UTC wall_now
            # skews tick_age by offset_hours*3600 seconds, which (at the
            # current 2-hour offset) pushes the effective staleness
            # threshold from 120s to ~2 hours — silently defeating dead-
            # reckoning for exactly the short MT5 hiccups it exists to catch.
            wall_now  = _time.time()
            broker_dt = mt5_epoch_to_datetime(ts)
            tick_age  = wall_now - broker_dt.timestamp()
            if tick_age <= self._MAX_TICK_AGE:
                self._cached_broker_ts = float(ts)
                self._cached_at        = mono
                self._last_good_broker = broker_dt
                self._last_good_wall   = mono
                return broker_dt
            # Stale tick (market closed / weekend) — dead-reckon from the
            # last good reading rather than trust a stale absolute value.
            if self._last_good_broker is not None:
                elapsed   = mono - self._last_good_wall
                estimated = self._last_good_broker + timedelta(seconds=elapsed)
                self._cached_broker_ts = estimated.timestamp()
                self._cached_at        = mono
                return estimated
            # No prior good reading — use the stale tick directly, but seed
            # the dead-reckoning base from it too. Without this, every
            # subsequent call (as long as MT5 keeps returning this same
            # stale tick — e.g. an entire weekend market closure) would
            # fall into this exact branch again and return the identical
            # frozen timestamp forever instead of advancing, silently
            # breaking anything that depends on "now" moving forward while
            # the market is closed (Scheduler's daily/weekly checks, the
            # periodic-checkpoint interval, gap-recovery boundary math).
            broker_dt              = mt5_epoch_to_datetime(ts)
            self._cached_broker_ts = float(ts)
            self._cached_at        = mono
            self._last_good_broker = broker_dt
            self._last_good_wall   = mono
            return broker_dt

        # MT5 unreachable this call — dead-reckon from last good reading.
        if self._last_good_broker is not None:
            elapsed   = mono - self._last_good_wall
            estimated = self._last_good_broker + timedelta(seconds=elapsed)
            self._cached_broker_ts = estimated.timestamp()
            self._cached_at        = mono
            logger.debug("BrokerClock: MT5 unavailable — dead-reckoning from last good MT5 time.")
            return estimated

        # Absolute last resort: MT5 has NEVER returned a usable tick since
        # startup. There is no MT5-derived time to dead-reckon from, so this
        # falls back to the host clock — loudly, because it means every
        # time-based decision made right now is running on the one clock
        # this bot is designed not to trust.
        logger.error(
            "BrokerClock: no MT5 time available and no prior reading — "
            "falling back to the HOST CLOCK. Trading decisions made right now "
            "are NOT using MT5 time. Check the MT5 connection."
        )
        return datetime.now(tz=timezone.utc)

    def invalidate(self) -> None:
        """Force the next now() call to re-fetch from MT5 instead of using the cache."""
        self._cached_broker_ts = None
        self._cached_at        = 0.0


class MT5MarketDataRepository(IMarketDataRepository):
    """
    Fetches OHLCV data and symbol metadata directly from MT5Gateway.
    Every timestamp is converted via mt5_epoch_to_datetime() on the way in,
    so candle.time is always MT5 server time, offset-corrected — never the
    host clock.
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
            ts = mt5_epoch_to_datetime(r["time"])
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

    def get_tick_size(self, symbol: str) -> float:
        info = self._get_symbol_info_cached(symbol)
        return info["trade_tick_size"] if info else 0.0

    def get_pip_value(self, symbol: str) -> float:
        """
        Per-PIP money value for 1.0 lot, in account currency.

        Derived correctly from MT5 tick data:
            pip_value = trade_tick_value × (pip_size / trade_tick_size)

        Returns 0.0 when symbol info is unavailable or inconsistent so the
        caller FAILS CLOSED (rejects the trade) rather than sizing off a bad
        number. This is the fix for the 10× lot-sizing error on 5-/3-digit
        symbols where trade_tick_size (the point) ≠ one pip.
        """
        info = self._get_symbol_info_cached(symbol)
        if not info:
            logger.error("get_pip_value(%s): no symbol info — cannot size safely", symbol)
            return 0.0
        pip_size = PipCalculator(digits=info["digits"]).pip_size
        pv = pip_value_per_lot(
            tick_value=info["trade_tick_value"],
            tick_size=info["trade_tick_size"],
            pip_size=pip_size,
        )
        if pv <= 0:
            logger.error(
                "get_pip_value(%s): invalid tick data (tick_value=%s tick_size=%s) — pv=%s",
                symbol, info.get("trade_tick_value"), info.get("trade_tick_size"), pv,
            )
        return pv

    def get_volume_constraints(self, symbol: str):
        info = self._get_symbol_info_cached(symbol)
        if not info:
            return None
        return (info["volume_min"], info["volume_max"], info["volume_step"])

    def get_symbol_meta(self, symbol: str) -> Optional[dict]:
        return self._get_symbol_info_cached(symbol)

    def get_symbol_digits(self, symbol: str) -> int:
        info = self._get_symbol_info_cached(symbol)
        return info["digits"] if info else 5

    def get_server_time_utc(self) -> datetime:
        """Return MT5 server time, offset-corrected.  Never uses the host clock."""
        ts = self._gw.get_server_time()
        if ts is None:
            # MT5 unavailable — callers should use BrokerClock.now() instead,
            # which has dead-reckoning.  Return a safe non-local fallback.
            logger.warning("get_server_time_utc: MT5 unavailable — returning epoch 0 as sentinel.")
            return datetime.fromtimestamp(0, tz=timezone.utc)
        return mt5_epoch_to_datetime(ts)

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
            ts = mt5_epoch_to_datetime(r["time"])
            candles.append(Candle(
                time=ts, open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                symbol=symbol, timeframe=timeframe,
            ))
        return candles