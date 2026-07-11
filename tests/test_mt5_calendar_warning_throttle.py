"""
test_mt5_calendar_warning_throttle.py — Regression test for calendar log spam.

Runs with plain Python (no pytest required):

    python -m tests.test_mt5_calendar_warning_throttle

Background (2026-07-07 QA audit, R1.5): this deployment's broker exposes no
MT5 calendar data, so MT5NewsClient logged "no event definitions found" at
WARNING on every fetch — 2,379 times over 32 days with zero successful
fetches ever, pure alert fatigue. ForexFactory carries the real news feed;
this client is best-effort enrichment. The fix logs the condition ONCE per
process at WARNING, then at DEBUG.
"""
from __future__ import annotations

import logging
import os
import sys
import types

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

# The module hard-imports MetaTrader5, which only exists inside the Wine
# runtime. Inject a stub WITHOUT calendar_event_by_currency — that exercises
# the exact "no event definitions" path under test via the hasattr guard.
sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

from src.infrastructure.mt5_news_client import MT5NewsClient  # noqa: E402


def test_no_defs_warning_fires_once_then_debug():
    client = MT5NewsClient()

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    mt5_logger = logging.getLogger("src.infrastructure.mt5_news_client")
    prior_level = mt5_logger.level
    mt5_logger.setLevel(logging.DEBUG)
    handler = _Capture()
    mt5_logger.addHandler(handler)
    try:
        for _ in range(5):
            events = client.get_today_events(["USD", "EUR"], "2026-07-10")
            assert events == []
    finally:
        mt5_logger.removeHandler(handler)
        mt5_logger.setLevel(prior_level)

    no_defs = [r for r in records if "no event definitions" in r.getMessage()]
    warnings = [r for r in no_defs if r.levelno == logging.WARNING]
    debugs   = [r for r in no_defs if r.levelno == logging.DEBUG]

    assert len(warnings) == 1, f"expected exactly 1 WARNING, got {len(warnings)}"
    assert len(debugs) == 4, f"expected 4 DEBUG follow-ups, got {len(debugs)}"
    print("PASS  test_no_defs_warning_fires_once_then_debug")


if __name__ == "__main__":
    test_no_defs_warning_fires_once_then_debug()
    print("\nAll MT5-calendar throttle checks passed.")
