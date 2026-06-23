"""
retest_state_machine.py — Per-symbol post-BOS/CHoCH retest entry gate.

Phase 3 of the edge-first migration. Instead of entering at the moment structure
breaks (chasing the move), we WAIT for price to retrace back to the broken level
(the BOS/CHoCH retest) and only then allow an entry. This produces a structurally
meaningful entry with a natural stop and — critically — a target large enough to
clear cost (the June-22 finding: M1 momentum entries had sub-pip targets).

Design constraints (deliberately small and debuggable):
  • Exactly three states: IDLE → WAIT_RETEST → ENTER (ENTER is transient, resets
    to IDLE the same bar).
  • One instance owns a per-symbol dict — no cross-symbol coupling.
  • Every transition logs a single explainable line.
  • WAIT_RETEST expires after a bounded number of bars (no zombie states).
  • Pure decision core (`_decide`) is unit-testable with plain numbers.
  • Feature-flagged by the caller; when disabled this module is never consulted.

The machine does NOT build signals or place orders. It answers one question per
M1 close: "is this bar a valid retest entry?" The caller keeps full control of
gating, sizing, and execution.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from src.domain.entities import Direction

logger = logging.getLogger(__name__)


class RetestState(enum.Enum):
    IDLE        = "IDLE"          # nothing pending; watching for a fresh BOS
    WAIT_RETEST = "WAIT_RETEST"   # BOS seen; waiting for price to return to the level
    ENTER       = "ENTER"         # retest confirmed this bar (transient)


class RetestDecision(enum.Enum):
    NO_ENTRY = "NO_ENTRY"   # caller must suppress the signal this bar
    ENTER    = "ENTER"      # caller may proceed to build/gate the signal


@dataclass
class _Pending:
    """A single armed retest setup for one symbol."""
    level:      float
    direction:  Direction
    armed_bar:  int          # bar counter when armed
    expires_at: int          # bar counter at which this setup is abandoned


class RetestStateMachine:
    """
    Owns per-symbol retest state. Call `observe()` once per M1 close per symbol.

    Parameters (all caller-supplied so config stays in one place):
      expiry_bars        — bars to wait for the retest before abandoning
      tolerance_atr      — how close to the level counts as a retest (× ATR)
      min_displacement_atr — breaking candle body must be ≥ this × ATR (filters
                             micro-BOS noise); 0 disables the displacement filter
    """

    def __init__(
        self,
        *,
        expiry_bars: int = 5,
        tolerance_atr: float = 0.5,
        min_displacement_atr: float = 1.0,
    ) -> None:
        self.expiry_bars          = int(expiry_bars)
        self.tolerance_atr        = float(tolerance_atr)
        self.min_displacement_atr = float(min_displacement_atr)
        self._pending: Dict[str, _Pending] = {}
        self._bar:     Dict[str, int]      = {}   # per-symbol monotonic bar counter

    # ── Public API ──────────────────────────────────────────────────────────
    def observe(
        self,
        *,
        symbol:            str,
        is_structure_break: bool,
        bos_level:         Optional[float],
        bos_direction:     Optional[Direction],
        current_price:     float,
        atr:               float,
        signal_direction:  Optional[Direction],
        break_body:        float = 0.0,
    ) -> RetestDecision:
        """
        Advance the machine one M1 close for `symbol` and return a decision.

        is_structure_break — structure manager reports STRUCTURE_BREAK on M1 now
        bos_level/direction — the broken level and its direction (from structure)
        current_price       — current mid price
        atr                 — M1 ATR for normalising tolerance/displacement
        signal_direction    — the alignment/consensus direction this bar (the
                              retest entry must agree with the BOS direction)
        break_body          — body size of the breaking candle (displacement test)
        """
        bar = self._bar.get(symbol, 0) + 1
        self._bar[symbol] = bar

        decision, new_pending, log = self._decide(
            symbol=symbol,
            bar=bar,
            pending=self._pending.get(symbol),
            is_structure_break=is_structure_break,
            bos_level=bos_level,
            bos_direction=bos_direction,
            current_price=current_price,
            atr=atr,
            signal_direction=signal_direction,
            break_body=break_body,
        )
        # Apply the resulting state
        if new_pending is None:
            self._pending.pop(symbol, None)
        else:
            self._pending[symbol] = new_pending
        if log:
            logger.info("RETEST [%s] %s", symbol, log)
        return decision

    def state_of(self, symbol: str) -> RetestState:
        p = self._pending.get(symbol)
        return RetestState.WAIT_RETEST if p is not None else RetestState.IDLE

    def reset(self, symbol: str) -> None:
        self._pending.pop(symbol, None)

    # ── Pure decision core (unit-testable) ──────────────────────────────────
    def _decide(
        self,
        *,
        symbol:             str,
        bar:                int,
        pending:            Optional[_Pending],
        is_structure_break: bool,
        bos_level:          Optional[float],
        bos_direction:      Optional[Direction],
        current_price:      float,
        atr:                float,
        signal_direction:   Optional[Direction],
        break_body:         float,
    ):
        """
        Returns (decision, new_pending, log_message).

        State logic:
          IDLE:
            fresh BOS (with a level, passing displacement) → arm WAIT_RETEST.
          WAIT_RETEST:
            expired                       → IDLE
            price within tolerance of the
              level AND direction agrees  → ENTER (then disarm → IDLE)
            a NEW BOS at a different level → re-arm to the newer level
            else                          → keep waiting
        """
        tol = self.tolerance_atr * atr if atr > 0 else 0.0

        # ── WAIT_RETEST branch ──────────────────────────────────────────────
        if pending is not None:
            # Re-arm if a fresh BOS prints at a meaningfully different level.
            if (is_structure_break and bos_level is not None and bos_direction is not None
                    and abs(bos_level - pending.level) > tol):
                if self._displacement_ok(break_body, atr):
                    new = _Pending(level=bos_level, direction=bos_direction,
                                   armed_bar=bar, expires_at=bar + self.expiry_bars)
                    return (RetestDecision.NO_ENTRY, new,
                            f"RE-ARM new BOS dir={bos_direction.value} level={bos_level:.5f} "
                            f"expires in {self.expiry_bars} bars")

            # Expiry.
            if bar >= pending.expires_at:
                return (RetestDecision.NO_ENTRY, None,
                        f"EXPIRED waiting for retest of {pending.level:.5f} "
                        f"(armed bar {pending.armed_bar}, {self.expiry_bars} bars elapsed) → IDLE")

            # Retest hit?
            if tol > 0 and abs(current_price - pending.level) <= tol:
                if signal_direction == pending.direction:
                    return (RetestDecision.ENTER, None,
                            f"ENTER retest confirmed dir={pending.direction.value} "
                            f"level={pending.level:.5f} price={current_price:.5f} "
                            f"(|Δ|={abs(current_price - pending.level):.5f} ≤ tol {tol:.5f})")
                # Price is back at the level but direction disagrees — let it wait;
                # a clean retest with agreeing direction may still come before expiry.
                return (RetestDecision.NO_ENTRY, pending,
                        f"retest level touched but signal_dir={signal_direction} "
                        f"≠ setup_dir={pending.direction.value} — holding")

            # Still waiting.
            return (RetestDecision.NO_ENTRY, pending, "")

        # ── IDLE branch ─────────────────────────────────────────────────────
        if is_structure_break and bos_level is not None and bos_direction is not None:
            if not self._displacement_ok(break_body, atr):
                return (RetestDecision.NO_ENTRY, None,
                        f"BOS ignored — displacement {break_body:.5f} < "
                        f"{self.min_displacement_atr}×ATR ({self.min_displacement_atr * atr:.5f})")
            new = _Pending(level=bos_level, direction=bos_direction,
                           armed_bar=bar, expires_at=bar + self.expiry_bars)
            return (RetestDecision.NO_ENTRY, new,
                    f"ARM WAIT_RETEST dir={bos_direction.value} level={bos_level:.5f} "
                    f"price={current_price:.5f} expires in {self.expiry_bars} bars")

        return (RetestDecision.NO_ENTRY, None, "")

    def _displacement_ok(self, break_body: float, atr: float) -> bool:
        """Breaking candle body must be ≥ min_displacement_atr × ATR (noise filter)."""
        if self.min_displacement_atr <= 0 or atr <= 0:
            return True
        return break_body >= self.min_displacement_atr * atr
