"""
scheduler.py — Lightweight background task scheduler.

Runs periodic tasks in daemon threads so they never block the main
trading loop.  All times are UTC.

Usage:
    sched = Scheduler()
    sched.add_daily("cleanup",          cleanup_fn, hour=2)
    sched.add_weekly("weekly_backtest", backtest_fn, weekday=5, hour=6)
    sched.start()
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Tuple, Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60   # seconds between schedule checks


class Scheduler:

    def __init__(self) -> None:
        self._tasks: List[Tuple[Any, ...]] = []
        self._last_run: dict = {}
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Registration ─────────────────────────────────────────────────

    def add_daily(self, name: str, fn: Callable, hour: int = 2) -> None:
        """Schedule fn to run once per day at `hour` UTC (0-23)."""
        self._tasks.append(("daily", name, fn, hour))
        logger.debug("Scheduler: registered daily task '%s' at %02d:00 UTC", name, hour)

    def add_weekly(
        self,
        name:    str,
        fn:      Callable,
        weekday: int = 5,   # 0=Mon … 5=Sat … 6=Sun
        hour:    int = 6,
    ) -> None:
        """Schedule fn to run once per week on `weekday` at `hour` UTC."""
        self._tasks.append(("weekly", name, fn, weekday, hour))
        logger.debug(
            "Scheduler: registered weekly task '%s' on weekday=%d at %02d:00 UTC",
            name, weekday, hour,
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="Scheduler"
        )
        self._thread.start()
        logger.info("Scheduler started (%d task(s) registered).", len(self._tasks))

    def stop(self) -> None:
        self._running = False
        logger.info("Scheduler stopped.")

    # ── Main loop ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            now = datetime.now(tz=timezone.utc)
            for task in self._tasks:
                kind = task[0]
                name = task[1]
                fn   = task[2]

                if kind == "daily":
                    target_hour = task[3]
                    run_key     = f"{name}_{now.date()}"
                    should_run  = (
                        now.hour == target_hour
                        and run_key not in self._last_run
                    )

                elif kind == "weekly":
                    target_weekday = task[3]
                    target_hour    = task[4]
                    week_num       = now.isocalendar()[1]
                    run_key        = f"{name}_W{week_num}"
                    should_run     = (
                        now.weekday() == target_weekday
                        and now.hour   == target_hour
                        and run_key not in self._last_run
                    )

                else:
                    continue

                if should_run:
                    self._last_run[run_key] = now
                    self._fire(name, fn)

            time.sleep(_POLL_INTERVAL)

    def _fire(self, name: str, fn: Callable) -> None:
        def _wrapper() -> None:
            try:
                logger.info("Scheduler: starting task '%s'.", name)
                fn()
                logger.info("Scheduler: task '%s' finished.", name)
            except Exception as exc:
                logger.exception("Scheduler: task '%s' raised: %s", name, exc)

        threading.Thread(target=_wrapper, daemon=True, name=f"Task-{name}").start()