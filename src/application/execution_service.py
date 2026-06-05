"""
execution_service.py — Entry + Open Trade Guardian (M1 Scalper mode only).
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import src.config as config
from src.domain.entities import Direction, Timeframe, Trade, TradeSignal, TradeStatus, Candle
from src.domain.repositories import IMarketDataRepository, ITradeRepository
from src.domain.value_objects import BrokerCost, PipCalculator, RiskParameters
from src.application.news_manager import NewsManager
from src.infrastructure.spread_calculator import SpreadCalculator
from src.infrastructure.trade_journal import TradeJournal

logger = logging.getLogger(__name__)


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
        self._md       = market_data
        self._tr       = trade_repo
        self._news     = news_manager
        self._spread   = spread_calculator
        self._symbols  = symbols
        self._risk_pct = risk_pct
        self._commission = commission
        self._min_rr   = min_rr
        self._currency = account_currency
        self._journal  = journal
        self._clock    = clock
        self._trade_states: Dict[int, _OpenTradeState] = {}
        self._loss_streak:  Dict[str, int]             = {}
        self._streak_pause: Dict[str, Optional[datetime]] = {}
        self._eval_ids:     Dict[int, str]             = {}   # ticket → analytics eval_id

    # ── Helpers ───────────────────────────────────────────────────────

    def open_count_for_symbol(self, symbol: str) -> int:
        return sum(1 for p in self._tr.get_open_positions() if p.symbol == symbol)

    def has_tp1_hit_for_symbol(self, symbol: str) -> bool:
        """True when at least one open trade on symbol has hit TP1 (proven winner)."""
        return any(s.tp1_hit for s in self._trade_states.values() if s.symbol == symbol)

    # ── Monitoring ────────────────────────────────────────────────────

    def run_monitoring_only(self, symbol: str) -> None:
        now = datetime.now(tz=timezone.utc)
        self._prune_closed_positions()
        if getattr(config, 'MONITOR_ENABLED', True):
            self._monitor_open_trades(symbol, now)

    # ── Trade execution ───────────────────────────────────────────────

    def execute_entry_candidate(self, candidate: EntryCandidate) -> Optional[Trade]:
        """Build, margin-check, and place the trade for a pre-evaluated candidate."""
        symbol = candidate.symbol
        signal = candidate.signal
        trade  = self._build_trade(signal, candidate.broker_cost, candidate.pip_calc,
                                    candidate.tick_value, candidate.risk_pct)
        if trade is None:
            return None

        # Margin check
        req_margin = self._tr.get_required_margin(trade) if hasattr(self._tr, 'get_required_margin') else None
        free_margin = self._tr.get_free_margin()
        safety = getattr(config, 'MARGIN_SAFETY_FACTOR', 2.0)
        if req_margin is not None and free_margin is not None:
            if req_margin * safety > free_margin:
                logger.warning("MARGIN SKIP | %s | trade needs %.2f x %.1f = %.2f but free margin is only %.2f — skipping.",
                               symbol, req_margin, safety, req_margin * safety, free_margin)
                return None
        elif req_margin is None:
            logger.debug("MARGIN CHECK unavailable for %s — proceeding (MT5 will enforce).", symbol)

        ticket = None
        try:
            ticket = self._tr.place_trade(trade)
        except Exception as exc:
            logger.error("Trade placement failed for %s: %s", symbol, exc)
            return None

        if ticket is None:
            return None

        trade.mt5_ticket  = ticket
        trade.status      = TradeStatus.OPEN

        initial_risk = abs(signal.entry_price - signal.stop_loss)
        state = _OpenTradeState(
            symbol=symbol, direction=signal.direction,
            entry=signal.entry_price, original_sl=signal.stop_loss,
            current_sl=signal.stop_loss, take_profit=signal.take_profit,
            tp1_price=candidate.tp1_price, tp2_price=candidate.tp2_price,
            lot_size=trade.lot_size, initial_risk=initial_risk,
            mfe_price=signal.entry_price,
            digits=candidate.digits, grade=candidate.grade,
            created_at=datetime.now(tz=timezone.utc),
        )
        self._trade_states[ticket] = state

        # Link this ticket to the analytics eval_id so outcome can be reported
        eval_id = getattr(candidate, "_eval_id", None)
        if eval_id:
            self._eval_ids[ticket] = eval_id
            try:
                from src.application.tick_analytics import get_analytics
                get_analytics().record_trade_executed(eval_id)
            except Exception as _exc:
                logger.debug("TickAnalytics record_trade_executed failed: %s", _exc)

        try:
            self._journal.record_open(trade, ticket)
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

    # ── Open trade monitoring ─────────────────────────────────────────

    def _monitor_open_trades(self, symbol: str, now: datetime) -> None:
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
                tp1_price = (pos.entry_price + abs(pos.entry_price - pos.stop_loss) *
                             getattr(config, 'TIERED_TP1_RATIO', 1.5) *
                             (1 if pos.direction == Direction.BULLISH else -1))
                tp2_price = (pos.entry_price + abs(pos.entry_price - pos.stop_loss) *
                             getattr(config, 'TIERED_TP2_RATIO', 2.0) *
                             (1 if pos.direction == Direction.BULLISH else -1))
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
            prev_mfe = state.mfe_price
            if state.direction == Direction.BULLISH:
                if price > state.mfe_price:
                    state.mfe_price = price
            else:
                if price < state.mfe_price or state.mfe_price == state.entry:
                    state.mfe_price = price

            mfe_pips = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
            live_pips = (pip_calc.price_to_pips(price - state.entry)
                         if state.direction == Direction.BULLISH
                         else pip_calc.price_to_pips(state.entry - price))
            duration_now = (now - state.created_at).total_seconds() / 60
            logger.info(
                "MFE  [%s] ticket=%s %s | price=%.5f entry=%.5f | live=%+.1fpips mfe=%.1fpips | sl=%.5f tp1=%.5f tp2=%.5f | held=%.1fmin",
                symbol, ticket,
                state.direction.value.upper(),
                price, state.entry,
                live_pips, mfe_pips,
                state.current_sl, state.tp1_price, state.tp2_price,
                duration_now,
            )

            # TP1 check
            tp1_reached = False
            if not state.tp1_hit:
                if state.direction == Direction.BULLISH and price >= state.tp1_price:
                    tp1_reached = True
                elif state.direction == Direction.BEARISH and price <= state.tp1_price:
                    tp1_reached = True

            if tp1_reached:
                close_pct = getattr(config, 'TIERED_TP1_CLOSE_PCT_A_PLUS', 0.40) if state.grade == 'A+' else \
                            getattr(config, 'TIERED_TP1_CLOSE_PCT_B', 0.50) if state.grade == 'B' else \
                            getattr(config, 'TIERED_TP1_CLOSE_PCT', 0.60)
                computed_vol = state.lot_size * close_pct
                close_vol = max(0.01, round(math.floor(computed_vol * 100) / 100, 2))
                closed = self._tr.partial_close_trade(ticket, close_vol, "TP1") if hasattr(self._tr, 'partial_close_trade') else False
                if closed:
                    remaining = state.lot_size - close_vol
                    state.lot_size    = max(0.0, remaining)
                    state.tp1_hit     = True
                    state.accumulated_pips += pip_calc.price_to_pips(abs(state.tp1_price - state.entry))
                    # Profit lock SL
                    profit_lock_dist = state.initial_risk * getattr(config, 'TP1_PROFIT_LOCK_R', 0.3)
                    new_sl = (state.entry + profit_lock_dist if state.direction == Direction.BULLISH
                              else state.entry - profit_lock_dist)
                    if self._tr.modify_sl(ticket, new_sl):
                        state.current_sl = new_sl
                        logger.info("Profit-lock SL moved to %.5f (entry + %.1fR)", new_sl, getattr(config, 'TP1_PROFIT_LOCK_R', 0.3))

            # TP2 check
            if state.tp1_hit and not state.tp2_hit:
                if state.direction == Direction.BULLISH and price >= state.tp2_price:
                    state.tp2_hit = True
                elif state.direction == Direction.BEARISH and price <= state.tp2_price:
                    state.tp2_hit = True

            # Time exit (scalper: 30-min hold with -0.5R cut)
            sl_dist = abs(state.entry - state.current_sl)
            current_profit_ratio = (
                abs(price - state.entry) / sl_dist if sl_dist > 0 else 0.0
            )
            if state.direction == Direction.BULLISH:
                in_profit = price > state.entry
            else:
                in_profit = price < state.entry

            duration = (now - state.created_at).total_seconds() / 60
            _max_dur = getattr(config, "SCALPER_MAX_HOLD_MINUTES", 30)

            should_close = False
            action = None

            if duration >= _max_dur:
                if in_profit:
                    should_close = True
                    action = "time_exit"
                elif current_profit_ratio < -0.5:
                    should_close = True
                    action = "scalper_time_cutloss"

            if should_close and getattr(config, 'MONITOR_ACT_ON_SIGNALS', True):
                if in_profit:
                    final_pips = pip_calc.price_to_pips(abs(price - state.entry))
                else:
                    final_pips = -pip_calc.price_to_pips(abs(price - state.entry))
                total_pips = state.accumulated_pips + final_pips
                _outcome = 'win' if total_pips > 0 else 'loss'
                self._tr.close_trade(ticket)
                self._log_trade_summary(pos, state, pip_calc)
                self._journal_close(ticket, total_pips, _outcome)
                self._update_streak(symbol, total_pips)
                logger.info("TRADE CLOSED action=%s | %s | pips=%.1f", action, symbol, total_pips)

    def _log_trade_summary(self, trade, state: _OpenTradeState, pip_calc: PipCalculator) -> None:
        sl_pips = pip_calc.price_to_pips(abs(state.initial_risk))
        mfe_pips = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
        realized_pnl_pips = state.accumulated_pips
        efficiency = realized_pnl_pips / mfe_pips if mfe_pips > 0 else 0
        logger.info(
            "TRADE CLOSED | %s | SL=%.1f pips | MFE=%.1f pips | PnL=%.1f pips | eff=%.0f%% | TP1=%s TP2=%s",
            state.symbol, sl_pips, mfe_pips, realized_pnl_pips, efficiency * 100,
            state.tp1_hit, state.tp2_hit,
        )

    def _prune_closed_positions(self) -> None:
        open_tickets = {int(p.mt5_ticket or p.id) for p in self._tr.get_open_positions()}
        stale = [t for t in list(self._trade_states.keys()) if t not in open_tickets]
        for t in stale:
            state = self._trade_states.pop(t, None)
            if state is None:
                continue
            pip_calc = PipCalculator(digits=state.digits)
            close_est = state.mfe_price
            direction_sign = 1 if state.direction == Direction.BULLISH else -1
            final_pips = direction_sign * pip_calc.price_to_pips(abs(close_est - state.entry))
            total_pips = state.accumulated_pips + final_pips
            _outcome = 'win' if total_pips > 0 else 'loss'
            self._journal_close(t, total_pips, _outcome)
            self._update_streak(state.symbol, total_pips)

    def _update_streak(self, symbol: str, total_pips: float) -> None:
        now = datetime.now(tz=timezone.utc)
        streak = self._loss_streak.get(symbol, 0)
        if total_pips < 0:
            streak += 1
            self._loss_streak[symbol] = streak
            max_streak = getattr(config, 'MAX_CONSECUTIVE_LOSSES', 2)
            if streak >= max_streak:
                pause_h = getattr(config, 'LOSS_STREAK_PAUSE_HOURS', 4)
                self._streak_pause[symbol] = now + timedelta(hours=pause_h)
                logger.warning("streak_pause | %s | blocked until %s",
                               symbol, self._streak_pause[symbol].isoformat())
        else:
            self._loss_streak[symbol] = 0
            self._streak_pause[symbol] = None

    def save_runtime_state(self) -> dict:
        states = {}
        for ticket, s in self._trade_states.items():
            states[str(ticket)] = {
                'symbol': s.symbol, 'direction': s.direction.value,
                'entry': s.entry, 'original_sl': s.original_sl,
                'current_sl': s.current_sl, 'take_profit': s.take_profit,
                'tp1_price': s.tp1_price, 'tp2_price': s.tp2_price,
                'tp1_hit': s.tp1_hit, 'tp2_hit': s.tp2_hit,
                'lot_size': s.lot_size, 'initial_risk': s.initial_risk,
                'mfe_price': s.mfe_price, 'accumulated_pips': s.accumulated_pips,
                'digits': s.digits, 'grade': s.grade,
                'created_at': s.created_at.isoformat(), 'sl_trailed': s.sl_trailed,
            }
        streaks = dict(self._loss_streak)
        pauses  = {sym: dt.isoformat() if dt else None
                   for sym, dt in self._streak_pause.items()}
        return {'trade_states': states, 'loss_streak': streaks, 'streak_pause': pauses}

    def load_runtime_state(self, data: dict) -> None:
        for ticket_str, s in data.get('trade_states', {}).items():
            try:
                ticket  = int(ticket_str)
                created = datetime.fromisoformat(s['created_at'])
                state   = _OpenTradeState(
                    symbol=s['symbol'], direction=Direction[s['direction'].upper()],
                    entry=float(s['entry']), original_sl=float(s['original_sl']),
                    current_sl=float(s['current_sl']), take_profit=float(s['take_profit']),
                    tp1_price=float(s['tp1_price']), tp2_price=float(s['tp2_price']),
                    tp1_hit=bool(s['tp1_hit']), tp2_hit=bool(s['tp2_hit']),
                    lot_size=float(s['lot_size']), initial_risk=float(s['initial_risk']),
                    mfe_price=float(s['mfe_price']), accumulated_pips=float(s['accumulated_pips']),
                    digits=int(s['digits']), grade=s.get('grade', 'B'),
                    created_at=created, sl_trailed=bool(s.get('sl_trailed', False)),
                )
                self._trade_states[ticket] = state
            except Exception as exc:
                logger.warning("load_runtime_state: skipping ticket %s: %s", ticket_str, exc)
        logger.info("Loaded %d trade states from checkpoint.", len(self._trade_states))
        for sym, count in data.get('loss_streak', {}).items():
            self._loss_streak[sym] = int(count)
        for sym, iso in data.get('streak_pause', {}).items():
            self._streak_pause[sym] = datetime.fromisoformat(iso) if iso else None

    def _journal_close(self, ticket: int, pips: float, outcome: str) -> None:
        try:
            self._journal.record_close(ticket, round(pips, 1), outcome)
        except Exception as exc:
            logger.warning("Journal record_close failed ticket=%s: %s", ticket, exc)

        # Report outcome to tick analytics so velocity gate effectiveness can be measured
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
        sym_commission = broker_cost.commission_usd
        risk_params = RiskParameters(
            account_balance=balance, risk_percent=eff_risk,
            commission_per_lot=sym_commission, min_rr_ratio=self._min_rr,
            account_currency=self._currency,
        )
        sl_pips  = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        raw_lot  = risk_params.lot_size(sl_pips, tick_value, pip_calc)
        max_lot  = getattr(config, 'MAX_LOT_SIZE', 10.0)
        lot_size = max(0.01, min(max_lot, math.floor(raw_lot * 100) / 100))
        final_tp = signal.take_profit
        return Trade(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=final_tp,
            lot_size=lot_size,
            status=TradeStatus.PENDING,
            created_at=datetime.now(tz=timezone.utc),
            commission=sym_commission * lot_size,
            spread_cost=broker_cost.spread_pips,
        )
