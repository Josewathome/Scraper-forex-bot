"""
mt5_time.py — The single conversion point from raw MT5 epoch time to bot time.

MT5 server time is the sole authoritative clock for this bot — not the host
machine's clock, not NTP, not any notion of "true" calendar UTC. The only
correction ever applied is config.BROKER_UTC_OFFSET_HOURS, a static offset
(auto-detected once at startup in main_stream.py, from MT5's own tick data)
that aligns MT5's server-clock hour-of-day with real calendar hours — this is
what lets session windows (e.g. "London opens at 08:00") and news-event
windows (reported in true UTC by ForexFactory/MyFxBook/MT5's calendar)
compare correctly against MT5 time.

Every MT5-derived timestamp in the bot (candles, ticks, BrokerClock.now(),
position open-times) MUST go through mt5_epoch_to_datetime() so the offset is
applied exactly once, consistently, everywhere. This module has no
dependency on MT5Gateway/market_data_repo so both can import it without a
circular-import problem.

Offset persistence (why it exists): detect_utc_offset() correctly refuses to
measure from a stale tick, so on a WEEKEND restart it cannot detect anything
and startup used to silently fall back to the static config constant. That
constant goes stale twice a year — the broker follows EET/EEST daylight
saving (UTC+2 winter / UTC+3 summer), so a weekend restart in the wrong half
of the year shifted every session window and news window by a full hour for
the entire following week (observed live: Fri 2026-07-10 detected UTC+3,
Sat restart fell back to the static +2). The fix: every successful live
detection is persisted to CHECKPOINT_DIR, and startup falls back to that
last-good measurement before ever trusting the static constant.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import src.config as config

logger = logging.getLogger(__name__)

_OFFSET_FILENAME = "broker_utc_offset.json"


def mt5_epoch_to_datetime(epoch_seconds: float) -> datetime:
    offset_hours = getattr(config, "BROKER_UTC_OFFSET_HOURS", 0)
    return datetime.fromtimestamp(epoch_seconds - offset_hours * 3600, tz=timezone.utc)


# ── Offset persistence ────────────────────────────────────────────────────────

def _offset_path() -> Path:
    return Path(getattr(config, "CHECKPOINT_DIR", ".checkpoints")) / _OFFSET_FILENAME


def save_detected_offset(offset_hours: int) -> None:
    """
    Persist a successfully LIVE-DETECTED offset. Never call this with a
    fallback value — the file must only ever contain real measurements,
    otherwise a bad fallback would perpetuate itself across restarts.
    """
    try:
        path = _offset_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "offset_hours":    int(offset_hours),
            "detected_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        }))
    except Exception as exc:
        # Persistence is best-effort — a write failure must never block startup.
        logger.warning("Could not persist broker UTC offset: %s", exc)


def load_persisted_offset() -> Optional[Tuple[int, str]]:
    """Return (offset_hours, detected_at_iso) from the last successful live
    detection, or None if no valid persisted measurement exists."""
    try:
        data = json.loads(_offset_path().read_text())
        offset = int(data["offset_hours"])
        if not (-12 <= offset <= 14):
            return None
        return offset, str(data.get("detected_at_utc", "unknown"))
    except Exception:
        return None


def resolve_startup_offset(detected: int) -> Tuple[int, str]:
    """
    Decide which broker-UTC offset to apply at startup and describe the source.

    Priority:
      1. live detection this startup (also persisted for future fallback)
      2. persisted value from the last successful live detection
         (covers weekend/market-closed restarts, when detection refuses
         stale ticks — the case the static constant got wrong)
      3. static config.BROKER_UTC_OFFSET_HOURS (last resort, may be a
         stale DST half-year value)

    NOTE: detected == 0 is treated as "detection failed" — detect_utc_offset()
    returns 0 on every failure path, so a genuine UTC+0 broker is
    indistinguishable from failure. That ambiguity predates this function and
    is harmless for this broker (EET/EEST, +2/+3 year-round).
    """
    if detected != 0:
        save_detected_offset(detected)
        return detected, "detected live"

    persisted = load_persisted_offset()
    if persisted is not None:
        offset, detected_at = persisted
        return offset, f"persisted from last live detection at {detected_at}"

    static = getattr(config, "BROKER_UTC_OFFSET_HOURS", 0)
    return static, "static config fallback (no detection, no persisted value)"
