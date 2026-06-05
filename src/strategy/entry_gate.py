"""
entry_gate.py — Entry & Exit Gate System (M1 Scalper).

Converts a StrategySignal into an EntryCandidate that
ExecutionService.execute_entry_candidate() can act on directly.

Gate chain (all must pass):
  1. News proximity     — no high-impact event within ±15 min
  2. Session filter     — per-symbol trading windows (UTC hours)
  3. Tick velocity      — minimum ticks/sec to confirm live market
  4. Daily trade limit  — SCALPER_MAX_DAILY_TRADES per calendar day
  5. Daily drawdown     — account equity hasn't dropped beyond limit
  6. Open position cap  — SCALPER_MAX_OPEN_TRADES globally
  7. Symbol cap         — MAX_TRADES_PER_SYMBOL per symbol
  8. Minimum SL         — SL distance must be at least SCALPER_MIN_SL_PIPS
  9. Minimum RR         — TP must be at least MIN_RR × SL distance

Per-symbol session windows (config.SCALPER_SYMBOL_SESSIONS):
  GBPUSD  06-16 UTC   London + NY
  XAUUSD  06-20 UTC   London + NY + EU afterhours
  USDJPY  00-16 UTC   Asian + London + NY
  AUDUSD  00-12 UTC   Asian + London
  USDCHF  06-16 UTC   London + NY
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional

import uuid

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
    TICK_VELOCITY = "tick_velocity_too_low"
    DAILY_LIMIT   = "daily_trade_limit_reached"
    DRAWDOWN      = "daily_drawdown_limit_hit"
    OPEN_CAP      = "max_open_trades_reached"
    SYMBOL_CAP    = "max_trades_for_symbol_reached"
    NO_SWING_REF  = "no_swing_reference_for_sl"
    SL_TOO_SMALL  = "sl_distance_below_minimum"
    RR_TOO_LOW    = "rr_below_minimum"


# ── Configurable thresholds ────────────────────────────────────────────────────

_DEFAULT_TP1_RR  = 1.5
_DEFAULT_TP2_RR  = 2.5
_DEFAULT_MIN_SL  = 3.0    # pips
_NEWS_BLOCK_SECS = 15 * 60


def _symbol_session(symbol: str) -> tuple:
    """
    Return the (start_hour_utc, end_hour_utc) trading window for a symbol.

    Reads from config.SCALPER_SYMBOL_SESSIONS first (per-symbol).
    Falls back to the legacy flat SCALPER_SESSION_START/END_UTC if the
    symbol is not in the per-symbol map.
    """
    sessions = getattr(config, "SCALPER_SYMBOL_SESSIONS", {})
    if symbol in sessions:
        return sessions[symbol]
    # Fallback: legacy flat window
    return (
        getattr(config, "SCALPER_SESSION_START_UTC", 6),
        getattr(config, "SCALPER_SESSION_END_UTC", 16),
    )


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
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NEWS)
            return None

        # ── Gate 2: Per-symbol session window ─────────────────────────
        if not self._in_session(sym, now):
            start, end = _symbol_session(sym)
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s | %s UTC (window %02d:00–%02d:00 UTC)",
                sym, GateBlockReason.SESSION, now.strftime("%H:%M"), start, end,
            )
            return None

        # ── Gate 3: Tick velocity — confirm live market activity ───────
        min_velocity  = getattr(config, "SCALPER_MIN_TICK_VELOCITY", 1.5)
        live_velocity = signal.tick_analysis.velocity
        vel_passed    = live_velocity >= min_velocity

        # Generate a stable eval_id that will link this evaluation to the
        # trade outcome when ExecutionService closes the position.
        eval_id = str(uuid.uuid4())

        # Record to tick analytics (always — both blocked and passed)
        try:
            from src.application.tick_analytics import get_analytics
            get_analytics().record_evaluation(
                eval_id=eval_id,
                symbol=sym,
                velocity=live_velocity,
                threshold=min_velocity,
                spread_pips=broker_cost.spread_pips,
                passed_velocity=vel_passed,
                gate_block_reason="velocity" if not vel_passed else "passed",
                now=now,
            )
        except Exception as _exc:
            logger.debug("TickAnalytics record_evaluation failed: %s", _exc)

        if not vel_passed:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s | velocity=%.2f ticks/s < %.2f — market too thin",
                sym, GateBlockReason.TICK_VELOCITY, live_velocity, min_velocity,
            )
            return None

        # Store eval_id on the signal so execute_entry_candidate can link the outcome
        signal._eval_id = eval_id

        # ── Gate 4 (renumbered): Daily trade limit ─────────────────────
        max_daily = getattr(config, "SCALPER_MAX_DAILY_TRADES", 20)
        today = now.date().isoformat()
        if self._daily_counts.get(today, 0) >= max_daily:
            logger.info("ENTRY_GATE_BLOCK [%s] %s (%d/%d)", sym, GateBlockReason.DAILY_LIMIT,
                         self._daily_counts.get(today, 0), max_daily)
            return None

        # ── Gate 5: Daily drawdown ─────────────────────────────────────
        drawdown_limit = getattr(config, "DAILY_DRAWDOWN_LIMIT_PCT", 2.0)
        if (self._session_start_equity is not None and
                self._session_start_equity > 0 and balance < self._session_start_equity):
            dd_pct = (self._session_start_equity - balance) / self._session_start_equity * 100
            if dd_pct >= drawdown_limit:
                logger.info("ENTRY_GATE_BLOCK [%s] %s: dd=%.2f%% >= %.2f%%",
                            sym, GateBlockReason.DRAWDOWN, dd_pct, drawdown_limit)
                return None

        # ── Gate 6: Open position cap ──────────────────────────────────
        max_open = getattr(config, "SCALPER_MAX_OPEN_TRADES", 3)
        try:
            open_count = len(self._exec._tr.get_open_positions())
        except Exception:
            open_count = 0
        if open_count >= max_open:
            logger.info("ENTRY_GATE_BLOCK [%s] %s (%d)", sym, GateBlockReason.OPEN_CAP, open_count)
            return None

        # ── Gate 7: Per-symbol cap ─────────────────────────────────────
        sym_cap = getattr(config, "MAX_TRADES_PER_SYMBOL", 1)
        try:
            sym_open = self._exec.open_count_for_symbol(sym)
        except Exception:
            sym_open = 0
        if sym_open >= sym_cap:
            logger.info("ENTRY_GATE_BLOCK [%s] %s (%d)", sym, GateBlockReason.SYMBOL_CAP, sym_open)
            return None

        # ── Compute SL level from swing structure ──────────────────────
        # Compute M5 ATR for scalper SL fallback
        _m5_atr = 0.0
        if getattr(config, "SCALPER_MODE", False):
            try:
                _m5_candles = builder.get_closed_candles(
                    __import__('src.domain.entities', fromlist=['Timeframe']).Timeframe.M5, 10
                )
                if _m5_candles and len(_m5_candles) >= 2:
                    _trs = [max(c.high - c.low, abs(c.high - _m5_candles[i-1].close),
                                abs(c.low - _m5_candles[i-1].close))
                            for i, c in enumerate(_m5_candles) if i > 0]
                    _m5_atr = sum(_trs) / len(_trs) if _trs else 0.0
            except Exception:
                _m5_atr = 0.0

        sl_price = self._compute_sl(signal, current_price, pip_calc, m5_atr=_m5_atr)
        if sl_price is None:
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NO_SWING_REF)
            return None

        sl_dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price))

        # ── Gate 7: Minimum SL distance ───────────────────────────────
        _min_sl_cfg = getattr(config, "MIN_SL_PIPS", _DEFAULT_MIN_SL)
        if isinstance(_min_sl_cfg, dict):
            min_sl = _min_sl_cfg.get(sym, getattr(config, "MIN_SL_PIPS_DEFAULT", _DEFAULT_MIN_SL))
        else:
            min_sl = float(_min_sl_cfg)
        if sl_dist_pips < min_sl:
            logger.info("ENTRY_GATE_BLOCK [%s] %s: sl=%.1f < %.1f pips",
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
            logger.info("ENTRY_GATE_BLOCK [%s] %s: rr=%.2f < %.2f",
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
            is_phase2=   False,
        )

        logger.info(
            "ENTRY_GATE_PASS [%s] %s | conf=%.2f | grade=%s | "
            "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f | sl=%.1f pips | rr=%.1f",
            sym, signal.direction.value.upper(), confidence, grade,
            current_price, sl_price, tp1_price, tp2_price, sl_dist_pips, tp1_rr,
        )

        # Tag the candidate with eval_id so ExecutionService can report outcome
        candidate._eval_id = eval_id

        return candidate

    # ── Private helpers ────────────────────────────────────────────────

    def _is_news_blocked(self, symbol: str, now: datetime) -> bool:
        try:
            return self._news.is_blocked(now, symbol)
        except Exception:
            return False

    @staticmethod
    def _in_session(symbol: str, now: datetime) -> bool:
        """
        Check whether the current UTC hour falls within the trading window
        for this specific symbol.  Windows wrap midnight for symbols that
        start at 00:00 UTC (USDJPY, AUDUSD).
        """
        start, end = _symbol_session(symbol)
        h = now.hour
        if start <= end:
            # Normal window e.g. 06–16: start <= h < end
            return start <= h < end
        else:
            # Wrapping window e.g. 22–06: h >= start OR h < end
            return h >= start or h < end

    @staticmethod
    def _compute_sl(
        signal:        StrategySignal,
        current_price: float,
        pip_calc:      PipCalculator,
        m5_atr:        float = 0.0,
    ) -> Optional[float]:
        """
        Compute the stop-loss price from swing structure.

        For BULLISH: SL = last_swing_support − buffer
        For BEARISH: SL = last_swing_resistance + buffer

        Scalper fallback: if no swing reference within SCALPER_MAX_SL_PIPS,
        use ATR × SCALPER_ATR_SL_FRACTION as SL distance.
        """
        _extra = getattr(config, "EXTRA_PIPS_SL", {})
        if isinstance(_extra, dict):
            extra_pips = _extra.get(signal.symbol, getattr(config, "EXTRA_PIPS_SL_DEFAULT", 3.0))
        else:
            extra_pips = float(_extra)
        buffer = pip_calc.pips_to_price(extra_pips)

        sl_price = None
        if signal.direction == Direction.BULLISH:
            ref = signal.last_swing_support
            if ref is not None:
                sl_price = ref - buffer
        else:
            ref = signal.last_swing_resistance
            if ref is not None:
                sl_price = ref + buffer

        # Validate SL distance (scalper ATR-based)
        _min_sl = getattr(config, "SCALPER_MIN_SL_PIPS", {})
        _max_sl = getattr(config, "SCALPER_MAX_SL_PIPS", {})
        min_pips = _min_sl.get(signal.symbol, getattr(config, "SCALPER_MIN_SL_PIPS_DEFAULT", 3.0)) if isinstance(_min_sl, dict) else float(_min_sl)
        max_pips = _max_sl.get(signal.symbol, getattr(config, "SCALPER_MAX_SL_PIPS_DEFAULT", 8.0)) if isinstance(_max_sl, dict) else float(_max_sl)

        if sl_price is not None:
            dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price))
            if dist_pips > max_pips:
                sl_price = None   # swing too far — discard, use ATR fallback

        # ATR fallback: use fraction of M5 ATR
        if sl_price is None and m5_atr > 0:
            atr_frac = getattr(config, "SCALPER_ATR_SL_FRACTION", 0.4)
            sl_dist  = m5_atr * atr_frac
            sl_dist_pips = pip_calc.price_to_pips(sl_dist)
            # Clamp to [min_pips, max_pips]
            sl_dist_pips = max(min_pips, min(sl_dist_pips, max_pips))
            sl_dist = pip_calc.pips_to_price(sl_dist_pips)
            if signal.direction == Direction.BULLISH:
                sl_price = current_price - sl_dist
            else:
                sl_price = current_price + sl_dist

        return sl_price
