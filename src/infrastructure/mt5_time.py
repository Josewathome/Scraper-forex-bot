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
"""
from __future__ import annotations
from datetime import datetime, timezone

import src.config as config


def mt5_epoch_to_datetime(epoch_seconds: float) -> datetime:
    offset_hours = getattr(config, "BROKER_UTC_OFFSET_HOURS", 0)
    return datetime.fromtimestamp(epoch_seconds - offset_hours * 3600, tz=timezone.utc)
