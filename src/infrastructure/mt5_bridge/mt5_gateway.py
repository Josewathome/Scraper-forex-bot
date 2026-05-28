"""
MT5 Gateway — Windows Side ONLY.
Pure MT5 API wrapper. Returns plain dicts. Zero business logic.
"""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

import os
_mt5_host = os.environ.get("MT5_HOST", "localhost")
_mt5_port = int(os.environ.get("MT5_PORT", 18812))
mt5 = None
_use_bridge = False
MT5_AVAILABLE = False
try:
    if os.name == "nt":
        import MetaTrader5 as mt5
        _use_bridge = False
    else:
        from mt5linux import MetaTrader5 as _MT5Bridge
        _use_bridge = True
        mt5 = None

    MT5_AVAILABLE = True

except ImportError as e:
    print("IMPORT ERROR:", e)
    MT5_AVAILABLE = False

def _mt5_log_has_invalid_account() -> bool:
    """
    Read the current MT5 terminal log and return True if the broker returned
    'Invalid account' for this session.  The log is UTF-16LE under Wine.
    Returns False if the log cannot be read (safe default — keep retrying).
    """
    import glob as _glob
    import datetime as _dt
    log_dir = r"C:\Program Files\MetaTrader 5\logs"
    today   = _dt.date.today().strftime("%Y%m%d")
    pattern = os.path.join(log_dir, f"{today}.log")
    matches = _glob.glob(pattern)
    if not matches:
        return False
    try:
        data = open(matches[0], "rb").read().decode("utf-16-le", errors="replace")
        return "invalid account" in data.lower()
    except Exception:
        return False


class MT5Gateway:
    """
    Thin, stateless wrapper over the MetaTrader5 Python package.
    All methods return plain dicts / lists / primitives (JSON-safe).
    Includes auto-reconnect on every public call.
    """

    def __init__(
        self,
        login:    int,
        password: str,
        server:   str,
        mt5_path: str = "",
    ) -> None:
        if not MT5_AVAILABLE:
            raise RuntimeError("MT5 not available (import failed). Check mt5linux or MetaTrader5 installation.")
        self.login    = login
        self.password = password
        self.server   = server
        self.mt5_path = mt5_path
        self._ready   = False
        self.TIMEFRAME_MAP = {}

    # ── Lifecycle ─────────────────────────────────────────────────

    def connect(self, retries: int = 3, delay: float = 5.0) -> bool:
        """
        Attach to the already-running MT5 terminal without disturbing its
        broker session.  Strategy:
          1. Call mt5.initialize() with NO credentials — this just opens the
             IPC pipe to the terminal process without touching the broker login.
          2. Check account_info() — if the terminal is already logged in and
             connected, we are done.
          3. Only if the terminal is NOT logged in yet, call mt5.login() once
             to send credentials to the broker.

        Passing login/password/server to mt5.initialize() on an already-logged-in
        terminal forces MT5 to re-authenticate, which kicks the existing session
        and causes the broker to return 'Invalid account' on the reconnect.
        That is the root cause of the -6 loop observed in production.
        """
        global mt5

        if _use_bridge and mt5 is None:
            from mt5linux import MetaTrader5 as _MT5Bridge
            mt5 = _MT5Bridge(host=_mt5_host, port=_mt5_port)

        import time as _time

        for attempt in range(1, retries + 1):
            logger.info("MT5 connect attempt %d/%d …", attempt, retries)

            # Step 1: attach to terminal IPC only — no credentials
            init_kwargs: dict = {"timeout": 60000}
            if self.mt5_path:
                init_kwargs["path"] = self.mt5_path

            if not mt5.initialize(**init_kwargs):
                code, msg = mt5.last_error()
                logger.error("mt5.initialize() failed: (%s, %s)", code, msg)
                if code == -10005:
                    logger.error("Terminal not running — waiting for MT5 to start.")
                if attempt < retries:
                    logger.info("Retrying in %.0fs …", delay)
                    _time.sleep(delay)
                continue

            # Step 2: check if terminal already has an active broker session
            acc = mt5.account_info()
            if acc is not None and acc.login == self.login:
                logger.info("Terminal already logged in as %s — skipping mt5.login().", acc.login)
            else:
                # Step 3: terminal is not logged in — send credentials to broker
                if acc is not None:
                    logger.info(
                        "Terminal logged in as %s but expected %s — calling mt5.login().",
                        acc.login, self.login,
                    )
                else:
                    logger.info("Terminal not logged in — calling mt5.login().")

                login_result = mt5.login(
                    login=self.login,
                    password=self.password,
                    server=self.server,
                )
                if not login_result:
                    code, msg = mt5.last_error()
                    logger.error(
                        "mt5.login() failed (attempt %d/%d): (%s, %s) — "
                        "MT5 may still be connecting to broker, will retry.",
                        attempt, retries, code, msg,
                    )
                    mt5.shutdown()
                    if attempt < retries:
                        logger.info("Retrying in %.0fs …", delay)
                        _time.sleep(delay)
                    continue
                logger.info("mt5.login() succeeded — broker session established.")

                acc = mt5.account_info()
                if acc is None:
                    logger.error("login() OK but account_info() None: %s", mt5.last_error())
                    mt5.shutdown()
                    if attempt < retries:
                        _time.sleep(delay)
                    continue

            self._ready = True

            self.TIMEFRAME_MAP = {
                "M1":  mt5.TIMEFRAME_M1,
                "M5":  mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1":  mt5.TIMEFRAME_H1,
                "H4":  mt5.TIMEFRAME_H4,
                "D1":  mt5.TIMEFRAME_D1,
            }
            logger.info(
                "MT5 connected ✓  account=%s  name=%s  balance=%.2f %s  server=%s",
                acc.login, acc.name, acc.balance, acc.currency, acc.server,
            )
            return True

        logger.critical("All %d MT5 connection attempts failed.", retries)
        return False

    def disconnect(self) -> None:
        mt5.shutdown()
        self._ready = False
        logger.info("MT5 disconnected.")

    def is_ready(self) -> bool:
        if not self._ready:
            return False
        return mt5.account_info() is not None

    def _ensure_ready(self) -> bool:
        """Auto-reconnect if MT5 dropped. Called before every data request."""
        if self._ready and mt5.account_info() is not None:
            return True
        logger.warning("MT5 not ready — attempting reconnect …")
        self._ready = False
        # Use 3 retries on mid-session drops — IC Markets access server pool
        # rotation means the first reconnect node may reject; retry gives a
        # chance to land on a working node.
        return self.connect(retries=3, delay=5.0)

    # ── Account ───────────────────────────────────────────────────

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "login":       int(info.login),
            "balance":     float(info.balance),
            "equity":      float(info.equity),
            "margin":      float(info.margin),
            "free_margin": float(info.margin_free),
            "currency":    str(info.currency),
            "leverage":    int(info.leverage),
            "server":      str(info.server),
            "name":        str(info.name),
        }

    # ── Market Data ───────────────────────────────────────────────

    def get_candles(
        self, symbol: str, timeframe: str, count: int
    ) -> Optional[List[Dict[str, Any]]]:
        if not self._ensure_ready():
            return None
        tf = self.TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error("Unknown timeframe or MT5 not initialized: %s", timeframe)
            return None
        # Ensure symbol is in Market Watch
        if not mt5.symbol_select(symbol, True):
            logger.error("Failed to select symbol %s", symbol)
            return None
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.error("copy_rates_from_pos empty %s/%s: %s", symbol, timeframe, mt5.last_error())
            return None
        result = []
        for r in rates:
            # prefer real_volume; fall back to tick_volume
            vol = float(r["real_volume"]) if r["real_volume"] > 0 else float(r["tick_volume"])
            result.append({
                "time":   int(r["time"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": vol,
            })
        return result

    def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("symbol_info(%s) None: %s", symbol, mt5.last_error())
            return None
        return {
            "symbol":              str(info.name),
            "digits":              int(info.digits),
            "point":               float(info.point),
            "trade_tick_value":    float(info.trade_tick_value),
            "trade_tick_size":     float(info.trade_tick_size),
            "trade_contract_size": float(info.trade_contract_size),
            "spread":              int(info.spread),
            "volume_min":          float(info.volume_min),
            "volume_max":          float(info.volume_max),
            "volume_step":         float(info.volume_step),
        }

    def get_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        # Ensure symbol is subscribed in Market Watch so MT5 streams live ticks.
        # symbol_select() can have a short latency before the first tick arrives,
        # so retry once after a brief wait if the first call returns None.
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid":  float(tick.bid),
            "ask":  float(tick.ask),
            "last": float(tick.last),
            "time": int(tick.time),
        }

    def get_server_time(self) -> Optional[int]:
        if not self._ensure_ready():
            return None
        tick = mt5.symbol_info_tick("EURUSD")
        return int(tick.time) if tick else None

    def detect_utc_offset(self) -> int:
        """
        Detect the broker server's UTC offset by comparing the latest
        EURUSD tick time (broker-local, encoded as epoch seconds) against
        real wall-clock UTC.  Returns whole hours, e.g. 2 for UTC+2.

        Falls back to 0 when no recent tick is available (weekend or
        market closed), so callers should keep the config default as a
        manual fallback for those cases.
        """
        import time as _time
        if not self._ensure_ready():
            return 0
        tick = mt5.symbol_info_tick("EURUSD")
        if tick is None:
            logger.warning("detect_utc_offset: no EURUSD tick — defaulting to 0")
            return 0
        server_ts   = int(tick.time)
        real_utc    = int(_time.time())
        age_seconds = real_utc - server_ts
        if age_seconds > 600:
            # Tick older than 10 min → market closed, timestamp stale and unreliable
            logger.warning(
                "detect_utc_offset: EURUSD tick is %ds old (market closed?) "
                "— defaulting to 0.", age_seconds,
            )
            return 0
        offset = round((server_ts - real_utc) / 3600)
        offset = max(-12, min(14, offset))   # clamp to valid timezone range
        logger.info(
            "Broker UTC offset detected: UTC%+d  "
            "(server_ts=%d  real_utc=%d  age=%ds)",
            offset, server_ts, real_utc, age_seconds,
        )
        return offset

    # ── Order Execution ───────────────────────────────────────────

    def place_order(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        order_type = mt5.ORDER_TYPE_BUY if order["type"] == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       str(order["symbol"]),
            "volume":       float(order["volume"]),
            "type":         order_type,
            "price":        float(order["price"]),
            "sl":           float(order["sl"]),
            "tp":           float(order["tp"]),
            "deviation":    int(order.get("deviation", 20)),
            "magic":        int(order.get("magic", 202400)),
            "comment":      str(order.get("comment", "ZoneBot")),
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("order_send failed: retcode=%s request=%s", 
                         result.retcode if result else "None", request)
            return None
        return {
            "ticket":  int(result.order),
            "retcode": int(result.retcode),
            "price":   float(result.price),
            "volume":  float(result.volume),
        }

    def close_position(self, ticket: int) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error("No open position with ticket %s", ticket)
            return None
        pos = positions[0]
        close_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick        = mt5.symbol_info_tick(pos.symbol)
        close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    20,
            "magic":        pos.magic,
            "comment":      "ZoneBot Close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("close_position failed ticket=%s: %s", ticket, result)
            return None
        return {"ticket": int(ticket), "retcode": int(result.retcode), "price": float(close_price)}
    
    def partial_close_position(self, ticket: int, volume: float, comment: str = "ZoneBot TP"):
        """
        Close `volume` lots of an open position (partial close).
    
        MT5 handles partial closes via a TRADE_ACTION_DEAL order in the
        opposite direction with the specific volume.  MT5 matches the incoming
        order against the existing position and leaves the remainder open.
    
        If volume >= position size, delegates to close_position() (full close).
    
        Returns:
            dict with {ticket, closed_volume, price}  on success
            None                                       on failure
        """
        if not self._ensure_ready():
            return None
    
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error("partial_close: no position found for ticket %s", ticket)
            return None
    
        pos = positions[0]
    
        # If we're asked to close more than we have, do a full close
        volume = round(volume, 2)
        if volume >= pos.volume:
            return self.close_position(ticket)
    
        close_type  = (mt5.ORDER_TYPE_SELL
                    if pos.type == mt5.ORDER_TYPE_BUY
                    else mt5.ORDER_TYPE_BUY)
        tick        = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error("partial_close: no tick for %s", pos.symbol)
            return None
        close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       float(volume),
            "type":         close_type,
            "position":     int(ticket),
            "price":        float(close_price),
            "deviation":    20,
            "magic":        int(pos.magic),
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "partial_close failed ticket=%s vol=%.2f retcode=%s",
                ticket, volume,
                result.retcode if result else "None",
            )
            return None
    
        logger.info(
            "Partial close ✓ ticket=%s | closed=%.2f lots | remaining=%.2f | price=%.5f",
            ticket, volume, pos.volume - volume, close_price,
        )
        return {
            "ticket":         int(ticket),
            "closed_volume":  float(volume),
            "remaining":      float(round(pos.volume - volume, 2)),
            "price":          float(close_price),
            "retcode":        int(result.retcode),
        }




    def check_order_margin(self, order: Dict[str, Any]) -> Optional[float]:
        """
        Ask MT5 how much margin a potential order would consume, without
        actually placing it.  Returns margin in account currency, or None
        if MT5 cannot compute it (symbol not available, market closed, etc.).
        """
        if not self._ensure_ready():
            return None
        order_type = mt5.ORDER_TYPE_BUY if order["type"] == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": str(order["symbol"]),
            "volume": float(order["volume"]),
            "type":   order_type,
            "price":  float(order["price"]),
            "sl":     float(order.get("sl", 0.0)),
            "tp":     float(order.get("tp", 0.0)),
        }
        result = mt5.order_check(request)
        if result is None:
            logger.warning("order_check returned None for %s", order["symbol"])
            return None
        # retcode 0 = OK; anything else means MT5 flagged a problem
        if result.retcode != 0:
            logger.warning("order_check retcode=%s (%s) for %s",
                           result.retcode, result.comment, order["symbol"])
            return None
        return float(result.margin)

    def modify_position(self, ticket: int, sl: float, tp: float) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.error("modify_position: no open position found for ticket=%s", ticket)
            return None
        symbol = positions[0].symbol
        result = mt5.order_send({
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   symbol,
            "position": ticket,
            "sl":       float(sl),
            "tp":       float(tp),
        })
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("modify_position failed ticket=%s: %s", ticket, result)
            return None
        return {"ticket": int(ticket), "retcode": int(result.retcode)}

    def get_position_close_deal(self, ticket: int) -> Optional[Dict[str, Any]]:
        """
        Look up the deal that closed the given position in MT5 history.
        Returns the deal's profit, price, and volume; or None if not found.
        Use this for accurate PnL on natural SL/TP closes rather than estimating.
        """
        if not self._ensure_ready():
            return None
        deals = mt5.history_deals_get(position=ticket)
        if deals is None or len(deals) == 0:
            return None
        # The closing deal has DEAL_ENTRY_OUT (1); the opening has DEAL_ENTRY_IN (0).
        for d in reversed(deals):
            if d.entry == mt5.DEAL_ENTRY_OUT:
                return {
                    "ticket":  int(d.ticket),
                    "profit":  float(d.profit),
                    "price":   float(d.price),
                    "volume":  float(d.volume),
                    "time":    int(d.time),
                }
        return None

    # ── Positions ─────────────────────────────────────────────────

    def get_open_positions(self) -> List[Dict[str, Any]]:
        if not self._ensure_ready():
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":     int(p.ticket),
                "symbol":     str(p.symbol),
                "type":       "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume":     float(p.volume),
                "open_price": float(p.price_open),
                "sl":         float(p.sl),
                "tp":         float(p.tp),
                "profit":     float(p.profit),
                "swap":       float(p.swap),
                "open_time":  int(p.time),
                "magic":      int(p.magic),
                "comment":    str(p.comment),
            }
            for p in positions
        ]

    def get_position_by_ticket(self, ticket: int) -> Optional[Dict[str, Any]]:
        if not self._ensure_ready():
            return None
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return None
        p = positions[0]
        return {
            "ticket":     int(p.ticket),
            "symbol":     str(p.symbol),
            "type":       "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume":     float(p.volume),
            "open_price": float(p.price_open),
            "sl":         float(p.sl),
            "tp":         float(p.tp),
            "profit":     float(p.profit),
            "open_time":  int(p.time),
        }
    
    def get_candles_range(
        self,
        symbol:    str,
        timeframe: str,
        start_dt:  datetime,
        end_dt:    datetime,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch all candles between two UTC datetimes. No bar-count cap."""
        if not self._ensure_ready():
            return None
        tf = self.TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error("Unknown timeframe or MT5 not initialized: %s", timeframe)
            return None
        if not mt5.symbol_select(symbol, True):
            logger.error("Failed to select symbol %s", symbol)
            return None

        # MT5 Python <= 5.0.36 rejects timezone-aware datetimes with
        # error -2 "Invalid params".  Strip tzinfo (datetimes are already UTC).
        if start_dt.tzinfo is not None:
            start_dt = start_dt.replace(tzinfo=None)
        if end_dt.tzinfo is not None:
            end_dt = end_dt.replace(tzinfo=None)

        # Prime MT5's history cache with a copy_rates_from_pos call before
        # the range query.  MT5 under Wine with a fresh session often hasn't
        # loaded historical M5 data into memory yet — copy_rates_from_pos
        # forces a cache load so the subsequent copy_rates_range succeeds.
        mt5.copy_rates_from_pos(symbol, tf, 0, 10)

        rates = None
        for attempt, delay in enumerate([0, 3, 8, 15]):
            if delay:
                logger.info(
                    "copy_rates_range %s/%s: retrying in %ds (attempt %d/4) ...",
                    symbol, timeframe, delay, attempt + 1,
                )
                time.sleep(delay)
            rates = mt5.copy_rates_range(symbol, tf, start_dt, end_dt)
            if rates is not None and len(rates) > 0:
                break
            err = mt5.last_error()
            if attempt < 3:
                logger.warning(
                    "copy_rates_range empty %s/%s: %s — will retry", symbol, timeframe, err,
                )
        if rates is None or len(rates) == 0:
            logger.error("copy_rates_range empty %s/%s: %s", symbol, timeframe, mt5.last_error())
            return None

        result = []
        for r in rates:
            vol = float(r["real_volume"]) if r["real_volume"] > 0 else float(r["tick_volume"])
            result.append({
                "time":   int(r["time"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": vol,
            })
        return result