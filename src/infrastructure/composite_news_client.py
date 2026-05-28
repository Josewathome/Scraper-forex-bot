"""
CompositeNewsClient — merges events from multiple INewsRepository sources.

Deduplication rule: (currency, 15-minute time bucket) is treated as the same event.
Earlier sources in the list take priority. Later sources only fill gaps.
When two sources disagree on impact level, the higher impact wins.
"""
from __future__ import annotations

import logging
from typing import Generator, List

from src.domain.entities import NewsEvent
from src.domain.repositories import INewsRepository

logger = logging.getLogger(__name__)

_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1}


def _bucket(event: NewsEvent) -> str:
    """15-minute floor key: same currency + same 15-min window = same event."""
    t = event.event_time
    floored = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
    return f"{event.currency}|{floored.isoformat()}"


class CompositeNewsClient(INewsRepository):
    """
    Merges news events from multiple sources into a single deduplicated stream.

    Typical usage:
      - API key present:  CompositeNewsClient([ForexNewsClient(...), MT5NewsClient()])
      - No API key:       MT5NewsClient() directly (no composite needed)
    """

    def __init__(self, sources: List[INewsRepository]) -> None:
        if not sources:
            raise ValueError("CompositeNewsClient requires at least one source")
        self._sources = sources

    # ── INewsRepository ───────────────────────────────────────────

    def get_today_events(self, currencies: List[str], date_str: str) -> List[NewsEvent]:
        return self._merge(
            src.get_today_events(currencies, date_str) for src in self._sources
        )

    def get_range_events(
        self,
        currencies: List[str],
        start_date: str,
        end_date: str,
    ) -> List[NewsEvent]:
        return self._merge(
            src.get_range_events(currencies, start_date, end_date) for src in self._sources
        )

    # ── Internals ─────────────────────────────────────────────────

    def _merge(self, iterables: Generator) -> List[NewsEvent]:
        """Merge events from all sources; deduplicate by 15-min bucket."""
        seen: dict = {}  # bucket -> NewsEvent (highest-impact win)
        for events in iterables:
            for ev in events:
                key = _bucket(ev)
                if key not in seen:
                    seen[key] = ev
                    continue
                # Replace only if incoming event carries a higher impact rating.
                # This handles the case where MT5 marks an event HIGH that the
                # external API labelled MEDIUM (or vice versa).
                existing_rank = _IMPACT_RANK.get(seen[key].impact, 0)
                incoming_rank = _IMPACT_RANK.get(ev.impact, 0)
                if incoming_rank > existing_rank:
                    seen[key] = ev

        merged = sorted(seen.values(), key=lambda e: e.event_time)
        logger.debug("CompositeNewsClient: merged %d unique events", len(merged))
        return merged
