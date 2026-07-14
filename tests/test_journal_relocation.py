"""
test_journal_relocation.py — Regression tests for journal persistence
(Phase 2B, R2.2).

Runs with plain Python (no pytest required):

    python -m tests.test_journal_relocation

Guards against the weekly EV-history wipe: trade_journal.json lived in
.cache, which CleanupService.market_close_wipe() clears every Friday and
the daily pass purges at CACHE_RETENTION_DAYS — so the EV gate's
broker-truth rolling window (needs 30+ samples) reset to bootstrap
assumptions every week. Fix: journal relocated to .checkpoints (never
wiped) with a one-time idempotent migration, and BOTH the journal and the
R1.6 broker-offset file added to the checkpoint purge's protected list
(they'd otherwise be deleted once older than CHECKPOINT_RETAIN_DAYS).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")

sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))

import src.config as config
from src.infrastructure.trade_journal import TradeJournal
from src.infrastructure.cleanup_service import CleanupService

_RECORD = {"ticket": 42, "symbol": "USDJPY", "outcome": "SL", "pnl": -1.0, "schema": 2}


def _fresh_cwd() -> Path:
    """Run each test in an isolated CWD (journal/cleanup paths are relative)."""
    d = Path(tempfile.mkdtemp(prefix="journal_test_"))
    os.chdir(d)
    config.CHECKPOINT_DIR = str(d / ".checkpoints")
    return d


def test_legacy_journal_migrates_once():
    d = _fresh_cwd()
    legacy = d / ".cache" / "trade_journal.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps([_RECORD]))

    j = TradeJournal()
    assert j.path == Path(".checkpoints") / "trade_journal.json"
    assert not legacy.exists(), "legacy file must be MOVED, not copied"
    assert j.path.exists() and json.loads(j.path.read_text())[0]["ticket"] == 42

    TradeJournal()  # idempotent — second construction must not raise or duplicate
    assert not legacy.exists() and j.path.exists()
    print("PASS  test_legacy_journal_migrates_once")


def test_both_files_exist_keeps_new():
    d = _fresh_cwd()
    new = d / ".checkpoints" / "trade_journal.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps([_RECORD]))
    legacy = d / ".cache" / "trade_journal.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps([{"ticket": 999, "schema": 2}]))

    j = TradeJournal()
    assert json.loads(j.path.read_text())[0]["ticket"] == 42, "new journal must win"
    assert legacy.exists(), "legacy left in place for the cache wipe"
    print("PASS  test_both_files_exist_keeps_new")


def test_explicit_path_never_migrates():
    d = _fresh_cwd()
    legacy = d / ".cache" / "trade_journal.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps([_RECORD]))

    custom = d / "elsewhere" / "j.json"
    TradeJournal(path=custom)
    assert legacy.exists(), "explicit-path construction must not touch the legacy file"
    print("PASS  test_explicit_path_never_migrates")


def test_market_close_wipe_spares_journal():
    d = _fresh_cwd()
    j = TradeJournal()
    j.path.write_text(json.dumps([_RECORD]))
    cache_file = d / ".cache" / "news_blob.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{}")

    CleanupService().market_close_wipe()

    assert not cache_file.exists(), "cache must still be wiped"
    assert j.path.exists(), "journal must survive the Friday market-close wipe"
    print("PASS  test_market_close_wipe_spares_journal")


def test_checkpoint_purge_protects_journal_and_offset():
    d = _fresh_cwd()
    ckpt = d / ".checkpoints"
    ckpt.mkdir(parents=True, exist_ok=True)
    old = time.time() - 10 * 86400          # 10 days old > 7-day retention

    journal = ckpt / "trade_journal.json"
    offset  = ckpt / "broker_utc_offset.json"
    victim  = ckpt / "checkpoint_2026-07-01.json"
    for f in (journal, offset, victim):
        f.write_text("{}")
        os.utime(f, (old, old))

    CleanupService()._purge_checkpoints()

    assert not victim.exists(), "aged dated backup must still be purged"
    assert journal.exists(), "aged journal must be protected from the checkpoint purge"
    assert offset.exists(), "aged broker-offset file must be protected (R1.6 regression)"
    print("PASS  test_checkpoint_purge_protects_journal_and_offset")


if __name__ == "__main__":
    _orig = os.getcwd()
    try:
        test_legacy_journal_migrates_once()
        test_both_files_exist_keeps_new()
        test_explicit_path_never_migrates()
        test_market_close_wipe_spares_journal()
        test_checkpoint_purge_protects_journal_and_offset()
    finally:
        os.chdir(_orig)
    print("\nAll journal-relocation checks passed.")
