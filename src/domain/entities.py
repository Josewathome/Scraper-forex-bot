"""
entities.py — Core domain data classes.

These are pure data containers with no external dependencies.
All strategy, infrastructure, and application code imports from here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ── Enumerations ──────────────────────────────────────────────────────────────

class Timeframe(str, Enum):
    M1  = "M1"
    M5  = "M5"
    M15 = "M15"
    M30 = "M30"
    H1  = "H1"
    H4  = "H4"
    D1  = "D1"


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN    = "open"
    CLOSED  = "closed"


class ZoneType(str, Enum):
    ORDER_BLOCK    = "ORDER_BLOCK"
    FAIR_VALUE_GAP = "FAIR_VALUE_GAP"


class SignalType(str, Enum):
    PREDICTIVE = "PREDICTIVE"
    STORY      = "STORY"
    CHOCH      = "CHOCH"
    PRECISION  = "PRECISION"


# ── Candle ────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    time:      datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    symbol:    str
    timeframe: Timeframe

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


# ── Trade ─────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    id:          str
    symbol:      str
    direction:   Direction
    entry_price: float
    stop_loss:   float
    take_profit: float
    lot_size:    float
    status:      TradeStatus = TradeStatus.PENDING
    created_at:  datetime    = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    mt5_ticket:  Optional[int] = None
    commission:   float = 0.0    # per-lot per-side commission paid (USD)
    spread_cost:  float = 0.0    # spread cost at entry (USD)


# ── Signal ────────────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    symbol:      str
    direction:   Direction
    entry_price: float
    stop_loss:   float
    take_profit: float
    signal_type: SignalType
    trade_score: int         = 0
    timeframe:   Optional[Timeframe] = None
    zone:        Optional[object]    = None   # Zone reference — typed loosely to avoid circular imports


# ── Zone ─────────────────────────────────────────────────────────────────────

@dataclass
class Zone:
    id:          str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol:      str = ""
    zone_type:   ZoneType = ZoneType.ORDER_BLOCK
    direction:   Direction = Direction.BULLISH
    high:        float = 0.0
    low:         float = 0.0
    created_at:  datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    touch_count: int   = 0
    active:      bool  = True

    def mark_tapped(self) -> None:
        self.touch_count += 1

    def invalidate(self) -> None:
        self.active = False


@dataclass
class LiquidityPool:
    symbol:    str
    direction: Direction   # BULLISH = pool of buy-side liquidity (above highs)
    price:     float
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ── News ─────────────────────────────────────────────────────────────────────

@dataclass
class NewsEvent:
    currency:   str
    event_name: str
    event_time: datetime
    impact:     str             # "high", "medium", "low"
    actual:     str | None = None
    forecast:   str | None = None
    previous:   str | None = None
    source:     str = ""


@dataclass
class NoTradeWindow:
    start:      datetime
    end:        datetime
    currency:   str
    event_name: str

    def is_blocking(self, now: datetime) -> bool:
        return self.start <= now <= self.end
