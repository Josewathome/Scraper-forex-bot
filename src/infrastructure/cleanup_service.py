"""
cleanup_service.py — Automated retention policy enforcement.

Retention rules (all configurable in config.py):
    Logs   : 3 days
    Cache  : 2 days
    Zones  : 5 days
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import src.config as config

logger = logging.getLogger(__name__)


# Files in .checkpoints that must never be deleted by the purge.
_CHECKPOINT_PROTECTED = {
    "checkpoint_latest.json",
    "balance_history.json",
    "config_overrides.json",
    "jwt_secret.txt",
    "jwt_blocklist.json",
    "symbols_override.json",
}


class CleanupService:
    def __init__(self) -> None:
        self.log_dir        = Path("logs")
        self.cache_dir      = Path(".cache")
        self.zones_dir      = Path(getattr(config, "ZONES_DIR", "zones"))
        self.checkpoint_dir = Path(getattr(config, "CHECKPOINT_DIR", ".checkpoints"))

        self.log_days        = getattr(config, "LOG_RETENTION_DAYS",     3)
        self.cache_days      = getattr(config, "CACHE_RETENTION_DAYS",   2)
        self.zone_days       = getattr(config, "ZONE_RETENTION_DAYS",    5)
        self.checkpoint_days = getattr(config, "CHECKPOINT_RETAIN_DAYS", 7)

    # ── Public ──────────────────────────────────────────────────────

    def run_all(self) -> None:
        """Run all cleanup passes. Called daily by the scheduler."""
        logger.info("CleanupService: starting daily retention pass.")
        self._purge(self.log_dir,   self.log_days,   "logs")
        self._purge(self.cache_dir, self.cache_days, "cache")
        self._purge(self.zones_dir, self.zone_days,  "zones")
        self._purge_checkpoints()
        logger.info("CleanupService: retention pass complete.")

    def _purge_checkpoints(self) -> None:
        """Delete dated checkpoint_YYYY-MM-DD.json backups older than CHECKPOINT_RETAIN_DAYS.
        Protected files (latest, balance history, config overrides, JWT secret) are never touched."""
        if not self.checkpoint_dir.exists():
            return
        cutoff  = datetime.now(tz=timezone.utc) - timedelta(days=self.checkpoint_days)
        removed = 0
        for f in self.checkpoint_dir.iterdir():
            if not f.is_file():
                continue
            if f.name in _CHECKPOINT_PROTECTED:
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Cleanup: could not delete %s — %s", f, exc)
        if removed:
            logger.info(
                "Cleanup [checkpts]: removed %d dated backup(s) older than %d days",
                removed, self.checkpoint_days,
            )

    def purge_zones(self, symbol: str | None = None) -> None:
        """Purge old zone files for one symbol or all symbols."""
        target = (self.zones_dir / symbol) if symbol else self.zones_dir
        self._purge(target, self.zone_days, f"zones/{symbol or '*'}")

    def market_close_wipe(self) -> None:
        """
        Full wipe of zones and news cache on Friday market close (21:00 UTC).
        Zones are rebuilt from scratch on Monday when the bot remaps at startup.
        Cache is repopulated on first fetch. Both directories are kept small this way
        regardless of how many months the bot has been running.
        """
        logger.info("CleanupService: Friday market-close wipe starting.")
        self._wipe(self.zones_dir, "zones")
        self._wipe(self.cache_dir, "cache")
        logger.info("CleanupService: market-close wipe complete — fresh start on Monday.")

    # ── Internal ────────────────────────────────────────────────────

    def _wipe(self, directory: Path, label: str) -> None:
        """Delete every file in directory tree (no age check — full wipe)."""
        if not directory.exists():
            return
        removed = 0
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            try:
                f.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Cleanup wipe: could not delete %s — %s", f, exc)
        # Remove any now-empty subdirectories
        for d in sorted(directory.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        logger.info("Cleanup [%-8s]: wiped %d file(s)", label, removed)

    def _purge(self, directory: Path, days: int, label: str) -> None:
        if not directory.exists():
            return
        cutoff  = datetime.now(tz=timezone.utc) - timedelta(days=days)
        removed = 0
        errors  = 0
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Cleanup: could not delete %s — %s", f, exc)
                    errors += 1
        if removed or errors:
            logger.info(
                "Cleanup [%-8s]: removed %d file(s) older than %d days  (errors=%d)",
                label, removed, days, errors,
            )