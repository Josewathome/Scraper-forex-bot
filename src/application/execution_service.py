"""
execution_service.py — Entry execution + Trade Guardian (M1 Scalper mode).

Two monitoring entry-points:

  run_monitoring_only()     — called on every TICK.
                              Price-level checks: TP1/TP2 partial closes,
                              time-based exit, MFE tracking.

  run_trade_revaluation()   — called on every M1 close (after entry eval).
                              Structural SL trail, thesis re-evaluation,
                              reversal detection.
                              Asks at each bar: "If I had no position open,
                              would I still enter this exact trade?"

Trade Guardian decision matrix (continuation score 0.0–1.0):
  >= REEVAL_HOLD_THRESHOLD (0.55):       thesis intact — hold
  >= REEVAL_DEFENSIVE_THRESHOLD (0.40):  thesis weakening — defensive mode,
                                          tighten SL to nearest structure
  >= REEVAL_EXIT_THRESHOLD (0.25):        partial defensive exit if deep in profit
  <  REEVAL_EXIT_THRESHOLD:              thesis invalidated — close trade
  opposing signal conf >= REVERSAL_CONF: market reversed — close trade
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import src.config as config
from src.domain.entities import Direction, Timeframe, Trade, TradeSignal, TradeStatus, Candle
from src.domain.repositories import IMarketDataRepository, ITradeRepository
from src.domain.value_objects import BrokerCost, PipCalculator, RiskParameters
from src.application.news_manager import NewsManager
from src.infrastructure.spread_calculator import SpreadCalculator
from src.infrastructure.trade_journal import TradeJournal

if TYPE_CHECKING:
    from src.strategy.strategy_manager import StrategySignal
    from src.strategy.structure_state import StructureStateManager

logger = logging.getLogger(__name__)


# ── Per-trade runtime state ────────────────────────────────────────────────────

@dataclass
class _OpenTradeState:
    """Runtime state tracked per open trade ticket."""
    symbol:           str
    direction:        Direction
    entry:            float
    original_sl:      float
    current_sl:       float
    take_profit:      float
    tp1_price:        float
    tp2_price:        float
    tp1_hit:          bool  = False
    tp2_hit:          bool  = False
    lot_size:         float = 0.0
    initial_risk:     float = 0.0
    mfe_price:        float = 0.0
    accumulated_pips: float = 0.0
    digits:           int   = 5
    grade:            str   = "B"
    created_at:       datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    sl_trailed:       bool  = False
    recent_signals:   list  = field(default_factory=list)
    mfe_pips_at_signal: float = 0.0
    # Trade Guardian
    entry_confidence:   float = 0.0   # signal confidence at trade open
    last_reeval_score:  float = 1.0   # most recent continuation score
    reeval_count:       int   = 0     # number of M1-close re-evaluations
    defensive_mode:     bool  = False # True when thesis is weakening

    @property
    def entry_price(self) -> float:
        return self.entry

    @property
    def stop_loss(self) -> float:
        return self.current_sl

    @property
    def risk_reward(self) -> float:
        sl_dist = abs(self.entry - self.original_sl)
        tp_dist = abs(self.take_profit - self.entry)
        return tp_dist / sl_dist if sl_dist > 0 else 0.0

    @property
    def mid(self) -> float:
        return (self.original_sl + self.take_profit) / 2


# ── Entry candidate (produced by EntryGate) ───────────────────────────────────

@dataclass
class EntryCandidate:
    """All data needed to execute a trade, collected during evaluation."""
    symbol:      str
    signal:      TradeSignal
    setup_score: int
    trade_score: int
    grade:       str
    risk_pct:    float
    tp1_price:   float
    tp2_price:   float
    broker_cost: BrokerCost
    pip_calc:    PipCalculator
    tick_value:  float
    digits:      int
    is_phase2:   bool = False

    @property
    def risk_reward(self) -> float:
        sl_dist = abs(self.signal.entry_price - self.signal.stop_loss)
        tp_dist = abs(self.signal.take_profit - self.signal.entry_price)
        return tp_dist / sl_dist if sl_dist > 0 else 0.0


# ── ExecutionService ───────────────────────────────────────────────────────────

class ExecutionService:
    def __init__(
        self,
        market_data:       IMarketDataRepository,
        trade_repo:        ITradeRepository,
        news_manager:      NewsManager,
        spread_calculator: SpreadCalculator,
        symbols:           List[str],
        risk_pct:          float,
        commission:        float,
        min_rr:            float,
        account_currency:  str,
        journal:           TradeJournal,
        clock=             None,
    ) -> None:
        self._md         = market_data
        self._tr         = trade_repo
        self._news       = news_manager
        self._spread     = spread_calculator
        self._symbols    = symbols
        self._risk_pct   = risk_pct
        self._commission = commission
        self._min_rr     = min_rr
        self._currency   = account_currency
        self._journal    = journal
        self._clock      = clock
        self._trade_states: Dict[int, _OpenTradeState]          = {}
        self._loss_streak:  Dict[str, int]                      = {}
        self._streak_pause: Dict[str, Optional[datetime]]       = {}
        self._eval_ids:     Dict[int, str]                      = {}
        self._mfe_last_logged: Dict[int, datetime]              = {}

    # ── Helpers ───────────────────────────────────────────────────────

    def open_count_for_symbol(self, symbol: str) -> int:
        return sum(1 for p in self._tr.get_open_positions() if p.symbol == symbol)

    def has_tp1_hit_for_symbol(self, symbol: str) -> bool:
        return any(s.tp1_hit for s in self._trade_states.values() if s.symbol == symbol)

    # ── Public monitoring entry-points ────────────────────────────────

    def run_monitoring_only(self, symbol: str) -> None:
        """Tick-level monitoring: TP hits, time exit, MFE tracking."""
        now = datetime.now(tz=timezone.utc)
        self._prune_closed_positions()
        if getattr(config, "MONITOR_ENABLED", True):
            self._monitor_open_trades(symbol, now)

    def run_trade_revaluation(
        self,
        symbol:       str,
        fresh_signal: Optional["StrategySignal"],
        structure:    Optional["StructureStateManager"],
        m1_candles:   List[Candle],
        m5_candles:   List[Candle],
        now:          datetime,
    ) -> None:
        """
        M1-close re-evaluation: structural SL trail + thesis scoring + reversal.

        Called after strategy.evaluate() on every M1 close so the open-trade
        analysis always uses the same fresh market data as the entry evaluation.

        fresh_signal may be None when no entry signal was produced — absence of
        a signal is itself information (market is inconclusive) and contributes
        a neutral score to the continuation assessment.
        """
        if not getattr(config, "MONITOR_ENABLED", True):
            return

        open_positions = self._tr.get_open_positions()
        if not open_positions:
            return

        digits   = self._md.get_symbol_digits(symbol)
        pip_calc = PipCalculator(digits=digits)
        price    = self._md.get_current_price(symbol)

        HOLD_THRESHOLD      = getattr(config, "REEVAL_HOLD_THRESHOLD",      0.55)
        DEFENSIVE_THRESHOLD = getattr(config, "REEVAL_DEFENSIVE_THRESHOLD", 0.40)
        EXIT_THRESHOLD      = getattr(config, "REEVAL_EXIT_THRESHOLD",      0.25)
        REVERSAL_THRESHOLD  = getattr(config, "REEVAL_REVERSAL_CONF",       0.70)
        act                 = getattr(config, "MONITOR_ACT_ON_SIGNALS",     True)

        for pos in open_positions:
            if pos.symbol != symbol:
                continue

            ticket = int(pos.mt5_ticket or pos.id)
            state  = self._trade_states.get(ticket)
            if state is None:
                continue  # trade not yet tracked (opened outside bot) — skip

            trade_dir = state.direction
            sl_dist   = abs(state.entry - state.current_sl)
            if trade_dir == Direction.BULLISH:
                in_profit    = price > state.entry
                profit_ratio = (price - state.entry) / sl_dist if sl_dist > 0 else 0.0
            else:
                in_profit    = price < state.entry
                profit_ratio = (state.entry - price) / sl_dist if sl_dist > 0 else 0.0

            mfe_pips = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))

            # ── 1. Structural SL trail ─────────────────────────────────
            # Only trail when in profit — never risk-increase the stop.
            if in_profit and structure is not None:
                new_sl = self._compute_structural_sl(state, structure, price, pip_calc)
                if new_sl is not None:
                    if self._tr.modify_sl(ticket, new_sl):
                        logger.info(
                            "STRUCTURAL_TRAIL [%s] ticket=%s %s | SL %.5f → %.5f (swing-based)",
                            symbol, ticket, trade_dir.value.upper(),
                            state.current_sl, new_sl,
                        )
                        state.current_sl = new_sl
                        state.sl_trailed = True

            # ── 2. Thesis re-evaluation ────────────────────────────────
            if structure is None:
                # Cannot re-evaluate without structure data — skip but trail is done
                continue

            cont = self._assess_continuation(
                state, fresh_signal, structure, price, pip_calc, now
            )
            state.last_reeval_score = cont
            state.reeval_count     += 1

            logger.info(
                "REEVAL [%s] ticket=%s %s | cont=%.2f | in_profit=%s | pr=%+.2fR | "
                "mfe=%.1fpips | defensive=%s | #%d",
                symbol, ticket, trade_dir.value.upper(),
                cont, in_profit, profit_ratio, mfe_pips,
                state.defensive_mode, state.reeval_count,
            )

            # ── Decision matrix ────────────────────────────────────────

            if cont >= HOLD_THRESHOLD:
                # Thesis intact — clear defensive mode if previously set
                if state.defensive_mode:
                    logger.info(
                        "REEVAL_HOLD [%s] ticket=%s — thesis restored (%.2f ≥ %.2f)",
                        symbol, ticket, cont, HOLD_THRESHOLD,
                    )
                    state.defensive_mode = False

            elif cont >= DEFENSIVE_THRESHOLD:
                # Thesis weakening — protect profit, tighten SL
                if not state.defensive_mode:
                    logger.info(
                        "REEVAL_DEFENSIVE [%s] ticket=%s — cont=%.2f entering defensive mode",
                        symbol, ticket, cont,
                    )
                    state.defensive_mode = True

                if in_profit and act:
                    tight_sl = self._compute_structural_sl_tight(
                        state, structure, price, pip_calc
                    )
                    if tight_sl is not None and tight_sl != state.current_sl:
                        if self._tr.modify_sl(ticket, tight_sl):
                            logger.info(
                                "DEFENSIVE_SL [%s] ticket=%s | SL %.5f → %.5f (tightened)",
                                symbol, ticket, state.current_sl, tight_sl,
                            )
                            state.current_sl = tight_sl

            elif cont >= EXIT_THRESHOLD:
                # Thesis degraded — close losing trades outright; partial exit only when in profit
                if not in_profit and act:
                    live_pips  = (pip_calc.price_to_pips(price - state.entry)
                                  if trade_dir == Direction.BULLISH
                                  else pip_calc.price_to_pips(state.entry - price))
                    total_pips = state.accumulated_pips + live_pips
                    _outcome   = "win" if total_pips > 0 else "loss"
                    self._tr.close_trade(ticket)
                    self._trade_states.pop(ticket, None)
                    self._log_trade_summary(pos, state, pip_calc)
                    self._journal_close(ticket, total_pips, _outcome)
                    self._update_streak(symbol, total_pips)
                    logger.info(
                        "TRADE CLOSED action=reeval_losing_exit | %s ticket=%s | "
                        "cont=%.2f | pips=%.1f | held=%.1fmin",
                        symbol, ticket, cont, total_pips, duration_min,
                    )
                elif in_profit and profit_ratio >= 0.8 and not state.tp1_hit and act:
                    close_pct = 0.50
                    close_vol = max(
                        0.01, round(math.floor(state.lot_size * close_pct * 100) / 100, 2)
                    )
                    closed = (self._tr.partial_close_trade(ticket, close_vol, "DEFENSIVE_TP")
                              if hasattr(self._tr, "partial_close_trade") else False)
                    if closed:
                        live_pips = (pip_calc.price_to_pips(price - state.entry)
                                     if trade_dir == Direction.BULLISH
                                     else pip_calc.price_to_pips(state.entry - price))
                        state.lot_size         = max(0.0, state.lot_size - close_vol)
                        state.accumulated_pips += live_pips * close_pct
                        logger.info(
                            "DEFENSIVE_PARTIAL [%s] ticket=%s — took %.0f%% at %.1fpips "
                            "(cont=%.2f, pr=%.2fR)",
                            symbol, ticket, close_pct * 100, live_pips, cont, profit_ratio,
                        )

            else:
                # cont < EXIT_THRESHOLD — thesis invalidated, close the position
                if act:
                    live_pips  = (pip_calc.price_to_pips(price - state.entry)
                                  if trade_dir == Direction.BULLISH
                                  else pip_calc.price_to_pips(state.entry - price))
                    total_pips = state.accumulated_pips + live_pips
                    _outcome   = "win" if total_pips > 0 else "loss"
                    self._tr.close_trade(ticket)
                    self._trade_states.pop(ticket, None)
                    self._log_trade_summary(pos, state, pip_calc)
                    self._journal_close(ticket, total_pips, _outcome)
                    self._update_streak(symbol, total_pips)
                    logger.info(
                        "REEVAL_EXIT [%s] ticket=%s %s — thesis invalidated (cont=%.2f) | "
                        "pips=%.1f | reeval_count=%d",
                        symbol, ticket, trade_dir.value.upper(),
                        cont, total_pips, state.reeval_count,
                    )
                    continue  # trade closed, skip reversal check

            # ── 3. Reversal detection ──────────────────────────────────
            # A fresh signal in the OPPOSITE direction with high confidence
            # means the market has structurally reversed.  Close the trade;
            # the entry gate will open a new one in the next M1 if conditions hold.
            if (fresh_signal is not None
                    and fresh_signal.direction != trade_dir
                    and fresh_signal.confidence >= REVERSAL_THRESHOLD
                    and act):
                live_pips  = (pip_calc.price_to_pips(price - state.entry)
                              if trade_dir == Direction.BULLISH
                              else pip_calc.price_to_pips(state.entry - price))
                total_pips = state.accumulated_pips + live_pips
                _outcome   = "win" if total_pips > 0 else "loss"
                self._tr.close_trade(ticket)
                self._trade_states.pop(ticket, None)
                self._log_trade_summary(pos, state, pip_calc)
                self._journal_close(ticket, total_pips, _outcome)
                self._update_streak(symbol, total_pips)
                logger.info(
                    "REVERSAL_EXIT [%s] ticket=%s | opposing %s signal conf=%.2f ≥ %.2f "
                    "— closed pips=%.1f",
                    symbol, ticket,
                    fresh_signal.direction.value.upper(), fresh_signal.confidence,
                    REVERSAL_THRESHOLD, total_pips,
                )

    # ── Trade execution ───────────────────────────────────────────────

    def execute_entry_candidate(self, candidate: EntryCandidate) -> Optional[Trade]:
        """Build, margin-check, and place the trade for a pre-evaluated candidate."""
        symbol = candidate.symbol
        signal = candidate.signal
        trade  = self._build_trade(
            signal, candidate.broker_cost, candidate.pip_calc,
            candidate.tick_value, candidate.risk_pct,
        )
        if trade is None:
            return None

        # Exact margin check using live MT5 figures
        req_margin  = (self._tr.get_required_margin(trade)
                       if hasattr(self._tr, "get_required_margin") else None)
        free_margin = self._tr.get_free_margin()
        safety      = getattr(config, "MARGIN_SAFETY_FACTOR", 1.5)
        if req_margin is not None and free_margin is not None:
            if req_margin * safety > free_margin:
                logger.warning(
                    "MARGIN SKIP | %s | required=%.2f × safety=%.1f = %.2f > free=%.2f",
                    symbol, req_margin, safety, req_margin * safety, free_margin,
                )
                return None
            logger.debug(
                "MARGIN OK | %s | required=%.2f free=%.2f (safety=%.1fx)",
                symbol, req_margin, free_margin, safety,
            )
        else:
            logger.debug("MARGIN CHECK unavailable for %s — proceeding (MT5 enforces).", symbol)

        ticket = None
        try:
            ticket = self._tr.place_trade(trade)
        except Exception as exc:
            logger.error("Trade placement failed for %s: %s", symbol, exc)
            return None

        if ticket is None:
            return None

        trade.mt5_ticket = ticket
        trade.status     = TradeStatus.OPEN

        initial_risk = abs(signal.entry_price - signal.stop_loss)
        confidence   = float(candidate.trade_score) / 100.0
        state = _OpenTradeState(
            symbol=symbol, direction=signal.direction,
            entry=signal.entry_price, original_sl=signal.stop_loss,
            current_sl=signal.stop_loss, take_profit=signal.take_profit,
            tp1_price=candidate.tp1_price, tp2_price=candidate.tp2_price,
            lot_size=trade.lot_size, initial_risk=initial_risk,
            mfe_price=signal.entry_price,
            digits=candidate.digits, grade=candidate.grade,
            created_at=datetime.now(tz=timezone.utc),
            entry_confidence=confidence,
            last_reeval_score=1.0,
        )
        self._trade_states[ticket] = state

        eval_id = getattr(candidate, "_eval_id", None)
        if eval_id:
            self._eval_ids[ticket] = eval_id
            try:
                from src.application.tick_analytics import get_analytics
                get_analytics().record_trade_executed(eval_id)
            except Exception as _exc:
                logger.debug("TickAnalytics record_trade_executed failed: %s", _exc)

        try:
            self._journal.record_open(
                ticket=ticket,
                symbol=state.symbol,
                direction=state.direction.value if hasattr(state.direction, "value") else state.direction,
                entry=state.entry,
                sl=state.current_sl,
                tp=state.take_profit,
                lot_size=state.lot_size,
                grade=state.grade,
            )
        except Exception as exc:
            logger.warning("Journal record_open failed ticket=%s: %s", ticket, exc)

        phase_tag = "P2" if candidate.is_phase2 else "P1"
        logger.info(
            "TRADE %s | %s %s | score=%d | %.2f lots | entry=%.5f SL=%.5f TP=%.5f",
            phase_tag, symbol, signal.direction.value.upper(),
            candidate.trade_score, trade.lot_size,
            signal.entry_price, signal.stop_loss, signal.take_profit,
        )
        return trade

    # ── Tick-level monitoring ─────────────────────────────────────────

    def _monitor_open_trades(self, symbol: str, now: datetime) -> None:
        """
        Price-level checks on every tick: TP1/TP2 partial closes, time exit.
        Structural analysis is NOT done here (that's run_trade_revaluation).
        """
        open_positions = self._tr.get_open_positions()
        if not open_positions:
            return

        digits   = self._md.get_symbol_digits(symbol)
        pip_calc = PipCalculator(digits=digits)

        for pos in open_positions:
            if pos.symbol != symbol:
                continue

            ticket = int(pos.mt5_ticket or pos.id)
            state  = self._trade_states.get(ticket)
            if state is None:
                # Trade opened outside bot — reconstruct state for monitoring
                tp1_price = (pos.entry_price
                             + abs(pos.entry_price - pos.stop_loss)
                             * getattr(config, "TIERED_TP1_RATIO", 1.5)
                             * (1 if pos.direction == Direction.BULLISH else -1))
                tp2_price = (pos.entry_price
                             + abs(pos.entry_price - pos.stop_loss)
                             * getattr(config, "TIERED_TP2_RATIO", 2.0)
                             * (1 if pos.direction == Direction.BULLISH else -1))
                state = _OpenTradeState(
                    symbol=symbol, direction=pos.direction,
                    entry=pos.entry_price, original_sl=pos.stop_loss,
                    current_sl=pos.stop_loss, take_profit=pos.take_profit,
                    tp1_price=tp1_price, tp2_price=tp2_price,
                    lot_size=pos.lot_size, digits=digits,
                )
                self._trade_states[ticket] = state

            price = self._md.get_current_price(symbol)

            # Update MFE
            if state.direction == Direction.BULLISH:
                if price > state.mfe_price:
                    state.mfe_price = price
            else:
                if price < state.mfe_price or state.mfe_price == state.entry:
                    state.mfe_price = price

            mfe_pips  = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
            live_pips = (pip_calc.price_to_pips(price - state.entry)
                         if state.direction == Direction.BULLISH
                         else pip_calc.price_to_pips(state.entry - price))
            # Clamp MFE sign: for BULLISH mfe_price >= entry (or = entry at start),
            # for BEARISH mfe_price <= entry. Negative live_pips means trade is losing.
            if state.direction == Direction.BULLISH and state.mfe_price < state.entry:
                mfe_pips = -mfe_pips
            elif state.direction == Direction.BEARISH and state.mfe_price > state.entry:
                mfe_pips = -mfe_pips
            duration_min = (now - state.created_at).total_seconds() / 60

            _last_mfe_log = self._mfe_last_logged.get(ticket)
            if _last_mfe_log is None or (now - _last_mfe_log).total_seconds() >= 60:
                logger.info(
                    "MFE  [%s] ticket=%s %s | price=%.5f | live=%+.1fpips mfe=%.1fpips | "
                    "sl=%.5f tp1=%.5f tp2=%.5f | held=%.1fmin | cont=%.2f",
                    symbol, ticket, state.direction.value.upper(),
                    price, live_pips, mfe_pips,
                    state.current_sl, state.tp1_price, state.tp2_price,
                    duration_min, state.last_reeval_score,
                )
                self._mfe_last_logged[ticket] = now

            # ── TP1 partial close ──────────────────────────────────────
            if not state.tp1_hit:
                tp1_reached = (
                    (state.direction == Direction.BULLISH and price >= state.tp1_price) or
                    (state.direction == Direction.BEARISH and price <= state.tp1_price)
                )
                if tp1_reached:
                    close_pct = (
                        getattr(config, "TIERED_TP1_CLOSE_PCT_A_PLUS", 0.40)
                        if state.grade == "A+" else
                        getattr(config, "TIERED_TP1_CLOSE_PCT_B",      0.50)
                        if state.grade == "B" else
                        getattr(config, "TIERED_TP1_CLOSE_PCT",        0.60)
                    )
                    close_vol = max(
                        0.01, round(math.floor(state.lot_size * close_pct * 100) / 100, 2)
                    )
                    closed = (self._tr.partial_close_trade(ticket, close_vol, "TP1")
                              if hasattr(self._tr, "partial_close_trade") else False)
                    if closed:
                        state.lot_size         = max(0.0, state.lot_size - close_vol)
                        state.tp1_hit          = True
                        state.accumulated_pips += pip_calc.price_to_pips(
                            abs(state.tp1_price - state.entry)
                        )
                        # Move SL to profit-lock level (entry + fraction of initial risk)
                        profit_lock = state.initial_risk * getattr(config, "TP1_PROFIT_LOCK_R", 0.3)
                        lock_sl = (state.entry + profit_lock if state.direction == Direction.BULLISH
                                   else state.entry - profit_lock)
                        if self._tr.modify_sl(ticket, lock_sl):
                            state.current_sl = lock_sl
                        logger.info(
                            "TP1 hit | ticket=%s | closed %.2f lots | profit-lock SL → %.5f | "
                            "runner=%.2f lots",
                            ticket, close_vol, lock_sl, state.lot_size,
                        )

            # ── TP2 partial close ──────────────────────────────────────
            if state.tp1_hit and not state.tp2_hit:
                tp2_reached = (
                    (state.direction == Direction.BULLISH and price >= state.tp2_price) or
                    (state.direction == Direction.BEARISH and price <= state.tp2_price)
                )
                if tp2_reached:
                    close_pct = getattr(config, "TIERED_TP2_CLOSE_PCT", 0.25)
                    close_vol = max(
                        0.01, round(math.floor(state.lot_size * close_pct * 100) / 100, 2)
                    )
                    closed = (self._tr.partial_close_trade(ticket, close_vol, "TP2")
                              if hasattr(self._tr, "partial_close_trade") else False)
                    if closed:
                        state.lot_size         = max(0.0, state.lot_size - close_vol)
                        state.tp2_hit          = True
                        state.accumulated_pips += pip_calc.price_to_pips(
                            abs(state.tp2_price - state.entry)
                        )
                        logger.info(
                            "TP2 hit | ticket=%s | closed %.2f lots | runner=%.2f lots",
                            ticket, close_vol, state.lot_size,
                        )

            # ── Time exit (scalper hold limit) ─────────────────────────
            sl_dist      = abs(state.entry - state.current_sl)
            in_profit    = (price > state.entry if state.direction == Direction.BULLISH
                            else price < state.entry)
            profit_ratio = abs(price - state.entry) / sl_dist if sl_dist > 0 else 0.0
            duration_min = (now - state.created_at).total_seconds() / 60
            _max_dur     = getattr(config, "SCALPER_MAX_HOLD_MINUTES", 30)

            should_close = False
            action       = None
            if duration_min >= _max_dur:
                if in_profit:
                    should_close = True
                    action       = "time_exit_profit"
                elif profit_ratio > 0.5:  # deeper than -0.5R
                    should_close = True
                    action       = "time_exit_cutloss"

            if should_close and getattr(config, "MONITOR_ACT_ON_SIGNALS", True):
                final_pips = (pip_calc.price_to_pips(price - state.entry)
                              if state.direction == Direction.BULLISH
                              else pip_calc.price_to_pips(state.entry - price))
                total_pips = state.accumulated_pips + final_pips
                _outcome   = "win" if total_pips > 0 else "loss"
                self._tr.close_trade(ticket)
                self._log_trade_summary(pos, state, pip_calc)
                self._journal_close(ticket, total_pips, _outcome)
                self._update_streak(symbol, total_pips)
                logger.info(
                    "TRADE CLOSED action=%s | %s ticket=%s | pips=%.1f | held=%.1fmin",
                    action, symbol, ticket, total_pips, duration_min,
                )

    # ── Trade Guardian: analytical helpers ───────────────────────────

    def _assess_continuation(
        self,
        state:        _OpenTradeState,
        fresh_signal: Optional["StrategySignal"],
        structure:    "StructureStateManager",
        price:        float,
        pip_calc:     PipCalculator,
        now:          datetime,
    ) -> float:
        """
        Compute a continuation score 0.0–1.0 for an open trade.

        The score answers: "How strongly does the current market support
        keeping this position open right now?"

        Components (weighted sum):
          40% — M1 structural state in the trade direction
          35% — Fresh signal alignment (confirming or opposing)
          25% — Current P&L relative to risk (in-profit = benefit of doubt)

        The three components are deliberately independent so that no single
        factor can override the others.  A trade needs ALL three components
        to remain healthy to score above the HOLD threshold.
        """
        from src.strategy.structure_state import StructureState

        trade_dir = state.direction

        # ── Factor 1: M1 structure (40%) ──────────────────────────────
        # Reads the live BOS/CHoCH state updated each M1 close.
        struct_score = 0.50  # neutral default (not enough data yet)
        m1_state     = structure.get_state(Timeframe.M1)
        bos_dir      = structure.get_last_bos_direction(Timeframe.M1)

        if trade_dir == Direction.BULLISH:
            if   m1_state == StructureState.BULLISH_TREND:                                        struct_score = 1.00
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BULLISH:     struct_score = 0.80
            elif m1_state == StructureState.MITIGATION_ZONE:                                      struct_score = 0.65
            elif m1_state == StructureState.RANGING:                                              struct_score = 0.35
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BEARISH:     struct_score = 0.10
            elif m1_state == StructureState.BEARISH_TREND:                                        struct_score = 0.00
        else:  # BEARISH trade
            if   m1_state == StructureState.BEARISH_TREND:                                        struct_score = 1.00
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BEARISH:     struct_score = 0.80
            elif m1_state == StructureState.MITIGATION_ZONE:                                      struct_score = 0.65
            elif m1_state == StructureState.RANGING:                                              struct_score = 0.35
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BULLISH:     struct_score = 0.10
            elif m1_state == StructureState.BULLISH_TREND:                                        struct_score = 0.00

        # ── Factor 2: fresh signal alignment (35%) ────────────────────
        # fresh_signal=None means "no clear setup at this M1 close" — neutral (0.50).
        # Same-direction signal confirms thesis; opposing signal is a warning.
        signal_score = 0.50
        if fresh_signal is not None:
            if fresh_signal.direction == trade_dir:
                # Continuation signal — thesis confirmed by fresh analysis
                signal_score = max(0.55, fresh_signal.confidence)
            else:
                # Opposing signal — the stronger it is, the worse for us
                # conf=0.70 → score=max(0, 1.0-1.05)=0.0
                # conf=0.50 → score=max(0, 1.0-0.75)=0.25
                signal_score = max(0.0, 1.0 - fresh_signal.confidence * 1.5)

        # ── Factor 3: P&L vs risk (25%) ───────────────────────────────
        # Profitable trades earn additional holding latitude:
        #   -1R → 0.25,  0R → 0.40,  +0.5R → 0.48,  +1R → 0.55,  +2R → 0.70
        sl_dist = abs(state.entry - state.current_sl)
        if sl_dist > 0:
            profit_ratio = ((price - state.entry) / sl_dist if trade_dir == Direction.BULLISH
                            else (state.entry - price) / sl_dist)
        else:
            profit_ratio = 0.0

        pnl_score = min(0.75, max(0.0, 0.40 + profit_ratio * 0.15))

        continuation = (struct_score * 0.40 + signal_score * 0.35 + pnl_score * 0.25)
        return round(max(0.0, min(1.0, continuation)), 3)

    def _compute_structural_sl(
        self,
        state:     _OpenTradeState,
        structure: "StructureStateManager",
        price:     float,
        pip_calc:  PipCalculator,
    ) -> Optional[float]:
        """
        Compute where SL logically belongs given current market structure.

        Answers: "If I were placing the stop right now, where would I put it?"

        For BULLISH trades: behind the most recent confirmed M1 swing low.
        For BEARISH trades: above the most recent confirmed M1 swing high.

        Returns a new SL level only when ALL conditions are met:
          (a) It is tighter than the current SL (genuine risk improvement)
          (b) It is on the correct side of current price (valid stop placement)
          (c) It is at least 2 pips away from price (not at risk of instant trigger)

        Never moves SL against the trade direction.
        Never moves SL in the absence of a confirmed structural swing.
        """
        buffer_price   = pip_calc.pips_to_price(getattr(config, "TRAIL_SL_BUFFER_PIPS", 1.5))
        min_dist_price = pip_calc.pips_to_price(2.0)

        if state.direction == Direction.BULLISH:
            candidates = [
                sp.price - buffer_price
                for sp in structure.get_swing_lows(Timeframe.M1)
                if (sp.price - buffer_price > state.current_sl       # tighter
                    and sp.price < price                              # below current price
                    and (price - (sp.price - buffer_price)) >= min_dist_price)
            ]
            return max(candidates) if candidates else None

        else:
            candidates = [
                sp.price + buffer_price
                for sp in structure.get_swing_highs(Timeframe.M1)
                if (sp.price + buffer_price < state.current_sl       # tighter
                    and sp.price > price                              # above current price
                    and ((sp.price + buffer_price) - price) >= min_dist_price)
            ]
            return min(candidates) if candidates else None

    def _compute_structural_sl_tight(
        self,
        state:     _OpenTradeState,
        structure: "StructureStateManager",
        price:     float,
        pip_calc:  PipCalculator,
    ) -> Optional[float]:
        """
        Defensive-mode SL: tighten aggressively to the nearest structural level
        between current SL and current price.

        Used when the thesis is weakening but not yet invalidated.  Attempts to
        lock in as much profit as possible while remaining structurally valid.

        Falls back to breakeven + 1 pip when no swing point is available.
        Always requires at least 2 pips of breathing room from current price.
        """
        buffer_price   = pip_calc.pips_to_price(1.0)   # tighter buffer in defensive mode
        min_dist_price = pip_calc.pips_to_price(2.0)

        if state.direction == Direction.BULLISH:
            candidates = [
                sp.price - buffer_price
                for sp in structure.get_swing_lows(Timeframe.M1)
                if (sp.price - buffer_price > state.current_sl
                    and sp.price < price
                    and (price - (sp.price - buffer_price)) >= min_dist_price)
            ]
            if candidates:
                return max(candidates)
            # No swing available — fall back to breakeven + 1 pip
            be_sl = state.entry + buffer_price
            if be_sl > state.current_sl and (price - be_sl) >= min_dist_price:
                return be_sl

        else:
            candidates = [
                sp.price + buffer_price
                for sp in structure.get_swing_highs(Timeframe.M1)
                if (sp.price + buffer_price < state.current_sl
                    and sp.price > price
                    and ((sp.price + buffer_price) - price) >= min_dist_price)
            ]
            if candidates:
                return min(candidates)
            be_sl = state.entry - buffer_price
            if be_sl < state.current_sl and (be_sl - price) >= min_dist_price:
                return be_sl

        return None

    # ── Internal utilities ────────────────────────────────────────────

    def _log_trade_summary(self, trade, state: _OpenTradeState, pip_calc: PipCalculator) -> None:
        sl_pips       = pip_calc.price_to_pips(abs(state.initial_risk))
        mfe_pips      = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
        realized_pips = state.accumulated_pips
        efficiency    = realized_pips / mfe_pips if mfe_pips > 0 else 0
        logger.info(
            "TRADE SUMMARY | %s %s | entry_conf=%.2f | SL=%.1fpips | MFE=%.1fpips | "
            "PnL=%.1fpips | eff=%.0f%% | TP1=%s TP2=%s | reeval=%d | last_cont=%.2f",
            state.symbol, state.direction.value.upper(),
            state.entry_confidence, sl_pips, mfe_pips,
            realized_pips, efficiency * 100,
            state.tp1_hit, state.tp2_hit,
            state.reeval_count, state.last_reeval_score,
        )

    def _prune_closed_positions(self) -> None:
        """Remove tracking state for positions that MT5 has already closed."""
        open_tickets = {int(p.mt5_ticket or p.id) for p in self._tr.get_open_positions()}
        stale = [t for t in list(self._trade_states.keys()) if t not in open_tickets]
        for t in stale:
            state = self._trade_states.pop(t, None)
            if state is None:
                continue
            pip_calc       = PipCalculator(digits=state.digits)
            direction_sign = 1 if state.direction == Direction.BULLISH else -1
            # Use MFE price as a conservative proxy for the close price
            final_pips     = direction_sign * pip_calc.price_to_pips(
                abs(state.mfe_price - state.entry)
            )
            total_pips = state.accumulated_pips + final_pips
            _outcome   = "win" if total_pips > 0 else "loss"
            self._journal_close(t, total_pips, _outcome)
            self._update_streak(state.symbol, total_pips)

    def _update_streak(self, symbol: str, total_pips: float) -> None:
        """Track loss streak for analytics and alerting (no trading pause by default)."""
        now    = datetime.now(tz=timezone.utc)
        streak = self._loss_streak.get(symbol, 0)
        if total_pips < 0:
            streak += 1
            self._loss_streak[symbol] = streak
            max_streak = getattr(config, "MAX_CONSECUTIVE_LOSSES",  5)
            pause_h    = getattr(config, "LOSS_STREAK_PAUSE_HOURS", 0)
            if streak >= max_streak:
                if pause_h > 0:
                    self._streak_pause[symbol] = now + timedelta(hours=pause_h)
                    logger.warning(
                        "streak_pause | %s | %d consecutive losses — blocked until %s",
                        symbol, streak, self._streak_pause[symbol].isoformat(),
                    )
                else:
                    logger.warning(
                        "streak_alert | %s | %d consecutive losses — monitoring (no pause)",
                        symbol, streak,
                    )
        else:
            self._loss_streak[symbol] = 0
            self._streak_pause[symbol] = None

    def save_runtime_state(self) -> dict:
        states = {}
        for ticket, s in self._trade_states.items():
            states[str(ticket)] = {
                "symbol": s.symbol, "direction": s.direction.value,
                "entry": s.entry, "original_sl": s.original_sl,
                "current_sl": s.current_sl, "take_profit": s.take_profit,
                "tp1_price": s.tp1_price, "tp2_price": s.tp2_price,
                "tp1_hit": s.tp1_hit, "tp2_hit": s.tp2_hit,
                "lot_size": s.lot_size, "initial_risk": s.initial_risk,
                "mfe_price": s.mfe_price, "accumulated_pips": s.accumulated_pips,
                "digits": s.digits, "grade": s.grade,
                "created_at": s.created_at.isoformat(),
                "sl_trailed": s.sl_trailed,
                "entry_confidence":  s.entry_confidence,
                "last_reeval_score": s.last_reeval_score,
                "reeval_count":      s.reeval_count,
                "defensive_mode":    s.defensive_mode,
            }
        streaks = dict(self._loss_streak)
        pauses  = {sym: dt.isoformat() if dt else None
                   for sym, dt in self._streak_pause.items()}
        return {"trade_states": states, "loss_streak": streaks, "streak_pause": pauses}

    def load_runtime_state(self, data: dict) -> None:
        for ticket_str, s in data.get("trade_states", {}).items():
            try:
                ticket  = int(ticket_str)
                created = datetime.fromisoformat(s["created_at"])
                state   = _OpenTradeState(
                    symbol=s["symbol"], direction=Direction[s["direction"].upper()],
                    entry=float(s["entry"]), original_sl=float(s["original_sl"]),
                    current_sl=float(s["current_sl"]), take_profit=float(s["take_profit"]),
                    tp1_price=float(s["tp1_price"]), tp2_price=float(s["tp2_price"]),
                    tp1_hit=bool(s["tp1_hit"]), tp2_hit=bool(s["tp2_hit"]),
                    lot_size=float(s["lot_size"]), initial_risk=float(s["initial_risk"]),
                    mfe_price=float(s["mfe_price"]), accumulated_pips=float(s["accumulated_pips"]),
                    digits=int(s["digits"]), grade=s.get("grade", "B"),
                    created_at=created, sl_trailed=bool(s.get("sl_trailed", False)),
                    entry_confidence=float(s.get("entry_confidence",  0.0)),
                    last_reeval_score=float(s.get("last_reeval_score", 1.0)),
                    reeval_count=int(s.get("reeval_count", 0)),
                    defensive_mode=bool(s.get("defensive_mode", False)),
                )
                self._trade_states[ticket] = state
            except Exception as exc:
                logger.warning("load_runtime_state: skipping ticket %s: %s", ticket_str, exc)
        logger.info("Loaded %d trade states from checkpoint.", len(self._trade_states))
        for sym, count in data.get("loss_streak", {}).items():
            self._loss_streak[sym] = int(count)
        for sym, iso in data.get("streak_pause", {}).items():
            self._streak_pause[sym] = datetime.fromisoformat(iso) if iso else None

    def _journal_close(self, ticket: int, pips: float, outcome: str) -> None:
        try:
            self._journal.record_close(ticket, round(pips, 1), outcome)
        except Exception as exc:
            logger.warning("Journal record_close failed ticket=%s: %s", ticket, exc)

        eval_id = self._eval_ids.pop(ticket, None)
        if eval_id:
            try:
                from src.application.tick_analytics import get_analytics
                get_analytics().record_outcome(eval_id, outcome, pips)
            except Exception as _exc:
                logger.debug("TickAnalytics record_outcome failed: %s", _exc)

    def _build_trade(
        self,
        signal:            TradeSignal,
        broker_cost:       BrokerCost,
        pip_calc:          PipCalculator,
        tick_value:        float,
        risk_pct_override: Optional[float] = None,
    ) -> Optional[Trade]:
        balance    = self._tr.get_account_balance()
        eff_risk   = risk_pct_override if risk_pct_override is not None else self._risk_pct
        risk_params = RiskParameters(
            account_balance=balance, risk_percent=eff_risk,
            commission_per_lot=broker_cost.commission_usd, min_rr_ratio=self._min_rr,
            account_currency=self._currency,
        )
        sl_pips  = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        raw_lot  = risk_params.lot_size(sl_pips, tick_value, pip_calc)
        max_lot  = getattr(config, "MAX_LOT_SIZE", 10.0)
        lot_size = max(0.01, min(max_lot, math.floor(raw_lot * 100) / 100))
        return Trade(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            lot_size=lot_size,
            status=TradeStatus.PENDING,
            created_at=datetime.now(tz=timezone.utc),
            commission=broker_cost.commission_usd * lot_size,
            spread_cost=broker_cost.spread_pips,
        )
