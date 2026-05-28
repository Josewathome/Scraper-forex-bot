"""
Cache Store — JSON file-based key-value cache.
Used by news_client.py to respect the 30-minute API refresh window
and avoid hammering the forex news endpoints.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent.parent / ".cache"


class JsonCacheStore:
    """
    Persistent local cache backed by individual JSON files.
    Each key maps to a file: .cache/{key}.json
    Each entry stores { "expires_at": <iso-utc>, "data": <any> }.
    """

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[Any]:
        """Return cached data if it exists and has not expired. None otherwise."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                entry = json.load(f)
            expires_at = datetime.fromisoformat(entry["expires_at"])
            if datetime.now(tz=timezone.utc) >= expires_at:
                path.unlink(missing_ok=True)
                return None
            return entry["data"]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Cache read error for key=%s: %s", key, exc)
            path.unlink(missing_ok=True)
            return None

    def set(self, key: str, data: Any, ttl_seconds: int) -> None:
        """Write data to cache with a TTL in seconds."""
        from datetime import timedelta
        expires_at = (datetime.now(tz=timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        entry = {"expires_at": expires_at, "data": data}
        path = self._path(key)
        try:
            with open(path, "w") as f:
                json.dump(entry, f, indent=2)
        except OSError as exc:
            logger.warning("Cache write error for key=%s: %s", key, exc)

    def invalidate(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def clear_all(self) -> None:
        for f in self._dir.glob("*.json"):
            f.unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        # Sanitise key to be safe as a filename
        safe = key.replace("/", "_").replace(":", "_").replace(" ", "_")
        return self._dir / f"{safe}.json"