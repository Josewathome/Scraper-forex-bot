"""
edge_floors.py — Startup configuration validator.

Refuses to boot if config violates edge-first minimum standards.
Every threshold here is a floor below which the system is provably
not edge-first: either the math shows negative EV, or the setting
actively incentivises frequency over quality.

Call validate_edge_floors() once at startup before any trading begins.
It raises SystemExit(1) with a clear message if any floor is violated,
so the operator sees the problem in logs rather than silent bad behaviour.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def validate_edge_floors() -> None:
    """
    Validate that config meets all edge-first minimums.

    Raises SystemExit(1) if any floor is violated.
    Call once at startup before any trading component initialises.
    """
    import src.config as cfg

    failures: list[str] = []

    # ── Floor 1: Minimum R:R ──────────────────────────────────────────────────
    # At 55% WR and 1.5:1 R:R, EV = 0.55×1.5 − 0.45×1.0 = +0.375 (positive).
    # At 0.8:1 R:R and 55% WR, EV = 0.55×0.8 − 0.45 = −0.01 (negative).
    # Below 1.5 the system cannot be profitable at realistic win rates.
    min_rr = getattr(cfg, "MIN_RR_FLOOR", None)
    if min_rr is None or min_rr < 1.5:
        failures.append(
            f"MIN_RR_FLOOR={min_rr} — must be ≥ 1.5. "
            "Below 1.5:1 the system is negative EV at 55% WR. "
            "Set MIN_RR_FLOOR=1.5 in .env or config."
        )

    # ── Floor 2: Cost fraction cap ────────────────────────────────────────────
    # Round-trip cost (spread + commission) eating 40% of TP1 = 60% remaining.
    # At 55% WR that is borderline. At 25% the trade has real expected profit.
    cost_cap = getattr(cfg, "COST_MAX_FRACTION_OF_TARGET", None)
    if cost_cap is None or cost_cap > 0.25:
        failures.append(
            f"COST_MAX_FRACTION_OF_TARGET={cost_cap} — must be ≤ 0.25. "
            "Above 25%, cost eats too much of the target for any realistic WR. "
            "Set COST_MAX_FRACTION_OF_TARGET=0.25 in .env or config."
        )

    # ── Floor 3: Per-symbol trade cap ────────────────────────────────────────
    # More than 2 trades per symbol per day = the system is treating one symbol
    # as a frequency vehicle, not a selective retest entry system.
    max_trades = getattr(cfg, "MAX_TRADES_PER_SYMBOL", None)
    if max_trades is None or max_trades > 2:
        failures.append(
            f"MAX_TRADES_PER_SYMBOL={max_trades} — must be ≤ 2. "
            "More than 2 trades/symbol/day = frequency system, not edge-first. "
            "Set MAX_TRADES_PER_SYMBOL=2 in .env or config."
        )

    # ── Floor 4: Currency exposure cap ───────────────────────────────────────
    # 3+ correlated positions = overexposure to a single macro event.
    # At $230 account, a correlated USD move can hit 3 positions simultaneously.
    max_ccy = getattr(cfg, "MAX_CURRENCY_EXPOSURE", None)
    if max_ccy is None or max_ccy > 2:
        failures.append(
            f"MAX_CURRENCY_EXPOSURE={max_ccy} — must be ≤ 2. "
            "3+ correlated positions compound drawdown on a single macro event. "
            "Set MAX_CURRENCY_EXPOSURE=2 in .env or config."
        )

    # ── Floor 5: Tick velocity gate ───────────────────────────────────────────
    # Below 1.0 ticks/sec the market is genuinely dead and fills are unreliable.
    # (Floor relaxed 1.5→1.0: velocity is a LIQUIDITY gate, not an edge gate.
    # 1.5 rejected ~39% of in-session signals; the per-trade edge is protected by
    # MIN_RR / cost / EV floors, not by velocity. Default matches the hard floor
    # at 1.0 — genuinely dead market.)
    tick_vel = getattr(cfg, "SCALPER_MIN_TICK_VELOCITY", None)
    if tick_vel is None or tick_vel < 1.0:
        failures.append(
            f"SCALPER_MIN_TICK_VELOCITY={tick_vel} — must be ≥ 1.0. "
            "Below 1.0 ticks/sec the market is dead and fills are unreliable. "
            "Set SCALPER_MIN_TICK_VELOCITY=1.2 in .env or config."
        )

    # ── Floor 6: Exploration disabled ────────────────────────────────────────
    # Exploration trades bypass the EV gate. In live trading that is systematic
    # drain: even 3 trades/symbol/day × 4 symbols = 12 guaranteed-negative probes.
    exploration = getattr(cfg, "EXPLORATION_ENABLED", None)
    if exploration:
        failures.append(
            f"EXPLORATION_ENABLED={exploration} — must be False for live trading. "
            "Exploration trades bypass the EV gate and are a guaranteed drain. "
            "Set EXPLORATION_ENABLED=false in .env or config."
        )

    # ── Floor 7: Trade quality minimum ───────────────────────────────────────
    # TQ 0.45 admits ranging/neutral entries. At 0.60 all four quality
    # components must score above neutral on average before entry is allowed.
    tq_min = getattr(cfg, "TQ_BASE_MIN", None)
    if tq_min is None or tq_min < 0.60:
        failures.append(
            f"TQ_BASE_MIN={tq_min} — must be ≥ 0.60. "
            "Below 0.60, ranging and mid-range entries pass quality check. "
            "Set TQ_BASE_MIN=0.60 in .env or config."
        )

    if failures:
        logger.critical(
            "EDGE_FLOORS VIOLATED — system refuses to start trading. "
            "Fix the following config errors:\n%s",
            "\n".join(f"  [{i+1}] {f}" for i, f in enumerate(failures)),
        )
        print(
            "\n[EDGE_FLOORS] STARTUP BLOCKED — config violates edge-first minimums:\n"
            + "\n".join(f"  [{i+1}] {f}" for i, f in enumerate(failures))
            + "\n\nFix these before enabling live trading.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info(
        "EDGE_FLOORS OK — all %d floors pass: "
        "min_rr=%.1f cost_cap=%.2f max_trades=%d max_ccy=%d "
        "tick_vel=%.1f exploration=%s tq_min=%.2f",
        7, min_rr, cost_cap, max_trades, max_ccy, tick_vel, exploration, tq_min,
    )
