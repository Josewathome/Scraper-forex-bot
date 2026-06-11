"""
streaming_market_data_repo.py — IMarketDataRepository backed by CandleBuilder.

Drop-in replacement for MT5MarketDataRepository. Serves candles from the
in-process CandleBuilder caches that are continuously updated by the ZMQ tick
feed.

All live-price methods (get_current_price, get_bid_ask, get_spread_pips,
get_tick_value, get_symbol_digits, get_server_time_utc) delegate to the real
MT5 repo because those must always be current broker values.

get_candles() is the only method served from the builder cache. Everything else
is a direct pass-through to MT5.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from src.domain.entities import Candle, Timeframe
from src.domain.repositories import IMarketDataRepository
from src.infrastructure.stream.candle_builder import CandleBuilder

logger = logging.getLogger(__name__)


class StreamingMarketDataRepo(IMarketDataRepository):
    """
    Implements IMarketDataRepository by serving candles from CandleBuilder
    caches rather than live MT5 COM requests.

    Parameters
    ----------
    builders : Dict[str, CandleBuilder]
        One CandleBuilder per symbol, keyed by symbol string (e.g. "GBPUSD").
    fallback : IMarketDataRepository
        The real MT5 repo. Used for all live-price methods and as a last-resort
        fallback when the builder cache is empty (startup race).
    """

    def __init__(
        self,
        builders: Dict[str, CandleBuilder],
        fallback: IMarketDataRepository,
    ) -> None:
        self._builders = builders
        self._fallback = fallback

    # ── Candle data — served from builder cache ────────────────────────

    def get_candles(
        self,
        symbol:    str,
        timeframe: Timeframe,
        count:     int,
    ) -> List[Candle]:
        builder = self._builders.get(symbol)
        if builder is None:
            logger.warning(
                "StreamingMarketDataRepo: no builder for %s, falling back to MT5",
                symbol,
            )
            return self._fallback.get_candles(symbol, timeframe, count)

        candles = builder.get_closed_candles(timeframe, count)

        if not candles:
            logger.debug(
                "StreamingMarketDataRepo[%s/%s]: cache empty — falling back to MT5",
                symbol, timeframe.value,
            )
            return self._fallback.get_candles(symbol, timeframe, count)

        if len(candles) < count:
            logger.debug(
                "StreamingMarketDataRepo[%s/%s]: cache has %d/%d bars",
                symbol, timeframe.value, len(candles), count,
            )

        return candles

    def get_forming_bar(self, symbol: str, timeframe: Timeframe) -> Optional[Candle]:
        """Return the currently-forming (incomplete) bar from the builder.

        This is not part of IMarketDataRepository but is called directly by
        ExecutionService when it needs the live forming M5 bar for the
        M5-colour gate in find_m1_predictive_signal.
        """
        builder = self._builders.get(symbol)
        if builder is None:
            return None
        return builder.get_forming_bar(timeframe)

    # ── Live price / server time — always MT5 ─────────────────────────

    def get_current_price(self, symbol: str) -> float:
        return self._fallback.get_current_price(symbol)

    def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        return self._fallback.get_bid_ask(symbol)

    def get_spread_pips(self, symbol: str) -> float:
        return self._fallback.get_spread_pips(symbol)

    def get_tick_value(self, symbol: str) -> float:
        return self._fallback.get_tick_value(symbol)

    def get_tick_size(self, symbol: str) -> float:
        return self._fallback.get_tick_size(symbol)

    def get_pip_value(self, symbol: str) -> float:
        return self._fallback.get_pip_value(symbol)

    def get_volume_constraints(self, symbol: str):
        return self._fallback.get_volume_constraints(symbol)

    def get_symbol_meta(self, symbol: str):
        return self._fallback.get_symbol_meta(symbol)

    def get_symbol_digits(self, symbol: str) -> int:
        return self._fallback.get_symbol_digits(symbol)

    def get_server_time_utc(self) -> datetime:
        return self._fallback.get_server_time_utc()
