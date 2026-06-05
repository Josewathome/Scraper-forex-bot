"""
zmq_feed.py — TCP Server Feed (EA connects here as TCP client).

WHY TCP instead of ZMQ
ZoneBotBridge v3 uses MT5's built-in SocketCreate/SocketConnect/SocketSend
instead of #import "libzmq.dll".  MT5 resets DLL import permissions on every
clean exit, so the permission dialog reappeared on every container restart and
OnInit() never completed.  Built-in sockets have no permission dialog at all.

The EA is a TCP CLIENT; this Python process is the TCP SERVER.
Message format is unchanged: newline-delimited "TOPIC {JSON}" strings.
The ZmqFeed class name and public API are preserved so main_stream.py is
unchanged.
"""
from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
from typing import Callable, Dict, Optional

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

FEED_STOPPED = object()

_ACCEPT_TIMEOUT_S = 1.0
_RECV_TIMEOUT_S   = 1.0


def _parse_port(endpoint: str) -> int:
    """Extract port from 'tcp://host:port'."""
    s = endpoint
    if s.startswith("tcp://"):
        s = s[6:]
    _, _, port_str = s.partition(":")
    return int(port_str or 5556)


class ZmqFeed:
    """
    TCP server that receives newline-delimited messages from the MT5 EA.

    Drop-in replacement for the previous ZMQ-based implementation.
    Same public API: start() / stop() / is_alive / FEED_STOPPED sentinel.
    """

    def __init__(
        self,
        endpoint:    str,
        builders:    Dict[str, CandleBuilder],
        event_queue: queue.Queue,
    ) -> None:
        self._port    = _parse_port(endpoint)
        self._builders = builders
        self._q        = event_queue
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None

        for sym in list(builders.keys()):
            def _make_cb(s: str) -> Callable:
                def _cb(candle) -> None:
                    self._q.put({"type": "CANDLE_CLOSED", "candle": candle})
                return _cb
            builders[sym]._on_close = _make_cb(sym)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tcp-feed", daemon=True)
        self._thread.start()
        logger.info("TcpFeed: listening on 0.0.0.0:%d for EA connection", self._port)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("TcpFeed: stopped")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self._port))
        except OSError as exc:
            logger.error("TcpFeed: bind 0.0.0.0:%d failed: %s", self._port, exc)
            self._q.put(FEED_STOPPED)
            return
        srv.listen(1)
        srv.settimeout(_ACCEPT_TIMEOUT_S)
        logger.info("TcpFeed: server ready on port %d — waiting for EA", self._port)

        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.info("TcpFeed: EA connected from %s", addr)
            conn.settimeout(_RECV_TIMEOUT_S)
            self._handle_connection(conn)
            logger.info("TcpFeed: EA disconnected — waiting for reconnect")

        try:
            srv.close()
        except Exception:
            pass
        self._q.put(FEED_STOPPED)
        logger.info("TcpFeed: thread exiting cleanly")

    def _handle_connection(self, conn: socket.socket) -> None:
        buf = ""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._dispatch(line)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, raw: str) -> None:
        space = raw.find(" ")
        if space < 0:
            logger.warning("TcpFeed: malformed message: %r", raw[:80])
            return
        topic   = raw[:space]
        payload = raw[space + 1:]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("TcpFeed: bad JSON topic=%s: %r", topic, payload[:80])
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
            logger.debug("TcpFeed: unknown topic %s", topic)

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
            logger.debug("TcpFeed: BAR_CLOSE unknown tf %s", tf_s)
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
