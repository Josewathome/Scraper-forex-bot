"""
trade_journal.py — Persistent trade record.

Every opened/closed trade is appended here so the dashboard can
show live analytics without querying MT5 history.

Storage: .cache/trade_journal.json
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(".cache") / "trade_journal.json"


class TradeJournal:

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = Path(path) if path else _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trades: List[Dict[str, Any]] = self._load()

    # ── Write ────────────────────────────────────────────────────────

    _MAX_CLOSED = 500   # keep last N closed trades; all OPEN trades are always kept

    def record_open(
        self,
        ticket:     int,
        symbol:     str,
        direction:  str,
        entry:      float,
        sl:         float,
        tp:         float,
        lot_size:   float,
        grade:      str = "?",
    ) -> None:
        record = {
            "ticket":    ticket,
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "lot_size":  lot_size,
            "grade":     grade,
            "pnl":       None,
            "outcome":   "OPEN",
            "opened_at": datetime.now(tz=timezone.utc).isoformat(),
            "closed_at": None,
        }
        self._trades.append(record)
        self._save()
        logger.debug("Journal: recorded OPEN ticket=%s %s %s", ticket, symbol, direction)

    def record_close(
        self,
        ticket:  int,
        pnl:     float,
        outcome: str,   # WIN_FULL | WIN_PARTIAL | LOSS | MONITOR_CLOSE
    ) -> None:
        for t in reversed(self._trades):
            if t["ticket"] == ticket and t["outcome"] == "OPEN":
                t["pnl"]       = round(pnl, 2)
                t["outcome"]   = outcome
                t["closed_at"] = datetime.now(tz=timezone.utc).isoformat()
                self._save()
                logger.debug("Journal: closed ticket=%s outcome=%s pnl=%.2f", ticket, outcome, pnl)
                return
        logger.warning("Journal: no open record found for ticket=%s", ticket)

    # ── Read ─────────────────────────────────────────────────────────

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self._trades)

    def get_recent(self, n: int = 50) -> List[Dict[str, Any]]:
        return self._trades[-n:]

    def get_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        return [t for t in self._trades if t.get("symbol") == symbol]

    def get_stats(
        self,
        trades: Optional[List[Dict[str, Any]]] = None,
        days:   Optional[int] = None,
        since:  Optional[str] = None,   # ISO timestamp — only count trades closed after this
    ) -> Dict[str, Any]:
        if trades is None:
            closed = [t for t in self._trades if t["outcome"] != "OPEN"]
            if since is not None:
                closed = [t for t in closed if (t.get("closed_at") or "") >= since]
            elif days is not None:
                cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
                closed = [t for t in closed if (t.get("closed_at") or "") >= cutoff]
        else:
            closed = [t for t in trades if t["outcome"] != "OPEN"]

        if not closed:
            return {"total": 0, "wins": 0, "losses": 0,
                    "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        wins      = [t for t in closed if t["outcome"] in ("WIN_FULL", "WIN_PARTIAL")]
        losses    = [t for t in closed if t["outcome"] == "LOSS"]
        total_pnl = sum(t["pnl"] or 0 for t in closed)
        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  round(len(wins) / len(closed) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl":   round(total_pnl / len(closed), 2),
        }

    def get_drawdown(
        self,
        initial_balance: float,
        days:  Optional[int] = None,
        since: Optional[str] = None,   # ISO timestamp — only measure drawdown after this
    ) -> float:
        """Return max drawdown % for the given period (all-time if days/since are None)."""
        all_closed = sorted(
            [t for t in self._trades if t["outcome"] != "OPEN"],
            key=lambda t: t.get("closed_at") or "",
        )
        if since is not None:
            # since takes priority over days when both are supplied
            period = [t for t in all_closed if (t.get("closed_at") or "") >= since]
            equity = initial_balance
        elif days is not None:
            cutoff     = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
            pre_trades = [t for t in all_closed if (t.get("closed_at") or "") < cutoff]
            period     = [t for t in all_closed if (t.get("closed_at") or "") >= cutoff]
            equity     = initial_balance + sum(t["pnl"] or 0 for t in pre_trades)
        else:
            period = all_closed
            equity = initial_balance

        peak   = equity
        max_dd = 0.0
        for t in period:
            equity += t["pnl"] or 0
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        return round(max_dd, 2)

    def get_stats_by_symbol(
        self,
        days:  Optional[int] = None,
        since: Optional[str] = None,   # ISO timestamp — only count trades closed after this
    ) -> Dict[str, Dict[str, Any]]:
        all_closed = [t for t in self._trades if t["outcome"] != "OPEN"]
        if since is not None:
            all_closed = [t for t in all_closed if (t.get("closed_at") or "") >= since]
        elif days is not None:
            cutoff     = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
            all_closed = [t for t in all_closed if (t.get("closed_at") or "") >= cutoff]
        symbols = sorted({t["symbol"] for t in all_closed})
        return {
            sym: self.get_stats([t for t in all_closed if t["symbol"] == sym])
            for sym in symbols
        }

    # ── Persistence ──────────────────────────────────────────────────

    def _trim_closed(self) -> None:
        """Keep all OPEN trades + last _MAX_CLOSED closed trades."""
        open_trades   = [t for t in self._trades if t["outcome"] == "OPEN"]
        closed_trades = [t for t in self._trades if t["outcome"] != "OPEN"]
        if len(closed_trades) > self._MAX_CLOSED:
            closed_trades = closed_trades[-self._MAX_CLOSED:]
            self._trades = open_trades + closed_trades

    def _save(self) -> None:
        try:
            self._trim_closed()
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._trades, fh, indent=2, default=str)
            tmp.replace(self.path)   # atomic on Windows; rename() can fail cross-drive
        except Exception as exc:
            logger.error("TradeJournal save failed: %s", exc)

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("TradeJournal load failed (starting fresh): %s", exc)
            return []