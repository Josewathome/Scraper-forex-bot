"""
execution_service.py — Phase 3 + 3.5: Entry + Open Trade Guardian  (v7 — Adaptive TP, Scoring, Time Exit)
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import src.config as config

from src.domain.entities import (Direction, LiquidityPool, Timeframe, Trade,
                                   TradeSignal, TradeStatus, Zone, ZoneType, Candle)
from src.domain.repositories import IMarketDataRepository, ITradeRepository, IZoneRepository
from src.domain.value_objects import BrokerCost, PipCalculator, RiskParameters
from src.application.analysis import (
    ZoneMapper, atr, find_m5_choch_signal, find_m1_predictive_signal,
    find_h4_bias, find_d1_bias, compute_h4_equilibrium, score_setup, grade_setup,
    calculate_zone_buffer, is_in_trading_session, compute_tp1_price, compute_tp2_price,
    get_initial_risk, check_min_sl_pips, compute_dynamic_tp, score_trade, dynamic_risk,
    is_strong_trend, is_kill_zone, evaluate_zone_health, find_opposing_opportunity,
    find_htf_zone_confluence, find_m30_zone_confluence, find_m5_zone_confluence,
    read_m5_zone_story, read_m30_zone_story, find_m5_precision_entry, get_current_m5_bar_open,
)
from src.application.news_manager import NewsManager
from src.application.trade_monitor import (TradeMonitor, MonitorEvent,
                                             find_swing_trail_sl, find_runner_protection_sl)
from src.infrastructure.spread_calculator import SpreadCalculator
from src.infrastructure.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

# Timeframe alias used internally
_M5 = Timeframe.M5


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
    m1_no_mfe:        int   = 0
    m5_no_mfe:        int   = 0
    m5_window:        list  = field(default_factory=list)
    opposing_zones:   list  = field(default_factory=list)
    last_m5_time:     Optional[datetime] = None
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
    zone:        Zone
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
    h1_atr:      float
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
        zone_repo:         IZoneRepository,
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
        self._zr       = zone_repo
        self._news     = news_manager
        self._spread   = spread_calculator
        self._symbols  = symbols
        self._risk_pct = risk_pct
        self._commission = commission
        self._min_rr   = min_rr
        self._currency = account_currency
        self._journal  = journal
        self._zone_mapper: ZoneMapper = ZoneMapper()
        self._trade_states: Dict[int, _OpenTradeState] = {}
        self._loss_streak:  Dict[str, int]             = {}
        self._streak_pause: Dict[str, Optional[datetime]] = {}
        self._m30_zones:   Dict[str, List[Zone]]       = {}
        self._m5_zones:    Dict[str, List[Zone]]       = {}

    # ── Zone mapping ─────────────────────────────────────────────────

    def run_zone_mapping(self, symbol: str) -> None:
        h1 = self._md.get_candles(symbol, Timeframe.H1, config.H1_CANDLE_COUNT)
        if not h1:
            return
        zones, pool = self._zone_mapper.map_zones(h1, symbol, Timeframe.H1)
        old = self._zr.get_active_zones(symbol)
        for z in old:
            self._zr.invalidate_zone(z.id)
        for z in zones:
            self._zr.save_zone(z)
        if pool is not None and hasattr(self._zr, 'save_liquidity_pool'):
            self._zr.save_liquidity_pool(pool)

    def run_m30_zone_mapping(self, symbol: str) -> None:
        if not getattr(config, 'M30_ZONE_MAPPING_ENABLED', True):
            return
        m30 = self._md.get_candles(symbol, Timeframe.M30, config.M30_CANDLE_COUNT)
        if not m30:
            return
        zones, _ = self._zone_mapper.map_zones(m30, symbol, Timeframe.M30)
        self._m30_zones[symbol] = zones
        logger.debug("M30 zones remapped | %s | %d zones", symbol, len(zones))

    def run_m5_zone_mapping(self, symbol: str) -> None:
        if not getattr(config, 'M5_ZONE_MAPPING_ENABLED', True):
            return
        m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
        if not m5:
            return
        zones, _ = self._zone_mapper.map_zones(m5, symbol, Timeframe.M5)
        self._m5_zones[symbol] = zones
        logger.debug("M5 zones remapped | %s | %d zones", symbol, len(zones))

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

    def run_monitoring_cycle(self, symbol: str) -> None:
        """Prune, monitor, and evaluate entry candidates."""
        self.run_monitoring_only(symbol)
        if len(self._tr.get_open_positions()) < config.MAX_OPEN_TRADES:
            if self.open_count_for_symbol(symbol) < getattr(config, 'MAX_TRADES_PER_SYMBOL', 2):
                candidate = self.collect_entry_candidate(symbol, is_phase2=False)
                if candidate:
                    self.execute_entry_candidate(candidate)

    # ── Entry evaluation ──────────────────────────────────────────────

    def collect_entry_candidate(self, symbol: str, is_phase2: bool = False) -> Optional[EntryCandidate]:
        """
        Evaluate all zones for symbol and return the highest-qualifying
        EntryCandidate without placing a trade.  Returns None when no
        signal qualifies or preconditions (news / session / data) fail.
        """
        now = datetime.now(tz=timezone.utc)

        # Streak pause
        pause_until = self._streak_pause.get(symbol)
        if pause_until and now < pause_until:
            logger.debug("streak_pause | %s | blocked until %s", symbol, pause_until.isoformat())
            return None

        # News / session gate
        if self._news.is_blocked(symbol, now):
            return None
        if not is_in_trading_session(now):
            return None

        # Get broker cost
        broker_cost = self._spread.get_broker_cost(symbol)

        # Candle data
        h1_short = self._md.get_candles(symbol, Timeframe.H1, 20)
        if not h1_short:
            return None
        h1_atr   = atr(h1_short, 14)
        digits   = self._md.get_symbol_digits(symbol)
        pip_calc = PipCalculator(digits=digits)
        tick_value = self._md.get_tick_value(symbol)

        # ATR cost gate
        if hasattr(self._spread, 'broker_cost_exceeds_atr'):
            if self._spread.broker_cost_exceeds_atr(symbol, h1_atr, tick_value):
                return None

        # H4 + D1 bias
        h4 = self._md.get_candles(symbol, Timeframe.H4, config.H4_CANDLE_COUNT)
        h4_bias = find_h4_bias(h4) if h4 else None
        if isinstance(h4_bias, tuple):
            h4_bias, h4_eq = h4_bias
        else:
            h4_eq = compute_h4_equilibrium(h4) if h4 else (0.0, 0.0)

        if getattr(config, 'REQUIRE_H4_BIAS', False) and h4_bias is None:
            logger.debug("H4 RANGING | %s | no trend — skipping", symbol)
            return None

        h4_zones_data = self._zone_mapper.map_zones(h4, symbol, Timeframe.H4) if h4 else ([], None)
        h4_zones = h4_zones_data[0] if h4_zones_data else []

        d1 = self._md.get_candles(symbol, Timeframe.D1, config.D1_CANDLE_COUNT)
        d1_bias = find_d1_bias(d1) if d1 else None

        # Get zones and liquidity pool
        ask = self._md.get_current_price(symbol)
        zone_buf = calculate_zone_buffer(h1_atr, 0.15)
        zones = self._zr.get_active_zones(symbol)
        pool  = self._zr.get_liquidity_pool(symbol)

        sym_min_score = config.SYMBOL_MIN_SCORE.get(symbol, config.SETUP_SCORE_THRESHOLD)
        baseline_atr  = h1_atr

        best_candidate: Optional[EntryCandidate] = None

        for zone in zones:
            if not zone.active:
                continue

            # D1 counter-trend gate
            if d1_bias and d1_bias != zone.direction:
                counter_min = getattr(config, 'COUNTER_D1_MIN_SCORE', 7)
                if getattr(config, 'BLOCK_COUNTER_D1', False):
                    logger.debug("COUNTER-D1 HARD BLOCK | %s | zone=%s d1=%s", symbol, zone.id, d1_bias)
                    continue

            # Confluence scoring
            _pre_h4c  = find_htf_zone_confluence(zone, h4_zones)
            _pre_m30c = find_m30_zone_confluence(zone, self._m30_zones.get(symbol, []))
            _pre_m5c  = find_m5_zone_confluence(zone, self._m5_zones.get(symbol, []))

            _pre_score = score_setup(zone.direction, h4_bias, ask, h4_eq, d1_bias, _pre_h4c)
            _pre_score += getattr(config, 'M30_ZONE_CONFLUENCE_BONUS', 1) if _pre_m30c else 0
            _pre_score += getattr(config, 'M5_ZONE_CONFLUENCE_BONUS',  1) if _pre_m5c  else 0

            if _pre_score < sym_min_score:
                logger.debug("SCORE BELOW SYM MIN | %s | %d < %d (H4=%s M30=%s M5=%s)",
                             symbol, _pre_score, sym_min_score, _pre_h4c, _pre_m30c, _pre_m5c)
                continue

            h4_confluent  = _pre_h4c
            m30_confluent = _pre_m30c
            m5_confluent  = _pre_m5c
            setup_score   = _pre_score
            grade_result  = grade_setup(setup_score)
            grade         = grade_result[0]
            min_rr        = grade_result[2]

            # Zone direction vs price position
            if zone.direction == Direction.BULLISH and ask > zone.high:
                logger.debug("ZONE FLIP | %s %s | %s — no opposing setup", symbol, zone.id, zone.direction.value)
                continue
            if zone.direction == Direction.BEARISH and ask < zone.low:
                logger.debug("ZONE FLIP | %s %s | %s — no opposing setup", symbol, zone.id, zone.direction.value)
                continue

            # Zone freshness check
            if zone.touch_count > 2:
                logger.debug("ZONE STALE | %s %s | %s — skipping", symbol, zone.id, zone.direction.value)
                continue

            # M5 zone story gate
            m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
            m5_story = read_m5_zone_story(m5, zone) if m5 else 'neutral'
            if m5_story == 'distributing':
                logger.debug("M5 STORY DISTRIBUTING | %s %s | M5 bars showing distribution — skip", symbol, zone.id)
                continue

            # M30 story gate
            m30_bars = self._md.get_candles(symbol, Timeframe.M30, getattr(config, 'M30_STORY_LOOKBACK', 8))
            m30_story = read_m30_zone_story(m30_bars, zone) if m30_bars else 'neutral'
            if m30_story == 'against':
                logger.debug("M30 STORY AGAINST | %s %s | M30 bar closed against zone — skip", symbol, zone.id)
                continue

            # Zone health
            healthy = evaluate_zone_health(zone, h1_short, m5 or [], h1_atr)
            if not healthy:
                continue

            # Check for opposing opportunity
            opp = find_opposing_opportunity(zone, m5 or [], pip_calc, broker_cost.spread_pips,
                                             pool, h1_atr, zone_buf, min_rr)

            # M1 entry signal
            m1 = self._md.get_candles(symbol, Timeframe.M1, config.M1_CANDLE_COUNT)
            current_m5_candle = m5[-1] if m5 else None

            signal = find_m1_predictive_signal(m1 or [], zone, pip_calc, broker_cost.spread_pips,
                                                pool, h1_atr) if m1 else None
            if signal is None:
                signal = find_m5_choch_signal(m5 or [], zone, pip_calc, broker_cost.spread_pips,
                                              pool, h1_atr, zone_buffer=zone_buf, min_rr_override=min_rr)
            if signal is None:
                continue

            # SL minimum pips check
            sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
            if not check_min_sl_pips(symbol, sl_pips):
                logger.debug("REJECTED | %s | SL=%.1f pips below minimum", symbol, sl_pips)
                continue

            # HTF alignment for trade scoring
            htf_aligned      = h4_bias == zone.direction
            precision_zone   = find_m5_precision_entry(m5 or [], zone, pip_calc)
            liquidity_sweep  = pool is not None and (
                (zone.direction == Direction.BULLISH and pool.price > signal.entry_price) or
                (zone.direction == Direction.BEARISH and pool.price < signal.entry_price)
            )
            fvg_clean        = zone.zone_type == ZoneType.FAIR_VALUE_GAP and zone.touch_count == 0
            strong_displacement = signal.signal_type.value in ('CHOCH', 'PREDICTIVE') and sl_pips > 5
            in_kill_zone     = is_kill_zone(now)

            trade_score = score_trade(htf_aligned, liquidity_sweep, fvg_clean,
                                       strong_displacement, in_kill_zone)
            min_ts = getattr(config, 'TRADE_SCORE_THRESHOLD', 2)
            if trade_score < min_ts:
                logger.debug("REJECTED | %s | low trade score %d < %d", symbol, trade_score, min_ts)
                continue

            risk_pct  = dynamic_risk(trade_score) * self._risk_pct
            tp1_ratio = getattr(config, 'TIERED_TP1_RATIO', 1.5)
            tp2_ratio = getattr(config, 'TIERED_TP2_RATIO', 2.0)
            tp1_price = compute_tp1_price(signal.direction, signal.entry_price, signal.stop_loss)
            tp2_price = compute_tp2_price(signal.direction, signal.entry_price, signal.stop_loss)

            logger.info(
                "M1 PREDICTIVE ENTRY | %s %s | RR=%.2f | entry=%.5f SL=%.5f (zone.mid=%.5f) | m5_story=%s precision=%s",
                symbol, signal.direction.value.upper(),
                abs(signal.take_profit - signal.entry_price) / max(0.00001, abs(signal.entry_price - signal.stop_loss)),
                signal.entry_price, signal.stop_loss, (zone.high + zone.low) / 2,
                m5_story, precision_zone is not None,
            )

            zone.mark_tapped()
            candidate = EntryCandidate(
                symbol=symbol, signal=signal, zone=zone,
                setup_score=setup_score, trade_score=trade_score, grade=grade,
                risk_pct=risk_pct, tp1_price=tp1_price, tp2_price=tp2_price,
                broker_cost=broker_cost, pip_calc=pip_calc, tick_value=tick_value,
                digits=digits, h1_atr=h1_atr, is_phase2=is_phase2,
            )
            if best_candidate is None or trade_score > best_candidate.trade_score:
                best_candidate = candidate

        return best_candidate

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

        initial_risk = get_initial_risk(signal.entry_price, signal.stop_loss)
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

        m1_bars  = self._md.get_candles(symbol, Timeframe.M1, config.M1_CANDLE_COUNT)
        m5_bars  = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
        h1_bars  = self._md.get_candles(symbol, Timeframe.H1, 20)
        zones    = self._zr.get_active_zones(symbol)
        digits   = self._md.get_symbol_digits(symbol)
        pip_calc = PipCalculator(digits=digits)
        h1_atr   = atr(h1_bars, 14) if h1_bars else 0.0

        for pos in open_positions:
            if pos.symbol != symbol:
                continue
            ticket = int(pos.mt5_ticket or pos.id)
            state  = self._trade_states.get(ticket)
            if state is None:
                state = _OpenTradeState(
                    symbol=symbol, direction=pos.direction,
                    entry=pos.entry_price, original_sl=pos.stop_loss,
                    current_sl=pos.stop_loss, take_profit=pos.take_profit,
                    tp1_price=compute_tp1_price(pos.direction, pos.entry_price, pos.stop_loss),
                    tp2_price=compute_tp2_price(pos.direction, pos.entry_price, pos.stop_loss),
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

            # Update M5 window
            if m5_bars:
                latest_m5 = m5_bars[-1]
                if state.last_m5_time != latest_m5.time:
                    state.m5_window.append(latest_m5)
                    if len(state.m5_window) > 10:
                        state.m5_window.pop(0)
                    state.last_m5_time = latest_m5.time

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
            tp2_reached = False
            if state.tp1_hit and not state.tp2_hit:
                if state.direction == Direction.BULLISH and price >= state.tp2_price:
                    tp2_reached = True
                elif state.direction == Direction.BEARISH and price <= state.tp2_price:
                    tp2_reached = True

            if tp2_reached:
                state.tp2_hit = True

            # Runner SL protection after TP1
            if state.tp1_hit and not state.tp2_hit and m5_bars:
                atr_buf = h1_atr * getattr(config, 'MONITOR_TRAIL_ATR_BUFFER', 0.5)
                runner_sl = find_runner_protection_sl(m5_bars, state.direction, state.entry, pip_calc, atr_buf)
                if runner_sl is not None:
                    if ((state.direction == Direction.BULLISH and runner_sl > state.current_sl) or
                            (state.direction == Direction.BEARISH and runner_sl < state.current_sl)):
                        if self._tr.modify_sl(ticket, runner_sl):
                            state.current_sl = runner_sl
                            state.sl_trailed = True

            # Monitor events (reverse ChoCh, stall)
            monitor = TradeMonitor(state.direction, state.entry, state.current_sl,
                                    state.take_profit, pip_calc, h1_atr)
            mid_sl = (state.entry + state.current_sl) / 2

            events: List[MonitorEvent] = []
            if m1_bars and m5_bars:
                events = monitor.check_live_dual(m1_bars, m5_bars, price, state.mfe_price)
            elif m5_bars:
                events = monitor.update(m5_bars, price, state.mfe_price, zones)

            state.recent_signals.extend(events)
            # Trim signal history to last 60 minutes
            cutoff = now - timedelta(minutes=60)
            state.recent_signals = [e for e in state.recent_signals
                                     if hasattr(e, 'signal_type')][-20:]

            # Determine if we should close based on conviction
            conviction_needed = getattr(config, 'MONITOR_CLOSE_CONVICTION', 2)
            action = None
            should_close = False

            if events:
                reverse_signals = [e for e in state.recent_signals if e.signal_type == 'reverse_choch']
                if len(reverse_signals) >= conviction_needed:
                    should_close = True
                    action = 'reverse_choch_conviction'

            # Stall check
            stall_event = None
            sl_dist = abs(state.entry - state.current_sl)
            current_profit_ratio = (
                abs(price - state.entry) / sl_dist if sl_dist > 0 else 0.0
            )
            min_profit_ratio = getattr(config, 'MIN_PROFIT_RATIO_TO_CLOSE', 0.1)
            requires_tp1 = getattr(config, 'STALL_EXIT_REQUIRES_TP1', False)
            tp1_gate_ok  = (not requires_tp1) or state.tp1_hit

            mfe_pips = pip_calc.price_to_pips(abs(state.mfe_price - state.entry))
            min_stall_mfe = getattr(config, 'MIN_STALL_MFE_PIPS', 2.0)

            # Time exit
            duration = (now - state.created_at).total_seconds() / 60
            if duration >= getattr(config, 'MAX_TRADE_DURATION_MINUTES', 600) and current_profit_ratio > 0:
                should_close = True
                action = 'time_exit'

            if should_close and getattr(config, 'MONITOR_ACT_ON_SIGNALS', True):
                direction_sign = 1 if state.direction == Direction.BULLISH else -1
                final_pips = direction_sign * pip_calc.price_to_pips(abs(price - state.entry))
                if price > state.entry and state.direction == Direction.BULLISH:
                    final_pips = pip_calc.price_to_pips(abs(price - state.entry))
                elif price < state.entry and state.direction == Direction.BEARISH:
                    final_pips = pip_calc.price_to_pips(abs(price - state.entry))
                else:
                    final_pips = -pip_calc.price_to_pips(abs(price - state.entry))
                total_pips = state.accumulated_pips + final_pips
                _outcome = 'win' if total_pips > 0 else 'loss'
                self._tr.close_trade(ticket)
                self._log_trade_summary(pos, state, pip_calc)
                self._journal_close(ticket, total_pips, _outcome)
                self._update_streak(symbol, total_pips)

    def _is_strong_trend(self, m5_window: List[Candle], direction: Direction) -> bool:
        return is_strong_trend(m5_window, direction)

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
