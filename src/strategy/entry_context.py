"""
entry_context.py — Autonomous entry gate context scorer.

Replaces hardcoded gate thresholds with reasoned, market-state-aware values.

The scorer ingests the full set of available signals at evaluation time and
produces an EntryContext object that:
  1. Classifies the current market state into named context flags
  2. Derives effective thresholds for GATE1/GATE5/GATE6 from that classification
  3. Records the full reasoning chain so every threshold adaptation is auditable

Design principles
─────────────────
- No magic numbers in callers. The scorer owns all threshold logic.
- Adaptation is additive/subtractive reasoning, not a single multiplier.
  Each context flag contributes a named adjustment with an explanation.
- Conservative default: start at base thresholds, relax only when evidence
  is specific and justified. Never raise thresholds in this scorer — that
  stays with config defaults.
- Full reasoning string on every evaluation so logs explain every decision.

Context flags (non-exclusive — multiple can be true simultaneously)
────────────────────────────────────────────────────────────────────
POST_BOS_LAG        M1 just had a fresh BOS; M5 EMA still pointing opposite.
                    The next 1–2 candles will have DOJI bodies because price
                    moved before the candle opened. Candle body is unreliable.

TICK_CONFIRMS       Tick flow has strong directional bias (imbalance > threshold
                    AND tick_score above a meaningful level). Tick stream is a
                    faster, more granular signal than the forming candle body.

EARLY_CANDLE        Elapsed time is < 40 % of bar length. Body has not yet had
                    time to form. Candle score at this point is a weak predictor.

BURST_MOVE          Recent burst (tick velocity spike) indicates price moved fast
                    in the prior window. The current candle opened after the burst,
                    so the body reflects the consolidation, not the move.

M5_WEAKENING        M5 is opposing M1 direction but its score is declining
                    (< 0.70). M5 resistance is fading; M1 is likely to prevail.

M5_AGREES           M5 score >= base and direction agrees with M1. Standard case
                    — no special handling needed.

RANGING_REGIME      Both M1 and M5 show no clear trend. All thresholds stay at
                    or above base — no relaxation in ranging markets.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

import src.config as config

logger = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EntryContext:
    """
    Fully reasoned gate thresholds for one evaluation.

    Callers use effective_* thresholds instead of config constants.
    The `reasoning` list records every adaptation applied.
    """
    # ── Context flags ──────────────────────────────────────────────────
    post_bos_lag:       bool = False   # M1 fresh BOS, M5 still opposing
    tick_confirms:      bool = False   # tick flow directionally strong
    early_candle:       bool = False   # < 40% of bar elapsed
    burst_move:         bool = False   # high burst in prior window
    m5_weakening:       bool = False   # M5 opposing but score < 0.70
    m5_agrees:          bool = False   # M5 aligned with M1
    ranging_regime:     bool = False   # no trend on either timeframe

    # ── Effective thresholds (what the gates should use) ───────────────
    effective_align_threshold:  float = 0.50
    effective_tick_threshold:   float = 0.20
    effective_candle_threshold: float = 0.20

    # ── Reasoning audit trail ─────────────────────────────────────────
    reasoning: List[str] = field(default_factory=list)

    def summary(self) -> str:
        flags = [
            k for k, v in {
                "POST_BOS_LAG":   self.post_bos_lag,
                "TICK_CONFIRMS":  self.tick_confirms,
                "EARLY_CANDLE":   self.early_candle,
                "BURST_MOVE":     self.burst_move,
                "M5_WEAKENING":   self.m5_weakening,
                "M5_AGREES":      self.m5_agrees,
                "RANGING":        self.ranging_regime,
            }.items() if v
        ]
        return (
            f"flags=[{','.join(flags) or 'none'}] "
            f"align≥{self.effective_align_threshold:.2f} "
            f"tick≥{self.effective_tick_threshold:.2f} "
            f"candle≥{self.effective_candle_threshold:.2f}"
        )


# ── Scorer ────────────────────────────────────────────────────────────────────

class EntryContextScorer:
    """
    Stateless scorer — instantiate once per StrategyManager, call score() on
    every evaluation.
    """

    def score(
        self,
        *,
        # M1/M5 alignment signals
        m1_score:        float,
        m1_direction:    Optional[str],   # "bullish" / "bearish" / None
        m5_score:        float,
        m5_direction:    Optional[str],   # "bullish" / "bearish" / None
        m5_slope:        float,
        m1_structure:    Optional[str],   # "STRUCTURE_BREAK" / "TRENDING" / "RANGING" etc.

        # Tick signals
        tick_score:      float,
        tick_imbalance:  float,           # 0.0–1.0; 0.5 = balanced
        tick_velocity:   float,           # ticks/sec
        tick_burst:      float,           # recent velocity spike ratio

        # Candle signals
        candle_score:    float,           # signed: + = bullish, - = bearish
        elapsed_seconds: float,
        bar_seconds:     float = 60.0,
    ) -> EntryContext:
        """
        Classify current market state and derive appropriate gate thresholds.

        Returns an EntryContext with effective thresholds and full reasoning.
        """
        ctx = EntryContext(
            effective_align_threshold  = getattr(config, "SCALPER_MIN_ALIGNMENT_SCORE", 0.55),
            effective_tick_threshold   = getattr(config, "SCALPER_MIN_TICK_SCORE",      0.50),
            effective_candle_threshold = getattr(config, "SCALPER_MIN_CANDLE_SCORE",    0.30),
        )
        reason = ctx.reasoning

        # ── Classify context flags ─────────────────────────────────────

        # Ranging: both timeframes showing no clear direction
        if m1_structure in ("RANGING", None) and abs(m5_slope) < 1e-5:
            ctx.ranging_regime = True
            reason.append(
                f"RANGING: m1_state={m1_structure} m5_slope={m5_slope:+.6f} — "
                "no trend, base thresholds enforced"
            )

        # Post-BOS lag: M1 just broke structure.
        # The DOJI candle body issue occurs regardless of M5 direction — the
        # forming candle opened after the breakout move so its body is tiny.
        # M5 direction conflict is tracked separately via M5_WEAKENING.
        m1_just_broke = m1_structure == "STRUCTURE_BREAK"
        m5_opposes_m1 = (
            m1_direction is not None
            and m5_direction is not None
            and m1_direction != m5_direction
        )
        if m1_just_broke:
            ctx.post_bos_lag = True
            reason.append(
                f"POST_BOS_LAG: M1 structure_break dir={m1_direction} — "
                "forming candle opened after the BOS move; body will be DOJI"
                + (f" (M5 still={m5_direction} — lagging)" if m5_opposes_m1 else "")
            )

        # M5 weakening: opposing but losing conviction
        if m5_opposes_m1 and m5_score < 0.70:
            ctx.m5_weakening = True
            reason.append(
                f"M5_WEAKENING: M5 opposing m1={m1_direction} but m5_score={m5_score:.2f} < 0.70 — "
                "M5 resistance fading, M1 direction likely to prevail"
            )

        # M5 agrees: aligned and above minimum
        if not m5_opposes_m1 and m5_direction is not None and m5_score >= 0.60:
            ctx.m5_agrees = True
            reason.append(
                f"M5_AGREES: m5_dir={m5_direction} score={m5_score:.2f} aligns with m1={m1_direction}"
            )

        # Tick direction confirmation
        tick_bias_strength = abs(tick_imbalance - 0.50) * 2.0   # 0=balanced, 1=fully one-sided
        tick_confirms_threshold = getattr(config, "ENTRY_TICK_CONFIRMS_MIN_SCORE",    0.25)
        tick_confirms_bias      = getattr(config, "ENTRY_TICK_CONFIRMS_MIN_BIAS",     0.20)
        if tick_score >= tick_confirms_threshold and tick_bias_strength >= tick_confirms_bias:
            ctx.tick_confirms = True
            reason.append(
                f"TICK_CONFIRMS: score={tick_score:.2f}≥{tick_confirms_threshold:.2f} "
                f"bias_strength={tick_bias_strength:.2f}≥{tick_confirms_bias:.2f} "
                f"vel={tick_velocity:.1f} — tick flow confirms direction"
            )

        # Early candle: body not yet reliable until past ~55% of bar
        # At 30s of 60s the price hasn't had full time to commit directionally.
        elapsed_fraction = elapsed_seconds / max(bar_seconds, 1)
        if elapsed_fraction < 0.55:
            ctx.early_candle = True
            reason.append(
                f"EARLY_CANDLE: elapsed={elapsed_seconds:.0f}s ({elapsed_fraction:.0%} of bar) — "
                "candle body not yet representative"
            )

        # Burst move (price moved fast recently, current candle is aftermath)
        burst_threshold = getattr(config, "ENTRY_BURST_MOVE_THRESHOLD", 0.40)
        if tick_burst >= burst_threshold:
            ctx.burst_move = True
            reason.append(
                f"BURST_MOVE: burst={tick_burst:.2f}≥{burst_threshold:.2f} — "
                "fast prior move means current candle opens mid-move"
            )

        # ── Derive effective thresholds from flags ─────────────────────

        # Ranging only blocks ALIGN relaxation — tick/candle can still ease
        # because range boundaries are valid scalp entries with tick confirmation.
        if not ctx.ranging_regime:
            self._adapt_align_threshold(ctx, m1_score, m5_score, m5_opposes_m1)
        self._adapt_tick_threshold(ctx, tick_score, tick_bias_strength, ctx.ranging_regime)
        self._adapt_candle_threshold(ctx, elapsed_fraction, tick_bias_strength)

        return ctx

    # ── Threshold adaptation helpers ──────────────────────────────────────────

    def _adapt_align_threshold(
        self,
        ctx:           EntryContext,
        m1_score:      float,
        m5_score:      float,
        m5_opposes:    bool,
    ) -> None:
        """
        Alignment threshold adaptation.

        M5_AGREES_STRONG: Both timeframes agree AND combined is high.
          Lower align further to 0.38 to make sure no near-perfect setups are
          blocked by small rounding differences near the threshold.

        M5_WEAKENING + M1 strong + tick confirms:
          M5 opposing but fading. Lower to 0.40 to let strong M1 BOS pass
          even when tick_confirms isn't present (we now have direction from M1).
        """
        # Floor: alignment never relaxes below 0.50 — a real structural bias is
        # required, adaptation only shaves rounding noise off the base threshold.
        if ctx.m5_agrees and m5_score >= 0.80 and m1_score >= 0.70:
            new_thresh = max(0.50, ctx.effective_align_threshold - 0.05)
            ctx.effective_align_threshold = new_thresh
            ctx.reasoning.append(
                f"ALIGN_RELAX: M5_AGREES_STRONG m5_score={m5_score:.2f} m1_score={m1_score:.2f} → "
                f"lower align to {new_thresh:.2f} (both TFs aligned, near-perfect setup)"
            )
        elif ctx.m5_weakening and m1_score >= 0.70:
            new_thresh = max(0.50, ctx.effective_align_threshold - 0.05)
            ctx.effective_align_threshold = new_thresh
            ctx.reasoning.append(
                f"ALIGN_RELAX: M5_WEAKENING m1_score={m1_score:.2f}≥0.70, m5_score={m5_score:.2f}<0.70 → "
                f"lower align to {new_thresh:.2f} (M5 fading; M1 structure sufficient)"
            )

    def _adapt_tick_threshold(
        self,
        ctx:              EntryContext,
        tick_score:       float,
        bias_strength:    float,
        is_ranging:       bool = False,
    ) -> None:
        """
        Tick threshold adaptation.

        POST_BOS + BURST: velocity already shown, lower tick threshold.
        M5_AGREES_STRONG: both TFs aligned, tick is confirmation not gatekeeper.
        RANGING: allow moderate relax so range-boundary scalps still work.
        """
        if ctx.post_bos_lag and ctx.burst_move and bias_strength >= 0.15:
            new_thresh = max(0.40, ctx.effective_tick_threshold - 0.05)
            ctx.effective_tick_threshold = new_thresh
            ctx.reasoning.append(
                f"TICK_RELAX: post-BOS + burst, bias={bias_strength:.2f} → "
                f"tick threshold {new_thresh:.2f} (burst confirms velocity)"
            )
        elif ctx.m5_agrees and bias_strength >= 0.10:
            new_thresh = max(0.40, ctx.effective_tick_threshold - 0.05)
            ctx.effective_tick_threshold = new_thresh
            ctx.reasoning.append(
                f"TICK_RELAX: M5_AGREES, tick is confirmation not gate → {new_thresh:.2f}"
            )
        elif is_ranging and ctx.tick_confirms:
            # Range boundary: tick confirms entry into structure extreme
            new_thresh = max(0.40, ctx.effective_tick_threshold - 0.03)
            ctx.effective_tick_threshold = new_thresh
            ctx.reasoning.append(
                f"TICK_RELAX: RANGING + TICK_CONFIRMS → {new_thresh:.2f} (range boundary scalp)"
            )

    def _adapt_candle_threshold(
        self,
        ctx:              EntryContext,
        elapsed_fraction: float,
        bias_strength:    float,
    ) -> None:
        """
        Candle threshold adaptation.

        Candle body (DOJI vs BULL/BEAR) is the weakest signal in the first half
        of a candle, especially after a BOS. We relax it progressively based on
        how many of the corroborating conditions hold.

        Each flag contributes an independent relaxation factor.
        The factors are additive — stronger combinations relax more.

        Max relaxation: 60% of base threshold (floor at 0.40 × base).
        This prevents fully ignoring candle direction even in ideal conditions.
        """
        base  = ctx.effective_candle_threshold
        relax = 0.0   # cumulative fractional reduction

        if ctx.post_bos_lag:
            relax += 0.20
            ctx.reasoning.append(
                f"CANDLE_RELAX +20%: POST_BOS_LAG — body unreliable after structure break"
            )

        if ctx.tick_confirms:
            tick_contrib = 0.15 + 0.10 * min(1.0, bias_strength / 0.40)
            relax += tick_contrib
            ctx.reasoning.append(
                f"CANDLE_RELAX +{tick_contrib:.0%}: TICK_CONFIRMS — "
                f"tick bias_strength={bias_strength:.2f} substitutes for weak body"
            )

        if ctx.early_candle:
            # Linear decay: full relaxation at 0s → zero at the 55% trigger point
            early_contrib = max(0.0, 0.10 * (1.0 - elapsed_fraction / 0.55))
            relax += early_contrib
            ctx.reasoning.append(
                f"CANDLE_RELAX +{early_contrib:.0%}: EARLY_CANDLE — "
                f"body not formed at {elapsed_fraction:.0%} of bar"
            )

        if ctx.burst_move:
            relax += 0.10
            ctx.reasoning.append(
                "CANDLE_RELAX +10%: BURST_MOVE — candle opened in aftermath of fast move"
            )

        if relax > 0:
            # Cap total relaxation at 60 % of base
            relax = min(relax, 0.50)
            new_thresh = round(base * (1.0 - relax), 3)
            ctx.effective_candle_threshold = new_thresh
            ctx.reasoning.append(
                f"CANDLE_THRESHOLD: {base:.2f} × {1.0 - relax:.2f} = {new_thresh:.3f} "
                f"(total relaxation {relax:.0%})"
            )
