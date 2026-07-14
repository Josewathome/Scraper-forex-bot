"""
test_entry_gate_persistence.py — Regression tests for gate-state checkpointing
(Phase 2B, R2.1 + R2.3).

Runs with plain Python (no pytest required):

    python -m tests.test_entry_gate_persistence

Guards against the restart-forgives-drawdown bug: EntryGate's daily-drawdown
baseline (_session_start_equity), day guard (_last_new_day_utc), daily/
exploration counters, and anti-duplicate fill times were memory-only, and
startup unconditionally re-ran on_new_day() — so ANY intraday restart
re-baselined the 4% breaker to current equity (forgiving the day's losses),
refreshed the exploration budget, and reset duplicate tracking.

Fix under test: get_state()/restore_state() persisted via the checkpoint;
startup restores BEFORE on_new_day(), whose same-day guard then preserves the
original baseline; a genuine new day still re-baselines.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

import src.config as config
from src.strategy.entry_gate import EntryGate

_TODAY = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


class _MarginStub:
    def __init__(self, equity):
        self.equity = equity
        self.calls = 0

    def get_equity(self, balance):
        self.calls += 1
        return self.equity


def _bare_gate(margin_equity=130.0) -> EntryGate:
    g = EntryGate.__new__(EntryGate)
    g._session_start_equity = None
    g._last_new_day_utc = None
    g._last_fill = {}
    g._daily_counts = {}
    g._exploration_counts = {}
    g._margin = _MarginStub(margin_equity)
    return g


def test_state_roundtrip_is_json_safe_and_faithful():
    g = _bare_gate()
    g._session_start_equity = 130.46
    g._last_new_day_utc = "2026-07-13"
    g._daily_counts = {"2026-07-13": 3}
    g._exploration_counts = {"USDJPY": 1}
    g._last_fill = {"USDJPY": {"BULLISH": datetime(2026, 7, 13, 10, 5, tzinfo=timezone.utc)}}

    blob = json.dumps(g.get_state())            # must survive the checkpoint's JSON layer
    restored = _bare_gate()
    restored.restore_state(json.loads(blob))

    assert restored._session_start_equity == 130.46
    assert restored._last_new_day_utc == "2026-07-13"
    assert restored._daily_counts == {"2026-07-13": 3}
    assert restored._exploration_counts == {"USDJPY": 1}
    assert restored._last_fill["USDJPY"]["BULLISH"] == datetime(2026, 7, 13, 10, 5, tzinfo=timezone.utc)
    print("PASS  test_state_roundtrip_is_json_safe_and_faithful")


def test_same_day_restart_keeps_baseline():
    """The core R2.1 scenario: intraday restart after losses. Restored
    baseline 130.46 must survive on_new_day() even though current equity
    (125.20) is lower — no more restart-forgiveness."""
    g = _bare_gate(margin_equity=125.20)
    g.restore_state({"session_start_equity": 130.46, "last_new_day_utc": _TODAY.date().isoformat()})

    g.on_new_day(balance=125.20, now=_TODAY)

    assert g._session_start_equity == 130.46, (
        f"intraday restart re-baselined to {g._session_start_equity} — drawdown forgiven again"
    )
    assert g._margin.calls == 0, "same-day guard must short-circuit before consulting equity"
    print("PASS  test_same_day_restart_keeps_baseline")


def test_next_day_still_rebaselines():
    g = _bare_gate(margin_equity=125.20)
    g.restore_state({
        "session_start_equity": 130.46,
        "last_new_day_utc": "2026-07-12",           # yesterday
        "daily_counts": {"2026-07-12": 2},
    })

    g.on_new_day(balance=125.20, now=_TODAY)

    assert g._session_start_equity == 125.20, "a genuine new day must re-baseline"
    assert g._last_new_day_utc == _TODAY.date().isoformat()
    assert g._daily_counts == {}, "daily counters must reset on a genuine new day"
    print("PASS  test_next_day_still_rebaselines")


def test_legacy_and_malformed_state_tolerated():
    for bad in (None, {}, {"session_start_equity": -5},
                {"session_start_equity": "junk", "last_fill": {"USDJPY": {"BULLISH": "not-a-date"}}},
                {"daily_counts": "not-a-dict"}):
        g = _bare_gate(margin_equity=131.0)
        g.restore_state(bad)                        # must not raise
        g.on_new_day(balance=131.0, now=_TODAY)     # falls back to fresh-day behavior
        assert g._session_start_equity == 131.0
    print("PASS  test_legacy_and_malformed_state_tolerated")


def test_gate4_measures_against_restored_baseline():
    """End-to-end: restored baseline + current floating-loss equity must trip
    the breaker (dd = (130.46-125.20)/130.46 = 4.03% >= 4.0%)."""
    config.DAILY_DRAWDOWN_LIMIT_PCT = 4.0

    class _Exec:
        def is_symbol_paused(self, s, n): return False
        def is_reversal_cooldown(self, s, n): return False

    class _News:
        def is_blocked(self, s, n): return False

    class _Tick: velocity = 2.0

    class _Sig:
        symbol = "USDJPY"; confidence = 0.9; tick_analysis = _Tick()

    class _Cost: spread_pips = 0.5

    g = _bare_gate(margin_equity=125.20)
    g._exec, g._news = _Exec(), _News()
    g.restore_state({"session_start_equity": 130.46, "last_new_day_utc": _TODAY.date().isoformat()})
    g.on_new_day(balance=125.20, now=_TODAY)        # same-day → baseline stays 130.46

    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, r): records.append(r)

    lg = logging.getLogger("src.strategy.entry_gate")
    h = _Cap(); lg.addHandler(h)
    try:
        result = g.evaluate(signal=_Sig(), builder=None, current_price=162.0, now=_TODAY,
                            balance=125.20, pip_calc=None, pip_value=6.37, digits=3,
                            h1_atr=0.05, broker_cost=_Cost())
    finally:
        lg.removeHandler(h)

    assert result is None and any("circuit breaker" in r.getMessage() for r in records), (
        "Gate 4 must trip against the RESTORED baseline after an intraday restart"
    )
    print("PASS  test_gate4_measures_against_restored_baseline")


if __name__ == "__main__":
    test_state_roundtrip_is_json_safe_and_faithful()
    test_same_day_restart_keeps_baseline()
    test_next_day_still_rebaselines()
    test_legacy_and_malformed_state_tolerated()
    test_gate4_measures_against_restored_baseline()
    print("\nAll entry-gate persistence checks passed.")
