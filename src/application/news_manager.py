"""
news_manager.py — Phase 2: The "Shield".
Blocks trading during high-impact news windows.
Settings come from config.py.
"""
from __future__ import annotations
import logging
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional

import src.config as config
from src.domain.entities import NewsEvent, NoTradeWindow
from src.domain.repositories import INewsRepository
from src.domain.value_objects import BrokerCost, PipCalculator

logger = logging.getLogger(__name__)


class NewsManager:

    def __init__(self, news_repo: INewsRepository, has_external_feed: bool = False) -> None:
        self._repo             = news_repo
        self._has_external     = bool(has_external_feed)
        self._windows:         List[NoTradeWindow] = []
        self._last_refresh:    Optional[datetime]  = None   # last attempt
        self._last_refresh_ok: Optional[datetime]  = None   # last SUCCESSFUL refresh
        self._events_seen_session: int = 0                  # cumulative events observed
        self._last_event_count: int = 0                     # events in most recent refresh
        self._refresh_interval = timedelta(minutes=config.NEWS_REFRESH_INTERVAL_MINUTES)
        self._max_staleness    = timedelta(
            minutes=getattr(config, "NEWS_MAX_STALENESS_MIN", 30)
        )
        self._require_external = getattr(config, "NEWS_REQUIRE_EXTERNAL_FEED", False)

    def health(self) -> dict:
        """Observability for the dashboard/status — exposes news-feed confidence."""
        return {
            "has_external_feed": self._has_external,
            "last_refresh_ok":   self._last_refresh_ok.isoformat() if self._last_refresh_ok else None,
            "events_seen_session": self._events_seen_session,
            "last_event_count":  self._last_event_count,
            "require_external":  self._require_external,
        }

    # ── Public API ────────────────────────────

    def refresh_if_needed(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(tz=timezone.utc)
        if self._last_refresh is None or (now - self._last_refresh) >= self._refresh_interval:
            self._last_refresh = now
            try:
                self._refresh_events(now)
                self._last_refresh_ok = now
            except Exception as exc:
                # Do NOT advance _last_refresh_ok — staleness will eventually
                # trip the fail-closed news gate if this keeps failing.
                logger.error("News refresh failed: %s — news data going stale", exc)

    def is_data_fresh(self, now: datetime) -> bool:
        """True only if we have a successful refresh within the staleness window."""
        if self._last_refresh_ok is None:
            return False
        return (now - self._last_refresh_ok) <= self._max_staleness

    def is_blocked(self, symbol: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(tz=timezone.utc)

        # ── FAIL CLOSED when no trustworthy news source ────────────
        # When configured to require an external feed and none is present, the
        # only coverage is MT5's best-effort calendar — block rather than trade
        # with weak news protection.
        if self._require_external and not self._has_external:
            logger.warning(
                "NEWS BLOCK | %s | NEWS_REQUIRE_EXTERNAL_FEED set but no external feed configured",
                symbol,
            )
            return True

        # ── FAIL CLOSED on stale data ──────────────────────────────
        # If we cannot prove the market is clear of high-impact news (no
        # successful refresh recently), block the entry rather than trade blind.
        if not self.is_data_fresh(now):
            logger.warning(
                "NEWS BLOCK | %s | news data stale (last_ok=%s) — failing closed",
                symbol,
                self._last_refresh_ok.strftime("%H:%M UTC") if self._last_refresh_ok else "never",
            )
            return True

        currencies = config.SYMBOL_CURRENCIES.get(symbol, [])
        for w in self._windows:
            if w.currency in currencies and w.is_blocking(now):
                logger.info("NEWS BLOCK | %s | event='%s' until %s",
                            symbol, w.event_name, w.end.strftime("%H:%M UTC"))
                return True
        return False

    def broker_cost_exceeds_atr(
        self,
        symbol:      str,
        atr_price:   float,
        broker_cost: BrokerCost,
        pip_calc:    PipCalculator,
    ) -> bool:
        if atr_price <= 0:
            return False
        atr_pips  = pip_calc.price_to_pips(atr_price)
        threshold = atr_pips * config.ATR_COST_THRESHOLD
        cost_pips = broker_cost.round_trip_cost_pips()
        if cost_pips > threshold:
            logger.warning("COST BLOCK | %s | cost=%.2f pips > ATR×%.0f%%=%.2f pips",
                           symbol, cost_pips, config.ATR_COST_THRESHOLD * 100, threshold)
            return True
        return False

    # ── Internals ─────────────────────────────

    def _refresh_events(self, now: datetime) -> None:
        date_str       = now.strftime("%Y-%m-%d")
        all_currencies = list({c for cs in config.SYMBOL_CURRENCIES.values() for c in cs})

        logger.info("Refreshing news for %s …", date_str)
        events: List[NewsEvent] = self._repo.get_today_events(
            currencies=all_currencies, date_str=date_str,
        )
        # Track feed health: cumulative + most-recent event counts. An MT5-only
        # feed that never returns events is best-effort coverage, surfaced via
        # health() and the startup warning so the operator is not misled.
        self._last_event_count = len(events or [])
        self._events_seen_session += self._last_event_count

        # Within 60 minutes of midnight, prefetch tomorrow's events so the
        # high-impact buffer at e.g. 00:30 is already loaded before it fires.
        if now.hour == 23:
            tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            logger.info("Pre-fetching next-day news for %s …", tomorrow)
            try:
                next_events = self._repo.get_today_events(
                    currencies=all_currencies, date_str=tomorrow,
                )
                events = events + next_events
            except Exception as exc:
                logger.warning("Next-day news prefetch failed: %s", exc)

        windows = []
        for e in events:
            impact = e.impact.lower() if hasattr(e, "impact") else "low"

            # ── Tiered buffer selection ───────────────────────────
            if impact == "high":
                buffer = timedelta(minutes=config.HIGH_IMPACT_BUFFER_MINS)
            elif impact == "medium":
                buffer = timedelta(minutes=config.MEDIUM_IMPACT_BUFFER_MINS)
            else:
                buffer = timedelta(minutes=config.NEWS_BUFFER_MINUTES)   # low / unknown

            # Skip low-impact entirely if STRICT_NEWS_FILTER is on
            if impact == "low" and config.STRICT_NEWS_FILTER:
                continue

            # Skip zero-width windows — a buffer of 0 creates a window that only
            # matches at the exact millisecond of the event (never blocks in practice)
            # and wastes memory. Matches the backtester's _build_windows() behaviour.
            if buffer.total_seconds() == 0:
                continue

            is_all_day = (
                e.event_time.hour == 0 and e.event_time.minute == 0
                and ("all day" in e.event_name.lower()
                     or (e.actual is None and e.forecast is None))
            )
            if is_all_day:
                start = e.event_time
                end   = e.event_time.replace(hour=23, minute=59)
            else:
                start = e.event_time - buffer
                end   = e.event_time + buffer
                logger.info(
                    "No-trade [%s impact]: %s [%s] %s→%s UTC",
                    impact.upper(), e.currency, e.event_name,
                    start.strftime("%H:%M"), end.strftime("%H:%M"),
                )
            windows.append(NoTradeWindow(start=start, end=end,
                                         currency=e.currency, event_name=e.event_name))

        self._windows = windows
        logger.info("Loaded %d no-trade windows.", len(windows))