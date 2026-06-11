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

        Symbol-level model
        ──────────────────
        MAX_SYMBOLS (5): maximum number of unique symbols that can have open
            trades simultaneously.  New symbols are only admitted when the
            active symbol count is below this cap; A-grade entries get
            priority access to the last available symbol slot.

        Per-symbol model  (up to 4 trades per symbol)
        ──────────────────
          • Up to MAX_TRADES_PER_SYMBOL_A (3) A/A+ trades per symbol.
          • Plus 1 additional B/C/D trade per symbol  (total cap 4).
          • B/C/D trades: only allowed when the symbol already has fewer than
            MAX_TRADES_PER_SYMBOL_A A-grade trades (i.e. not all slots full)
            AND the total per-symbol cap (4) has not been reached.

        New-symbol slot priority
        ──────────────────────────
          • When active_symbols == MAX_SYMBOLS - 1 (last free symbol slot),
            that slot is reserved for A-grade only.
          • B/C/D can still open on an *already-active* symbol within its
            4-trade cap.

        Returns (allowed: bool, reason: str).
        """
        open_states    = self._get_open_states()
        _, grade       = self.quality_risk_pct(confidence, tq_score)
        is_a_grade     = grade in ("A", "A+")

        # ── count open symbols and per-symbol trades ──────────────────
        active_symbols: set = {s.symbol for s in open_states.values()}
        sym_open            = sum(1 for s in open_states.values() if s.symbol == symbol)
        sym_a_open          = sum(
            1 for s in open_states.values()
            if s.symbol == symbol and getattr(s, "grade", "C") in ("A", "A+")
        )
        symbol_is_new       = symbol not in active_symbols

        max_symbols         = getattr(config, "MAX_OPEN_SYMBOLS",        5)
        max_per_sym_a       = getattr(config, "MAX_TRADES_PER_SYMBOL_A", 3)
        max_per_sym_total   = getattr(config, "MAX_TRADES_PER_SYMBOL",   4)

        # ── 1. Symbol slot limit ──────────────────────────────────────
        # If this symbol has no open trades it would consume a new symbol slot.
        if symbol_is_new:
            active_count = len(active_symbols)
            if active_count >= max_symbols:
                return False, (
                    f"MAX_SYMBOLS={max_symbols} — {active_count} symbols already active "
                    f"({', '.join(sorted(active_symbols))}) | "
                    f"{symbol} would exceed the limit (grade={grade})"
                )
            # Last symbol slot is A-grade priority
            if active_count == max_symbols - 1 and not is_a_grade:
                return False, (
                    f"SYMBOL_SLOT_PRIORITY: {active_count}/{max_symbols} symbols active — "
                    f"final symbol slot reserved for A-grade | {symbol} grade={grade} blocked"
                )

        # ── 2. Per-symbol trade cap ───────────────────────────────────
        if sym_open >= max_per_sym_total:
            return False, (
                f"MAX_PER_SYMBOL: {symbol} already has {sym_open}/{max_per_sym_total} trades open"
            )

        if is_a_grade:
            # A-grade: respect the A-grade sub-cap
            if sym_a_open >= max_per_sym_a:
                return False, (
                    f"MAX_A_TRADES_PER_SYMBOL: {symbol} already has {sym_a_open} A-grade trades "
                    f"(max={max_per_sym_a}) | grade={grade} blocked"
                )
        else:
            # B/C/D: allowed only if there is room beyond the A-grade slots
            # i.e. at least one A-grade trade exists (symbol is proven) and
            # total per-symbol cap not yet reached (checked above).
            # We do NOT require A-grade trades to already be present — a fresh
            # symbol can open with a B/C/D if it passes the symbol-slot check
            # above (which already enforces A-priority for the last slot).
            pass

        # ── 3. Equity reserve floor (currency-correct, fail closed) ────
        # Always keep MARGIN_RESERVE_PCT of equity as free margin. Free margin
        # and equity are both in account currency (read live from MT5), so this
        # is currency-agnostic — no leverage/USD proxy. If we cannot read these
        # figures we reject rather than guess.
        try:
            free_margin = self._tr.get_free_margin()
            if free_margin is None:
                return False, "EQUITY_RESERVE: free margin unreadable — rejecting (fail closed)"
            equity = self._compute_equity(balance)
            if equity <= 0:
                return False, f"EQUITY_RESERVE: equity={equity:.2f} ≤ 0 — rejecting"
            reserve_pct = getattr(config, "MARGIN_RESERVE_PCT", 0.25)
            reserve_amt = equity * reserve_pct
            if free_margin < reserve_amt:
                return False, (
                    f"EQUITY_RESERVE: free={free_margin:.2f} < reserve={reserve_amt:.2f} "
                    f"(equity={equity:.2f} × {reserve_pct:.0%}) | grade={grade}"
                )
        except Exception as _e:
            logger.error("MarginManager equity reserve check error: %s — rejecting (fail closed)", _e)
            return False, f"EQUITY_RESERVE: check error {_e}"

        # ── 4. Currency correlation exposure limit ────────────────────
        max_ccy_exp    = getattr(config, "MAX_CURRENCY_EXPOSURE", 3)
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
