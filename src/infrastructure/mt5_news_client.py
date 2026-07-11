"""
MT5 Economic Calendar client.
Reads broker-provided calendar data directly from MetaTrader 5.
Works without NEWS_API_KEY — MT5 ships free calendar data for all connected brokers.

MT5 API used:
  mt5.calendar_event_by_currency(currency) -> event definitions with importance levels
  mt5.calendar_value_history_range(date_from, date_to) -> scheduled event values
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import MetaTrader5 as mt5

from src.domain.entities import NewsEvent
from src.domain.repositories import INewsRepository

logger = logging.getLogger(__name__)

_IMPORTANCE: Dict[int, str] = {1: "low", 2: "medium", 3: "high"}
_SENTINEL_ABS = 1e15  # MT5 uses very large floats (e.g. 2^63-1) to mean "not released"


def _safe_val(val) -> Optional[str]:
    """Convert MT5 calendar float to string; None when unreleased or missing."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if abs(f) >= _SENTINEL_ABS else str(round(f, 4))
    except (TypeError, ValueError):
        return None


class MT5NewsClient(INewsRepository):
    """
    Economic calendar sourced directly from MT5's built-in data feed.
    Used as the sole news source when NEWS_API_KEY is absent,
    or as an enrichment layer when it is present.
    """

    def __init__(self) -> None:
        # event_id -> (currency, name, importance_int)
        self._event_defs: Dict[int, Tuple[str, str, int]] = {}
        # The "no event definitions" condition is chronic on brokers that
        # don't expose calendar data (this deployment logged it 2,379 times
        # in 32 days at WARNING with zero successful fetches ever). Surface
        # it ONCE per process at WARNING, then drop to DEBUG — ForexFactory
        # carries the real news feed; this client is best-effort enrichment.
        self._warned_no_defs = False

    # ── INewsRepository ───────────────────────────────────────────

    def get_today_events(self, currencies: List[str], date_str: str) -> List[NewsEvent]:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return self._fetch(currencies, day, day + timedelta(days=1))
        except Exception as exc:
            logger.error("MT5 calendar get_today_events error: %s", exc)
            return []

    def get_range_events(
        self,
        currencies: List[str],
        start_date: str,
        end_date: str,
    ) -> List[NewsEvent]:
        try:
            date_from = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            date_to   = (
                datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            return self._fetch(currencies, date_from, date_to)
        except Exception as exc:
            logger.error("MT5 calendar get_range_events error: %s", exc)
            return []

    # ── Internals ─────────────────────────────────────────────────

    def _load_defs(self, currencies: List[str]) -> None:
        """Populate event_id -> (currency, name, importance) for the given currencies."""
        self._event_defs.clear()
        if not hasattr(mt5, "calendar_event_by_currency"):
            # MT5 Python < 5.0.37 does not ship this API — silently skip.
            logger.debug("MT5 calendar_event_by_currency unavailable (MT5 version too old) — skipping")
            return
        for currency in currencies:
            result = mt5.calendar_event_by_currency(currency)
            if not result:
                logger.debug("MT5 calendar: no event definitions for currency %s", currency)
                continue
            for ev in result:
                self._event_defs[ev.id] = (currency, ev.name, ev.importance)
        logger.debug("MT5 calendar: %d event definitions loaded for %s", len(self._event_defs), currencies)

    def _fetch(
        self,
        currencies: List[str],
        date_from: datetime,
        date_to: datetime,
    ) -> List[NewsEvent]:
        self._load_defs(currencies)
        if not self._event_defs:
            log_fn = logger.debug if self._warned_no_defs else logger.warning
            self._warned_no_defs = True
            log_fn(
                "MT5 calendar: no event definitions found for currencies %s. "
                "Ensure MT5 is connected and calendar data is available for "
                "your broker. (Further occurrences log at DEBUG.)",
                currencies,
            )
            return []

        values = mt5.calendar_value_history_range(date_from, date_to)
        if values is None:
            err = mt5.last_error()
            logger.warning("MT5 calendar_value_history_range returned None: %s", err)
            return []

        events: List[NewsEvent] = []
        for val in values:
            meta = self._event_defs.get(val.event_id)
            if meta is None:
                continue  # event belongs to a different currency — skip

            currency, name, importance = meta
            try:
                event_time = datetime.fromtimestamp(val.time, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue

            events.append(NewsEvent(
                currency=   currency,
                event_name= name,
                impact=     _IMPORTANCE.get(importance, "low"),
                event_time= event_time,
                actual=     _safe_val(val.actual_value),
                forecast=   _safe_val(val.forecast_value),
                previous=   _safe_val(val.prev_value),
                source=     "mt5",
            ))

        logger.info(
            "MT5 calendar: %d events for %d currencies  %s → %s",
            len(events), len(currencies), date_from.date(), date_to.date(),
        )
        return events
