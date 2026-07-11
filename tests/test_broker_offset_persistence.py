"""
test_broker_offset_persistence.py — Regression tests for startup offset resolution.

Runs with plain Python (no pytest required):

    python -m tests.test_broker_offset_persistence

Guards against a real bug observed live on 2026-07-11 (Saturday): the broker
runs EET/EEST (UTC+2 winter / UTC+3 summer). detect_utc_offset() correctly
refuses to measure from a weekend-stale tick, but startup then fell back to
the STATIC config constant (+2 — stale winter value), so a weekend restart
left the bot 1 hour off for the entire following week: session windows
shifted, and news-event windows (true UTC) misaligned against bot time.
Fri 2026-07-10 detected UTC+3 live; the Sat restart silently applied +2.

Fix under test: every successful live detection is persisted; startup
resolution priority is live detection → persisted last-good → static config.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

import src.config as config
from src.infrastructure import mt5_time


def _fresh_dir() -> str:
    """Point CHECKPOINT_DIR at an empty temp dir; return its path."""
    d = tempfile.mkdtemp(prefix="offset_test_")
    config.CHECKPOINT_DIR = d
    return d


def test_live_detection_wins_and_persists():
    _fresh_dir()
    offset, source = mt5_time.resolve_startup_offset(detected=3)
    assert offset == 3, f"live detection must win, got {offset}"
    assert "detected live" in source

    persisted = mt5_time.load_persisted_offset()
    assert persisted is not None, "successful detection must be persisted"
    assert persisted[0] == 3
    print("PASS  test_live_detection_wins_and_persists")


def test_weekend_restart_uses_persisted_not_static():
    """The exact live scenario: Friday detects +3, Saturday restart detects
    nothing (0) — the bot must resume +3, not the stale static constant."""
    _fresh_dir()
    config.BROKER_UTC_OFFSET_HOURS = 2          # stale winter value
    mt5_time.resolve_startup_offset(detected=3)  # Friday: live detection

    offset, source = mt5_time.resolve_startup_offset(detected=0)  # Saturday
    assert offset == 3, f"weekend restart must use persisted +3, got {offset} ({source})"
    assert "persisted" in source
    print("PASS  test_weekend_restart_uses_persisted_not_static")


def test_static_fallback_when_nothing_persisted():
    _fresh_dir()
    config.BROKER_UTC_OFFSET_HOURS = 2
    offset, source = mt5_time.resolve_startup_offset(detected=0)
    assert offset == 2, f"with no persisted value the static config must apply, got {offset}"
    assert "static config fallback" in source
    print("PASS  test_static_fallback_when_nothing_persisted")


def test_corrupt_or_out_of_range_file_is_ignored():
    d = _fresh_dir()
    config.BROKER_UTC_OFFSET_HOURS = 2

    path = os.path.join(d, "broker_utc_offset.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    assert mt5_time.load_persisted_offset() is None, "corrupt file must be ignored"
    offset, source = mt5_time.resolve_startup_offset(detected=0)
    assert offset == 2 and "static" in source

    with open(path, "w") as fh:
        fh.write('{"offset_hours": 99, "detected_at_utc": "x"}')
    assert mt5_time.load_persisted_offset() is None, "out-of-range offset must be ignored"
    print("PASS  test_corrupt_or_out_of_range_file_is_ignored")


if __name__ == "__main__":
    test_live_detection_wins_and_persists()
    test_weekend_restart_uses_persisted_not_static()
    test_static_fallback_when_nothing_persisted()
    test_corrupt_or_out_of_range_file_is_ignored()
    print("\nAll broker-offset persistence checks passed.")
