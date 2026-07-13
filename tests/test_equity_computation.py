"""
test_equity_computation.py — Regression tests for broker-truth equity (Phase 2A).

Runs with plain Python (no pytest required):

    python -m tests.test_equity_computation

Guards against the equity-vs-balance finding (QA audit → design review,
2026-07-13): MarginManager._compute_equity() used to sum p.profit over open
positions, but the Trade entity has no `profit` field, so the hasattr() guard
silently zeroed the sum and "equity" was ALWAYS just realized balance —
floating losses were invisible to the daily-drawdown circuit breaker (Gate 4)
and to can_enter's equity-reserve floor.

Fix under test: equity now comes from MT5's own account_info().equity via
ITradeRepository.get_account_equity(); balance is only the degraded-mode
fallback when the broker is unreachable.
"""
from __future__ import annotations

import logging
import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

# Stub the Wine-only MetaTrader5 package for any transitive import.
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

import src.config as config
from src.application.margin_manager import MarginManager
from src.strategy.entry_gate import EntryGate


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _Repo:
    """ITradeRepository stand-in for the paths MarginManager touches."""

    def __init__(self, equity, free_margin=1000.0):
        self._equity = equity
        self._free_margin = free_margin

    def get_account_equity(self):
        return self._equity

    def get_free_margin(self):
        return self._free_margin

    def get_open_positions(self):
        return []


class _ExecStub:
    _trade_states: dict = {}

    def is_symbol_paused(self, symbol, now):
        return False

    def is_reversal_cooldown(self, symbol, now):
        return False


class _NewsStub:
    def is_blocked(self, symbol, now):
        return False


class _TickAnalysis:
    velocity = 2.0


class _Signal:
    symbol = "USDJPY"
    confidence = 0.9          # read at the Gate-5 call site
    tick_analysis = _TickAnalysis()


class _BrokerCostStub:
    spread_pips = 0.5


class _Gate5Sentinel(Exception):
    """Raised by the margin stub's can_enter — reaching it proves Gate 4 passed."""


# ── MarginManager tests ───────────────────────────────────────────────────────

def test_broker_equity_preferred_over_balance():
    mm = MarginManager(trade_repo=_Repo(equity=125.11))
    result = mm.get_equity(balance=128.91)
    assert result == 125.11, (
        f"broker-truth equity must win over balance, got {result} "
        "(if 128.91: the balance-only bug is back)"
    )
    print("PASS  test_broker_equity_preferred_over_balance")


def test_fallback_to_balance_when_unreachable_throttled():
    mm = MarginManager(trade_repo=_Repo(equity=None))

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    mm_logger = logging.getLogger("src.application.margin_manager")
    handler = _Capture()
    mm_logger.addHandler(handler)
    try:
        for _ in range(5):
            result = mm.get_equity(balance=128.91)
            assert result == 128.91, f"None equity must fall back to balance, got {result}"
    finally:
        mm_logger.removeHandler(handler)

    warns = [r for r in records if r.levelno == logging.WARNING and "falling back" in r.getMessage()]
    assert len(warns) == 1, f"fallback warning must be throttled to 1, got {len(warns)}"
    print("PASS  test_fallback_to_balance_when_unreachable_throttled")


def test_reserve_floor_uses_true_equity():
    """can_enter's equity-reserve floor (equity × MARGIN_RESERVE_PCT) must
    derive from broker equity, not balance. equity=90 vs balance=130 with
    reserve 25%: floor is 22.50 — free margin of 25.00 passes; against the
    old balance-based floor (32.50) it would have been rejected."""
    config.MARGIN_RESERVE_PCT = 0.25

    mm = MarginManager(trade_repo=_Repo(equity=90.0, free_margin=25.0))
    allowed, reason = mm.can_enter(symbol="USDJPY", confidence=0.9, tq_score=0.9, balance=130.0)
    assert allowed, f"free=25.00 ≥ equity-based reserve 22.50 must pass, got: {reason}"

    # Inverse: floating PROFIT (equity 200 vs balance 130) → floor 50.00;
    # free margin 40.00 must now be rejected (balance-based floor 32.50
    # would wrongly have passed it).
    mm = MarginManager(trade_repo=_Repo(equity=200.0, free_margin=40.0))
    allowed, reason = mm.can_enter(symbol="USDJPY", confidence=0.9, tq_score=0.9, balance=130.0)
    assert not allowed and "EQUITY_RESERVE" in reason, (
        f"free=40.00 < equity-based reserve 50.00 must reject, got allowed={allowed} ({reason})"
    )
    print("PASS  test_reserve_floor_uses_true_equity")


# ── Gate 4 breaker end-to-end ────────────────────────────────────────────────

def _bare_gate(equity: float, baseline: float) -> EntryGate:
    gate = EntryGate.__new__(EntryGate)
    gate._exec = _ExecStub()
    gate._news = _NewsStub()
    gate._session_start_equity = baseline

    class _MarginStub:
        def get_equity(self, balance):
            return equity

        def can_enter(self, **kwargs):
            raise _Gate5Sentinel()

    gate._margin = _MarginStub()
    return gate


def _run_gate4(equity: float, baseline: float):
    """Run evaluate() up to Gate 4/5. Returns (blocked_by_dd, records)."""
    gate = _bare_gate(equity, baseline)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)  # in USDJPY session

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    eg_logger = logging.getLogger("src.strategy.entry_gate")
    handler = _Capture()
    eg_logger.addHandler(handler)
    reached_gate5 = False
    try:
        try:
            result = gate.evaluate(
                signal=_Signal(), builder=None, current_price=162.0, now=now,
                balance=128.91, pip_calc=None, pip_value=6.37, digits=3,
                h1_atr=0.05, broker_cost=_BrokerCostStub(),
            )
        except _Gate5Sentinel:
            reached_gate5 = True
            result = None
    finally:
        eg_logger.removeHandler(handler)

    dd_blocked = any("circuit breaker" in r.getMessage() for r in records)
    if dd_blocked:
        assert result is None, "a drawdown block must return None"
        assert not reached_gate5, "drawdown block must not reach Gate 5"
    return dd_blocked, reached_gate5


def test_gate4_trips_on_floating_loss_equity():
    """Baseline 130.46, broker equity 125.20 → dd 4.03% ≥ 4.0% limit → block.
    This is the exact scenario the old code could NOT see: equity diverged
    from balance (128.91) only via floating P&L."""
    config.DAILY_DRAWDOWN_LIMIT_PCT = 4.0
    dd_blocked, _ = _run_gate4(equity=125.20, baseline=130.46)
    assert dd_blocked, (
        "Gate 4 must trip on floating-loss-driven equity dd of 4.03% — "
        "the breaker is still blind to floating losses"
    )
    print("PASS  test_gate4_trips_on_floating_loss_equity")


def test_gate4_passes_below_limit():
    """Equity 125.30 → dd 3.95% < 4.0% → Gate 4 passes (proof: Gate 5 reached)."""
    config.DAILY_DRAWDOWN_LIMIT_PCT = 4.0
    dd_blocked, reached_gate5 = _run_gate4(equity=125.30, baseline=130.46)
    assert not dd_blocked, "dd 3.95% must not trip the 4.0% breaker"
    assert reached_gate5, "evaluation should have proceeded to Gate 5"
    print("PASS  test_gate4_passes_below_limit")


if __name__ == "__main__":
    test_broker_equity_preferred_over_balance()
    test_fallback_to_balance_when_unreachable_throttled()
    test_reserve_floor_uses_true_equity()
    test_gate4_trips_on_floating_loss_equity()
    test_gate4_passes_below_limit()
    print("\nAll equity-computation checks passed.")
