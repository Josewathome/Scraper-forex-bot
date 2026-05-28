"""
repositories.py — Abstract repository interfaces.

Concrete implementations live in src/infrastructure/.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Tuple

from src.domain.entities import Candle, NewsEvent, Timeframe, Trade, Zone, LiquidityPool


class IMarketDataRepository(ABC):

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: Timeframe, count: int) -> List[Candle]: ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> float: ...

    @abstractmethod
    def get_bid_ask(self, symbol: str) -> Tuple[float, float]: ...

    @abstractmethod
    def get_spread_pips(self, symbol: str) -> float: ...

    @abstractmethod
    def get_tick_value(self, symbol: str) -> float: ...

    @abstractmethod
    def get_symbol_digits(self, symbol: str) -> int: ...

    @abstractmethod
    def get_server_time_utc(self) -> datetime: ...

    def get_candles_range(
        self,
        symbol:    str,
        timeframe: Timeframe,
        start:     datetime,
        end:       datetime,
    ) -> List[Candle]:
        """Optional: fetch candles between two timestamps. Not all repos support this."""
        raise NotImplementedError


class ITradeRepository(ABC):

    @abstractmethod
    def place_trade(self, trade: Trade) -> Optional[int]: ...

    @abstractmethod
    def close_trade(self, ticket: int) -> bool: ...

    @abstractmethod
    def modify_sl(self, ticket: int, new_sl: float) -> bool: ...

    @abstractmethod
    def get_open_positions(self) -> List[Trade]: ...

    @abstractmethod
    def get_account_balance(self) -> float: ...

    @abstractmethod
    def get_free_margin(self) -> float: ...

    def partial_close_trade(self, ticket: int, volume: float, comment: str = "") -> bool:
        raise NotImplementedError

    def get_remaining_volume(self, ticket: int) -> Optional[float]:
        raise NotImplementedError

    def get_required_margin(self, trade: Trade) -> Optional[float]:
        raise NotImplementedError


class IZoneRepository(ABC):

    @abstractmethod
    def get_active_zones(self, symbol: str) -> List[Zone]: ...

    @abstractmethod
    def save_zone(self, zone: Zone) -> None: ...

    @abstractmethod
    def invalidate_zone(self, zone_id: str) -> None: ...

    @abstractmethod
    def get_liquidity_pool(self, symbol: str) -> Optional[LiquidityPool]: ...


class INewsRepository(ABC):

    @abstractmethod
    def get_today_events(self, currencies: List[str]) -> List[NewsEvent]: ...
