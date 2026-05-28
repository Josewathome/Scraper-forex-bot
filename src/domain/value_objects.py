"""
value_objects.py — Immutable calculation helpers used across layers.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipCalculator:
    """Convert between price and pips for a given symbol's digit count."""
    digits: int

    @property
    def pip_size(self) -> float:
        """One pip in price units (0.0001 for 4/5-digit, 0.01 for 2/3-digit JPY/XAU)."""
        if self.digits in (3, 2):   # JPY pairs, indices
            return 0.01
        if self.digits == 0:        # integer-quoted instruments
            return 1.0
        return 0.0001               # standard 4/5-digit forex

    def price_to_pips(self, price_distance: float) -> float:
        return abs(price_distance) / self.pip_size

    def pips_to_price(self, pips: float) -> float:
        return pips * self.pip_size


@dataclass(frozen=True)
class BrokerCost:
    """
    Unified cost container for one symbol at one moment.

    spread_pips    — current bid/ask spread in pips
    commission_usd — per-lot per-side commission in USD (0.0 for spread-only accounts)
    """
    spread_pips:    float
    commission_usd: float

    def total_cost_pips(self, pip_value_usd: float, lot_size: float = 1.0) -> float:
        """Round-trip cost in pips (spread + both sides of commission)."""
        if pip_value_usd <= 0:
            return self.spread_pips
        commission_pips = (2 * self.commission_usd * lot_size) / pip_value_usd
        return self.spread_pips + commission_pips
