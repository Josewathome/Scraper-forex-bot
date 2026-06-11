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
from src.infrastructure.time_sync import TrueTimeClock

logger = logging.getLogger(__name__)


class BrokerClock:
    """
    Single source of truth for "what time is it now?" in the bot.

    Uses TrueTimeClock (NTP-calibrated) as the authoritative time source.
    The broker's MT5 tick timestamps are NOT used for absolute time — they
    are broker-local epoch values (UTC+3) and produce wrong UTC datetimes
    when passed to datetime.fromtimestamp(..., UTC).

    TrueTimeClock hierarchy:
      1. NTP UDP (pool.ntp.org, time.cloudflare.com, time.google.com)
      2. HTTP time API (worldtimeapi.org) — UDP/123 firewalled fallback
      3. System clock — Docker containers are NTP-synced from host

    Usage
    ─────
        clock = BrokerClock(gateway)
        now   = clock.now()   # datetime, true UTC, NTP-calibrated
    """

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw  = gateway
        self._true_clock = TrueTimeClock()

    def now(self) -> datetime:
        """Return current true UTC datetime (NTP-calibrated). Never raises."""
        return self._true_clock.utc_now()

    def invalidate(self) -> None:
        """No-op: TrueTimeClock manages its own re-sync schedule."""
        pass
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