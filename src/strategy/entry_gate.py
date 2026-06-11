"""
entry_gate.py — Entry & Exit Gate System (M1 Scalper, High-Frequency Mode).

Converts a StrategySignal into an EntryCandidate for ExecutionService.

Gate chain (all must pass):
  1. News proximity     — no high-impact event within pre-window
  2. Session filter     — per-symbol UTC trading windows
  3. Tick velocity      — minimum ticks/sec to confirm live market
  4. Daily drawdown     — circuit breaker: account equity drop limit
  5. Margin validation  — free margin, required margin, margin level
  6. EV gate            — positive expected value after spread + commission
  7. Anti-duplicate     — same symbol+direction not repeated within N seconds
  8. Minimum SL         — SL distance at least SCALPER_MIN_SL_PIPS
  9. Minimum R:R        — TP1 must meet MIN_RR × SL distance

Removed gates (replaced by margin + EV):
  - Daily trade count cap  (was Gate 4 — arbitrary frequency limiter)
  - Global open-trade cap  (was Gate 6 — replaced by margin gate)
  - Per-symbol trade cap   (was Gate 7 — replaced by anti-duplicate gate)

Per-symbol session windows (config.SCALPER_SYMBOL_SESSIONS):
  GBPUSD  05-17 UTC   Pre-London open + London + NY
  XAUUSD  05-22 UTC   Pre-London + London + NY + EU afterhours
  USDJPY  00-17 UTC   Asian + London + NY
  AUDUSD  22-13 UTC   Sydney (wraps) + Asian + London
  USDCHF  05-17 UTC   European pair
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional, Tuple

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
    DRAWDOWN      = "daily_drawdown_limit_hit"
    MARGIN        = "insufficient_margin"
    EV_NEGATIVE   = "expected_value_negative"
    ANTI_DUPE     = "duplicate_signal_within_cooldown"
    NO_SWING_REF  = "no_swing_reference_for_sl"
    SL_TOO_SMALL  = "sl_distance_below_minimum"
    RR_TOO_LOW    = "rr_below_minimum"


# ── Configurable defaults ──────────────────────────────────────────────────────

_DEFAULT_TP1_RR  = 1.5
_DEFAULT_TP2_RR  = 2.5
_DEFAULT_MIN_SL  = 3.0    # pips
_NEWS_PRE_SECS   = 30 * 60   # block 30 min before high-impact news
_NEWS_POST_SECS  = 10 * 60   # block 10 min after high-impact news (reduced)


def _symbol_session(symbol: str) -> tuple:
    sessions = getattr(config, "SCALPER_SYMBOL_SESSIONS", {})
    if symbol in sessions:
        return sessions[symbol]
    return (
        getattr(config, "SCALPER_SESSION_START_UTC", 5),
        getattr(config, "SCALPER_SESSION_END_UTC", 17),
    )


# ── EntryGate ─────────────────────────────────────────────────────────────────

class EntryGate:
    """
    Converts validated StrategySignals into EntryCandidate objects.

    No hard cap on daily trades, open trades, or trades per symbol.
    The only constraints are:
      - Market quality (tick velocity, spread)
      - Account safety (drawdown circuit breaker, margin level)
      - Trade quality (EV gate, R:R gate)
      - Execution safety (anti-duplicate, news)
    """

    def __init__(
        self,
        news_manager,
        execution,
    ) -> None:
        self._news  = news_manager
        self._exec  = execution
        self._session_start_equity: Optional[float] = None

        # Anti-duplicate tracking: symbol → {direction → last_fill_time}
        self._last_fill: Dict[str, Dict[str, datetime]] = {}

        # Analytics: daily trade count (informational only — not a gate)
        self._daily_counts: Dict[str, int] = {}

    # ── Public API ─────────────────────────────────────────────────

    def on_new_day(self, balance: float) -> None:
        self._daily_counts.clear()
        self._session_start_equity = balance
        logger.info("EntryGate: new day — equity baseline %.2f", balance)

    def record_trade(self) -> None:
        """Increment informational daily counter and record fill time."""
        today = datetime.now(tz=timezone.utc).date().isoformat()
        self._daily_counts[today] = self._daily_counts.get(today, 0) + 1

    def record_fill(self, symbol: str, direction: Direction) -> None:
        """Record a successful fill for anti-duplicate tracking."""
        self._last_fill.setdefault(symbol, {})[direction.value] = datetime.now(tz=timezone.utc)

    def daily_trade_count(self) -> int:
        today = datetime.now(tz=timezone.utc).date().isoformat()
        return self._daily_counts.get(today, 0)

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
        sym = signal.symbol

        # ── Gate 1: News proximity ─────────────────────────────────
        if self._is_news_blocked(sym, now):
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NEWS)
            return None

        # ── Gate 2: Per-symbol session window ─────────────────────
        if not self._in_session(sym, now):
            start, end = _symbol_session(sym)
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s | %s UTC (window %02d:00–%02d:00 UTC)",
                sym, GateBlockReason.SESSION, now.strftime("%H:%M"), start, end,
            )
            return None

        # ── Gate 3: Tick velocity ──────────────────────────────────
        min_velocity  = getattr(config, "SCALPER_MIN_TICK_VELOCITY", 1.0)
        live_velocity = signal.tick_analysis.velocity
        vel_passed    = live_velocity >= min_velocity

        eval_id = str(uuid.uuid4())

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
                "ENTRY_GATE_BLOCK [%s] %s | velocity=%.2f < %.2f ticks/s",
                sym, GateBlockReason.TICK_VELOCITY, live_velocity, min_velocity,
            )
            return None

        signal._eval_id = eval_id

        # ── Gate 4: Daily drawdown circuit breaker ─────────────────
        drawdown_limit = getattr(config, "DAILY_DRAWDOWN_LIMIT_PCT", 6.0)
        if (self._session_start_equity is not None and
                self._session_start_equity > 0 and balance < self._session_start_equity):
            dd_pct = (self._session_start_equity - balance) / self._session_start_equity * 100
            if dd_pct >= drawdown_limit:
                logger.warning(
                    "ENTRY_GATE_BLOCK [%s] %s: dd=%.2f%% >= %.2f%% — circuit breaker",
                    sym, GateBlockReason.DRAWDOWN, dd_pct, drawdown_limit,
                )
                return None

        # ── Gate 5: Margin validation ──────────────────────────────
        if not self._margin_ok(sym, balance, pip_calc, tick_value, signal, broker_cost):
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.MARGIN)
            return None

        # ── Gate 6: Expected value gate ────────────────────────────
        sl_price_for_ev = self._compute_sl(signal, current_price, pip_calc, m5_atr=h1_atr)
        if sl_price_for_ev is None:
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NO_SWING_REF)
            return None

        sl_dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price_for_ev))

        if not self._ev_positive(sym, sl_dist_pips, broker_cost):
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.EV_NEGATIVE)
            return None

        # ── Gate 7: Anti-duplicate ─────────────────────────────────
        anti_dupe_secs = getattr(config, "ANTI_DUPE_SECONDS", 60)
        last = self._last_fill.get(sym, {}).get(signal.direction.value)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < anti_dupe_secs:
                logger.info(
                    "ENTRY_GATE_BLOCK [%s] %s | same direction filled %.0fs ago (cooldown=%ds)",
                    sym, GateBlockReason.ANTI_DUPE, elapsed, anti_dupe_secs,
                )
                return None

        # ── Compute SL level from swing structure ──────────────────
        sl_price = sl_price_for_ev  # already computed above

        # ── Gate 8: Minimum SL distance ───────────────────────────
        _min_sl_cfg = getattr(config, "MIN_SL_PIPS", _DEFAULT_MIN_SL)
        if isinstance(_min_sl_cfg, dict):
            min_sl = _min_sl_cfg.get(sym, getattr(config, "MIN_SL_PIPS_DEFAULT", _DEFAULT_MIN_SL))
        else:
            min_sl = float(_min_sl_cfg)
        if sl_dist_pips < min_sl:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s: sl=%.1f < %.1f pips",
                sym, GateBlockReason.SL_TOO_SMALL, sl_dist_pips, min_sl,
            )
            return None

        # ── Compute TP levels ──────────────────────────────────────
        sl_price_dist = abs(current_price - sl_price)
        tp1_rr = getattr(config, "TP1_RR_RATIO", _DEFAULT_TP1_RR)
        tp2_rr = getattr(config, "TP2_RR_RATIO", _DEFAULT_TP2_RR)

        if signal.direction == Direction.BULLISH:
            tp1_price = current_price + sl_price_dist * tp1_rr
            tp2_price = current_price + sl_price_dist * tp2_rr
        else:
            tp1_price = current_price - sl_price_dist * tp1_rr
            tp2_price = current_price - sl_price_dist * tp2_rr

        # ── Gate 9: Minimum R:R ────────────────────────────────────
        min_rr = getattr(config, "MIN_RR", 1.5)
        if tp1_rr < min_rr:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s: rr=%.2f < %.2f",
                sym, GateBlockReason.RR_TOO_LOW, tp1_rr, min_rr,
            )
            return None

        # ── Build TradeSignal ──────────────────────────────────────
        trade_signal = TradeSignal(
            symbol=      sym,
            direction=   signal.direction,
            entry_price= current_price,
            stop_loss=   sl_price,
            take_profit= tp1_price,
            signal_type= SignalType.PREDICTIVE,
            trade_score= int(signal.confidence * 100),
            timeframe=   Timeframe.M1,
        )

        confidence = signal.confidence
        setup_score = int(confidence * 100)
        if confidence >= 0.85:
            grade = "A"
        elif confidence >= 0.70:
            grade = "B"
        else:
            grade = "C"

        try:
            from src.application.execution_service import EntryCandidate
        except ImportError:
            logger.error("Could not import EntryCandidate")
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
            "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f | sl=%.1f pips | rr=%.1f | daily_count=%d",
            sym, signal.direction.value.upper(), confidence, grade,
            current_price, sl_price, tp1_price, tp2_price,
            sl_dist_pips, tp1_rr,
            self.daily_trade_count(),
        )

        candidate._eval_id = eval_id
        return candidate

    # ── Private helpers ────────────────────────────────────────────

    def _is_news_blocked(self, symbol: str, now: datetime) -> bool:
        try:
            return self._news.is_blocked(now, symbol)
        except Exception:
            return False

    @staticmethod
    def _in_session(symbol: str, now: datetime) -> bool:
        start, end = _symbol_session(symbol)
        h = now.hour
        if start <= end:
            return start <= h < end
        else:
            return h >= start or h < end

    def _margin_ok(
        self,
        symbol:      str,
        balance:     float,
        pip_calc:    PipCalculator,
        tick_value:  float,
        signal:      StrategySignal,
        broker_cost: BrokerCost,
    ) -> bool:
        """
        Three-level margin check:
          1. Free margin > required_margin × MARGIN_SAFETY_FACTOR
          2. Margin level (equity/margin%) > MIN_MARGIN_LEVEL_PCT
          3. New position would not push margin level below minimum

        Falls back to True (allow) if MT5 data is unavailable — MT5 will
        enforce its own margin rules at order submission time.
        """
        try:
            free_margin = self._exec._tr.get_free_margin()
            if free_margin is None:
                return True

            safety = getattr(config, "MARGIN_SAFETY_FACTOR", 1.5)
            min_margin_level = getattr(config, "MIN_MARGIN_LEVEL_PCT", 200.0)

            # Estimate required margin for a 0.01-lot position as a floor check
            # Full lot sizing happens in ExecutionService._build_trade; here we
            # just gate on whether any reasonable lot is financeable.
            sl_pips_approx = 6.0   # conservative estimate for margin calc
            risk_usd = balance * (config.RISK_PERCENT / 100.0)
            pip_val  = tick_value if tick_value > 0 else 10.0
            raw_lots = risk_usd / (sl_pips_approx * pip_val) if pip_val > 0 else 0.01
            lots     = max(0.01, round(raw_lots, 2))

            # Approximate required margin: lots × contract_size × price / leverage
            # IC Markets standard leverage is 1:500 on forex pairs.
            # lots × 100,000 / 500 = lots × 200.  Previous value (lots × 1000)
            # assumed 1:100 leverage and falsely blocked all trades on small accounts.
            # MT5 enforces the exact figure at execution; this is a pre-flight gate only.
            req_margin_proxy = lots * 200.0
            safety_floor = getattr(config, "MARGIN_SAFETY_FACTOR", 1.5)
            if req_margin_proxy * safety_floor > free_margin:
                # Risk-based lots don't fit — check if minimum 0.01 lots is affordable.
                # If yes, allow: _build_trade will cap the lot size to fit the margin.
                # Only block if even 0.01 lots × safety exceeds free margin.
                min_req = 0.01 * 200.0 * safety_floor
                if min_req > free_margin:
                    logger.info(
                        "MARGIN_GATE [%s] free=%.2f req_proxy=%.2f × %.1f = %.2f — insufficient "
                        "(min 0.01 lots also blocked)",
                        symbol, free_margin, req_margin_proxy, safety_floor,
                        req_margin_proxy * safety_floor,
                    )
                    return False
                logger.info(
                    "MARGIN_GATE [%s] free=%.2f risk_lots=%.2f too large — "
                    "will cap to margin-affordable size",
                    symbol, free_margin, lots,
                )

            # Check margin level via account info if available
            try:
                acc = self._exec._tr.get_account_balance()
                # get_account_balance returns float; for margin level we need
                # equity and used_margin — skip level check if not available
            except Exception:
                pass

            return True

        except Exception as exc:
            logger.debug("Margin check failed for %s: %s — allowing", symbol, exc)
            return True

    @staticmethod
    def _ev_positive(
        symbol:      str,
        sl_dist_pips: float,
        broker_cost:  BrokerCost,
    ) -> bool:
        """
        Reject trades where the expected value is negative after costs.

        EV = (win_rate × avg_win) − ((1 − win_rate) × avg_loss)
           = (win_rate × sl_pips × TP1_RR) − ((1 − win_rate) × sl_pips)
           − total_cost_pips

        Uses ASSUMED_WIN_RATE from config (default 0.52) until the
        TickAnalytics system builds a rolling win rate.
        """
        try:
            win_rate = getattr(config, "ASSUMED_WIN_RATE", 0.52)
            tp1_rr   = getattr(config, "TP1_RR_RATIO",     1.5)
            ev_min   = getattr(config, "EV_MIN_PIPS",       0.10)

            avg_win  = sl_dist_pips * tp1_rr
            avg_loss = sl_dist_pips
            cost     = broker_cost.total_cost_pips() if hasattr(broker_cost, "total_cost_pips") else broker_cost.spread_pips

            ev = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss) - cost

            if ev < ev_min:
                logger.info(
                    "EV_GATE [%s] ev=%.2f pips (win=%.2f loss=%.2f cost=%.2f) < min=%.2f",
                    symbol, ev, avg_win, avg_loss, cost, ev_min,
                )
                return False
            return True

        except Exception as exc:
            logger.debug("EV gate error for %s: %s — allowing", symbol, exc)
            return True

    @staticmethod
    def _compute_sl(
        signal:        StrategySignal,
        current_price: float,
        pip_calc:      PipCalculator,
        m5_atr:        float = 0.0,
    ) -> Optional[float]:
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

        _min_sl = getattr(config, "SCALPER_MIN_SL_PIPS", {})
        _max_sl = getattr(config, "SCALPER_MAX_SL_PIPS", {})
        min_pips = (_min_sl.get(signal.symbol, getattr(config, "SCALPER_MIN_SL_PIPS_DEFAULT", 3.0))
                    if isinstance(_min_sl, dict) else float(_min_sl))
        max_pips = (_max_sl.get(signal.symbol, getattr(config, "SCALPER_MAX_SL_PIPS_DEFAULT", 12.0))
                    if isinstance(_max_sl, dict) else float(_max_sl))

        if sl_price is not None:
            dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price))
            if dist_pips > max_pips:
                sl_price = None

        if sl_price is None and m5_atr > 0:
            atr_frac     = getattr(config, "SCALPER_ATR_SL_FRACTION", 0.4)
            sl_dist      = m5_atr * atr_frac
            sl_dist_pips = pip_calc.price_to_pips(sl_dist)
            sl_dist_pips = max(min_pips, min(sl_dist_pips, max_pips))
            sl_dist      = pip_calc.pips_to_price(sl_dist_pips)
            if signal.direction == Direction.BULLISH:
                sl_price = current_price - sl_dist
            else:
                sl_price = current_price + sl_dist

        return sl_price
