"""
test_position_reconciliation.py — Regression tests for the phantom-close fix.

Runs with plain Python (no pytest required):

    python -m tests.test_position_reconciliation

Guards against QA audit finding R1.3: get_open_positions() used to return []
identically for "MT5 not ready", "positions_get() errored" and "genuinely no
positions". _prune_closed_positions treated [] as "every tracked ticket is
closed" and called _finalize_close(action="broker_close") on each — phantom
journal entries, corrupted EV window and loss-streak state, dropped trade
management, all from one transient MT5 hiccup.

Contract under test: None = broker state UNKNOWN (never prune, fail closed);
[] = confirmed flat (pruning tracked tickets is correct and must keep working).
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

# Stub the Wine-only MetaTrader5 package so transitive imports resolve on host.
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

from src.application.execution_service import ExecutionService  # noqa: E402
from src.application.margin_manager import MarginManager        # noqa: E402

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class _Repo:
    def __init__(self, positions):
        self.positions = positions

    def get_open_positions(self):
        return self.positions


def _bare_execution_service(repo, tracked_tickets):
    """ExecutionService via __new__ — _prune_closed_positions and
    open_count_for_symbol only touch _tr, _trade_states and _finalize_close."""
    es = ExecutionService.__new__(ExecutionService)
    es._tr = repo
    es._trade_states = {t: object() for t in tracked_tickets}
    es.finalized = []
    es._finalize_close = lambda ticket, state, action, now: es.finalized.append((ticket, action))
    return es


def test_none_from_broker_never_prunes():
    es = _bare_execution_service(_Repo(None), tracked_tickets=[111, 222])
    es._prune_closed_positions(_NOW)
    assert es.finalized == [], (
        f"UNKNOWN broker state must never finalize trades — phantom closes: {es.finalized}"
    )
    print("PASS  test_none_from_broker_never_prunes")


def test_confirmed_empty_still_prunes():
    """Legacy behavior preserved: a CONFIRMED flat book finalizes tracked tickets."""
    es = _bare_execution_service(_Repo([]), tracked_tickets=[111])
    es._prune_closed_positions(_NOW)
    assert es.finalized == [(111, "broker_close")], (
        f"confirmed-empty must still prune (got {es.finalized}) — reconcile path broken"
    )
    print("PASS  test_confirmed_empty_still_prunes")


def test_open_count_fails_closed_on_unknown():
    es = _bare_execution_service(_Repo(None), tracked_tickets=[])
    count = es.open_count_for_symbol("USDJPY")
    assert count >= 10**6, (
        f"unknown broker state must report a blocking count, got {count} (fail-open!)"
    )
    print("PASS  test_open_count_fails_closed_on_unknown")


def test_equity_falls_back_to_balance_on_unknown():
    mm = MarginManager.__new__(MarginManager)
    mm._tr = _Repo(None)
    assert mm._compute_equity(131.0) == 131.0
    print("PASS  test_equity_falls_back_to_balance_on_unknown")


if __name__ == "__main__":
    test_none_from_broker_never_prunes()
    test_confirmed_empty_still_prunes()
    test_open_count_fails_closed_on_unknown()
    test_equity_falls_back_to_balance_on_unknown()
    print("\nAll position-reconciliation checks passed.")
