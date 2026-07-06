"""
test_broker_clock.py — Regression test for BrokerClock's staleness detection.

Runs with plain Python (no pytest required):

    python -m tests.test_broker_clock

It is also pytest-compatible (every check is a `test_*` function).

Guards against a real bug found in code review: BrokerClock.now() used to
compare a genuinely-stale tick's RAW (broker-local, uncorrected) epoch
directly against the host's true-UTC wall clock. With a nonzero
BROKER_UTC_OFFSET_HOURS that skews the computed "tick age" by
offset_hours*3600 seconds — at the current 2-hour offset, a tick that is
really only 300s stale (well within a realistic MT5 IPC hiccup) looked like
tick_age = 300 - 7200 = -6900, i.e. "fresh", so the dead-reckoning fallback
never fired and now() silently froze instead of advancing. Fixed by diffing
wall_now against the OFFSET-CORRECTED broker time, not the raw epoch.
"""
from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

import src.config as config
from src.infrastructure.market_data_repo import BrokerClock

OFFSET_HOURS = 2
OFFSET_SECS = OFFSET_HOURS * 3600


class _FakeGateway:
    """Returns a raw MT5 epoch for a tick that is genuinely `stale_by`
    seconds old, encoded in broker-local time (ahead of true UTC by
    OFFSET_SECS, matching this bot's live BROKER_UTC_OFFSET_HOURS)."""

    def __init__(self, stale_by: float):
        self.stale_by = stale_by

    def get_server_time(self):
        tick_true_utc = _time.time() - self.stale_by
        return int(tick_true_utc + OFFSET_SECS)


def test_fresh_tick_returns_broker_time_directly():
    config.BROKER_UTC_OFFSET_HOURS = OFFSET_HOURS
    clock = BrokerClock(_FakeGateway(stale_by=0.0))
    drift = abs((datetime.now(tz=timezone.utc) - clock.now()).total_seconds())
    assert drift < 5, f"Fresh tick path drifted {drift:.1f}s from true now."
    print(f"PASS  test_fresh_tick_returns_broker_time_directly (drift={drift:.3f}s)")


def test_short_stale_tick_is_dead_reckoned_not_frozen():
    """A tick 300s old (> MAX_TICK_AGE=120s) must be detected as stale and
    dead-reckoned forward to ~true-now -- not returned frozen 300s in the
    past, which is what the pre-fix code did (tick_age's offset skew made
    a 300s-stale tick look "fresh")."""
    config.BROKER_UTC_OFFSET_HOURS = OFFSET_HOURS
    clock = BrokerClock(_FakeGateway(stale_by=300.0))

    # Seed a consistent "last good" pair: the broker time recorded 300s ago,
    # alongside the monotonic instant it was recorded.
    clock._last_good_broker = datetime.now(tz=timezone.utc) - timedelta(seconds=300.0)
    clock._last_good_wall = _time.monotonic() - 300.0

    result = clock.now()
    drift = abs((datetime.now(tz=timezone.utc) - result).total_seconds())
    assert drift < 5, (
        f"BrokerClock.now() returned a time {drift:.1f}s away from true now — "
        f"staleness detection is not correctly dead-reckoning a short-stale tick."
    )
    print(f"PASS  test_short_stale_tick_is_dead_reckoned_not_frozen (drift={drift:.3f}s)")


if __name__ == "__main__":
    test_fresh_tick_returns_broker_time_directly()
    test_short_stale_tick_is_dead_reckoned_not_frozen()
    print("\nAll BrokerClock checks passed.")
