"""
time_sync.py — Authoritative UTC clock for the trading bot.

Problem
───────
The broker (MT5) sends tick timestamps in broker-local time (UTC+3) encoded
as Unix epoch seconds.  When code does datetime.fromtimestamp(tick.time, UTC)
it gets a datetime that is 3 hours ahead of true UTC.  Additionally, a Docker
container's system clock may drift if the host's NTP daemon is misconfigured.

Solution
────────
TrueTimeClock queries an external NTP source at startup and every
NTP_RESYNC_INTERVAL_SECS seconds thereafter.  It maintains an offset:

    ntp_offset = ntp_time - system_time

Every call to utc_now() returns:

    datetime.now(UTC) + timedelta(seconds=ntp_offset)

Sync hierarchy
──────────────
  1. NTP via raw UDP (pool.ntp.org) — fast, accurate, no dependencies
  2. HTTP time API (worldtimeapi.org) — fallback when UDP/123 is firewalled
  3. System clock — last resort; Docker system clocks are normally NTP-synced

The measured offset is logged so operators can see how much the system clock
deviated from true UTC.
"""
from __future__ import annotations

import logging
import socket
import struct
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# NTP constants
_NTP_DELTA      = 2208988800          # seconds between 1900-01-01 and 1970-01-01
_NTP_SERVERS    = [
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.google.com",
    "time.windows.com",
]
_NTP_TIMEOUT    = 3.0                  # seconds per server attempt
_NTP_PORT       = 123
_HTTP_TIME_URL  = "http://worldtimeapi.org/api/timezone/UTC"
_HTTP_TIMEOUT   = 4.0

NTP_RESYNC_INTERVAL_SECS: float = 30 * 60   # re-sync every 30 minutes


class TrueTimeClock:
    """
    Authoritative UTC clock.  Calibrates against NTP on startup and re-syncs
    every NTP_RESYNC_INTERVAL_SECS seconds.

    Usage
    ─────
        clock = TrueTimeClock()
        now   = clock.utc_now()    # datetime, timezone-aware, true UTC

    One instance should live on the application root (main_stream.py) and be
    passed to every component that needs accurate time.
    """

    def __init__(self) -> None:
        self._ntp_offset:   float = 0.0     # ntp_time - system_time (seconds)
        self._last_sync_at: float = 0.0     # monotonic clock when last synced
        self._synced_once:  bool  = False
        self._sync()

    # ── Public API ────────────────────────────────────────────────────

    def utc_now(self) -> datetime:
        """Return current UTC time, calibrated against NTP."""
        # Re-sync if interval has elapsed and we've synced at least once before.
        mono = _time.monotonic()
        if self._synced_once and (mono - self._last_sync_at) >= NTP_RESYNC_INTERVAL_SECS:
            self._sync()

        return datetime.now(tz=timezone.utc) + timedelta(seconds=self._ntp_offset)

    def offset_seconds(self) -> float:
        """Return the measured NTP calibration offset (ntp - system, seconds)."""
        return self._ntp_offset

    # ── Sync helpers ──────────────────────────────────────────────────

    def _sync(self) -> None:
        """Try each sync source in order.  Always succeeds (falls back to 0)."""
        offset = self._try_ntp()
        if offset is None:
            logger.info("TrueTimeClock: NTP UDP failed, trying HTTP time API …")
            offset = self._try_http()
        if offset is None:
            if not self._synced_once:
                logger.warning(
                    "TrueTimeClock: all time sources failed — using system clock. "
                    "Docker system clocks are normally NTP-synced from the host."
                )
            offset = 0.0

        prev = self._ntp_offset
        self._ntp_offset   = offset
        self._last_sync_at = _time.monotonic()
        self._synced_once  = True

        if abs(offset) < 0.5:
            level = logger.debug
        elif abs(offset) < 5.0:
            level = logger.info
        else:
            level = logger.warning

        level(
            "TrueTimeClock: ntp_offset=%.3fs (system clock is %.3fs %s true UTC)%s",
            offset,
            abs(offset),
            "ahead of" if offset < 0 else "behind",
            f"  [prev={prev:.3f}s, drift={offset - prev:+.3f}s]" if self._synced_once else "",
        )

    def _try_ntp(self) -> Optional[float]:
        """
        Query NTP via UDP port 123.  Returns offset (ntp - system) or None.

        NTP packet: 48 bytes.  Byte 0 = 0x1B (LI=0, VN=3, Mode=3).
        Response: transmit timestamp at bytes 40-47 (NTP epoch, fixed-point).
        """
        for server in _NTP_SERVERS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(_NTP_TIMEOUT)
                    packet = b"\x1b" + b"\x00" * 47
                    t_send = _time.time()
                    s.sendto(packet, (server, _NTP_PORT))
                    data, _ = s.recvfrom(1024)
                    t_recv = _time.time()

                if len(data) < 48:
                    continue

                # Extract transmit timestamp (seconds + fraction, 32-bit each)
                tx_secs  = struct.unpack("!I", data[40:44])[0] - _NTP_DELTA
                tx_frac  = struct.unpack("!I", data[44:48])[0] / 2**32
                ntp_time = tx_secs + tx_frac
                # Use midpoint of send/receive as local time estimate
                local_time = (t_send + t_recv) / 2
                offset = ntp_time - local_time

                logger.info(
                    "TrueTimeClock: NTP sync via %s — offset=%.3fs  ntp=%s  sys=%s",
                    server,
                    offset,
                    datetime.fromtimestamp(ntp_time, tz=timezone.utc).strftime("%H:%M:%S"),
                    datetime.fromtimestamp(local_time, tz=timezone.utc).strftime("%H:%M:%S"),
                )
                return offset

            except (socket.timeout, socket.gaierror, OSError):
                continue

        return None

    def _try_http(self) -> Optional[float]:
        """
        Fall back to worldtimeapi.org HTTP API.  Returns offset or None.
        """
        try:
            import requests
            t_before = _time.time()
            resp = requests.get(_HTTP_TIME_URL, timeout=_HTTP_TIMEOUT)
            t_after  = _time.time()
            resp.raise_for_status()
            data     = resp.json()
            unix_utc = float(data["unixtime"])
            local    = (t_before + t_after) / 2
            offset   = unix_utc - local
            logger.info(
                "TrueTimeClock: HTTP time sync — offset=%.3fs  api=%s",
                offset,
                datetime.fromtimestamp(unix_utc, tz=timezone.utc).strftime("%H:%M:%S"),
            )
            return offset
        except Exception as exc:
            logger.warning("TrueTimeClock: HTTP time API failed: %s", exc)
            return None
