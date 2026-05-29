"""
zone_repo.py — In-memory zone repository with optional JSON persistence.

Stores active H1 Order Block and Fair Value Gap zones per symbol.
Also tracks one LiquidityPool per symbol (the highest-priority liquidity level).

ExecutionService calls:
    get_active_zones(symbol)      → List[Zone]    (used every M1 entry evaluation)
    save_zone(zone)               → None          (called by ZoneMapper after scan)
    invalidate_zone(zone_id)      → None          (called when price taps through a zone)
    get_liquidity_pool(symbol)    → Optional[LiquidityPool]

The persist=True flag saves the zone state to zones/.zone_cache.json on every
write so zones survive a bot restart without needing a full remap.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Optional

from src.domain.entities import Direction, LiquidityPool, Zone, ZoneType
from src.domain.repositories import IZoneRepository

logger = logging.getLogger(__name__)

_CACHE_PATH = os.path.join("zones", ".zone_cache.json")


class InMemoryZoneRepository(IZoneRepository):
    """
    Thread-safe in-memory store for Zone and LiquidityPool objects.

    All public methods acquire a single reentrant lock so that ZoneMapper
    writes (from M1/M5/M30/H1 bar-close callbacks) never race with
    ExecutionService reads (also triggered by bar-close, different symbol).
    """

    def __init__(self, persist: bool = False) -> None:
        self._persist = persist
        self._lock    = Lock()

        # symbol → list of active zones (newest last — ZoneMapper appends)
        self._zones:  Dict[str, List[Zone]]              = {}
        # symbol → single highest-priority liquidity pool
        self._pools:  Dict[str, Optional[LiquidityPool]] = {}

        if persist:
            self._load_cache()

    # ── IZoneRepository interface ─────────────────────────────────────

    def get_active_zones(self, symbol: str) -> List[Zone]:
        """Return all active (not invalidated) zones for a symbol."""
        with self._lock:
            return [z for z in self._zones.get(symbol, []) if z.active]

    def save_zone(self, zone: Zone) -> None:
        """Append a zone. Silently replaces any existing zone with the same id."""
        with self._lock:
            zones = self._zones.setdefault(zone.symbol, [])
            # Remove duplicate id if present (ZoneMapper can re-scan same bar)
            self._zones[zone.symbol] = [z for z in zones if z.id != zone.id]
            self._zones[zone.symbol].append(zone)
            if self._persist:
                self._save_cache()

    def invalidate_zone(self, zone_id: str) -> None:
        """Mark a zone as inactive. ExecutionService calls this when price taps through."""
        with self._lock:
            for zones in self._zones.values():
                for z in zones:
                    if z.id == zone_id:
                        z.invalidate()
                        logger.debug("Zone invalidated: %s %s %.5f–%.5f",
                                     z.symbol, z.direction.value, z.high, z.low)
                        break
            if self._persist:
                self._save_cache()

    def get_liquidity_pool(self, symbol: str) -> Optional[LiquidityPool]:
        """Return the current liquidity pool for a symbol, or None."""
        with self._lock:
            return self._pools.get(symbol)

    # ── Additional helpers (used by ExecutionService internally) ──────

    def save_liquidity_pool(self, pool: LiquidityPool) -> None:
        """Store / overwrite the liquidity pool for a symbol."""
        with self._lock:
            self._pools[pool.symbol] = pool
            if self._persist:
                self._save_cache()

    def clear_zones(self, symbol: str) -> None:
        """
        Remove all zones for a symbol. Called by ZoneMapper before each remap
        so stale zones from the previous scan don't accumulate.
        """
        with self._lock:
            self._zones[symbol] = []
            if self._persist:
                self._save_cache()

    def all_symbols(self) -> List[str]:
        with self._lock:
            return list(self._zones.keys())

    # ── Persistence helpers ───────────────────────────────────────────

    def _save_cache(self) -> None:
        """Serialise current state to JSON. Called under the lock."""
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            data: dict = {"zones": {}, "pools": {}}

            for sym, zones in self._zones.items():
                data["zones"][sym] = [
                    {
                        "id":         z.id,
                        "symbol":     z.symbol,
                        "zone_type":  z.zone_type.value,
                        "direction":  z.direction.value,
                        "high":       z.high,
                        "low":        z.low,
                        "created_at": z.created_at.isoformat(),
                        "touch_count": z.touch_count,
                        "active":     z.active,
                    }
                    for z in zones
                ]

            for sym, pool in self._pools.items():
                if pool is not None:
                    data["pools"][sym] = {
                        "symbol":     pool.symbol,
                        "direction":  pool.direction.value,
                        "price":      pool.price,
                        "created_at": pool.created_at.isoformat(),
                    }

            with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)

        except Exception as exc:
            logger.warning("Zone cache save failed: %s", exc)

    def _load_cache(self) -> None:
        """Load persisted zones from JSON at startup."""
        if not os.path.exists(_CACHE_PATH):
            return
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            for sym, zone_list in data.get("zones", {}).items():
                self._zones[sym] = []
                for d in zone_list:
                    z = Zone(
                        id=          d["id"],
                        symbol=      d["symbol"],
                        zone_type=   ZoneType(d["zone_type"]),
                        direction=   Direction(d["direction"]),
                        high=        d["high"],
                        low=         d["low"],
                        created_at=  datetime.fromisoformat(d["created_at"]),
                        touch_count= d.get("touch_count", 0),
                        active=      d.get("active", True),
                    )
                    self._zones[sym].append(z)

            for sym, d in data.get("pools", {}).items():
                self._pools[sym] = LiquidityPool(
                    symbol=     d["symbol"],
                    direction=  Direction(d["direction"]),
                    price=      d["price"],
                    created_at= datetime.fromisoformat(d["created_at"]),
                )

            total = sum(len(v) for v in self._zones.values())
            logger.info("Zone cache loaded: %d zones across %d symbols",
                        total, len(self._zones))

        except Exception as exc:
            logger.warning("Zone cache load failed (starting fresh): %s", exc)
            self._zones  = {}
            self._pools  = {}
