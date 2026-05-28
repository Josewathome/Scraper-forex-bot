"""
zmq_feed.py — ZeroMQ Subscriber Thread.

CONTAINER ARCHITECTURE
Both the MT5 EA (ZoneBotBridge.mq5) and this Python bot run inside
the same Wine prefix within the same Docker container.  The EA binds
a ZMQ PUB socket on tcp://127.0.0.1:5556 (loopback only).  This
subscriber connects to that same address — no Docker networking,
no cross-container routing, pure Wine loopback TCP.

STARTUP SEQUENCING
The EA starts when the MT5 terminal launches (inside Wine).
This Python process starts a few seconds later (also inside Wine).
The subscriber must tolerate the EA's PUB socket not being ready
immediately — zmq.SUB connect() is non-blocking and will reconnect
transparently once the EA binds.  The RCVTIMEO timeout in the recv
loop ensures we don't block forever if no messages arrive yet.

RESTART SAFETY
On `docker compose restart` the Wine prefix persists (/config volume).
Both sides restart cleanly:
  - EA rebinds the PUB socket at OnInit()
  - This thread reconnects on the next recv() attempt after restart

RECONNECT BEHAVIOUR
zmq.SUB connect() is idempotent — reconnect attempts are safe to
repeat.  If the EA crashes and rebinds, the SUB automatically
reattaches within ZMQ's reconnect interval.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable, Dict, Optional

import zmq

from src.domain.entities import Timeframe
from src.infrastructure.stream.candle_builder import CandleBuilder

logger = logging.getLogger(__name__)

_TF_MAP: Dict[str, Timeframe] = {
    "M1":  Timeframe.M1,
    "M5":  Timeframe.M5,
    "M30": Timeframe.M30,
    "H1":  Timeframe.H1,
    "H4":  Timeframe.H4,
}

# Sentinel placed on the event queue when the feed thread exits unrecoverably
FEED_STOPPED = object()

# How long recv() blocks before checking the stop event (milliseconds)
_RECV_TIMEOUT_MS = 1000

# How long to wait before attempting a reconnect after a ZMQ error (seconds)
_RECONNECT_DELAY_S = 3.0


class ZmqFeed:
    """
    ZeroMQ SUB thread that drives CandleBuilders from live EA ticks.

    Usage:
        feed = ZmqFeed(
            endpoint="tcp://127.0.0.1:5556",
            builders={"GBPUSD": CandleBuilder("GBPUSD")},
            event_queue=queue.Queue(),
        )
        feed.start()
        ...
        feed.stop()

    All parsed events are pushed onto `event_queue` as dicts:
        {"type": "CANDLE_CLOSED", "candle": <Candle>}    ← from builder callbacks
        {"type": "TICK",    "sym": str, "bid": float, "ask": float, "time": int}
        {"type": "BAR_CLOSE", "sym": str, "tf": str, open/high/low/close/vol/time}
        {"type": "BAR_OPEN",  "sym": str, "tf": str, "time": int}
        {"type": "TRADE",     ...raw EA payload dict...}
        {"type": "HEARTBEAT", "time": int}
    """

    def __init__(
        self,
        endpoint:    str,
        builders:    Dict[str, CandleBuilder],
        event_queue: queue.Queue,
    ) -> None:
        self._endpoint = endpoint
        self._builders = builders
        self._q        = event_queue
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Wire each builder's on_close callback → event queue
        for sym in list(builders.keys()):
            def _make_cb(s: str) -> Callable:
                def _cb(candle) -> None:
                    self._q.put({"type": "CANDLE_CLOSED", "candle": candle})
                return _cb
            builders[sym]._on_close = _make_cb(sym)

    def start(self) -> None:
        """Start the subscriber thread.  Safe to call multiple times — restarts if stopped."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="zmq-feed",
            daemon=True,
        )
        self._thread.start()
        logger.info("ZmqFeed: thread started, connecting to %s", self._endpoint)

    def stop(self) -> None:
        """Signal the thread to stop and wait up to 5 s for it to exit."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("ZmqFeed: stopped")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Private ────────────────────────────────────────────────────────

    def _run(self) -> None:
        """
        Main subscriber loop.  Runs until stop() is called or an unrecoverable
        ZMQ context error occurs.  On recoverable errors (socket error while
        the stop flag is not set) it logs, sleeps briefly, and reconnects.
        """
        ctx = zmq.Context.instance()

        while not self._stop.is_set():
            sock = ctx.socket(zmq.SUB)
            sock.setsockopt(zmq.RCVTIMEO, _RECV_TIMEOUT_MS)
            # LINGER=0: don't block on close if there are pending messages
            sock.setsockopt(zmq.LINGER, 0)
            # Reconnect interval: ZMQ will silently reconnect after EA restart
            sock.setsockopt(zmq.RECONNECT_IVL,     500)   # 500 ms initial
            sock.setsockopt(zmq.RECONNECT_IVL_MAX, 5000)  # 5 s max back-off
            sock.connect(self._endpoint)
            sock.setsockopt_string(zmq.SUBSCRIBE, "")  # subscribe to all topics

            logger.info("ZmqFeed: connected to %s", self._endpoint)

            try:
                while not self._stop.is_set():
                    try:
                        raw = sock.recv_string()
                    except zmq.Again:
                        # Timeout — no message arrived; loop back to check stop flag
                        continue
                    except zmq.ZMQError as exc:
                        logger.warning("ZmqFeed: recv error: %s — reconnecting", exc)
                        break   # exit inner loop → reconnect outer loop

                    self._dispatch(raw)

            finally:
                try:
                    sock.close()
                except Exception:
                    pass

            if not self._stop.is_set():
                logger.info(
                    "ZmqFeed: socket closed — reconnecting in %.1fs", _RECONNECT_DELAY_S
                )
                time.sleep(_RECONNECT_DELAY_S)

        self._q.put(FEED_STOPPED)
        logger.info("ZmqFeed: thread exiting cleanly")

    def _dispatch(self, raw: str) -> None:
        """Parse a raw EA message and route it to builders / event queue."""
        space = raw.find(" ")
        if space < 0:
            logger.warning("ZmqFeed: malformed message (no space): %r", raw[:80])
            return

        topic   = raw[:space]
        payload = raw[space + 1:]

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("ZmqFeed: bad JSON for topic %s: %r", topic, payload[:80])
            return

        if topic == "TICK":
            self._handle_tick(data)
        elif topic == "BAR_CLOSE":
            self._handle_bar_close(data)
        elif topic == "BAR_OPEN":
            self._q.put({"type": "BAR_OPEN",  **data})
        elif topic == "TRADE":
            self._q.put({"type": "TRADE",     **data})
        elif topic == "HEARTBEAT":
            self._q.put({"type": "HEARTBEAT", **data})
        else:
            logger.debug("ZmqFeed: unknown topic %s", topic)

    def _handle_tick(self, data: dict) -> None:
        sym = data.get("sym", "")
        bid = data.get("bid", 0.0)
        ask = data.get("ask", 0.0)
        t   = data.get("time", 0)

        builder = self._builders.get(sym)
        if builder:
            builder.on_tick(bid=bid, ask=ask, tick_time=t)

        self._q.put({"type": "TICK", "sym": sym, "bid": bid, "ask": ask, "time": t})

    def _handle_bar_close(self, data: dict) -> None:
        sym  = data.get("sym", "")
        tf_s = data.get("tf", "")
        tf   = _TF_MAP.get(tf_s)

        if tf is None:
            logger.debug("ZmqFeed: BAR_CLOSE unknown tf %s — ignored", tf_s)
            return

        builder = self._builders.get(sym)
        if builder:
            builder.on_bar_close_from_ea(
                timeframe=tf,
                bar_time= data["time"],
                open_p=   data["open"],
                high_p=   data["high"],
                low_p=    data["low"],
                close_p=  data["close"],
                volume=   int(data.get("vol", 0)),
            )

        self._q.put({"type": "BAR_CLOSE", **data})
