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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Deque, Dict, List, Optional

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
    initial_lot:      float = 0.0
    pip_value:        float = 0.0   # per-pip money value per 1.0 lot, account ccy
    initial_risk:     float = 0.0
    mfe_price:        float = 0.0
    accumulated_pips: float = 0.0
    realized_money:   float = 0.0   # money banked on partial closes (price-based, non-MFE)
    digits:           int   = 5
    grade:            str   = "B"
    # Defensive default only — every real construction site (execute_entry_candidate,
    # the outside-bot-position reconstruction in _monitor_open_trades, load_runtime_state)
    # passes created_at explicitly using bot time (MT5-derived). This host-clock
    # fallback should never actually be hit on the live path.
    created_at:       datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    sl_trailed:       bool  = False
    recent_signals:   list  = field(default_factory=list)
    mfe_pips_at_signal: float = 0.0
    # Trade Guardian
    entry_confidence:   float = 0.0   # signal confidence at trade open
    last_reeval_score:  float = 1.0   # most recent continuation score
    reeval_count:       int   = 0     # number of M1-close re-evaluations
    defensive_mode:     bool  = False # True when thesis is weakening
    # Autonomous recovery tracking
    cont_score_history: List[float] = field(default_factory=list)   # last 5 cont scores
    recent_prices:      Deque       = field(default_factory=lambda: deque(maxlen=10))

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
    pip_value:   float          # per-pip money value per 1.0 lot, account ccy
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
        self._trade_states: Dict[int, _OpenTradeState]          = {}
        self._loss_streak:  Dict[str, int]                      = {}
        self._streak_pause: Dict[str, Optional[datetime]]       = {}
        self._reversal_cooldown: Dict[str, datetime]            = {}
        self._eval_ids:     Dict[int, str]                      = {}
        self._mfe_last_logged: Dict[int, datetime]              = {}

    # ── Helpers ───────────────────────────────────────────────────────

    def open_count_for_symbol(self, symbol: str) -> int:
        return sum(1 for p in self._tr.get_open_positions() if p.symbol == symbol)

    def is_symbol_paused(self, symbol: str, now: datetime) -> bool:
        """
        True while a symbol is in a loss-streak cooldown. Enforced by EntryGate
        as a hard gate so the bot actually stops after a run of losses (the old
        code tracked the streak but never blocked on it).
        """
        until = self._streak_pause.get(symbol)
        if until is None:
            return False
        if now >= until:
            self._streak_pause[symbol] = None
            return False
        return True

    def is_reversal_cooldown(self, symbol: str, now: datetime) -> bool:
        """
        True while a symbol is in a post-reversal cooldown — blocks re-entry churn
        after the bot has just flipped/closed a position against a reversal.
        """
        until = self._reversal_cooldown.get(symbol)
        if until is None:
            return False
        if now >= until:
            self._reversal_cooldown[symbol] = None
            return False
        return True

    def _arm_reversal_cooldown(self, symbol: str, now: datetime) -> None:
        secs = getattr(config, "REVERSAL_REENTRY_COOLDOWN_SEC", 120)
        if secs and secs > 0:
            self._reversal_cooldown[symbol] = now + timedelta(seconds=secs)

    def has_tp1_hit_for_symbol(self, symbol: str) -> bool:
        return any(s.tp1_hit for s in self._trade_states.values() if s.symbol == symbol)

    # ── Public monitoring entry-points ────────────────────────────────

    def run_monitoring_only(self, symbol: str, now: datetime) -> None:
        """Tick-level monitoring: TP hits, time exit, MFE tracking."""
        self._prune_closed_positions(now)
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

            # Maintain cont score history for recovery conviction velocity
            state.cont_score_history.append(cont)
            if len(state.cont_score_history) > 5:
                state.cont_score_history = state.cont_score_history[-5:]

            # ── Recovery conviction ────────────────────────────────────
            # Only compute when thesis is under pressure (cont < HOLD_THRESHOLD).
            # When thesis is healthy (cont >= HOLD) we hold regardless.
            recovery = None
            if cont < HOLD_THRESHOLD and len(state.cont_score_history) >= 2:
                try:
                    from src.application.recovery_scorer import compute_recovery_conviction
                    recovery = compute_recovery_conviction(
                        direction=trade_dir,
                        entry=state.entry,
                        current_sl=state.current_sl,
                        price=price,
                        recent_prices=list(state.recent_prices),
                        cont_score_history=state.cont_score_history,
                        structure=structure,
                        m1_candles=m1_candles,
                        pip_calc=pip_calc,
                    )
                except Exception as _exc:
                    logger.debug("RecoveryConviction compute failed: %s", _exc)

            logger.info(
                "REEVAL [%s] ticket=%s %s | cont=%.2f | rcv=%.2f | in_profit=%s | pr=%+.2fR | "
                "mfe=%.1fpips | defensive=%s | #%d",
                symbol, ticket, trade_dir.value.upper(),
                cont, recovery.score if recovery else 1.0,
                in_profit, profit_ratio, mfe_pips,
                state.defensive_mode, state.reeval_count,
            )
            if recovery is not None:
                logger.debug("RECOVERY_DETAIL [%s] ticket=%s | %s", symbol, ticket, recovery.detail)

            # ── Decision matrix ────────────────────────────────────────
            # Recovery conviction overrides hard thresholds when cont is marginal.
            # High conviction (≥0.60) = market physics say hold → upgrade decision.
            # Low conviction (<0.35)  = market moving against us → accelerate exit.
            rcv_score = recovery.score if recovery is not None else None

            # Dynamically adjust effective thresholds based on recovery conviction
            eff_hold      = HOLD_THRESHOLD
            eff_defensive = DEFENSIVE_THRESHOLD
            eff_exit      = EXIT_THRESHOLD
            if rcv_score is not None:
                # High conviction: widen hold window (recovery is likely)
                if rcv_score >= 0.65:
                    eff_hold      = max(0.40, HOLD_THRESHOLD      - 0.10)
                    eff_defensive = max(0.30, DEFENSIVE_THRESHOLD - 0.08)
                # Low conviction: tighten exit window (cut losses faster)
                elif rcv_score <= 0.35:
                    eff_exit      = min(0.40, EXIT_THRESHOLD      + 0.10)
                    eff_defensive = min(0.55, DEFENSIVE_THRESHOLD + 0.08)

            if cont >= eff_hold:
                # Thesis intact — clear defensive mode if previously set
                if state.defensive_mode:
                    logger.info(
                        "REEVAL_HOLD [%s] ticket=%s — thesis restored (%.2f ≥ %.2f)",
                        symbol, ticket, cont, HOLD_THRESHOLD,
                    )
                    state.defensive_mode = False

            elif cont >= eff_defensive:
                # Thesis weakening — protect profit, tighten SL
                if not state.defensive_mode:
                    logger.info(
                        "REEVAL_DEFENSIVE [%s] ticket=%s — cont=%.2f rcv=%.2f entering defensive mode",
                        symbol, ticket, cont, rcv_score if rcv_score is not None else 1.0,
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

            elif cont >= eff_exit:
                # Thesis degraded — close losing trades outright; partial exit only when in profit
                if not in_profit and act:
                    duration_min = (now - state.created_at).total_seconds() / 60
                    if self._tr.close_trade(ticket):
                        self._finalize_close(ticket, state, action="reeval_losing_exit", now=now)
                        logger.info(
                            "REEVAL_LOSING_EXIT [%s] ticket=%s | cont=%.2f | held=%.1fmin",
                            symbol, ticket, cont, duration_min,
                        )
                        continue  # trade closed — skip reversal checks
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
                        state.realized_money  += live_pips * state.pip_value * close_vol
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
                    if self._tr.close_trade(ticket):
                        self._finalize_close(ticket, state, action="reeval_exit", now=now)
                        logger.info(
                            "REEVAL_EXIT [%s] ticket=%s %s — thesis invalidated (cont=%.2f) | reeval=%d",
                            symbol, ticket, trade_dir.value.upper(), cont, state.reeval_count,
                        )
                    continue  # trade closed (or close attempted) — skip reversal check

            # ── 3. Reversal detection ──────────────────────────────────
            # Two triggers:
            # (a) Fresh opposing signal at or above REVERSAL_THRESHOLD — full confidence reversal.
            # (b) Trade already in defensive/losing mode AND M1 structure has fully flipped
            #     against the trade direction — structural reversal even without a fresh signal.
            effective_reversal_conf = REVERSAL_THRESHOLD
            if state.defensive_mode and not in_profit:
                if rcv_score is not None and rcv_score <= 0.35:
                    effective_reversal_conf = 0.45  # market physics confirm the reversal
                else:
                    effective_reversal_conf = 0.55

            # Structural reversal: use the ISOLATED structure factor (0.0 only when
            # M1 structure has fully flipped against the trade). The previous code
            # multiplied the full continuation score by 0.40 and checked == 0.0,
            # which could never be true — this is the corrected, reachable check.
            struct_factor   = self._structure_factor(state, structure)
            struct_reversed = (struct_factor == 0.0) and cont < DEFENSIVE_THRESHOLD

            if act and struct_reversed and state.defensive_mode and not in_profit:
                duration_min = (now - state.created_at).total_seconds() / 60
                if self._tr.close_trade(ticket):
                    self._finalize_close(ticket, state, action="structural_reversal_exit", now=now)
                    self._arm_reversal_cooldown(symbol, now)
                    logger.info(
                        "STRUCTURAL_REVERSAL_EXIT [%s] ticket=%s | M1 structure fully reversed "
                        "| cont=%.2f | held=%.1fmin",
                        symbol, ticket, cont, duration_min,
                    )
                continue

            if (fresh_signal is not None
                    and fresh_signal.direction != trade_dir
                    and fresh_signal.confidence >= effective_reversal_conf
                    and act):
                if self._tr.close_trade(ticket):
                    self._finalize_close(ticket, state, action="reversal_exit", now=now)
                    self._arm_reversal_cooldown(symbol, now)
                    logger.info(
                        "REVERSAL_EXIT [%s] ticket=%s | opposing %s signal conf=%.2f ≥ %.2f",
                        symbol, ticket,
                        fresh_signal.direction.value.upper(), fresh_signal.confidence,
                        effective_reversal_conf,
                    )

    # ── Trade execution ───────────────────────────────────────────────

    def execute_entry_candidate(self, candidate: EntryCandidate, now: datetime) -> Optional[Trade]:
        """Build, margin-check, and place the trade for a pre-evaluated candidate."""
        symbol = candidate.symbol
        signal = candidate.signal
        trade  = self._build_trade(
            signal, candidate.broker_cost, candidate.pip_calc,
            candidate.pip_value, now, candidate.risk_pct,
        )
        if trade is None:
            return None

        # Exact margin check using live MT5 figures (FAIL CLOSED if unknown).
        req_margin  = (self._tr.get_required_margin(trade)
                       if hasattr(self._tr, "get_required_margin") else None)
        free_margin = self._tr.get_free_margin()
        safety      = getattr(config, "MARGIN_SAFETY_FACTOR", 1.5)
        if req_margin is None or free_margin is None:
            logger.error(
                "MARGIN ABORT | %s | required/free margin unreadable (req=%s free=%s) — rejecting",
                symbol, req_margin, free_margin,
            )
            return None
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

        # ── POST-FILL VALIDATION (M1 latency / slippage guard) ─────────
        # The trade is now LIVE at the broker's ACTUAL fill, which can differ
        # from the signal price by up to the deviation band. The entry gate
        # validated SIGNAL-TIME geometry — now stale. Re-validate the REAL
        # geometry against the fill; if slippage has invalidated the trade,
        # CLOSE it immediately (fail closed) rather than manage a position whose
        # risk/reward no longer matches what was approved. The close costs one
        # round-trip (~$0.6) — far cheaper than nursing a broken scalp.
        fill_delta = trade.entry_price - signal.entry_price
        pip_calc   = candidate.pip_calc
        is_long    = signal.direction == Direction.BULLISH

        # TP1 is the STRUCTURAL target (a fixed price); do NOT shift it by the
        # fill slippage — shifting pushes it PAST the swing (the day-12 failure).
        tp1_anchored   = candidate.tp1_price        # structural target, NOT slippage-shifted
        tp2_anchored   = trade.take_profit          # broker TP2 backstop (already fill-ref)
        actual_sl_dist = abs(trade.entry_price - trade.stop_loss)
        try:
            balance = self._tr.get_account_balance() or 0.0
        except Exception:
            balance = 0.0

        # Pure decision: re-validate (1) money risk vs the hard ceiling and
        # (2) TP1 reachable R:R, both against the ACTUAL fill (see helper).
        abort_reason, dbg = self._post_fill_abort_reason(
            entry=trade.entry_price, stop=trade.stop_loss, tp1=tp1_anchored,
            is_long=is_long, lot=trade.lot_size, pip_value=candidate.pip_value,
            balance=balance, pip_calc=pip_calc,
        )
        if abort_reason is not None:
            logger.warning(
                "POST_FILL_ABORT [%s] ticket=%s | %s | sl=%.1fp risk=%.2f tp1=%.1fp "
                "— closing immediately",
                symbol, ticket, abort_reason,
                dbg["sl_pips"], dbg["risk"], dbg["tp1_pips"],
            )
            try:
                self._tr.close_trade(ticket)
            except Exception as exc:
                logger.error("POST_FILL_ABORT close failed ticket=%s: %s", ticket, exc)
            return None

        initial_risk = actual_sl_dist
        if abs(fill_delta) > 0:
            logger.info(
                "FILL_CHECK [%s] signal=%.5f fill=%.5f Δ=%.5f | SL=%.5f(%.1fp) "
                "TP1=%.5f(%.1fp) TP2=%.5f | risk=%.2f",
                symbol, signal.entry_price, trade.entry_price, fill_delta,
                trade.stop_loss, dbg["sl_pips"], tp1_anchored, dbg["tp1_pips"],
                tp2_anchored, dbg["risk"],
            )
        confidence   = float(candidate.trade_score) / 100.0
        state = _OpenTradeState(
            symbol=symbol, direction=signal.direction,
            entry=trade.entry_price, original_sl=trade.stop_loss,
            current_sl=trade.stop_loss, take_profit=trade.take_profit,
            tp1_price=tp1_anchored, tp2_price=tp2_anchored,
            lot_size=trade.lot_size, initial_lot=trade.lot_size,
            pip_value=candidate.pip_value, initial_risk=initial_risk,
            mfe_price=trade.entry_price,
            digits=candidate.digits, grade=candidate.grade,
            created_at=now,
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
                    lot_size=pos.lot_size, initial_lot=pos.lot_size,
                    mfe_price=pos.entry_price, digits=digits,
                    # pos.created_at is MT5-derived (trade_repo.get_open_positions()
                    # via mt5_epoch_to_datetime) — always pass it explicitly so the
                    # time-based exit above compares like-for-like against `now`
                    # (also MT5-derived). Without this it silently defaulted to the
                    # dataclass field's host-clock fallback.
                    created_at=pos.created_at,
                )
                # Per-pip value for analytics pip conversion (best-effort).
                try:
                    state.pip_value = self._md.get_pip_value(symbol)
                except Exception:
                    state.pip_value = 0.0
                self._trade_states[ticket] = state

            price = self._md.get_current_price(symbol)

            # Track recent tick prices for recovery conviction scoring
            if price:
                state.recent_prices.append(price)

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
                        tp1_pips = pip_calc.price_to_pips(abs(state.tp1_price - state.entry))
                        state.realized_money  += tp1_pips * state.pip_value * close_vol
                        state.lot_size         = max(0.0, state.lot_size - close_vol)
                        state.tp1_hit          = True
                        state.accumulated_pips += tp1_pips
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
                        tp2_pips = pip_calc.price_to_pips(abs(state.tp2_price - state.entry))
                        state.realized_money  += tp2_pips * state.pip_value * close_vol
                        state.lot_size         = max(0.0, state.lot_size - close_vol)
                        state.tp2_hit          = True
                        state.accumulated_pips += tp2_pips
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
            _hold_overrides = getattr(config, "SCALPER_MAX_HOLD_MINUTES_PER_SYMBOL", {})
            _max_dur        = (_hold_overrides.get(symbol, getattr(config, "SCALPER_MAX_HOLD_MINUTES", 30))
                               if isinstance(_hold_overrides, dict)
                               else getattr(config, "SCALPER_MAX_HOLD_MINUTES", 30))

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
                if self._tr.close_trade(ticket):
                    self._finalize_close(ticket, state, action=action, now=now)
                    logger.info(
                        "TIME_EXIT [%s] action=%s ticket=%s | held=%.1fmin",
                        symbol, action, ticket, duration_min,
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
        trade_dir = state.direction

        # ── Factor 1: M1 structure (40%) ──────────────────────────────
        struct_score = self._structure_factor(state, structure)

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

    @staticmethod
    def _structure_factor(state: "_OpenTradeState", structure: "StructureStateManager") -> float:
        """
        Isolated M1-structure score (0.0–1.0) in the trade's direction.

        0.0 means M1 structure has fully flipped AGAINST the trade — used both as
        the 40% factor of the continuation score and as the structural-reversal
        trigger. Defaults to neutral 0.50 when structure data is missing so we
        never spuriously read a flip.
        """
        from src.strategy.structure_state import StructureState

        if structure is None:
            return 0.50
        trade_dir = state.direction
        m1_state  = structure.get_state(Timeframe.M1)
        bos_dir   = structure.get_last_bos_direction(Timeframe.M1)

        if trade_dir == Direction.BULLISH:
            if   m1_state == StructureState.BULLISH_TREND:                                    return 1.00
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BULLISH: return 0.80
            elif m1_state == StructureState.MITIGATION_ZONE:                                  return 0.65
            elif m1_state == StructureState.RANGING:                                          return 0.35
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BEARISH: return 0.10
            elif m1_state == StructureState.BEARISH_TREND:                                    return 0.00
        else:
            if   m1_state == StructureState.BEARISH_TREND:                                    return 1.00
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BEARISH: return 0.80
            elif m1_state == StructureState.MITIGATION_ZONE:                                  return 0.65
            elif m1_state == StructureState.RANGING:                                          return 0.35
            elif m1_state == StructureState.STRUCTURE_BREAK and bos_dir == Direction.BULLISH: return 0.10
            elif m1_state == StructureState.BULLISH_TREND:                                    return 0.00
        return 0.50

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

    def _log_trade_summary(self, state: _OpenTradeState, pip_calc: PipCalculator,
                           pnl_money: float) -> None:
        sl_pips    = pip_calc.price_to_pips(abs(state.initial_risk))
        mfe_pips   = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
        pnl_pips   = self._money_to_pips(state, pnl_money)
        efficiency = pnl_pips / mfe_pips if mfe_pips > 0 else 0
        logger.info(
            "TRADE SUMMARY | %s %s | entry_conf=%.2f | SL=%.1fpips | MFE=%.1fpips | "
            "PnL=%.2f %s (≈%.1fpips) | eff=%.0f%% | TP1=%s TP2=%s | reeval=%d | last_cont=%.2f",
            state.symbol, state.direction.value.upper(),
            state.entry_confidence, sl_pips, mfe_pips,
            pnl_money, self._currency, pnl_pips, efficiency * 100,
            state.tp1_hit, state.tp2_hit,
            state.reeval_count, state.last_reeval_score,
        )

    def _prune_closed_positions(self, now: datetime) -> None:
        """
        Reconcile tracking state for positions MT5 has already closed
        (server-side SL/TP, or manual). Uses BROKER-TRUTH realised P&L — never
        an MFE/intended-level reconstruction.
        """
        open_tickets = {int(p.mt5_ticket or p.id) for p in self._tr.get_open_positions()}
        stale = [t for t in list(self._trade_states.keys()) if t not in open_tickets]
        for t in stale:
            state = self._trade_states.get(t)
            if state is None:
                continue
            self._finalize_close(t, state, action="broker_close", now=now)

    # ── Broker-truth close finalisation ───────────────────────────────

    def _finalize_close(self, ticket: int, state: "_OpenTradeState", action: str, now: datetime) -> float:
        """
        Single funnel for EVERY trade close. Reads the broker's realised net P&L
        (money, account currency) from deal history and journals THAT. Falls back
        to a real price-based estimate (never MFE) only if history is unavailable,
        and logs that loudly. Returns the realised money P&L.
        """
        pip_calc = PipCalculator(digits=state.digits)
        money: Optional[float] = None
        source = "broker"
        try:
            deal = self._tr.get_position_realized_pnl(ticket)
            if deal is not None and deal.get("profit") is not None:
                money = float(deal["profit"])
        except Exception as exc:
            logger.error("CLOSE_PNL [%s] ticket=%s realized P&L query failed: %s",
                         state.symbol, ticket, exc)

        if money is None:
            money = self._estimate_money_pnl(state)
            source = "ESTIMATE(no broker history)"
            logger.error(
                "CLOSE_PNL [%s] ticket=%s broker realised P&L unavailable — "
                "using price-based estimate=%.2f (NOT MFE)",
                state.symbol, ticket, money,
            )

        outcome    = "win" if money > 0 else "loss" if money < 0 else "breakeven"
        pips_equiv = self._money_to_pips(state, money)

        # Drop tracking BEFORE side-effects so a re-entry can't double count.
        self._trade_states.pop(ticket, None)
        self._log_trade_summary(state, pip_calc, money)
        self._journal_close(ticket, money, outcome, pips_equiv)
        self._update_streak(state.symbol, money, now)
        logger.info(
            "TRADE CLOSED action=%s | %s ticket=%s | pnl=%.2f %s [%s] | pips≈%.1f | grade=%s",
            action, state.symbol, ticket, money, self._currency, source,
            pips_equiv, state.grade,
        )

        # Arm re-entry cooldown for any loss close, including broker-side SL hits.
        # Prevents whipsaw churn: stopped out → bar closes with opposing tick
        # pressure → re-enter immediately → stopped out again.
        if money < 0:
            self._arm_reversal_cooldown(state.symbol, now)
            logger.debug(
                "COOLDOWN ARMED [%s] after %s loss — blocking re-entry for %ss",
                state.symbol, action,
                getattr(config, "REVERSAL_REENTRY_COOLDOWN_SEC", 120),
            )

        return money

    def _estimate_money_pnl(self, state: "_OpenTradeState") -> float:
        """
        Real (non-MFE) fallback P&L estimate in account currency: money already
        banked on partial closes + mark-to-market of the remaining lot at the
        CURRENT price. Used only when broker deal history is unavailable.
        """
        try:
            price = self._md.get_current_price(state.symbol)
        except Exception:
            price = state.entry
        pip_calc = PipCalculator(digits=state.digits)
        if state.direction == Direction.BULLISH:
            live_pips = pip_calc.price_to_pips(price - state.entry) if price >= state.entry \
                        else -pip_calc.price_to_pips(state.entry - price)
        else:
            live_pips = pip_calc.price_to_pips(state.entry - price) if price <= state.entry \
                        else -pip_calc.price_to_pips(price - state.entry)
        remaining_money = live_pips * state.pip_value * max(0.0, state.lot_size)
        return state.realized_money + remaining_money

    @staticmethod
    def _money_to_pips(state: "_OpenTradeState", money: float) -> float:
        """
        Approximate pips for analytics from broker money P&L. Sign is exact;
        magnitude uses the initial lot and per-pip value. Returns 0 if unknown.
        """
        denom = state.pip_value * (state.initial_lot or state.lot_size)
        if denom <= 0:
            return 0.0
        return round(money / denom, 2)

    def _update_streak(self, symbol: str, pnl_money: float, now: datetime) -> None:
        """Track loss streak for analytics and alerting (no trading pause by default)."""
        streak = self._loss_streak.get(symbol, 0)
        if pnl_money < 0:
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
                "lot_size": s.lot_size, "initial_lot": s.initial_lot,
                "pip_value": s.pip_value, "realized_money": s.realized_money,
                "initial_risk": s.initial_risk,
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
                    lot_size=float(s["lot_size"]),
                    initial_lot=float(s.get("initial_lot", s["lot_size"])),
                    pip_value=float(s.get("pip_value", 0.0)),
                    realized_money=float(s.get("realized_money", 0.0)),
                    initial_risk=float(s["initial_risk"]),
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

    def _journal_close(self, ticket: int, pnl_money: float, outcome: str,
                       pips: float = 0.0) -> None:
        try:
            self._journal.record_close(ticket, round(pnl_money, 2), outcome, round(pips, 2))
        except Exception as exc:
            logger.warning("Journal record_close failed ticket=%s: %s", ticket, exc)

        eval_id = self._eval_ids.pop(ticket, None)
        if eval_id:
            try:
                from src.application.tick_analytics import get_analytics
                get_analytics().record_outcome(eval_id, outcome, pips)
            except Exception as _exc:
                logger.debug("TickAnalytics record_outcome failed: %s", _exc)

    @staticmethod
    def _post_fill_abort_reason(
        entry:     float,
        stop:      float,
        tp1:       float,
        is_long:   bool,
        lot:       float,
        pip_value: float,
        balance:   float,
        pip_calc:  PipCalculator,
    ):
        """
        Pure post-fill viability check against the ACTUAL fill price.

        Returns (reason_or_None, debug_dict). The entry gate validated
        SIGNAL-TIME geometry; the broker fills at a price that may differ by up
        to the deviation band, so the live trade must be re-validated:

          (1) MONEY RISK — adverse slippage widens the real stop distance (the
              SL is a fixed structural price). If realised risk exceeds the hard
              MAX_TRADE_RISK_PCT ceiling (with a 5% tolerance band), abort.
          (2) TP1 GEOMETRY — favorable slippage can run price toward the
              structural target before fill, leaving TP1 on the wrong side or
              below MIN_RR_FLOOR × the real stop. If the reachable reward no
              longer justifies the risk, abort.

        reason is None when the trade is still viable.
        """
        sl_pips = pip_calc.price_to_pips(abs(entry - stop))
        risk    = sl_pips * pip_value * lot
        max_risk = balance * (getattr(config, "MAX_TRADE_RISK_PCT", 3.0) / 100.0)
        tp1_dist = (tp1 - entry) if is_long else (entry - tp1)
        tp1_pips = pip_calc.price_to_pips(tp1_dist) if tp1_dist > 0 else 0.0
        min_rr   = getattr(config, "MIN_RR_FLOOR", 0.8)
        dbg = {"sl_pips": sl_pips, "risk": risk, "tp1_pips": tp1_pips}

        if balance > 0 and risk > max_risk * 1.05:
            return (
                "slippage widened risk to %.2f (%.1f%% > ceiling %.2f)" % (
                    risk, (risk / balance * 100.0), max_risk),
                dbg,
            )
        if tp1_dist <= 0 or (sl_pips > 0 and tp1_pips < min_rr * sl_pips):
            return (
                "slippage compressed TP1 to %.1f pips (need ≥ %.2f×%.1f = %.1f)" % (
                    tp1_pips, min_rr, sl_pips, min_rr * sl_pips),
                dbg,
            )
        return (None, dbg)

    def _build_trade(
        self,
        signal:            TradeSignal,
        broker_cost:       BrokerCost,
        pip_calc:          PipCalculator,
        pip_value:         float,
        now:               datetime,
        risk_pct_override: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        Size the trade from money risk using the CORRECT per-pip value, then fit
        it to real MT5 margin and enforce an absolute money-risk ceiling.

        Returns None (fail closed) when inputs are invalid or even the broker
        minimum lot cannot be financed / would exceed the risk ceiling.
        """
        symbol  = signal.symbol
        balance = self._tr.get_account_balance()
        if balance is None or balance <= 0:
            logger.error("BUILD_TRADE [%s] balance unreadable/≤0 (%s) — rejecting", symbol, balance)
            return None
        if pip_value is None or pip_value <= 0:
            logger.error("BUILD_TRADE [%s] pip_value invalid (%s) — rejecting", symbol, pip_value)
            return None

        eff_risk   = risk_pct_override if risk_pct_override is not None else self._risk_pct
        risk_params = RiskParameters(
            account_balance=balance, risk_percent=eff_risk,
            commission_per_lot=broker_cost.commission_per_lot, min_rr_ratio=self._min_rr,
            account_currency=self._currency,
        )
        sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        if sl_pips <= 0:
            logger.error("BUILD_TRADE [%s] sl_pips ≤ 0 — rejecting", symbol)
            return None

        raw_lot = risk_params.lot_size(sl_pips, pip_value)
        if raw_lot <= 0:
            logger.error("BUILD_TRADE [%s] computed raw_lot ≤ 0 — rejecting", symbol)
            return None

        # Broker volume constraints (min / max / step). Fail closed if unknown.
        vmin, vmax, vstep = 0.01, getattr(config, "MAX_LOT_SIZE", 10.0), 0.01
        try:
            vc = self._md.get_volume_constraints(symbol)
            if vc:
                vmin, vmax, vstep = vc
        except Exception as exc:
            logger.warning("BUILD_TRADE [%s] volume constraints unreadable: %s — using defaults", symbol, exc)
        vstep   = vstep if vstep and vstep > 0 else 0.01
        max_lot = min(getattr(config, "MAX_LOT_SIZE", 10.0), vmax)
        # Decimal places implied by the broker volume step (e.g. 0.01→2, 0.001→3)
        # so rounding never corrupts sub-0.01-step instruments.
        step_decimals = max(0, min(8, -int(math.floor(math.log10(vstep))))) if vstep < 1 else 0

        def _round_step(v: float) -> float:
            steps = math.floor(v / vstep + 1e-9)
            return round(steps * vstep, step_decimals)

        lot_size = _round_step(raw_lot)
        lot_size = max(vmin, min(max_lot, lot_size))

        # ── Fit to real MT5 margin (currency-correct, no leverage proxy) ──
        safety = getattr(config, "MARGIN_SAFETY_FACTOR", 1.5)
        try:
            free_margin = self._tr.get_free_margin()
        except Exception as exc:
            logger.error("BUILD_TRADE [%s] free margin unreadable: %s — rejecting", symbol, exc)
            return None
        if free_margin is None or free_margin <= 0:
            logger.error("BUILD_TRADE [%s] free margin %s — rejecting", symbol, free_margin)
            return None

        def _fits(volume: float) -> Optional[bool]:
            """True/False if `volume` fits within free margin × safety; None if unknown."""
            try:
                req = self._tr.get_margin_for_volume(
                    symbol, signal.direction, signal.entry_price, volume
                )
            except Exception:
                req = None
            if req is None:
                return None
            return (req * safety) <= free_margin

        # Reduce by volume step until it fits, or reject at the minimum.
        guard = 0
        while lot_size >= vmin and guard < 1000:
            fit = _fits(lot_size)
            if fit is None:
                logger.error(
                    "BUILD_TRADE [%s] MT5 margin check unavailable — rejecting (fail closed)", symbol,
                )
                return None
            if fit:
                break
            next_lot = _round_step(lot_size - vstep)
            if next_lot < vmin or next_lot >= lot_size:
                logger.info(
                    "BUILD_TRADE [%s] even min lot %.2f does not fit free_margin=%.2f×%.1f — rejecting",
                    symbol, vmin, free_margin, safety,
                )
                return None
            lot_size = next_lot
            guard += 1

        # ── Absolute money-risk ceiling (fail closed) ──────────────
        realized_risk_money = sl_pips * pip_value * lot_size
        max_risk_money      = balance * (getattr(config, "MAX_TRADE_RISK_PCT", 3.0) / 100.0)
        if realized_risk_money > max_risk_money * 1.0001:
            logger.warning(
                "BUILD_TRADE [%s] realized risk %.2f > ceiling %.2f (%.1f%% of %.2f) at min lot %.2f — rejecting",
                symbol, realized_risk_money, max_risk_money,
                getattr(config, "MAX_TRADE_RISK_PCT", 3.0), balance, lot_size,
            )
            return None

        logger.info(
            "SIZE [%s] balance=%.2f risk%%=%.2f sl=%.1fpips pip_val=%.4f → raw=%.4f lot=%.2f "
            "| realized_risk=%.2f (%.2f%%)",
            symbol, balance, eff_risk, sl_pips, pip_value, raw_lot, lot_size,
            realized_risk_money, realized_risk_money / balance * 100,
        )

        return Trade(
            id=str(uuid.uuid4()),
            symbol=symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            lot_size=lot_size,
            status=TradeStatus.PENDING,
            created_at=now,
            commission=broker_cost.commission_per_lot * lot_size,
            spread_cost=broker_cost.spread_pips,
        )
