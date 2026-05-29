"""
zone_store.py — Timestamped JSON snapshots of zone state per symbol.

ZoneFileStore writes one JSON file per symbol every time zones are remapped
(H1, M30, M5 bar closes).  These files serve two purposes:

1. Gap recovery on bot restart — GapRecoveryService reads the last snapshot
   to decide whether to trigger a full remap or resume from the saved zones.

2. Dashboard / audit trail — each snapshot is timestamped so you can inspect
   what zones were active at any point in the session.

File layout:
    zones/
        GBPUSD_zones.json      ← current snapshot (overwritten on every remap)
        GBPUSD_zones_<ts>.json ← dated backup (kept for ZONE_RETENTION_DAYS)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from src.domain.entities import Direction, LiquidityPool, Zone, ZoneType

logger = logging.getLogger(__name__)

try:
    import src.config as config
    _ZONES_DIR        = getattr(config, "ZONES_DIR", "zones")
    _RETENTION_DAYS   = getattr(config, "ZONE_RETENTION_DAYS", 5)
except Exception:
    _ZONES_DIR      = "zones"
    _RETENTION_DAYS = 5


class ZoneFileStore:
    """
    Writes and reads per-symbol zone snapshots to/from disk.

    All methods are safe to call from the main event loop — they do
    no networking and complete in < 5 ms on a modern SSD.
    """

    def __init__(self, zones_dir: str = _ZONES_DIR) -> None:
        self._dir = zones_dir
        os.makedirs(self._dir, exist_ok=True)

    # ── Write ─────────────────────────────────────────────────────────

    def save_snapshot(
        self,
        symbol:    str,
        zones:     List[Zone],
        pool:      Optional[LiquidityPool],
    ) -> None:
        """
        Persist the current zone state for a symbol.

        Overwrites <symbol>_zones.json and also writes a timestamped backup
        so GapRecoveryService can find the most recent remap time.
        """
        ts  = datetime.now(tz=timezone.utc)
        doc = self._serialise(symbol, zones, pool, ts)

        # Current snapshot — always up to date
        current_path = os.path.join(self._dir, f"{symbol}_zones.json")
        self._write(current_path, doc)

        # Timestamped backup for gap recovery lookback
        stamp = ts.strftime("%Y%m%dT%H%M%S")
        backup_path = os.path.join(self._dir, f"{symbol}_zones_{stamp}.json")
        self._write(backup_path, doc)

        logger.debug(
            "ZoneFileStore saved %d zones + pool=%s for %s",
            len([z for z in zones if z.active]),
            pool.price if pool else None,
            symbol,
        )

        # Prune old backups asynchronously (best-effort, never raises)
        self._prune_old(symbol)

    # ── Read ──────────────────────────────────────────────────────────

    def load_snapshot(self, symbol: str) -> Optional[dict]:
        """
        Load the most recent snapshot for a symbol.
        Returns the raw dict (caller decides what to do with it), or None
        if no snapshot exists.
        """
        path = os.path.join(self._dir, f"{symbol}_zones.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("ZoneFileStore load failed [%s]: %s", symbol, exc)
            return None

    def last_remap_time(self, symbol: str) -> Optional[datetime]:
        """
        Return the timestamp of the most recent zone remap for a symbol,
        or None if no snapshot exists.  Used by GapRecoveryService to
        decide whether a remap is needed after a restart.
        """
        doc = self.load_snapshot(symbol)
        if doc is None:
            return None
        try:
            return datetime.fromisoformat(doc["saved_at"])
        except Exception:
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _serialise(
        symbol: str,
        zones:  List[Zone],
        pool:   Optional[LiquidityPool],
        ts:     datetime,
    ) -> dict:
        return {
            "symbol":   symbol,
            "saved_at": ts.isoformat(),
            "zones": [
                {
                    "id":          z.id,
                    "symbol":      z.symbol,
                    "zone_type":   z.zone_type.value,
                    "direction":   z.direction.value,
                    "high":        z.high,
                    "low":         z.low,
                    "created_at":  z.created_at.isoformat(),
                    "touch_count": z.touch_count,
                    "active":      z.active,
                }
                for z in zones
            ],
            "liquidity_pool": {
                "symbol":     pool.symbol,
                "direction":  pool.direction.value,
                "price":      pool.price,
                "created_at": pool.created_at.isoformat(),
            } if pool else None,
        }

    @staticmethod
    def _write(path: str, doc: dict) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
        except Exception as exc:
            logger.warning("ZoneFileStore write failed [%s]: %s", path, exc)

    def _prune_old(self, symbol: str) -> None:
        """Delete timestamped backups older than ZONE_RETENTION_DAYS."""
        try:
            cutoff = datetime.now(tz=timezone.utc).timestamp() - _RETENTION_DAYS * 86_400
            prefix = f"{symbol}_zones_"
            for fname in os.listdir(self._dir):
                if fname.startswith(prefix) and fname.endswith(".json"):
                    fpath = os.path.join(self._dir, fname)
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
        except Exception as exc:
            logger.debug("ZoneFileStore prune error: %s", exc)
