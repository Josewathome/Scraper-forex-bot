"""
test_tick_guard.py — Regression tests for the malformed-tick guard.

Runs with plain Python (no pytest required):

    python -m tests.test_tick_guard

Guards against a real bug found in the 2026-07-07 QA audit (R1.2): a TICK
message missing bid/ask defaulted those fields to 0.0 in ZmqFeed._handle_tick,
and CandleBuilder.on_tick has no positivity check — so one malformed EA
message wrote O/H/L/C=0 into the forming candle and pushed a 0-price TICK
event into strategy evaluation.

The fix drops any tick with bid <= 0 or ask <= 0 before it reaches the
builder or the event queue (throttled warning, <= 1/min).
"""
from __future__ import annotations

import os
import queue

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

from src.infrastructure.stream.zmq_feed import ZmqFeed


class _SpyBuilder:
    """Stands in for CandleBuilder; records every on_tick call."""

    def __init__(self):
        self.ticks: list[tuple] = []
        self._on_close = None   # ZmqFeed.__init__ assigns this

    def on_tick(self, bid, ask, tick_time):
        self.ticks.append((bid, ask, tick_time))


def _make_feed():
    builder = _SpyBuilder()
    q = queue.Queue()
    feed = ZmqFeed("tcp://127.0.0.1:5556", {"GBPUSD": builder}, q)
    return feed, builder, q


def test_malformed_tick_is_dropped_entirely():
    feed, builder, q = _make_feed()

    feed._handle_tick({"sym": "GBPUSD", "ask": 1.33970, "time": 1})   # bid missing → 0.0
    feed._handle_tick({"sym": "GBPUSD", "bid": 1.33960, "time": 2})   # ask missing → 0.0
    feed._handle_tick({"sym": "GBPUSD", "bid": -1.0, "ask": 1.3, "time": 3})
    feed._handle_tick({"sym": "GBPUSD", "bid": 0.0, "ask": 0.0, "time": 4})

    assert builder.ticks == [], f"malformed ticks reached the builder: {builder.ticks}"
    assert q.empty(), "malformed ticks were enqueued as TICK events"
    assert feed._bad_ticks_dropped == 4
    print("PASS  test_malformed_tick_is_dropped_entirely")


def test_valid_tick_passes_through_unchanged():
    feed, builder, q = _make_feed()

    feed._handle_tick({"sym": "GBPUSD", "bid": 1.33960, "ask": 1.33972, "time": 42})

    assert builder.ticks == [(1.33960, 1.33972, 42)], f"valid tick mangled: {builder.ticks}"
    evt = q.get_nowait()
    assert evt == {"type": "TICK", "sym": "GBPUSD", "bid": 1.33960, "ask": 1.33972, "time": 42}
    assert feed._bad_ticks_dropped == 0
    print("PASS  test_valid_tick_passes_through_unchanged")


def test_warning_is_throttled_not_per_tick():
    feed, _, _ = _make_feed()

    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    zmq_logger = logging.getLogger("src.infrastructure.stream.zmq_feed")
    handler = _Capture()
    zmq_logger.addHandler(handler)
    try:
        for i in range(50):
            feed._handle_tick({"sym": "GBPUSD", "time": i})   # all malformed
    finally:
        zmq_logger.removeHandler(handler)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected 1 throttled warning for a bad-tick storm, got {len(warnings)}"
    assert feed._bad_ticks_dropped == 50
    print("PASS  test_warning_is_throttled_not_per_tick")


if __name__ == "__main__":
    test_malformed_tick_is_dropped_entirely()
    test_valid_tick_passes_through_unchanged()
    test_warning_is_throttled_not_per_tick()
    print("\nAll tick-guard checks passed.")
