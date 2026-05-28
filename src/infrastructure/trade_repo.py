"""
trade_repo.py — MT5 Trade Execution  (v3 — partial close + modify_sl)
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src import config

from src.domain.entities import Direction, Trade, TradeStatus
from src.domain.repositories import ITradeRepository
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway

logger = logging.getLogger(__name__)


class MT5TradeRepository(ITradeRepository):

    def __init__(self, gateway: MT5Gateway) -> None:
        self._gw = gateway

    def place_trade(self, trade: Trade) -> Optional[int]:
        order = {
            "symbol":  trade.symbol,
            "type":    "BUY" if trade.direction == Direction.BULLISH else "SELL",
            "volume":  round(trade.lot_size, 2),
            "price":   trade.entry_price,
            "sl":      trade.stop_loss,
            "tp":      trade.take_profit,
            "comment": f"ZoneBot-{trade.id[:8]}",
        }
        result = self._gw.place_order(order)
        if result is None:
            logger.error("place_trade failed for %s", trade.id)
            return None
        # Update trade with actual MT5 fill price (may differ from signal price
        # due to slippage within the 20-pip deviation band).
        actual_fill = result.get("price", trade.entry_price)
        if actual_fill and actual_fill != trade.entry_price:
            logger.info("Fill slippage | %s | signal=%.5f actual=%.5f (%.1f pts)",
                        trade.symbol, trade.entry_price, actual_fill,
                        abs(actual_fill - trade.entry_price) / (10 ** -5))
            trade.entry_price = actual_fill
        logger.info("Trade placed ✓ ticket=%s %s %s vol=%.2f fill=%.5f",
                    result["ticket"], trade.symbol, order["type"], order["volume"],
                    actual_fill)
        return result["ticket"]

    def close_trade(self, ticket: int) -> bool:
        result = self._gw.close_position(ticket)
        if result is None:
            logger.error("close_trade failed ticket=%s", ticket)
            return False
        logger.info("Trade closed ✓ ticket=%s @ %s", ticket, result.get("price"))
        return True

    def partial_close_trade(self, ticket: int, volume: float, comment: str = "ZoneBot TP") -> bool:
        """
        Close `volume` lots of the position at market price.

        The MT5 gateway sends a TRADE_ACTION_DEAL in the opposite direction
        with the specified volume against the open position.  MT5 matches
        it as a partial close, leaving (original_volume - volume) lots open.

        If volume >= full position size, falls back to full close.
        Pass ``comment`` to label the close in the MT5 trade history correctly
        (e.g. "ZoneBot TP1" vs "ZoneBot TP2").
        """
        result = self._gw.partial_close_position(ticket, volume, comment=comment)
        if result is None:
            logger.error("partial_close failed ticket=%s vol=%.2f", ticket, volume)
            return False
        logger.info(
            "Partial close ✓ ticket=%s | closed=%.2f lots @ %.5f",
            ticket, result.get("closed_volume", volume), result.get("price", 0),
        )
        return True

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        """Move SL to new_sl, preserving the current TP."""
        pos = self._gw.get_position_by_ticket(ticket)
        if pos is None:
            logger.error("modify_sl: position %s not found", ticket)
            return False
        result = self._gw.modify_position(ticket, sl=new_sl, tp=pos["tp"])
        if result is None:
            logger.error("modify_sl failed ticket=%s new_sl=%.5f", ticket, new_sl)
            return False
        logger.info("SL trailed ✓ ticket=%s | %.5f → %.5f",
                    ticket, pos["sl"], new_sl)
        return True

    def get_open_positions(self) -> List[Trade]:
        raw = self._gw.get_open_positions()
        trades = []
        for p in raw:
            direction = Direction.BULLISH if p["type"] == "BUY" else Direction.BEARISH
            open_dt   = datetime.fromtimestamp(p["open_time"], tz=timezone.utc) \
                        - timedelta(hours=getattr(config, "BROKER_UTC_OFFSET_HOURS", 0))
            trades.append(Trade(
                id=          str(p["ticket"]),
                symbol=      p["symbol"],
                direction=   direction,
                entry_price= p["open_price"],
                stop_loss=   p["sl"],
                take_profit= p["tp"],
                lot_size=    p["volume"],
                status=      TradeStatus.OPEN,
                created_at=  open_dt,
                mt5_ticket=  p["ticket"],
            ))
        return trades

    def get_remaining_volume(self, ticket: int) -> Optional[float]:
        pos = self._gw.get_position_by_ticket(ticket)
        if pos is None:
            return None
        return float(pos["volume"])

    def get_account_balance(self) -> float:
        info = self._gw.get_account_info()
        return info["balance"] if info else 0.0

    def get_free_margin(self) -> float:
        info = self._gw.get_account_info()
        return info["free_margin"] if info else 0.0

    def get_required_margin(self, trade: Trade) -> Optional[float]:
        order = {
            "symbol": trade.symbol,
            "type":   "BUY" if trade.direction == Direction.BULLISH else "SELL",
            "volume": round(trade.lot_size, 2),
            "price":  trade.entry_price,
            "sl":     trade.stop_loss,
            "tp":     trade.take_profit,
        }
        return self._gw.check_order_margin(order)