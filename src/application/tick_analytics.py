"""
tick_analytics.py — Tick Velocity Monitoring & Parameter Tuning Analytics.

Purpose
-------
Collects every tick-velocity evaluation the entry gate performs and links
each observation to the final trade outcome (win / loss / blocked).

This allows fully automated, data-driven decisions about whether the
SCALPER_MIN_TICK_VELOCITY threshold should be raised, lowered, or left
unchanged — without requiring any human observation of live logs.

Data flow
---------
1. EntryGate.evaluate() calls TickAnalytics.record_evaluation() on every
   signal that reaches the velocity check (after session gate passes).

2. ExecutionService calls TickAnalytics.record_outcome() when a trade
   closes, linking the outcome back to the original evaluation record.

3. A background Scheduler job calls TickAnalytics.run_hourly() /
   run_daily() / run_weekly() to emit structured summaries and
   auto-recommendations to a dedicated log file.

Output files  (all inside analytics/ directory)
-----------
  analytics/tick_velocity_raw.jsonl      — one JSON line per evaluation
  analytics/tick_velocity_hourly.jsonl   — hourly aggregates
  analytics/tick_velocity_daily.jsonl    — daily aggregates
  analytics/tick_velocity_weekly.jsonl   — weekly aggregates + recommendations
  analytics/tick_velocity_report.txt     — human-readable latest report

Recommendation logic
--------------------
The system compares win rates and profitability (avg pips) across three
velocity bands for executed trades:

  LOW    : velocity < threshold × 0.75
  BORDER : threshold × 0.75 ≤ velocity < threshold × 1.25
  HIGH   : velocity ≥ threshold × 1.25

Decision rules (all applied independently, reported together):

  RAISE threshold if:
    - Win rate in BORDER band < overall win rate by > 10pp  AND
    - Sample size ≥ 20 executed trades in that band

  LOWER threshold if:
    - Blocked count exceeds passed count by > 3× AND
    - Win rate of HIGH-band executed trades > 60% (i.e., filter would
      have been more lenient and quality would have held)

  KEEP threshold if:
    - Win rate in BORDER band ≥ overall win rate, OR
    - Sample size < 20 (insufficient data — defer)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import src.config as config

logger = logging.getLogger(__name__)

# ── Directory setup ────────────────────────────────────────────────────────────
# The analytics directory is created by override_start.sh before Wine Python
# launches, so it already exists with correct Linux permissions when this
# module loads.  override_start.sh creates /bot/analytics (and .checkpoints,
# logs) as root in the Linux layer — Wine Python then writes into it via the
# Z: drive mapping (Z:\bot\analytics) without needing to mkdir itself.
_ANALYTICS_DIR = os.environ.get("BOT_ANALYTICS_DIR", "/bot/analytics")

_RAW_FILE     = os.path.join(_ANALYTICS_DIR, "tick_velocity_raw.jsonl")
_HOURLY_FILE  = os.path.join(_ANALYTICS_DIR, "tick_velocity_hourly.jsonl")
_DAILY_FILE   = os.path.join(_ANALYTICS_DIR, "tick_velocity_daily.jsonl")
_WEEKLY_FILE  = os.path.join(_ANALYTICS_DIR, "tick_velocity_weekly.jsonl")
_REPORT_FILE  = os.path.join(_ANALYTICS_DIR, "tick_velocity_report.txt")


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class VelocityEval:
    """One evaluation record — created at gate evaluation time."""
    eval_id:        str          # unique ID linking eval to outcome
    timestamp:      str          # ISO UTC
    symbol:         str
    velocity:       float        # ticks/sec at evaluation time
    threshold:      float        # SCALPER_MIN_TICK_VELOCITY at that moment
    spread_pips:    float        # current spread in pips
    passed_velocity: bool        # True = velocity gate passed
    gate_block_reason: str       # "velocity" | "session" | "passed" | other
    trade_executed: bool = False # updated when ExecutionService places order
    outcome:        str  = ""    # "win" | "loss" | "breakeven" | "" (pending)
    pips:           float = 0.0  # final pips (+ win, − loss) — filled on close
    session_hour:   int   = 0    # UTC hour at eval time (for session analysis)


@dataclass
class BandStats:
    """Aggregated stats for a velocity band."""
    band:         str    # "LOW" | "BORDER" | "HIGH" | "BLOCKED"
    count:        int = 0
    executed:     int = 0
    wins:         int = 0
    losses:       int = 0
    total_pips:   float = 0.0

    @property
    def win_rate(self) -> float:
        resolved = self.wins + self.losses
        return self.wins / resolved if resolved > 0 else 0.0

    @property
    def avg_pips(self) -> float:
        resolved = self.wins + self.losses
        return self.total_pips / resolved if resolved > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "band": self.band,
            "evaluations": self.count,
            "executed": self.executed,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate * 100, 1),
            "avg_pips": round(self.avg_pips, 2),
            "total_pips": round(self.total_pips, 2),
        }


# ── TickAnalytics ──────────────────────────────────────────────────────────────

class TickAnalytics:
    """
    Thread-safe analytics engine.  One global instance shared across
    EntryGate and ExecutionService.

    All public methods are safe to call from the main event loop.
    File I/O is done inside a threading.Lock to prevent partial writes.
    """

    def __init__(self) -> None:
        # Check master kill-switch first
        if not getattr(config, "TICK_ANALYTICS_ENABLED", True):
            logger.info("TickAnalytics: disabled via TICK_ANALYTICS_ENABLED=false")
            self._disabled = True
        elif not os.path.isdir(_ANALYTICS_DIR):
            logger.warning(
                "TickAnalytics: directory does not exist: %s — analytics will be disabled. "
                "Fix: add './analytics:/bot/analytics' bind mount in docker-compose.yml "
                "and run 'mkdir -p analytics' on the host before starting the container.",
                _ANALYTICS_DIR,
            )
            self._disabled = True
        else:
            self._disabled = False

        # Separate flag: in-memory recording still runs, but nothing is written to disk
        self._save_disabled = not getattr(config, "TICK_ANALYTICS_SAVE", True)
        self._lock      = threading.Lock()
        self._pending:  Dict[str, VelocityEval] = {}   # eval_id → eval (awaiting outcome)
        self._hourly:   List[VelocityEval]       = []   # buffer for current hour
        self._daily:    List[VelocityEval]       = []   # buffer for current day
        self._weekly:   List[VelocityEval]       = []   # buffer for current week
        self._last_hour = -1
        self._last_day  = -1
        self._last_week = -1
        if not self._disabled:
            logger.info("TickAnalytics initialised — writing to %s", _ANALYTICS_DIR)

    # ── Public API called by EntryGate ─────────────────────────────────

    def record_evaluation(
        self,
        eval_id:           str,
        symbol:            str,
        velocity:          float,
        threshold:         float,
        spread_pips:       float,
        passed_velocity:   bool,
        gate_block_reason: str,
        now:               datetime,
    ) -> None:
        """
        Record one gate evaluation.  Called by EntryGate.evaluate() every
        time a signal reaches the velocity check.
        """
        if self._disabled:
            return
        record = VelocityEval(
            eval_id=eval_id,
            timestamp=now.isoformat(),
            symbol=symbol,
            velocity=round(velocity, 3),
            threshold=threshold,
            spread_pips=round(spread_pips, 4),
            passed_velocity=passed_velocity,
            gate_block_reason=gate_block_reason,
            session_hour=now.hour,
        )
        with self._lock:
            self._write_raw(record)
            if passed_velocity:
                self._pending[eval_id] = record
            self._hourly.append(record)
            self._daily.append(record)
            self._weekly.append(record)

    def record_trade_executed(self, eval_id: str) -> None:
        """Call from EntryGate after execute_entry_candidate confirms the order."""
        if self._disabled:
            return
        with self._lock:
            rec = self._pending.get(eval_id)
            if rec:
                rec.trade_executed = True

    def record_outcome(
        self,
        eval_id: str,
        outcome: str,   # "win" | "loss" | "breakeven"
        pips:    float,
    ) -> None:
        """
        Link a trade outcome back to the original evaluation.
        Called by ExecutionService._journal_close().
        Eval_id is the ticket number cast to string.
        """
        if self._disabled:
            return
        with self._lock:
            rec = self._pending.pop(eval_id, None)
            if rec is None:
                logger.debug("TickAnalytics: outcome for unknown eval_id=%s (already flushed)", eval_id)
                return
            rec.outcome = outcome
            rec.pips    = round(pips, 2)
            self._write_raw(rec)

    # ── Scheduled report triggers ──────────────────────────────────────

    def run_hourly(self, now: datetime) -> None:
        """Call once per hour from Scheduler."""
        if self._disabled:
            return
        with self._lock:
            if not self._hourly:
                return
            records = list(self._hourly)
            self._hourly.clear()
            self._last_hour = now.hour

        summary = self._aggregate(records, now, "hourly")
        if not self._save_disabled:
            self._write_aggregate(_HOURLY_FILE, summary)
        logger.info(
            "TickAnalytics HOURLY | evals=%d passed=%d blocked=%d executed=%d%s",
            summary["total_evals"], summary["passed"], summary["blocked"],
            summary["total_executed"],
            " (save disabled)" if self._save_disabled else "",
        )

    def run_daily(self, now: datetime) -> None:
        """Call once per day from Scheduler."""
        if self._disabled:
            return
        with self._lock:
            if not self._daily:
                return
            records = list(self._daily)
            self._daily.clear()
            self._last_day = now.day

        summary = self._aggregate(records, now, "daily")
        if not self._save_disabled:
            self._write_aggregate(_DAILY_FILE, summary)
            self._write_report(summary, now)
        logger.info(
            "TickAnalytics DAILY | evals=%d passed=%d blocked=%d "
            "win_rate=%.1f%% recommendation=%s%s",
            summary["total_evals"], summary["passed"], summary["blocked"],
            summary["overall_win_rate_pct"],
            summary["recommendation"]["action"],
            " (save disabled)" if self._save_disabled else "",
        )

    def run_weekly(self, now: datetime) -> None:
        """Call once per week from Scheduler."""
        if self._disabled:
            return
        with self._lock:
            if not self._weekly:
                return
            records = list(self._weekly)
            self._weekly.clear()
            self._last_week = now.isocalendar()[1]

        summary = self._aggregate(records, now, "weekly")
        if not self._save_disabled:
            self._write_aggregate(_WEEKLY_FILE, summary)
            self._write_report(summary, now)
        logger.info(
            "TickAnalytics WEEKLY | evals=%d recommendation=%s target_threshold=%.2f%s",
            summary["total_evals"],
            summary["recommendation"]["action"],
            summary["recommendation"]["suggested_threshold"],
            " (save disabled)" if self._save_disabled else "",
        )

    # ── Aggregation core ───────────────────────────────────────────────

    def _aggregate(
        self,
        records: List[VelocityEval],
        now:     datetime,
        period:  str,
    ) -> dict:
        """
        Produce a complete analytics summary for a list of evaluation records.
        Returns a dict that is both machine-readable (JSON) and used for the
        human-readable report.
        """
        threshold = getattr(config, "SCALPER_MIN_TICK_VELOCITY", 1.5)
        low_ceiling    = threshold * 0.75
        border_ceiling = threshold * 1.25

        # ── Band assignment ────────────────────────────────────────────
        bands: Dict[str, BandStats] = {
            "BLOCKED": BandStats("BLOCKED"),
            "LOW":     BandStats("LOW"),
            "BORDER":  BandStats("BORDER"),
            "HIGH":    BandStats("HIGH"),
        }

        per_symbol: Dict[str, Dict] = defaultdict(
            lambda: {"evals": 0, "passed": 0, "blocked": 0, "executed": 0,
                     "wins": 0, "losses": 0, "total_pips": 0.0}
        )
        velocity_buckets: Dict[str, int] = defaultdict(int)  # "0.0-0.5" → count

        for rec in records:
            sym_stats = per_symbol[rec.symbol]
            sym_stats["evals"] += 1

            # Velocity histogram bucket (0.5 pip width)
            bucket_floor = int(rec.velocity * 2) / 2.0
            bucket_key   = f"{bucket_floor:.1f}-{bucket_floor + 0.5:.1f}"
            velocity_buckets[bucket_key] += 1

            if not rec.passed_velocity:
                bands["BLOCKED"].count += 1
                sym_stats["blocked"] += 1
                continue

            sym_stats["passed"] += 1

            # Assign to velocity band
            if rec.velocity < low_ceiling:
                band = bands["LOW"]
            elif rec.velocity < border_ceiling:
                band = bands["BORDER"]
            else:
                band = bands["HIGH"]

            band.count += 1

            if rec.trade_executed:
                band.executed += 1
                sym_stats["executed"] += 1
                if rec.outcome == "win":
                    band.wins      += 1
                    band.total_pips += rec.pips
                    sym_stats["wins"]       += 1
                    sym_stats["total_pips"] += rec.pips
                elif rec.outcome == "loss":
                    band.losses     += 1
                    band.total_pips += rec.pips   # rec.pips is negative for losses
                    sym_stats["losses"]      += 1
                    sym_stats["total_pips"]  += rec.pips

        # ── Overall stats ──────────────────────────────────────────────
        total_evals    = len(records)
        total_blocked  = bands["BLOCKED"].count
        total_passed   = total_evals - total_blocked
        total_executed = sum(b.executed for b in bands.values() if b.band != "BLOCKED")
        total_wins     = sum(b.wins    for b in bands.values() if b.band != "BLOCKED")
        total_losses   = sum(b.losses  for b in bands.values() if b.band != "BLOCKED")
        total_pips     = sum(b.total_pips for b in bands.values() if b.band != "BLOCKED")
        overall_wr     = total_wins / (total_wins + total_losses) if (total_wins + total_losses) > 0 else 0.0

        # ── Hourly distribution ────────────────────────────────────────
        hourly_dist: Dict[str, int] = defaultdict(int)
        for rec in records:
            hourly_dist[f"{rec.session_hour:02d}h"] += 1

        # ── Recommendation engine ──────────────────────────────────────
        recommendation = self._recommend(
            bands=bands,
            overall_wr=overall_wr,
            total_blocked=total_blocked,
            total_passed=total_passed,
            threshold=threshold,
        )

        return {
            "period":           period,
            "generated_at":     now.isoformat(),
            "current_threshold": threshold,
            "total_evals":      total_evals,
            "passed":           total_passed,
            "blocked":          total_blocked,
            "block_rate_pct":   round(total_blocked / total_evals * 100, 1) if total_evals else 0,
            "total_executed":   total_executed,
            "total_wins":       total_wins,
            "total_losses":     total_losses,
            "total_pips":       round(total_pips, 2),
            "overall_win_rate_pct": round(overall_wr * 100, 1),
            "bands":            {k: v.to_dict() for k, v in bands.items()},
            "per_symbol":       dict(per_symbol),
            "velocity_histogram": dict(sorted(velocity_buckets.items())),
            "hourly_distribution": dict(sorted(hourly_dist.items())),
            "recommendation":   recommendation,
        }

    # ── Recommendation logic ───────────────────────────────────────────

    @staticmethod
    def _recommend(
        bands:         Dict[str, BandStats],
        overall_wr:    float,
        total_blocked: int,
        total_passed:  int,
        threshold:     float,
    ) -> dict:
        """
        Apply decision rules and return a structured recommendation.
        Always returns a dict with: action, suggested_threshold, confidence,
        reasons (list of strings).
        """
        border = bands["BORDER"]
        low    = bands["LOW"]
        high   = bands["HIGH"]

        MIN_SAMPLE   = 20    # minimum executed trades before making a call
        WIN_RATE_GAP = 0.10  # 10 percentage points
        BLOCK_RATIO  = 3.0   # blocked must be 3× passed before suggesting lower

        reasons:    List[str] = []
        raise_score = 0
        lower_score = 0

        # ── Rule 1: BORDER band underperforms overall ──────────────────
        border_resolved = border.wins + border.losses
        if border_resolved >= MIN_SAMPLE:
            gap = overall_wr - border.win_rate
            if gap > WIN_RATE_GAP:
                raise_score += 2
                reasons.append(
                    f"BORDER band win rate ({border.win_rate*100:.1f}%) is "
                    f"{gap*100:.1f}pp below overall ({overall_wr*100:.1f}%) "
                    f"over {border_resolved} trades — signals near the threshold "
                    f"are underperforming. Consider raising threshold."
                )
            elif gap < -WIN_RATE_GAP:
                lower_score += 1
                reasons.append(
                    f"BORDER band win rate ({border.win_rate*100:.1f}%) is "
                    f"{abs(gap)*100:.1f}pp ABOVE overall ({overall_wr*100:.1f}%) "
                    f"— some good trades live near the threshold."
                )
        else:
            reasons.append(
                f"BORDER band has only {border_resolved} resolved trades "
                f"(need {MIN_SAMPLE}) — deferring RAISE/LOWER decision."
            )

        # ── Rule 2: HIGH proportion of blocks with good HIGH-band perf ─
        if total_blocked > total_passed * BLOCK_RATIO:
            high_resolved = high.wins + high.losses
            if high_resolved >= MIN_SAMPLE and high.win_rate >= 0.60:
                lower_score += 2
                reasons.append(
                    f"Blocking {total_blocked} vs {total_passed} passing evaluations "
                    f"(ratio {total_blocked/max(total_passed,1):.1f}×) while HIGH-band "
                    f"win rate is {high.win_rate*100:.1f}% — threshold may be cutting "
                    f"too many opportunities. Consider lowering threshold."
                )
            elif total_blocked > total_passed * BLOCK_RATIO:
                reasons.append(
                    f"High block ratio ({total_blocked} blocked vs {total_passed} passed) "
                    f"but insufficient HIGH-band sample ({high.wins+high.losses} trades) "
                    f"to confirm lowering is safe."
                )

        # ── Rule 3: LOW band (below threshold) has negative avg pips ──
        low_resolved = low.wins + low.losses
        if low_resolved >= MIN_SAMPLE and low.avg_pips < -1.0:
            raise_score += 1
            reasons.append(
                f"LOW-velocity band (< {threshold*0.75:.2f} t/s) averaged "
                f"{low.avg_pips:.2f} pips over {low_resolved} trades — "
                f"thin-market entries are net negative."
            )

        # ── Rule 4: Insufficient data overall ─────────────────────────
        total_resolved = sum(
            b.wins + b.losses for b in bands.values() if b.band != "BLOCKED"
        )
        if total_resolved < MIN_SAMPLE:
            return {
                "action":             "INSUFFICIENT_DATA",
                "suggested_threshold": threshold,
                "confidence":         "LOW",
                "min_sample_needed":  MIN_SAMPLE,
                "current_sample":     total_resolved,
                "reasons":            [
                    f"Only {total_resolved} resolved trades — need {MIN_SAMPLE} "
                    f"before making a threshold recommendation. Keep collecting data."
                ],
            }

        # ── Final decision ─────────────────────────────────────────────
        if raise_score > lower_score and raise_score >= 2:
            suggested = round(min(threshold + 0.5, 3.0), 1)
            action, confidence = "RAISE", "MEDIUM" if raise_score == 2 else "HIGH"
        elif lower_score > raise_score and lower_score >= 2:
            suggested = round(max(threshold - 0.5, 0.5), 1)
            action, confidence = "LOWER", "MEDIUM" if lower_score == 2 else "HIGH"
        else:
            suggested = threshold
            action, confidence = "KEEP", "MEDIUM"
            if not reasons:
                reasons.append(
                    f"BORDER band performing in line with overall. "
                    f"Current threshold {threshold} appears well-calibrated."
                )

        return {
            "action":              action,
            "suggested_threshold": suggested,
            "confidence":          confidence,
            "raise_score":         raise_score,
            "lower_score":         lower_score,
            "reasons":             reasons,
        }

    # ── File I/O ───────────────────────────────────────────────────────

    @staticmethod
    def _write_raw(record: VelocityEval) -> None:
        """Append one JSON line to the raw log. Called under lock."""
        try:
            with open(_RAW_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except Exception as exc:
            logger.warning("TickAnalytics raw write failed: %s", exc)

    @staticmethod
    def _write_aggregate(path: str, summary: dict) -> None:
        """Append one JSON line to an aggregate log."""
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception as exc:
            logger.warning("TickAnalytics aggregate write failed [%s]: %s", path, exc)

    @staticmethod
    def _write_report(summary: dict, now: datetime) -> None:
        """Overwrite the human-readable report file with the latest summary."""
        try:
            threshold = summary["current_threshold"]
            rec       = summary["recommendation"]
            bands     = summary["bands"]
            period    = summary["period"].upper()

            lines = [
                "=" * 72,
                f"  TICK VELOCITY ANALYTICS REPORT  —  {period}",
                f"  Generated : {now.strftime('%Y-%m-%d %H:%M UTC')}",
                f"  Period    : {summary['period']}",
                "=" * 72,
                "",
                "CURRENT CONFIGURATION",
                f"  Threshold (SCALPER_MIN_TICK_VELOCITY) : {threshold} ticks/sec",
                "",
                "EVALUATION SUMMARY",
                f"  Total evaluations  : {summary['total_evals']}",
                f"  Passed filter      : {summary['passed']}",
                f"  Blocked by filter  : {summary['blocked']}  ({summary['block_rate_pct']}% block rate)",
                f"  Trades executed    : {summary['total_executed']}",
                f"  Resolved (W+L)     : {summary['total_wins'] + summary['total_losses']}",
                f"  Overall win rate   : {summary['overall_win_rate_pct']}%",
                f"  Total pips P&L     : {summary['total_pips']:+.2f}",
                "",
                "PERFORMANCE BY VELOCITY BAND",
                f"  {'Band':<10} {'Vel range':<18} {'Evals':>6} {'Exec':>6} "
                f"{'Wins':>5} {'Losses':>6} {'WinRate':>8} {'AvgPips':>8}",
                "  " + "-" * 68,
            ]

            band_ranges = {
                "BLOCKED": f"< {threshold}",
                "LOW":     f"< {threshold*0.75:.2f}",
                "BORDER":  f"{threshold*0.75:.2f} – {threshold*1.25:.2f}",
                "HIGH":    f">= {threshold*1.25:.2f}",
            }
            for bname in ["HIGH", "BORDER", "LOW", "BLOCKED"]:
                b = bands[bname]
                wr_str = f"{b['win_rate_pct']:.1f}%" if b['wins'] + b['losses'] > 0 else "n/a"
                ap_str = f"{b['avg_pips']:+.2f}" if b['wins'] + b['losses'] > 0 else "n/a"
                lines.append(
                    f"  {bname:<10} {band_ranges[bname]:<18} "
                    f"{b['evaluations']:>6} {b['executed']:>6} "
                    f"{b['wins']:>5} {b['losses']:>6} "
                    f"{wr_str:>8} {ap_str:>8}"
                )

            lines += [
                "",
                "VELOCITY HISTOGRAM  (ticks/sec bucket → evaluation count)",
            ]
            for bucket, count in sorted(summary["velocity_histogram"].items()):
                bar = "#" * min(count, 40)
                lines.append(f"  {bucket:<12} {bar}  ({count})")

            lines += [
                "",
                "PERFORMANCE BY SYMBOL",
                f"  {'Symbol':<8} {'Evals':>6} {'Passed':>7} {'Exec':>6} "
                f"{'Wins':>5} {'Losses':>6} {'Pips':>8}",
                "  " + "-" * 52,
            ]
            for sym, s in sorted(summary["per_symbol"].items()):
                lines.append(
                    f"  {sym:<8} {s['evals']:>6} {s['passed']:>7} "
                    f"{s['executed']:>6} {s['wins']:>5} {s['losses']:>6} "
                    f"{s['total_pips']:>+8.2f}"
                )

            lines += [
                "",
                "HOURLY DISTRIBUTION  (UTC hour → evaluations)",
            ]
            for hour_key, count in sorted(summary["hourly_distribution"].items()):
                bar = "#" * min(count // 2, 40)
                lines.append(f"  {hour_key}  {bar}  ({count})")

            lines += [
                "",
                "=" * 72,
                "RECOMMENDATION",
                f"  Action             : {rec['action']}",
                f"  Suggested threshold: {rec['suggested_threshold']} ticks/sec",
                f"  Confidence         : {rec['confidence']}",
                "",
                "  Reasoning:",
            ]
            for reason in rec["reasons"]:
                # Word-wrap at 65 chars
                import textwrap
                wrapped = textwrap.wrap(reason, width=65)
                lines.append(f"    - {wrapped[0]}")
                for cont in wrapped[1:]:
                    lines.append(f"      {cont}")

            if rec["action"] != "KEEP" and rec["action"] != "INSUFFICIENT_DATA":
                lines += [
                    "",
                    "  TO APPLY THIS RECOMMENDATION:",
                    f"    Edit .env and set:",
                    f"    SCALPER_MIN_TICK_VELOCITY={rec['suggested_threshold']}",
                    f"    Then restart the container.",
                ]

            lines += ["", "=" * 72, ""]

            with open(_REPORT_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        except Exception as exc:
            logger.warning("TickAnalytics report write failed: %s", exc)


# ── Module-level singleton ─────────────────────────────────────────────────────

_instance: Optional[TickAnalytics] = None


def get_analytics() -> TickAnalytics:
    """Return the module-level singleton.  Creates it on first call."""
    global _instance
    if _instance is None:
        _instance = TickAnalytics()
    return _instance
