"""
News API Client — ForexFactory + MyFxBook HTTP layer.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Only responsibility: make HTTP calls and return parsed JSON.
All times are normalised to UTC before returning.

Common 403 causes fixed here:
  1. Empty / placeholder API key → caught early with clear error
  2. python-requests User-Agent blocked → spoofed to match Postman
  3. Incorrect multi-value params → built as list of tuples
  4. API key sent as header AND query param for maximum compatibility
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
import src.config as config

from src.domain.entities import NewsEvent
from src.domain.repositories import INewsRepository
from .cache_store import JsonCacheStore

logger = logging.getLogger(__name__)

BASE_FF     = f"{config.NEWS_API_URL}/forexfactory/events"
BASE_MFX    = f"{config.NEWS_API_URL}/myfxbook/events"

FF_CACHE_TTL  = 30 * 60
MFX_CACHE_TTL = 1220 * 60


# ─────────────────────────────────────────────
#  Time parsing helpers
# ─────────────────────────────────────────────

# Sentinel returned for "All Day" events so the caller can
# create a full-day window instead of a point-in-time window.
ALL_DAY_SENTINEL = "ALL_DAY"


def _parse_ff_time(time_str: str, date_str: str) -> Optional[datetime]:
    """
    Parse ForexFactory time string → UTC datetime.

    Handles:
      '5:30pm'   → normal timed event
      'All Day'  → returns midnight UTC of that date (caller widens window)
      'Tentative'→ treated as midnight (no fixed time known)
      ''         → skip (returns None)
    """
    clean = time_str.strip()

    # Full-day / tentative events → anchor to midnight UTC
    if clean.lower() in ("all day", "tentative", ""):
        if not clean:
            return None
        try:
            base = datetime.strptime(date_str, "%Y-%m-%d")
            # Return midnight UTC — _build_windows will expand to full day
            return datetime(base.year, base.month, base.day, 0, 0,
                            tzinfo=timezone.utc)
        except ValueError:
            return None

    # Normal timed event e.g. '5:30pm', '11am'
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            t    = datetime.strptime(clean.upper(), fmt)
            base = datetime.strptime(date_str, "%Y-%m-%d")
            return datetime(base.year, base.month, base.day,
                            t.hour, t.minute, tzinfo=timezone.utc)
        except ValueError:
            continue

    logger.debug("Unrecognised FF time format: '%s' — skipping event", time_str)
    return None


def _parse_mfxbook_time(
    time_str: str, date_str: str, tz_label: str
) -> Optional[datetime]:
    """Parse MyFxBook time like '03:30' with GMT offset label."""
    try:
        h, m = map(int, time_str.split(":"))
        base  = datetime.strptime(date_str, "%Y-%m-%d")
        match = re.search(r"GMT\s*([+-]\d+):(\d+)", tz_label)
        if match:
            sign  = 1 if "+" in match.group(1) else -1
            off_h = abs(int(match.group(1)))
            off_m = int(match.group(2))
            offset = timezone(timedelta(hours=sign * off_h, minutes=sign * off_m))
        else:
            offset = timezone.utc
        return datetime(base.year, base.month, base.day, h, m,
                        tzinfo=offset).astimezone(timezone.utc)
    except Exception as exc:
        logger.warning("Could not parse MFX time '%s' / '%s': %s", time_str, tz_label, exc)
        return None


# ─────────────────────────────────────────────
#  Client
# ─────────────────────────────────────────────

class ForexNewsClient(INewsRepository):
    """
    Fetches and caches economic calendar events.
    All times returned are UTC datetime objects.
    """

    # Match what Postman sends — avoids bot-blocking 403s
    _HEADERS = {
        "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/123.0.0.0 Safari/537.36",
        "Accept":      "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, api_key: str, cache: JsonCacheStore) -> None:
        self._api_key = api_key.strip()
        self._cache   = cache
        self._session = requests.Session()

        # Validate key is not empty / placeholder.
        # CRITICAL: if the key is missing the news filter is completely disabled —
        # the bot will trade straight through NFP, FOMC, CPI and other high-impact
        # events with no protection. Emit a CRITICAL log (visible in all log levels)
        # so the operator cannot miss it, then mark the key empty so callers know
        # to skip all API fetches.
        if not self._api_key or self._api_key.startswith("sk-YOUR"):
            logger.critical(
                "┌─────────────────────────────────────────────────────────────┐\n"
                "│  NEWS GUARDIAN DISABLED — NEWS_API_KEY is missing/invalid   │\n"
                "│  The bot will trade through ALL high-impact news events.     │\n"
                "│  Set NEWS_API_KEY in your .env file to re-enable protection. │\n"
                "└─────────────────────────────────────────────────────────────┘"
            )
            self._api_key = ""
        else:
            logger.info("News API key loaded (%d chars) — news guardian active.", len(self._api_key))

        self._session.headers.update(self._HEADERS)
        if self._api_key:
            # Set in header (primary) — also passed as query param below
            self._session.headers["X-API-Key"] = self._api_key

    # ── INewsRepository ───────────────────────────────────────────

    def get_today_events(
        self,
        currencies: List[str],
        date_str:   str,
    ) -> List[NewsEvent]:
        """ForexFactory — today's events, already UTC."""
        if not self._api_key:
            logger.warning("ForexFactory fetch skipped — NEWS_API_KEY not set. News filtering inactive.")
            return []

        cache_key = f"ff_{date_str}_{'_'.join(sorted(currencies))}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            logger.info("ForexFactory: %d events from cache for %s", len(cached), date_str)
            return [self._dict_to_event(e) for e in cached]

        # Build params as list of tuples so requests sends
        # ?date=...&currency=USD&currency=EUR  (not currency[]=...)
        params: List[tuple] = [("date", date_str), ("api_key", self._api_key)]
        for c in currencies:
            params.append(("currency", c))

        logger.info("Fetching ForexFactory events for %s — %s", date_str, currencies)
        try:
            # timeout is mandatory here: this runs on the MAIN event loop
            # (news refresh every NEWS_REFRESH_INTERVAL_MINUTES), and requests
            # blocks forever without one — a hung news host would freeze tick
            # processing and SL/TP management. Timeout → RequestException →
            # [] → NewsManager's stale-data fail-closed path takes over.
            resp = self._session.get(BASE_FF, params=params, timeout=(3.05, 10))
            self._log_response_debug(resp)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as exc:
            logger.error(
                "ForexFactory HTTP %s: %s\n"
                "  URL: %s\n"
                "  Tip: confirm NEWS_API_KEY in .env matches what works in Postman.",
                exc.response.status_code if exc.response else "?",
                exc,
                exc.response.url if exc.response else "",
            )
            return []
        except requests.RequestException as exc:
            logger.error("ForexFactory request failed: %s", exc)
            return []

        events: List[NewsEvent] = []
        for raw in data.get("events", []):
            dt = _parse_ff_time(raw.get("time", ""), date_str)
            if dt is None:
                continue
            events.append(NewsEvent(
                currency=   raw["currency"],
                event_name= raw["event"],
                impact=     raw.get("impact", "low"),
                event_time= dt,
                actual=     raw.get("actual"),
                forecast=   raw.get("forecast"),
                previous=   raw.get("previous"),
                source=     "forexfactory",
            ))

        logger.info("ForexFactory returned %d events for %s", len(events), date_str)
        self._cache.set(cache_key, [self._event_to_dict(e) for e in events], FF_CACHE_TTL)
        return events

    def get_range_events(
        self,
        currencies: List[str],
        start_date: str,
        end_date:   str,
    ) -> List[NewsEvent]:
        """MyFxBook — date range, converts local timezone → UTC.

        NOTE: Call this with short ranges (≤7 days) only.
        For multi-month backtests use Backtester._fetch_news_chunked()
        which splits the full range into weekly requests automatically.
        """
        if not self._api_key:
            logger.warning("MyFxBook fetch skipped — NEWS_API_KEY not set. News filtering inactive.")
            return []

        cache_key = f"mfx_{start_date}_{end_date}_{'_'.join(sorted(currencies))}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "MyFxBook cache hit: %s → %s (%d events)",
                start_date, end_date, len(cached),
            )
            return [self._dict_to_event(e) for e in cached]

        params: List[tuple] = [
            ("start_date", start_date),
            ("end_date",   end_date),
            ("api_key",    self._api_key),
        ]
        for c in currencies:
            params.append(("currency", c))

        logger.info(
            "Fetching MyFxBook events: %s → %s  currencies=%s",
            start_date, end_date, currencies,
        )
        try:
            # Same main-loop freeze rationale as the ForexFactory call above.
            resp = self._session.get(BASE_MFX, params=params, timeout=(3.05, 10))
            self._log_response_debug(resp)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as exc:
            logger.error(
                "MyFxBook HTTP %s: %s\n"
                "  URL: %s\n"
                "  Tip: confirm NEWS_API_KEY in config.py matches what works in Postman.",
                exc.response.status_code if exc.response else "?",
                exc,
                exc.response.url if exc.response else "",
            )
            return []
        except requests.RequestException as exc:
            logger.error("MyFxBook request failed: %s", exc)
            return []

        # ── FIX: log what the API actually sent back ───────────────
        raw_count = len(data.get("events", []))
        logger.info(
            "MyFxBook API responded: %s → %s | raw events=%d | cached=%s",
            start_date, end_date, raw_count, data.get("cached", "?"),
        )
        if raw_count == 0:
            logger.warning(
                "MyFxBook returned 0 events for %s → %s. "
                "This usually means the date range is too large for a single request. "
                "Use _fetch_news_chunked() instead of calling get_range_events() directly "
                "for multi-month ranges.",
                start_date, end_date,
            )

        tz_label = data.get("timezone", "UTC")
        events: List[NewsEvent] = []
        for raw in data.get("events", []):
            dt = _parse_mfxbook_time(
                raw.get("time", "00:00"),
                raw.get("date", start_date),
                tz_label,
            )
            if dt is None:
                continue
            events.append(NewsEvent(
                currency=   raw["currency"],
                event_name= raw["event"],
                impact=     raw.get("impact", "low"),
                event_time= dt,
                actual=     raw.get("actual"),
                forecast=   raw.get("forecast"),
                previous=   raw.get("previous"),
                source=     "myfxbook",
            ))

        logger.info(
            "MyFxBook parsed %d valid events (after timezone conversion) for %s → %s",
            len(events), start_date, end_date,
        )
        self._cache.set(cache_key, [self._event_to_dict(e) for e in events], MFX_CACHE_TTL)
        return events

    # ── Helpers ───────────────────────────────────────────────────

    def _log_response_debug(self, resp: requests.Response) -> None:
        """Log request details at DEBUG level — useful when diagnosing 403s."""
        logger.debug(
            "HTTP %s  %s\n"
            "  Request headers: %s",
            resp.status_code, resp.url,
            dict(resp.request.headers) if resp.request else {},
        )
        if resp.status_code == 403:
            logger.error(
                "403 Forbidden from %s\n"
                "  Sent key: %s…  (first 8 chars)\n"
                "  Full URL: %s\n"
                "  Check: does this exact key work in Postman with X-API-Key header?",
                resp.url.split("?")[0],
                self._api_key[:8] if self._api_key else "(empty)",
                resp.url,
            )

    def _event_to_dict(self, e: NewsEvent) -> dict:
        return {
            "currency":   e.currency, "event_name": e.event_name,
            "impact":     e.impact,   "event_time": e.event_time.isoformat(),
            "actual":     e.actual,   "forecast":   e.forecast,
            "previous":   e.previous, "source":     e.source,
        }

    def _dict_to_event(self, d: dict) -> NewsEvent:
        return NewsEvent(
            currency=   d["currency"],   event_name= d["event_name"],
            impact=     d["impact"],     event_time= datetime.fromisoformat(d["event_time"]),
            actual=     d.get("actual"), forecast=   d.get("forecast"),
            previous=   d.get("previous"), source=  d.get("source", ""),
        )