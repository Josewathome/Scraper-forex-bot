"""
entry_gate.py — Phase 4: Entry & Exit Gate System.

Converts a StrategySignal (from Phase 1+2) into an EntryCandidate that
ExecutionService.execute_entry_candidate() can act on directly.

Gate chain (all must pass):
  1. News proximity     — no high-impact event within ±15 min
  2. Session filter     — London 06:00–09:00 UTC, NY 13:00–16:00 UTC
  3. Daily trade limit  — config.MAX_DAILY_STRATEGY_TRADES (default 5)
  4. Daily drawdown     — account equity hasn't hit −2% for the day
  5. Open position cap  — no more than MAX_OPEN_TRADES open
  6. Symbol cap         — no more than MAX_TRADES_PER_SYMBOL for this symbol
  7. Minimum SL         — SL distance must be at least MIN_SL_PIPS
  8. Minimum RR         — TP must be at least MIN_RR × SL distance

Entry logic:
  SL  = last swing structure level (from StrategySignal.last_swing_support/resistance)
        with EXTRA_PIPS_SL buffer added
  TP1 = entry ± (SL distance × TP1_RR_RATIO)   (default 1.5)
  TP2 = entry ± (SL distance × TP2_RR_RATIO)   (default 2.5)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional

import src.config as config
from src.domain.entities import Direction, SignalType, Timeframe, TradeSignal
from src.domain.value_objects import BrokerCost, PipCalculator
from src.strategy.strategy_manager import StrategySignal

if TYPE_CHECKING:
    from src.application.news_manager import NewsManager
    from src.infrastructure.stream.candle_builder import CandleBuilder

logger = logging.getLogger(__name__)


# ── Gate block reasons ─────────────────────────────────────────────────────────

class GateBlockReason:
    NEWS          = "news_proximity"
    SESSION       = "outside_session_window"
    DAILY_LIMIT   = "daily_trade_limit_reached"
    DRAWDOWN      = "daily_drawdown_limit_hit"
    OPEN_CAP      = "max_open_trades_reached"
    SYMBOL_CAP    = "max_trades_for_symbol_reached"
    NO_SWING_REF  = "no_swing_reference_for_sl"
    SL_TOO_SMALL  = "sl_distance_below_minimum"
    RR_TOO_LOW    = "rr_below_minimum"


# ── Configurable thresholds (read from config with sensible defaults) ──────────

_DEFAULT_TP1_RR   = 1.5
_DEFAULT_TP2_RR   = 2.5
_DEFAULT_MIN_SL   = 3.0    # pips
_NEWS_BLOCK_SECS  = 15 * 60

# London: 06:00–09:00 UTC,  NY: 13:00–16:00 UTC
_SESSION_WINDOWS = [
    (6,  9),   # London Open
    (13, 16),  # NY Open
]


# ── EntryGate ─────────────────────────────────────────────────────────────────

class EntryGate:
    """
    Converts validated StrategySignals into EntryCandidate objects.

    Usage (in main_stream.py):
        gate = EntryGate(news_manager, execution)
        candidate = gate.evaluate(signal, builder, current_price, now)
        if candidate:
            trade = execution.execute_entry_candidate(candidate)
    """

    def __init__(
        self,
        news_manager,          # NewsManager
        execution,             # ExecutionService (for open position counts)
    ) -> None:
        self._news    = news_manager
        self._exec    = execution
        self._daily_counts: Dict[str, int] = {}   # date_str → count
        self._session_start_equity: Optional[float] = None

    # ── Public API ─────────────────────────────────────────────────────

    def on_new_day(self, balance: float) -> None:
        """Call once at the start of each trading day (or at startup)."""
        self._daily_counts.clear()
        self._session_start_equity = balance

    def record_trade(self) -> None:
        """Call after a strategy-gated trade is placed."""
        today = datetime.now(tz=timezone.utc).date().isoformat()
        self._daily_counts[today] = self._daily_counts.get(today, 0) + 1

    def evaluate(
        self,
        signal:        StrategySignal,
        builder:       "CandleBuilder",
        current_price: float,
        now:           datetime,
        balance:       float,
        pip_calc:      PipCalculator,
        tick_value:    float,
        digits:        int,
        h1_atr:        float,
        broker_cost:   BrokerCost,
    ) -> Optional[object]:
        """
        Run all gates.  Returns an EntryCandidate if all pass, else None.

        The returned object is passed directly to
        ExecutionService.execute_entry_candidate().
        """
        sym = signal.symbol

        # ── Gate 1: News proximity ─────────────────────────────────────
        if self._is_news_blocked(sym, now):
            logger.debug("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NEWS)
            return None

        # ── Gate 2: Session window ─────────────────────────────────────
        if not self._in_session(now):
            logger.debug("ENTRY_GATE_BLOCK [%s] %s @ %s UTC", sym, GateBlockReason.SESSION, now.strftime("%H:%M"))
            return None

        # ── Gate 3: Daily trade limit ──────────────────────────────────
        max_daily = getattr(config, "MAX_DAILY_STRATEGY_TRADES", 5)
        today = now.date().isoformat()
        if self._daily_counts.get(today, 0) >= max_daily:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s (%d/%d)", sym, GateBlockReason.DAILY_LIMIT,
                         self._daily_counts.get(today, 0), max_daily)
            return None

        # ── Gate 4: Daily drawdown ─────────────────────────────────────
        drawdown_limit = getattr(config, "DAILY_DRAWDOWN_LIMIT_PCT", 2.0)
        if (self._session_start_equity is not None and
                self._session_start_equity > 0 and balance < self._session_start_equity):
            dd_pct = (self._session_start_equity - balance) / self._session_start_equity * 100
            if dd_pct >= drawdown_limit:
                logger.info("ENTRY_GATE_BLOCK [%s] %s: dd=%.2f%% >= %.2f%%",
                            sym, GateBlockReason.DRAWDOWN, dd_pct, drawdown_limit)
                return None

        # ── Gate 5: Open position cap ──────────────────────────────────
        max_open = getattr(config, "MAX_OPEN_TRADES", 6)
        try:
            open_count = len(self._exec._tr.get_open_positions())
        except Exception:
            open_count = 0
        if open_count >= max_open:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s (%d)", sym, GateBlockReason.OPEN_CAP, open_count)
            return None

        # ── Gate 6: Per-symbol cap ─────────────────────────────────────
        sym_cap = getattr(config, "MAX_TRADES_PER_SYMBOL", 1)
        try:
            sym_open = self._exec.open_count_for_symbol(sym)
        except Exception:
            sym_open = 0
        if sym_open >= sym_cap:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s (%d)", sym, GateBlockReason.SYMBOL_CAP, sym_open)
            return None

        # ── Compute SL level from swing structure ──────────────────────
        sl_price = self._compute_sl(signal, current_price, pip_calc)
        if sl_price is None:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NO_SWING_REF)
            return None

        sl_dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price))

        # ── Gate 7: Minimum SL distance ───────────────────────────────
        _min_sl_cfg = getattr(config, "MIN_SL_PIPS", _DEFAULT_MIN_SL)
        if isinstance(_min_sl_cfg, dict):
            min_sl = _min_sl_cfg.get(sym, getattr(config, "MIN_SL_PIPS_DEFAULT", _DEFAULT_MIN_SL))
        else:
            min_sl = float(_min_sl_cfg)
        if sl_dist_pips < min_sl:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s: sl=%.1f < %.1f pips",
                         sym, GateBlockReason.SL_TOO_SMALL, sl_dist_pips, min_sl)
            return None

        # ── Compute TP levels ─────────────────────────────────────────
        sl_price_dist = abs(current_price - sl_price)
        tp1_rr = getattr(config, "TP1_RR_RATIO", _DEFAULT_TP1_RR)
        tp2_rr = getattr(config, "TP2_RR_RATIO", _DEFAULT_TP2_RR)

        if signal.direction == Direction.BULLISH:
            tp1_price = current_price + sl_price_dist * tp1_rr
            tp2_price = current_price + sl_price_dist * tp2_rr
        else:
            tp1_price = current_price - sl_price_dist * tp1_rr
            tp2_price = current_price - sl_price_dist * tp2_rr

        # ── Gate 8: Minimum R:R ───────────────────────────────────────
        min_rr = getattr(config, "MIN_RR", 1.5)
        achieved_rr = tp1_rr  # TP1 defines the minimum RR
        if achieved_rr < min_rr:
            logger.debug("ENTRY_GATE_BLOCK [%s] %s: rr=%.2f < %.2f",
                         sym, GateBlockReason.RR_TOO_LOW, achieved_rr, min_rr)
            return None

        # ── Build TradeSignal ─────────────────────────────────────────
        trade_signal = TradeSignal(
            symbol=      sym,
            direction=   signal.direction,
            entry_price= current_price,
            stop_loss=   sl_price,
            take_profit= tp1_price,   # TP1 as primary target
            signal_type= SignalType.PREDICTIVE,
            trade_score= int(signal.confidence * 100),
            timeframe=   Timeframe.M1,
        )

        # ── Build setup_score / grade from confidence ──────────────────
        confidence = signal.confidence
        setup_score = int(confidence * 100)
        if confidence >= 0.85:
            grade = "A"
        elif confidence >= 0.75:
            grade = "B"
        else:
            grade = "C"

        # ── Build EntryCandidate ──────────────────────────────────────
        try:
            from src.application.execution_service import EntryCandidate
        except ImportError:
            logger.error("Could not import EntryCandidate — execution_service not available")
            return None

        candidate = EntryCandidate(
            symbol=      sym,
            signal=      trade_signal,
            zone=        None,          # strategy engine doesn't use zone-based SL
            setup_score= setup_score,
            trade_score= setup_score,
            grade=       grade,
            risk_pct=    config.RISK_PERCENT,
            tp1_price=   tp1_price,
            tp2_price=   tp2_price,
            broker_cost= broker_cost,
            pip_calc=    pip_calc,
            tick_value=  tick_value,
            digits=      digits,
            h1_atr=      h1_atr,
            is_phase2=   False,
        )

        logger.info(
            "ENTRY_GATE_PASS [%s] %s | conf=%.2f | grade=%s | "
            "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f | sl=%.1f pips | rr=%.1f",
            sym, signal.direction.value.upper(), confidence, grade,
            current_price, sl_price, tp1_price, tp2_price, sl_dist_pips, tp1_rr,
        )
        return candidate

    # ── Private helpers ────────────────────────────────────────────────

    def _is_news_blocked(self, symbol: str, now: datetime) -> bool:
        try:
            return self._news.is_blocked(now, symbol)
        except Exception:
            return False

    @staticmethod
    def _in_session(now: datetime) -> bool:
        h = now.hour
        for start, end in _SESSION_WINDOWS:
            if start <= h < end:
                return True
        return False

    @staticmethod
    def _compute_sl(
        signal:        StrategySignal,
        current_price: float,
        pip_calc:      PipCalculator,
    ) -> Optional[float]:
        """
        Compute the stop-loss price from swing structure.

        For BULLISH: SL = last_swing_support − EXTRA_PIPS_SL buffer
        For BEARISH: SL = last_swing_resistance + EXTRA_PIPS_SL buffer

        Falls back to ATR-based SL if no swing reference is available.
        """
        _extra = getattr(config, "EXTRA_PIPS_SL", {})
        if isinstance(_extra, dict):
            extra_pips = _extra.get(signal.symbol, getattr(config, "EXTRA_PIPS_SL_DEFAULT", 3.0))
        else:
            extra_pips = float(_extra)
        buffer = pip_calc.pips_to_price(extra_pips)

        if signal.direction == Direction.BULLISH:
            ref = signal.last_swing_support
            if ref is not None:
                return ref - buffer
        else:
            ref = signal.last_swing_resistance
            if ref is not None:
                return ref + buffer

        # No swing reference — no trade (avoids arbitrary SL placement)
        return None
