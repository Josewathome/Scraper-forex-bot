"""
margin_manager.py — Portfolio-level capital allocation authority.

Single source of truth for:
  - Quality-weighted risk percentage (A+/A/B/C trade grading)
  - Portfolio-level entry constraints (max open, per-symbol, equity reserve)
  - Currency correlation exposure limits

Conservative approach: weaker new trades are blocked when capacity is consumed.
We NEVER close an existing trade to free margin for a new one — a running trade
that looks weak may still recover, and we respect that.

Called by EntryGate before any trade is placed. Stateless — reads live MT5
data on every call so it always reflects current portfolio state.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import src.config as config

logger = logging.getLogger(__name__)

# 1:400 leverage → lots × 250.0 = approximate required margin in account currency.
# The old code used 200.0 (1:500) which was wrong for a 1:400 account.
_LEVERAGE_DIVISOR = getattr(config, "LEVERAGE_MARGIN_DIVISOR", 250.0)


class MarginManager:
    """
    Portfolio-level capital allocation authority.

    One instance per bot, created in EntryGate and reused across all symbols.
    """

    def __init__(self, trade_repo, execution_service=None) -> None:
        self._tr   = trade_repo
        self._exec = execution_service  # optional — used to read _trade_states

    # ── Quality grading and risk allocation ───────────────────────────────────

    def quality_risk_pct(
        self,
        confidence: float,
        tq_score:   float = 0.0,
    ) -> Tuple[float, str]:
        """
        Map trade quality to risk percentage and grade label.

        Thresholds (all configurable via config/env):
          A+  conf ≥ 0.85 AND tq ≥ 0.75  →  RISK_HIGH_CONVICTION  (default 2.5%)
          A   conf ≥ 0.70 OR  tq ≥ 0.65  →  RISK_MEDIUM_CONVICTION (default 1.8%)
          B   conf ≥ 0.60 OR  tq ≥ 0.50  →  RISK_LOW_CONVICTION   (default 1.0%)
          C   otherwise                   →  RISK_MIN_CONVICTION    (default 0.5%)

        Returns (risk_pct, grade_string).
        """
        r_aplus = getattr(config, "RISK_HIGH_CONVICTION",    2.5)
        r_a     = getattr(config, "RISK_MEDIUM_CONVICTION",  1.8)
        r_b     = getattr(config, "RISK_LOW_CONVICTION",     1.0)
        r_c     = getattr(config, "RISK_MIN_CONVICTION",     0.5)

        if confidence >= 0.85 and tq_score >= 0.75:
            return r_aplus, "A+"
        elif confidence >= 0.70 or tq_score >= 0.65:
            return r_a, "A"
        elif confidence >= 0.60 or tq_score >= 0.50:
            return r_b, "B"
        else:
            return r_c, "C"

    # ── Portfolio-level entry check ───────────────────────────────────────────

    def can_enter(
        self,
        symbol:     str,
        confidence: float,
        tq_score:   float,
        balance:    float,
    ) -> Tuple[bool, str]:
        """
        Check all portfolio-level constraints before allowing a new entry.

        Returns (allowed: bool, reason: str).
        On allow:  (True,  "ok")
        On block:  (False, human-readable reason for logs)
        """
        open_states = self._get_open_states()
        _, grade    = self.quality_risk_pct(confidence, tq_score)
        open_count  = len(open_states)

        # ── 1. Hard maximum simultaneous open trades ──────────────────
        max_trades = getattr(config, "SCALPER_MAX_OPEN_TRADES", 3)
        if max_trades > 0 and open_count >= max_trades:
            return False, (
                f"MAX_OPEN={max_trades} — {open_count} trades already open | "
                f"new grade={grade} waits for a natural slot"
            )

        # ── 2. Per-symbol limit ───────────────────────────────────────
        # Default: only 1 open trade per symbol at a time.
        max_per_sym = getattr(config, "MAX_TRADES_PER_SYMBOL", 1)
        sym_open    = sum(1 for s in open_states.values() if s.symbol == symbol)
        if sym_open >= max_per_sym:
            return False, (
                f"MAX_PER_SYMBOL={max_per_sym} — {symbol} already has {sym_open} open"
            )

        # ── 3. Soft capacity: block C-grade trades when slots are filling up ─
        # When open_count ≥ SCALPER_SOFT_CAPACITY, we save the remaining
        # slot(s) for A/B quality setups.  C-grade trades can still enter
        # when below soft capacity.
        soft_cap = getattr(config, "SCALPER_SOFT_CAPACITY", 2)
        if open_count >= soft_cap and grade == "C":
            return False, (
                f"SOFT_CAPACITY={soft_cap} reached ({open_count} open) — "
                f"grade=C blocked; reserving slot for A/B setup"
            )

        # ── 4. Equity reserve floor ───────────────────────────────────
        # Always keep MARGIN_RESERVE_PCT of equity free so future strong
        # setups always have enough room.
        try:
            free_margin = self._tr.get_free_margin()
            if free_margin is not None and balance > 0:
                equity      = self._compute_equity(balance)
                reserve_pct = getattr(config, "MARGIN_RESERVE_PCT", 0.25)
                reserve_amt = equity * reserve_pct
                safety      = getattr(config, "MARGIN_SAFETY_FACTOR", 1.2)
                # Minimum margin for a 0.01-lot trade at 1:400 leverage
                min_margin  = 0.01 * _LEVERAGE_DIVISOR * safety
                if free_margin < reserve_amt + min_margin:
                    return False, (
                        f"EQUITY_RESERVE: free={free_margin:.2f} < "
                        f"reserve={reserve_amt:.2f} (equity={equity:.2f} × {reserve_pct:.0%}) "
                        f"+ min_trade={min_margin:.2f} | grade={grade}"
                    )
        except Exception as _e:
            logger.debug("MarginManager equity reserve check error: %s", _e)

        # ── 5. Currency correlation exposure limit ────────────────────
        # Prevent opening too many trades that share the same currency leg
        # (e.g. GBPUSD + USDJPY + USDCHF all have USD exposure).
        max_ccy_exp   = getattr(config, "MAX_CURRENCY_EXPOSURE", 3)
        sym_currencies = getattr(config, "SYMBOL_CURRENCIES", {}).get(symbol, [])
        for ccy in sym_currencies:
            ccy_open = sum(
                1 for s in open_states.values()
                if ccy in getattr(config, "SYMBOL_CURRENCIES", {}).get(s.symbol, [])
            )
            if ccy_open >= max_ccy_exp:
                return False, (
                    f"CURRENCY_EXPOSURE: {ccy} leg already in {ccy_open} open trades "
                    f"(max={max_ccy_exp}) — {symbol} would add correlated exposure"
                )

        return True, "ok"

    # ── Equity calculation ────────────────────────────────────────────────────

    def _compute_equity(self, balance: float) -> float:
        """
        Equity = realized balance + sum of unrealized P&L on open positions.
        Falls back to balance if open position profit data is unavailable.
        """
        try:
            positions   = self._tr.get_open_positions()
            unrealized  = sum(
                float(p.profit)
                for p in positions
                if hasattr(p, "profit") and p.profit is not None
            )
            return balance + unrealized
        except Exception:
            return balance

    def get_equity(self, balance: float) -> float:
        """Public equity accessor for drawdown check in entry_gate."""
        return self._compute_equity(balance)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_open_states(self) -> Dict:
        """Fetch current open trade states from ExecutionService."""
        try:
            if self._exec is not None:
                return getattr(self._exec, "_trade_states", {})
        except Exception:
            pass
        return {}
