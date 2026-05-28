"""
Spread Calculator — Live MT5-based spread computation.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Spread = ask − bid  (from MT5 tick, always available)

HFM account types:
  ZERO_SPREAD — raw spread ≈ 0 pips + fixed per-lot commission
  PREMIUM     — no commission, spread ≈ 1.2+ pips (EURUSD)
  PRO         — no commission, spread ≈ 0.5+ pips (EURUSD)

Fallback chain when live tick unavailable:
  1. Live tick from MT5 via ZMQ                ← primary
  2. symbol_info.spread (in points from MT5)   ← secondary
  3. Account-type-specific static estimate     ← tertiary
  4. Hard floor: 1.0 pip                       ← last resort
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import logging
from typing import Dict, Optional

import src.config as config
from src.domain.repositories import IMarketDataRepository
from src.domain.value_objects import BrokerCost, PipCalculator

logger = logging.getLogger(__name__)

# ── HFM Zero Spread fallbacks (near-zero pips, commission-based) ─────
ZERO_SPREAD_FALLBACK_PIPS: dict[str, float] = {
    "EURUSD": 0.1, "GBPUSD": 0.4, "USDJPY": 0.2,
    "AUDUSD": 0.4, "USDCAD": 0.5, "USDCHF": 0.3,
    "NZDUSD": 0.4, "EURGBP": 0.3, "EURJPY": 0.3, "GBPJPY": 0.5,
    "XAUUSD": 0.1, "XAGUSD": 1.0,
    "US30":   3.0, "NAS100": 1.5,
}

# ── HFM Premium / Pro fallbacks (spread-based, no commission) ────────
# Values include a ~30% safety margin above HFM minimums to cover Monday-open
# and news-spike conditions where live spread data may be unavailable.
PREMIUM_FALLBACK_PIPS: dict[str, float] = {
    "EURUSD": 1.8, "GBPUSD": 2.4, "USDJPY": 2.1,
    "AUDUSD": 2.4, "USDCAD": 2.6, "USDCHF": 2.6,
    "NZDUSD": 2.6, "EURGBP": 2.1, "EURJPY": 2.6, "GBPJPY": 3.3,
    "XAUUSD": 4.6, "XAGUSD": 7.8,
    "US30":   5.2, "NAS100": 2.6,
}

# ── IC Markets Raw Spread fallbacks (ECN spread + $3.50/lot commission) ─
# Raw ECN spreads with ~30% safety margin for Monday-open / news spikes.
# These are used only when MT5 tick data is unavailable at startup.
IC_MARKETS_RAW_FALLBACK_PIPS: dict[str, float] = {
    "EURUSD": 0.2, "GBPUSD": 0.5, "USDJPY": 0.3,
    "AUDUSD": 0.5, "USDCAD": 0.6, "USDCHF": 0.4,
    "NZDUSD": 0.5, "EURGBP": 0.4, "EURJPY": 0.4, "GBPJPY": 0.6,
    "XAUUSD": 0.2, "XAGUSD": 1.2,
    "US30":   3.5, "NAS100": 1.8,
}

# Legacy alias — kept so backtester import still resolves.
FALLBACK_SPREAD_PIPS = ZERO_SPREAD_FALLBACK_PIPS
DEFAULT_FALLBACK_PIPS = 1.5


class SpreadCalculator:
    """
    Computes live BrokerCost from MT5 tick data.
    Supports per-symbol commission (XAUUSD differs from forex pairs on HFM).
    Never raises — always returns a valid BrokerCost even on failure.
    """

    def __init__(
        self,
        market_data:        IMarketDataRepository,
        commission_per_lot: float,                        # default in account currency
        commission_map:     Optional[Dict[str, float]] = None,  # per-symbol override
    ) -> None:
        self._md             = market_data
        self._commission     = commission_per_lot         # fallback
        self._commission_map = commission_map or {}
        # Last known good spread per symbol — used instead of static fallback
        # when MT5 tick is momentarily unavailable. No sleep, no delay.
        self._last_spread:   Dict[str, float] = {}

    def get_broker_cost(self, symbol: str) -> BrokerCost:
        digits     = self._md.get_symbol_digits(symbol)
        tick_value = self._md.get_tick_value(symbol)
        pip_calc   = PipCalculator(digits=digits)

        spread_pips = self._get_spread_pips(symbol, pip_calc)
        commission  = self._commission_map.get(symbol, self._commission)

        cost = BrokerCost(
            spread_pips=    spread_pips,
            commission_usd= commission,
            tick_value=     tick_value,
        )
        logger.debug(
            "BrokerCost %s | spread=%.2f pips | commission=%.2f | "
            "tick_val=%.4f | total_cost=%.2f pips",
            symbol, spread_pips, commission,
            tick_value, cost.total_cost_pips,
        )
        return cost

    def _get_spread_pips(self, symbol: str, pip_calc: PipCalculator) -> float:
        # ── Method 1: live bid/ask from tick ──────────────────────
        try:
            bid, ask = self._md.get_bid_ask(symbol)
            if ask > 0 and bid > 0 and ask > bid:
                spread_pips = pip_calc.price_to_pips(ask - bid)
                if 0 < spread_pips < 50:
                    logger.debug("%s live spread: %.2f pips (bid=%.5f ask=%.5f)",
                                 symbol, spread_pips, bid, ask)
                    self._last_spread[symbol] = spread_pips  # cache it
                    return spread_pips
                logger.warning("%s spread %.2f pips out of range — using fallback", symbol, spread_pips)
        except Exception as exc:
            logger.warning("Live tick unavailable for %s: %s", symbol, exc)

        # ── Method 2: symbol_info.spread (in points, from MT5) ────
        try:
            sp = self._md.get_spread_pips(symbol)
            if sp and 0 < sp < 50:
                logger.debug("%s spread from symbol_info: %.2f pips", symbol, sp)
                self._last_spread[symbol] = sp  # cache it
                return sp
        except Exception as exc:
            logger.warning("symbol_info spread unavailable for %s: %s", symbol, exc)

        # ── Method 3: last known good spread (no delay, no stale static) ─
        if symbol in self._last_spread:
            logger.debug("%s using last known spread: %.2f pips", symbol, self._last_spread[symbol])
            return self._last_spread[symbol]

        # ── Method 4: account-type-specific static fallback (first-run only) ─
        acct_type = getattr(config, "BROKER_ACCOUNT_TYPE", "ZERO_SPREAD").upper()
        if acct_type in ("PREMIUM", "PRO"):
            table = PREMIUM_FALLBACK_PIPS
        elif acct_type == "IC_MARKETS_RAW":
            table = IC_MARKETS_RAW_FALLBACK_PIPS
        else:
            table = ZERO_SPREAD_FALLBACK_PIPS
        fallback = table.get(symbol, DEFAULT_FALLBACK_PIPS)
        logger.warning(
            "%s using static fallback spread: %.2f pips "
            "(no live tick and no cached spread yet — market may be closed; account_type=%s)",
            symbol, fallback, acct_type,
        )
        return fallback