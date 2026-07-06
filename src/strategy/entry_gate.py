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
from datetime import datetime
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

        # Exploration bootstrap: per-symbol count of EV-bypassed probe trades today.
        self._exploration_counts: Dict[str, int] = {}

        # Portfolio-level capital allocation authority
        from src.application.margin_manager import MarginManager
        self._margin = MarginManager(
            trade_repo=execution._tr,
            execution_service=execution,
        )

        # UTC date of last on_new_day call — prevents double-fire within same day
        self._last_new_day_utc: Optional[str] = None

    # ── Public API ─────────────────────────────────────────────────

    def on_new_day(self, balance: float, now: datetime) -> None:
        # Guard: only execute once per calendar day (bot/MT5 time) regardless
        # of how many times main_stream calls this (one call per symbol per
        # event loop tick).
        today_utc = now.date().isoformat()
        if self._last_new_day_utc == today_utc:
            return
        self._last_new_day_utc = today_utc

        self._daily_counts.clear()
        self._exploration_counts.clear()
        # Use equity (balance + unrealized P&L) as the daily baseline so that
        # open losing positions are counted in the drawdown check, not just
        # realized balance which lags until trades close.
        equity = self._margin.get_equity(balance)
        self._session_start_equity = equity
        logger.info("EntryGate: new day — equity baseline %.2f (balance=%.2f)", equity, balance)

    def record_trade(self, now: datetime) -> None:
        """Increment informational daily counter and record fill time."""
        today = now.date().isoformat()
        self._daily_counts[today] = self._daily_counts.get(today, 0) + 1

    def record_fill(self, symbol: str, direction: Direction, now: datetime) -> None:
        """Record a successful fill for anti-duplicate tracking."""
        self._last_fill.setdefault(symbol, {})[direction.value] = now

    def daily_trade_count(self, now: datetime) -> int:
        today = now.date().isoformat()
        return self._daily_counts.get(today, 0)

    def evaluate(
        self,
        signal:        StrategySignal,
        builder:       "CandleBuilder",
        current_price: float,
        now:           datetime,
        balance:       float,
        pip_calc:      PipCalculator,
        pip_value:     float,          # per-pip money value per 1.0 lot, account ccy
        digits:        int,
        h1_atr:        float,
        broker_cost:   BrokerCost,
    ) -> Optional[object]:
        sym = signal.symbol

        # ── Fail-closed sizing precondition ────────────────────────
        # Without a valid per-pip value we cannot size the trade correctly.
        # Reject rather than guess (this is the root-cause of the old 10× bug).
        if pip_value is None or pip_value <= 0:
            logger.error(
                "ENTRY_GATE_BLOCK [%s] pip_value invalid (%s) — cannot size safely, rejecting",
                sym, pip_value,
            )
            return None

        # ── Gate 0: Loss-streak cooldown ───────────────────────────
        try:
            if self._exec.is_symbol_paused(sym, now):
                logger.info("ENTRY_GATE_BLOCK [%s] loss_streak_cooldown", sym)
                return None
        except Exception as exc:
            logger.error("Loss-streak check failed for %s: %s — rejecting (fail closed)", sym, exc)
            return None

        # ── Gate 0b: Reversal re-entry cooldown ────────────────────
        try:
            if self._exec.is_reversal_cooldown(sym, now):
                logger.info("ENTRY_GATE_BLOCK [%s] reversal_reentry_cooldown", sym)
                return None
        except Exception as exc:
            logger.error("Reversal-cooldown check failed for %s: %s — rejecting (fail closed)", sym, exc)
            return None

        # ── Gate 1: News proximity ─────────────────────────────────
        if self._is_news_blocked(sym, now):
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NEWS)
            return None

        # ── Gate 2: Per-symbol session window ─────────────────────
        if not self._in_session(sym, now):
            start, end = _symbol_session(sym)
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s | %02d:%02d UTC (window %02d:00–%02d:00 UTC)",
                sym, GateBlockReason.SESSION, now.hour, now.minute, start, end,
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
        # Use equity (balance + unrealized P&L) so open losing positions
        # are included in the drawdown calculation, not just realized balance.
        drawdown_limit = getattr(config, "DAILY_DRAWDOWN_LIMIT_PCT", 6.0)
        current_equity = self._margin.get_equity(balance)
        if (self._session_start_equity is not None and
                self._session_start_equity > 0 and
                current_equity < self._session_start_equity):
            dd_pct = (self._session_start_equity - current_equity) / self._session_start_equity * 100
            if dd_pct >= drawdown_limit:
                logger.warning(
                    "ENTRY_GATE_BLOCK [%s] %s: equity_dd=%.2f%% >= %.2f%% — circuit breaker "
                    "(equity=%.2f vs baseline=%.2f)",
                    sym, GateBlockReason.DRAWDOWN, dd_pct, drawdown_limit,
                    current_equity, self._session_start_equity,
                )
                return None

        # ── Gate 5: Portfolio-level allocation check ───────────────
        # Enforces: max open trades, per-symbol limit, soft-capacity
        # C-grade blocking, equity reserve floor, currency exposure.
        tq_score = getattr(signal, "tq_score", 0.0)
        allowed, mm_reason = self._margin.can_enter(
            symbol=sym, confidence=signal.confidence,
            tq_score=tq_score, balance=balance,
        )
        if not allowed:
            logger.info("ENTRY_GATE_BLOCK [%s] PORTFOLIO_LIMIT | %s", sym, mm_reason)
            return None

        # ── Gate 6 (old Gate 5): Margin validation (real MT5 figures) ──
        if not self._margin_ok(sym):
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.MARGIN)
            return None

        # ── Compute SL first — needed by cost, EV and R:R gates ────
        sl_price_for_ev = self._compute_sl(signal, current_price, pip_calc, m5_atr=h1_atr)
        if sl_price_for_ev is None:
            logger.info("ENTRY_GATE_BLOCK [%s] %s", sym, GateBlockReason.NO_SWING_REF)
            return None

        sl_dist_pips = pip_calc.price_to_pips(abs(current_price - sl_price_for_ev))

        # ── Structure-aware targets ────────────────────────────────
        # TP1/TP2 are the NEAREST reachable target — the minimum of {fixed RR,
        # the next opposing swing − buffer, an ATR horizon cap} — instead of a
        # blind 1.5R that frequently sits past where price actually turns
        # (day-12 data: only 2/13 hit TP1 while 6/13 reached ≥5 pips). h1_atr
        # carries the M5 ATR. The cost/EV gates below evaluate this REAL target.
        sl_price = sl_price_for_ev
        tp1_price, tp2_price, tp1_dist_pips, eff_rr = self._compute_targets(
            signal, current_price, sl_price, pip_calc, h1_atr
        )

        # ── Gate 6a: Spread / cost sanity (fail closed) ────────────
        if not self._cost_ok(sym, tp1_dist_pips, broker_cost):
            logger.info("ENTRY_GATE_BLOCK [%s] spread_cost_excessive", sym)
            return None

        # ── Gate 6b: Expected value (real cost + rolling win rate) ──
        # Status: "pass" | "negative" | "unknown".
        #   unknown  → data problem → reject (fail closed, never explored)
        #   negative → reject UNLESS a bootstrap exploration slot is available
        is_exploration = False
        ev_status = self._ev_status(sym, sl_dist_pips, tp1_dist_pips, broker_cost)
        if ev_status == "unknown":
            logger.info("ENTRY_GATE_BLOCK [%s] %s (cost/EV undeterminable)", sym, GateBlockReason.EV_NEGATIVE)
            return None
        if ev_status == "negative":
            if self._exploration_allowed(sym):
                is_exploration = True
                logger.info(
                    "EXPLORATION [%s] EV-negative but bootstrap probe slot available "
                    "(used %d/%d today) — bypassing EV gate ONLY, at minimum risk",
                    sym, self._exploration_counts.get(sym, 0),
                    getattr(config, "EXPLORATION_TRADES_PER_DAY", 8),
                )
            else:
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

        # ── Gate 8: Minimum SL distance (scalper SL floor) ────────
        _min_sl_cfg = getattr(config, "SCALPER_MIN_SL_PIPS",
                              getattr(config, "MIN_SL_PIPS", _DEFAULT_MIN_SL))
        if isinstance(_min_sl_cfg, dict):
            min_sl = _min_sl_cfg.get(sym, getattr(config, "SCALPER_MIN_SL_PIPS_DEFAULT", _DEFAULT_MIN_SL))
        else:
            min_sl = float(_min_sl_cfg)
        if sl_dist_pips < min_sl:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s: sl=%.1f < %.1f pips",
                sym, GateBlockReason.SL_TOO_SMALL, sl_dist_pips, min_sl,
            )
            return None

        # ── Gate 9: Minimum R:R on the REACHABLE (capped) target ───
        # After structure/ATR capping, require at least MIN_RR_FLOOR reward:risk
        # — a trade whose only *reachable* target is < 1R is not worth the risk.
        # (Nominal MIN_RR/TP1_RR still drive the uncapped target; this floor
        # accepts the pulled-in target when a barrier sits inside 1.5R.)
        min_rr_floor = getattr(config, "MIN_RR_FLOOR", getattr(config, "MIN_RR", 1.5))
        if eff_rr < min_rr_floor - 1e-6:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] %s: reachable_rr=%.2f < %.2f (tp1=%.1f sl=%.1f pips)",
                sym, GateBlockReason.RR_TOO_LOW, eff_rr, min_rr_floor,
                tp1_dist_pips, sl_dist_pips,
            )
            return None

        # ── Gate 9b: NET-of-cost reward:risk (the real viability test) ──
        # The target must net more profit than the trade risks AFTER paying the
        # round-trip cost. This is what rejects sub-pip structural targets that
        # look fine on gross R:R but are net-negative once spread+commission is
        # charged. See config.MIN_NET_RR_AFTER_COST.
        net_rr_floor = getattr(config, "MIN_NET_RR_AFTER_COST", 1.5)
        net_rr, net_reward_pips, cost_pips = self._net_rr_after_cost(
            tp1_dist_pips, sl_dist_pips, broker_cost,
        )
        if net_rr is None:
            logger.info("ENTRY_GATE_BLOCK [%s] net_rr undeterminable (cost) — rejecting", sym)
            return None
        if net_rr < net_rr_floor - 1e-6:
            logger.info(
                "ENTRY_GATE_BLOCK [%s] net_rr_below_minimum: net_rr=%.2f < %.2f | "
                "tp1=%.1f − cost=%.1f = net %.1f pips vs risk %.1f pips "
                "(target must net ≥ %.2f× the risk after cost)",
                sym, net_rr, net_rr_floor,
                tp1_dist_pips, cost_pips, net_reward_pips, sl_dist_pips, net_rr_floor,
            )
            return None

        # ── Build TradeSignal ──────────────────────────────────────
        # Broker take-profit is set to TP2 (final target) so the software-managed
        # TP1 partial fires FIRST and the tiered exit / trailing actually works.
        # TP2 also serves as a hard broker-side backstop if the bot stops.
        trade_signal = TradeSignal(
            symbol=      sym,
            direction=   signal.direction,
            entry_price= current_price,
            stop_loss=   sl_price,
            take_profit= tp2_price,
            signal_type= SignalType.PREDICTIVE,
            trade_score= int(signal.confidence * 100),
            timeframe=   Timeframe.M1,
        )

        confidence  = signal.confidence
        tq_score    = getattr(signal, "tq_score", 0.0)
        setup_score = int(confidence * 100)

        # Quality-weighted risk: better setups risk more, weaker setups risk less.
        # This is what the RISK_HIGH/MEDIUM/LOW_CONVICTION config values were
        # always intended for — they are now wired here via MarginManager.
        risk_pct, grade = self._margin.quality_risk_pct(confidence, tq_score)

        # Exploration probes are forced to the MINIMUM risk grade — they exist to
        # gather broker-truth samples cheaply, not to express conviction.
        if is_exploration:
            risk_pct = min(risk_pct, getattr(config, "RISK_MIN_CONVICTION", 0.5))
            grade    = "C"

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
            risk_pct=    risk_pct,
            tp1_price=   tp1_price,
            tp2_price=   tp2_price,
            broker_cost= broker_cost,
            pip_calc=    pip_calc,
            pip_value=   pip_value,
            digits=      digits,
            is_phase2=   False,
        )

        candidate._exploration = is_exploration

        logger.info(
            "ENTRY_GATE_PASS [%s] %s | conf=%.2f tq=%.2f | grade=%s risk=%.1f%% | "
            "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f | sl=%.1f pips | rr=%.2f%s | %s | daily_count=%d",
            sym, signal.direction.value.upper(), confidence, tq_score,
            grade, risk_pct,
            current_price, sl_price, tp1_price, tp2_price,
            sl_dist_pips, eff_rr,
            " (capped)" if eff_rr < getattr(config, "TP1_RR_RATIO", 1.5) - 1e-6 else "",
            "EXPLORATION" if is_exploration else "EV_OK",
            self.daily_trade_count(now=now),
        )

        candidate._eval_id = eval_id
        return candidate

    # ── Exploration bootstrap ──────────────────────────────────────

    def _exploration_allowed(self, symbol: str) -> bool:
        """
        True when the EV gate may be bypassed for a capped, minimum-risk probe to
        gather broker-truth samples. Only while the symbol has fewer than
        EV_MIN_SAMPLES closed trades AND the daily exploration cap is not reached.
        """
        if not getattr(config, "EXPLORATION_ENABLED", True):
            return False
        cap = getattr(config, "EXPLORATION_TRADES_PER_DAY", 8)
        if self._exploration_counts.get(symbol, 0) >= cap:
            return False
        try:
            journal = getattr(self._exec, "_journal", None)
            samples = 0
            if journal is not None and hasattr(journal, "rolling_performance"):
                samples = journal.rolling_performance(
                    symbol, n=getattr(config, "EV_ROLLING_WINDOW", 50)
                ).get("samples", 0)
            return samples < getattr(config, "EV_MIN_SAMPLES", 30)
        except Exception as exc:
            # If we can't determine sample count, do NOT explore (fail closed).
            logger.error("Exploration check failed for %s: %s — not exploring", symbol, exc)
            return False

    def record_exploration(self, symbol: str) -> None:
        """Increment the per-symbol daily exploration counter after a probe fills."""
        self._exploration_counts[symbol] = self._exploration_counts.get(symbol, 0) + 1

    # ── Private helpers ────────────────────────────────────────────

    def _is_news_blocked(self, symbol: str, now: datetime) -> bool:
        # NOTE: NewsManager.is_blocked(symbol, now) — argument order matters.
        # FAIL CLOSED: if the news subsystem errors, treat as blocked.
        try:
            return self._news.is_blocked(symbol, now)
        except Exception as exc:
            logger.error("News check failed for %s: %s — blocking (fail closed)", symbol, exc)
            return True

    @staticmethod
    def _in_session(symbol: str, now: datetime) -> bool:
        start, end = _symbol_session(symbol)
        # now is bot time (MT5 server time, offset-corrected via BrokerClock). Use hour directly.
        h = now.hour
        if start <= end:
            return start <= h < end
        else:
            return h >= start or h < end

    def _margin_ok(self, symbol: str) -> bool:
        """
        Real (currency-correct) account-safety check using live MT5 figures.

        Blocks a new entry when:
          1. Free margin is unreadable        → FAIL CLOSED (reject)
          2. Free margin ≤ 0                   → reject
          3. Margin level < MIN_MARGIN_LEVEL_PCT (when any margin is in use)

        The EXACT per-order margin check (required × safety ≤ free) is performed
        in ExecutionService.execute_entry_candidate() using the final lot size,
        so this gate only enforces the account-level floor. No leverage proxy.
        """
        try:
            free_margin = self._exec._tr.get_free_margin()
            if free_margin is None:
                logger.warning("MARGIN_GATE [%s] free margin unreadable — rejecting (fail closed)", symbol)
                return False
            if free_margin <= 0:
                logger.info("MARGIN_GATE [%s] free margin %.2f ≤ 0 — rejecting", symbol, free_margin)
                return False

            min_level = getattr(config, "MIN_MARGIN_LEVEL_PCT", 200.0)
            try:
                level = self._exec._tr.get_margin_level_pct()
            except Exception as exc:
                logger.warning("MARGIN_GATE [%s] margin level unreadable: %s — rejecting", symbol, exc)
                return False
            # level is None only when NO margin is in use (no open trades) — that
            # is the safest possible state, so allow.
            if level is not None and level < min_level:
                logger.info(
                    "MARGIN_GATE [%s] margin_level=%.0f%% < %.0f%% — rejecting",
                    symbol, level, min_level,
                )
                return False
            return True
        except Exception as exc:
            logger.error("Margin check failed for %s: %s — rejecting (fail closed)", symbol, exc)
            return False

    @staticmethod
    def _cost_ok(symbol: str, tp1_dist_pips: float, broker_cost: BrokerCost) -> bool:
        """
        Spread/cost sanity. Rejects when the round-trip cost is missing, insane,
        or eats too much of the TP1 target. A scalp whose cost approaches its
        target has no edge. FAIL CLOSED on bad/unknown data.
        """
        try:
            spread = broker_cost.spread_pips
            # Per-symbol absolute spread cap (gold's pips are $0.01 → naturally
            # wider), falling back to the global MAX_SPREAD_PIPS for everything else.
            _spread_caps = getattr(config, "SCALPER_MAX_SPREAD_PIPS", {})
            max_spread = (_spread_caps.get(symbol, getattr(config, "MAX_SPREAD_PIPS", 40.0))
                          if isinstance(_spread_caps, dict) else getattr(config, "MAX_SPREAD_PIPS", 40.0))
            if spread is None or spread <= 0 or spread > max_spread:
                logger.info("COST_GATE [%s] spread=%.2f pips > cap=%.1f — rejecting",
                            symbol, spread if spread is not None else -1, max_spread)
                return False

            cost = broker_cost.round_trip_cost_pips()
            if not (cost == cost) or cost == float("inf"):   # NaN or inf → unknown
                logger.info("COST_GATE [%s] round-trip cost unknown — rejecting (fail closed)", symbol)
                return False

            max_frac = getattr(config, "COST_MAX_FRACTION_OF_TARGET", 0.40)
            if tp1_dist_pips <= 0:
                logger.info("COST_GATE [%s] tp1 distance ≤ 0 — rejecting", symbol)
                return False
            if cost > tp1_dist_pips * max_frac:
                logger.info(
                    "COST_GATE [%s] cost=%.2f pips > %.0f%% of TP1 target (%.2f pips) — rejecting",
                    symbol, cost, max_frac * 100, tp1_dist_pips,
                )
                return False
            return True
        except Exception as exc:
            logger.error("Cost gate failed for %s: %s — rejecting (fail closed)", symbol, exc)
            return False

    @staticmethod
    def _net_rr_after_cost(tp1_dist_pips: float, sl_dist_pips: float, broker_cost: "BrokerCost"):
        """
        Net-of-cost reward:risk. Pure + unit-testable.

        net_reward_pips = tp1_dist_pips − round_trip_cost_pips
        net_rr          = net_reward_pips / sl_dist_pips

        Because pip_value×lot cancels, this pip ratio equals the money ratio
        (net profit if TP hit) / (risk). Returns (net_rr, net_reward_pips,
        cost_pips); net_rr is None if cost is unknown or SL distance is zero
        (caller fails closed). net_rr can be negative when cost exceeds the
        target — that is exactly the sub-pip-target case we want to reject.
        """
        try:
            cost_pips = broker_cost.round_trip_cost_pips()
        except Exception:
            return None, 0.0, float("inf")
        if cost_pips == float("inf") or not (cost_pips == cost_pips):  # inf or NaN
            return None, 0.0, float("inf")
        if sl_dist_pips <= 0:
            return None, 0.0, cost_pips
        net_reward_pips = tp1_dist_pips - cost_pips
        net_rr = net_reward_pips / sl_dist_pips
        return net_rr, net_reward_pips, cost_pips

    @staticmethod
    def _blended_win_pips(sl_dist_pips: float) -> float:
        """
        Expected win in pips that reflects the ACTUAL tiered-exit plan, not a
        naive "100% exits at TP1". Weights TP1/TP2/runner by their close
        fractions; the runner is assumed to give back to RUNNER_EXIT_RR
        (default = TP1 RR) for conservatism.

            w1 = TIERED_TP1_CLOSE_PCT
            w2 = TIERED_TP2_CLOSE_PCT × (1 − w1)        (25% of the remainder)
            runner = (1 − w1) − w2
            blended_R = w1·TP1_RR + w2·TP2_RR + runner·RUNNER_EXIT_RR
        """
        tp1_rr  = getattr(config, "TP1_RR_RATIO", 1.5)
        tp2_rr  = getattr(config, "TP2_RR_RATIO", 2.0)
        run_rr  = getattr(config, "RUNNER_EXIT_RR", tp1_rr)
        w1      = getattr(config, "TIERED_TP1_CLOSE_PCT", 0.60)
        rem     = max(0.0, 1.0 - w1)
        w2      = getattr(config, "TIERED_TP2_CLOSE_PCT", 0.25) * rem
        runner  = max(0.0, rem - w2)
        blended_rr = w1 * tp1_rr + w2 * tp2_rr + runner * run_rr
        return sl_dist_pips * blended_rr

    def _ev_status(
        self,
        symbol:        str,
        sl_dist_pips:  float,
        tp1_dist_pips: float,
        broker_cost:   BrokerCost,
    ) -> str:
        """
        Returns "pass", "negative", or "unknown".

            EV = win_rate × avg_win − (1 − win_rate) × avg_loss − round_trip_cost

        Uses broker-truth ROLLING win rate / avg win / avg loss (pips) once
        EV_MIN_SAMPLES outcomes exist; before that a HAIRCUT ASSUMED_WIN_RATE and
        the BLENDED tiered-exit win (not a naive TP1-only win). "unknown" when
        cost cannot be computed or an error occurs (caller rejects, fail closed —
        exploration never bypasses "unknown").
        """
        try:
            ev_min = getattr(config, "EV_MIN_PIPS", 0.10)
            cost   = broker_cost.round_trip_cost_pips()
            if cost == float("inf") or not (cost == cost):
                logger.info("EV_GATE [%s] cost unknown — rejecting (fail closed)", symbol)
                return "unknown"

            win_rate = getattr(config, "ASSUMED_WIN_RATE", 0.50)
            avg_win  = self._blended_win_pips(sl_dist_pips)
            avg_loss = sl_dist_pips
            source   = "bootstrap"

            perf = None
            try:
                journal = getattr(self._exec, "_journal", None)
                if journal is not None and hasattr(journal, "rolling_performance"):
                    perf = journal.rolling_performance(
                        symbol, n=getattr(config, "EV_ROLLING_WINDOW", 50),
                    )
            except Exception as exc:
                logger.debug("rolling_performance unavailable for %s: %s", symbol, exc)

            min_samples = getattr(config, "EV_MIN_SAMPLES", 30)
            if perf and perf.get("samples", 0) >= min_samples \
                    and perf.get("avg_win_pips", 0) > 0 and perf.get("avg_loss_pips", 0) > 0:
                win_rate = perf["win_rate"]
                avg_win  = perf["avg_win_pips"]
                avg_loss = perf["avg_loss_pips"]
                source   = f"rolling(n={perf['samples']})"
            else:
                win_rate = max(0.0, win_rate - getattr(config, "ASSUMED_WIN_RATE_HAIRCUT", 0.05))
                # Bootstrap only: charge a slippage allowance so the gate models the
                # REAL ~56% break-even (spread+commission alone imply ~50%, which lets
                # through trades that only win under perfect fills). Rolling broker-truth
                # avg_win/avg_loss already include realised slippage — never add it there.
                cost += getattr(config, "EV_SLIPPAGE_PIPS", 1.0)

            ev = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss) - cost
            if ev < ev_min:
                logger.info(
                    "EV_GATE [%s] ev=%.2f pips (wr=%.2f win=%.2f loss=%.2f cost=%.2f src=%s) < min=%.2f",
                    symbol, ev, win_rate, avg_win, avg_loss, cost, source, ev_min,
                )
                return "negative"
            logger.debug(
                "EV_GATE [%s] PASS ev=%.2f (wr=%.2f win=%.2f loss=%.2f cost=%.2f src=%s)",
                symbol, ev, win_rate, avg_win, avg_loss, cost, source,
            )
            return "pass"
        except Exception as exc:
            logger.error("EV gate error for %s: %s — rejecting (fail closed)", symbol, exc)
            return "unknown"

    @staticmethod
    def _compute_targets(
        signal:        StrategySignal,
        current_price: float,
        sl_price:      float,
        pip_calc:      PipCalculator,
        m5_atr:        float = 0.0,
    ):
        """
        Fully structure-driven take-profit. Returns (tp1_price, tp2_price,
        tp1_dist_pips, effective_rr).

        PRIMARY: target the actual market-structure levels carried on the signal
        (`tp_levels` — opposing swing highs/lows, nearest first). TP1 = nearest
        opposing swing − buffer (the first place price is likely to react);
        TP2 = the next swing beyond it (else exit all at TP1). These are where
        price genuinely turns, so the target is neither blindly far (1.5R past a
        wall) nor arbitrarily tight (an ATR guess).

        FALLBACK (only when no structural level exists beyond price — e.g. a
        breakout making new highs): fixed RR (TP1_RR_RATIO / TP2_RR_RATIO),
        optionally ATR-capped. Disable structure with TP_STRUCTURE_AWARE=false.

        Gate 9 (MIN_RR_FLOOR) + the EV gate still arbitrate: if the nearest real
        target is too close to be worth the risk, the trade is rejected.
        """
        is_long  = signal.direction == Direction.BULLISH
        sl_dist  = abs(current_price - sl_price)
        tp1_rr   = getattr(config, "TP1_RR_RATIO", _DEFAULT_TP1_RR)
        tp2_rr   = getattr(config, "TP2_RR_RATIO", _DEFAULT_TP2_RR)
        buf      = pip_calc.pips_to_price(getattr(config, "TP_STRUCT_BUFFER_PIPS", 1.0))

        tp1_dist = None
        tp2_dist = None

        if getattr(config, "TP_STRUCTURE_AWARE", True):
            levels = list(getattr(signal, "tp_levels", []) or [])
            # Skip structural levels that are too close to be viable after spread cost.
            # When the nearest swing is only 2–3 pips away the spread eats ≥25% of
            # the target (spread_cost_excessive). Fall back to the RR-based target
            # instead of locking in a guaranteed loser.
            min_tp1_dist = pip_calc.pips_to_price(
                getattr(config, "MIN_TP1_DISTANCE_PIPS", 4.0)
            )
            if is_long:
                # opposing levels above price, buffered, at least min_tp1_dist away
                tgts = sorted(
                    p - buf for p in levels
                    if (p - buf) - current_price >= min_tp1_dist
                )
                if tgts:
                    tp1_dist = tgts[0] - current_price
                    if len(tgts) > 1:
                        tp2_dist = tgts[1] - current_price
            else:
                tgts = sorted(
                    (p + buf for p in levels if current_price - (p + buf) >= min_tp1_dist),
                    reverse=True,
                )
                if tgts:
                    tp1_dist = current_price - tgts[0]
                    if len(tgts) > 1:
                        tp2_dist = current_price - tgts[1]

        source = "structure"
        if tp1_dist is None or tp1_dist <= 0:
            # Fallback: fixed RR, optionally ATR-capped (no structural target).
            nom1 = sl_dist * tp1_rr
            nom2 = sl_dist * tp2_rr
            cap  = (m5_atr * getattr(config, "TP1_ATR_CAP_MULT", 2.5)
                    if (m5_atr and m5_atr > 0) else float("inf"))
            tp1_dist = min(nom1, cap)
            tp2_dist = max(min(nom2, cap), tp1_dist)
            source = "fallback"
        elif tp2_dist is None or tp2_dist < tp1_dist:
            # Only one structural level → exit fully at it (no runner past the
            # only known barrier).
            tp2_dist = tp1_dist

        if is_long:
            tp1_price = current_price + tp1_dist
            tp2_price = current_price + tp2_dist
        else:
            tp1_price = current_price - tp1_dist
            tp2_price = current_price - tp2_dist

        eff_rr = (tp1_dist / sl_dist) if sl_dist > 0 else 0.0
        return tp1_price, tp2_price, pip_calc.price_to_pips(tp1_dist), eff_rr

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

        is_long   = signal.direction == Direction.BULLISH
        swing_ref = signal.last_swing_support if is_long else signal.last_swing_resistance

        _min_sl = getattr(config, "SCALPER_MIN_SL_PIPS", {})
        _max_sl = getattr(config, "SCALPER_MAX_SL_PIPS", {})
        min_pips = (_min_sl.get(signal.symbol, getattr(config, "SCALPER_MIN_SL_PIPS_DEFAULT", 3.0))
                    if isinstance(_min_sl, dict) else float(_min_sl))
        max_pips = (_max_sl.get(signal.symbol, getattr(config, "SCALPER_MAX_SL_PIPS_DEFAULT", 12.0))
                    if isinstance(_max_sl, dict) else float(_max_sl))

        # ── GOLD (ATR-primary): volatility-adaptive stop ──────────────
        # For symbols in SCALPER_ATR_PRIMARY_SYMBOLS the stop is sized from the
        # instrument's own M5 ATR (so it auto-adapts to calm vs volatile gold),
        # placed behind structure when available, then clamped to ATR bounds and
        # the absolute SCALPER_MIN/MAX_SL_PIPS guards. Other symbols skip this
        # block entirely and keep the unchanged swing-first / ATR-fallback path.
        atr_primary = signal.symbol in getattr(config, "SCALPER_ATR_PRIMARY_SYMBOLS", set())
        if atr_primary and m5_atr > 0:
            atr_floor_pips = pip_calc.price_to_pips(m5_atr * getattr(config, "SCALPER_ATR_SL_MIN_MULT", 0.7))
            atr_ceil_pips  = pip_calc.price_to_pips(m5_atr * getattr(config, "SCALPER_ATR_SL_MAX_MULT", 1.5))
            default_pips   = pip_calc.price_to_pips(m5_atr * getattr(config, "SCALPER_ATR_SL_DEFAULT_MULT", 1.0))
            if swing_ref is not None:
                swing_price  = swing_ref - buffer if is_long else swing_ref + buffer
                swing_pips   = pip_calc.price_to_pips(abs(current_price - swing_price))
                # Behind structure, but never tighter than the ATR floor (noise)
                # nor wider than the ATR ceiling (risk).
                sl_dist_pips = max(atr_floor_pips, min(swing_pips, atr_ceil_pips))
            else:
                sl_dist_pips = default_pips
            # Absolute hard guards (e.g. gold 150–500 pips = $1.50–$5.00).
            sl_dist_pips = max(min_pips, min(sl_dist_pips, max_pips))
            sl_dist      = pip_calc.pips_to_price(sl_dist_pips)
            return current_price - sl_dist if is_long else current_price + sl_dist

        # ── Standard (FX): swing-first, ATR fallback (UNCHANGED) ──────
        sl_price = None
        if swing_ref is not None:
            sl_price = swing_ref - buffer if is_long else swing_ref + buffer

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
            if is_long:
                sl_price = current_price - sl_dist
            else:
                sl_price = current_price + sl_dist

        return sl_price
