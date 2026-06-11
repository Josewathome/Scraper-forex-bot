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


def pip_value_per_lot(
    tick_value: float,
    tick_size:  float,
    pip_size:   float,
) -> float:
    """
    Convert MT5's per-tick value into a per-PIP value per 1.0 lot, in the
    account currency.

    MT5 ``symbol_info.trade_tick_value`` is the money value of one
    ``trade_tick_size`` price move on 1.0 lot. ``trade_tick_size`` is the
    broker's minimum price increment (the point) — for a 5-digit FX symbol
    that is 0.00001, and for a 3-digit JPY symbol 0.001 — which is NOT one pip.

        per_pip_value = tick_value × (pip_size / tick_size)

    Returns 0.0 when inputs are invalid so callers can FAIL CLOSED (reject the
    trade) rather than size off a bad number.
    """
    if tick_value <= 0 or tick_size <= 0 or pip_size <= 0:
        return 0.0
    return tick_value * (pip_size / tick_size)


@dataclass(frozen=True)
class BrokerCost:
    """
    Unified cost container for one symbol at one moment.

    spread_pips        — current bid/ask spread in pips
    commission_per_lot — per-lot per-SIDE commission in ACCOUNT currency
                         (0.0 for spread-only accounts)
    pip_value_per_lot  — money value of 1 pip on 1.0 lot, in ACCOUNT currency.
                         Used to express commission as a pip-equivalent cost.
    """
    spread_pips:        float
    commission_per_lot: float
    pip_value_per_lot:  float = 0.0

    def round_trip_cost_pips(self) -> float:
        """
        Round-trip cost in pips (spread + both sides of commission).

        Commission-in-pips is lot-independent: total commission = 2 × c × lots
        and pip P&L = pip_value × lots, so the lots cancel:
            commission_pips = 2 × commission_per_lot / pip_value_per_lot

        Returns float('inf') when the cost cannot be computed (pip value
        unavailable) so the EV/cost gate FAILS CLOSED instead of treating an
        unknown commission as zero.
        """
        if self.commission_per_lot <= 0:
            # Spread-only account — spread is the whole cost.
            return max(0.0, self.spread_pips)
        if self.pip_value_per_lot <= 0:
            return float("inf")
        commission_pips = (2.0 * self.commission_per_lot) / self.pip_value_per_lot
        return max(0.0, self.spread_pips) + commission_pips


@dataclass(frozen=True)
class RiskParameters:
    """
    Encapsulates the lot-size calculation for a single trade.

    account_balance    — current balance in account currency
    risk_percent       — percent of balance to risk (e.g. 1.0 = 1%)
    commission_per_lot — per-lot per-side commission in account currency
    min_rr_ratio       — minimum R:R required (used for final TP validation)
    account_currency   — e.g. "KES", "USD"
    """
    account_balance:   float
    risk_percent:      float
    commission_per_lot: float
    min_rr_ratio:      float
    account_currency:  str

    def lot_size(self, sl_pips: float, pip_value_per_lot: float) -> float:
        """
        Calculate the raw (unrounded) lot size from money risk.

            risk_money = balance × risk_pct/100        (account currency)
            lot_size   = risk_money / (sl_pips × pip_value_per_lot)

        ``pip_value_per_lot`` MUST be the per-PIP value (account currency) — use
        ``pip_value_per_lot()`` to derive it from MT5 tick data. Returns 0.0 on
        any invalid input so the caller can fail closed.
        """
        if sl_pips <= 0 or pip_value_per_lot <= 0 or self.account_balance <= 0:
            return 0.0
        risk_amount = self.account_balance * (self.risk_percent / 100.0)
        return risk_amount / (sl_pips * pip_value_per_lot)
