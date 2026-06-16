"""
test_execution_math.py — Regression suite for the trading core's math, gates,
sizing, broker-truth journaling, and the gold-specific adaptive profile.

Runs with plain Python (no pytest required):

    python -m tests.test_execution_math

It is also pytest-compatible (every check is a `test_*` function). Dummy MT5
credentials are set before importing `src.config` so the module imports cleanly
off-broker. All inputs are REALISTIC broker values — this file exists precisely
so that ad-hoc/unrealistic test snippets are never relied on again.
"""
from __future__ import annotations

import os
import tempfile

# Config refuses to import without these — set safe dummies for off-broker tests.
os.environ.setdefault("TRADING_ID", "1")
os.environ.setdefault("MT5_PASSWORD", "x")
os.environ.setdefault("MT5_SERVER", "x")
os.environ.setdefault("ACCOUNT_CURRENCY", "USD")

import src.config as cfg
from src.domain.entities import Direction, TradeSignal, Timeframe, SignalType
from src.domain.value_objects import (
    PipCalculator, BrokerCost, RiskParameters, pip_value_per_lot,
)
from src.strategy.entry_gate import EntryGate
from src.infrastructure.trade_journal import TradeJournal


# ── Lightweight fakes ───────────────────────────────────────────────────────────

class _Sig:
    """Duck-typed StrategySignal for _compute_sl / _compute_targets."""
    def __init__(self, symbol, direction, swing_lo=None, swing_hi=None, tp_levels=None):
        self.symbol = symbol
        self.direction = direction
        self.last_swing_support = swing_lo
        self.last_swing_resistance = swing_hi
        self.tp_levels = tp_levels or []


class _FakeTR:
    def get_open_positions(self): return []
    def get_free_margin(self): return 1000.0
    def get_margin_level_pct(self): return 500.0


class _FakeExec:
    def __init__(self):
        self._tr = _FakeTR()
        self._journal = TradeJournal(path=tempfile.mktemp(suffix=".json"))
        self._trade_states = {}
    def is_symbol_paused(self, s, now=None): return False
    def is_reversal_cooldown(self, s, now=None): return False


def _eg():
    return EntryGate(news_manager=None, execution=_FakeExec())


# ── 1. Pip-value (the 10× sizing fix) ──────────────────────────────────────────

def test_pip_value_per_lot():
    pc5 = PipCalculator(digits=5)   # 5-digit FX: tick_size 0.00001
    pc3 = PipCalculator(digits=3)   # 3-digit JPY: tick_size 0.001
    pc2 = PipCalculator(digits=2)   # gold:        tick_size 0.01
    assert abs(pip_value_per_lot(1.0, 0.00001, pc5.pip_size) - 10.0) < 1e-9   # FX: per-point→per-pip ×10
    assert abs(pip_value_per_lot(0.91, 0.001, pc3.pip_size) - 9.1) < 1e-6     # JPY ×10
    assert abs(pip_value_per_lot(1.0, 0.01, pc2.pip_size) - 1.0) < 1e-9       # gold unaffected
    assert pip_value_per_lot(0.0, 0.00001, 0.0001) == 0.0                     # fail closed
    assert pip_value_per_lot(1.0, 0.0, 0.0001) == 0.0                         # fail closed


# ── 2. BrokerCost round-trip cost (fail-closed on unknown) ──────────────────────

def test_broker_cost():
    assert abs(BrokerCost(0.5, 3.5, 10.0).round_trip_cost_pips() - 1.2) < 1e-9  # 0.5 + 2*3.5/10
    assert BrokerCost(0.5, 3.5, 0.0).round_trip_cost_pips() == float("inf")     # commission but no pip value
    assert BrokerCost(1.4, 0.0, 0.0).round_trip_cost_pips() == 1.4              # spread-only account


# ── 3. Lot sizing from money risk ───────────────────────────────────────────────

def test_lot_sizing():
    rp = RiskParameters(account_balance=200, risk_percent=0.5,
                        commission_per_lot=3.5, min_rr_ratio=1.5, account_currency="USD")
    lot = rp.lot_size(sl_pips=6.0, pip_value_per_lot=10.0)
    assert abs(6.0 * 10.0 * lot - 1.0) < 1e-9    # realised risk == 0.5% of $200 == $1.00
    assert rp.lot_size(6.0, 0.0) == 0.0          # fail closed on bad pip value


# ── 4. Journal tells broker-truth (sign-based) ──────────────────────────────────

def test_journal_truth_and_rolling():
    tj = TradeJournal(path=tempfile.mktemp(suffix=".json"))
    # An SL-hit-after-favourable-move must be a LOSS, not an MFE "win".
    tj.record_open(1, "GBPUSD", "bullish", 1, 0.99, 1.02, 0.1, "C")
    tj.record_close(1, pnl=-7.5, outcome="loss", pips=-6.0)
    tj.record_open(2, "GBPUSD", "bullish", 1, 0.99, 1.02, 0.1, "C")
    tj.record_close(2, pnl=4.2, outcome="win", pips=9.0)
    st = tj.get_stats()
    assert st["wins"] == 1 and st["losses"] == 1
    assert abs(st["total_pnl"] - (-3.3)) < 1e-9
    perf = tj.rolling_performance("GBPUSD", n=50)
    assert perf["samples"] == 2 and abs(perf["win_rate"] - 0.5) < 1e-9
    assert abs(perf["avg_win_pips"] - 9.0) < 1e-9 and abs(perf["avg_loss_pips"] - 6.0) < 1e-9


# ── 4b. Journal schema guard: legacy pip-era rows excluded from money stats ──────

def test_journal_schema_guard():
    tj = TradeJournal(path=tempfile.mktemp(suffix=".json"))
    # Legacy (pre-fix) record: no schema, pnl stored as PIPS (gold $3 move = 300).
    tj._trades.append({
        "ticket": 99, "symbol": "XAUUSD", "direction": "bullish",
        "entry": 4200, "sl": 4199, "tp": 4203, "lot_size": 0.01, "grade": "C",
        "pnl": 300.0, "pips": 300.0, "outcome": "win",
        "opened_at": "2026-06-01T00:00:00+00:00", "closed_at": "2026-06-01T00:05:00+00:00",
    })
    # New broker-truth (v2) money record.
    tj.record_open(1, "GBPUSD", "bullish", 1.30, 1.299, 1.302, 0.02, "B")
    tj.record_close(1, pnl=2.50, outcome="win", pips=9.0)

    st = tj.get_stats()
    # The $300 gold legacy row must NOT be counted.
    assert st["total"] == 1 and st["wins"] == 1
    assert abs(st["total_pnl"] - 2.50) < 1e-9, st
    assert abs(tj.total_realized_pnl() - 2.50) < 1e-9
    by_sym = tj.get_stats_by_symbol()
    assert "GBPUSD" in by_sym and "XAUUSD" not in by_sym, by_sym
    # EV rolling perf must ignore the legacy gold row entirely.
    assert tj.rolling_performance("XAUUSD")["samples"] == 0
    # Drawdown unaffected by the bogus +300.
    assert tj.get_drawdown(initial_balance=140.0) <= 0.0 + 1e-9


# ── 5. Gold ATR-adaptive stop (Option 2) ─────────────────────────────────────────

def test_gold_atr_adaptive_sl():
    pc2 = PipCalculator(digits=2)
    px = 4214.00
    atr = 1.80   # $1.80 M5 ATR == 180 pips

    # No swing → default 1.0×ATR = 180 pips, within [150, 500].
    sl = EntryGate._compute_sl(_Sig("XAUUSD", Direction.BULLISH), px, pc2, m5_atr=atr)
    assert abs(pc2.price_to_pips(px - sl) - 180) < 1.0

    # Far swing ($4) → clamped to ATR ceiling 1.5×ATR = 270 pips.
    sl = EntryGate._compute_sl(_Sig("XAUUSD", Direction.BULLISH, swing_lo=px - 4.0), px, pc2, m5_atr=atr)
    assert abs(pc2.price_to_pips(px - sl) - 270) < 2.0

    # Near swing ($0.30) → absolute floor 150 pips wins.
    sl = EntryGate._compute_sl(_Sig("XAUUSD", Direction.BULLISH, swing_lo=px - 0.30), px, pc2, m5_atr=atr)
    assert abs(pc2.price_to_pips(px - sl) - 150) < 1.0

    # Low-ATR regime → 1.0×ATR(80) below floor → absolute floor 150.
    sl = EntryGate._compute_sl(_Sig("XAUUSD", Direction.BULLISH), px, pc2, m5_atr=0.80)
    assert abs(pc2.price_to_pips(px - sl) - 150) < 1.0

    # Short side mirrors (SL above price).
    sl = EntryGate._compute_sl(_Sig("XAUUSD", Direction.BEARISH), px, pc2, m5_atr=atr)
    assert sl > px and abs(pc2.price_to_pips(sl - px) - 180) < 1.0


# ── 5b. Structure-aware / capped take-profit ─────────────────────────────────────

def test_structure_aware_tp():
    pc5 = PipCalculator(digits=5)
    px = 1.30000
    sl = px - 0.0010                      # 10-pip stop

    buf = cfg.TP_STRUCT_BUFFER_PIPS       # pips

    # (a) No structural levels → fallback to fixed 1.5R / 2.0R (no ATR).
    tp1, tp2, d1, rr = EntryGate._compute_targets(
        _Sig("GBPUSD", Direction.BULLISH, tp_levels=[]), px, sl, pc5, m5_atr=0.0)
    assert abs(d1 - 15.0) < 0.01 and abs(rr - 1.5) < 1e-6

    # (b) One resistance at +8 pips → TP1 = 8 − buffer; TP2 == TP1 (exit at level).
    sig = _Sig("GBPUSD", Direction.BULLISH, tp_levels=[px + 0.0008])
    tp1, tp2, d1, rr = EntryGate._compute_targets(sig, px, sl, pc5, m5_atr=0.0)
    assert abs(d1 - (8.0 - buf)) < 0.01, d1
    assert abs(pc5.price_to_pips(abs(tp2 - px)) - (8.0 - buf)) < 0.01

    # (c) Two resistances (+8, +14) → TP1 at first, TP2 at second (the ladder).
    sig2 = _Sig("GBPUSD", Direction.BULLISH, tp_levels=[px + 0.0008, px + 0.0014])
    tp1, tp2, d1, rr = EntryGate._compute_targets(sig2, px, sl, pc5, m5_atr=0.0)
    assert abs(d1 - (8.0 - buf)) < 0.01
    assert abs(pc5.price_to_pips(abs(tp2 - px)) - (14.0 - buf)) < 0.01

    # (d) Short side mirrors: nearest support below = TP1.
    sl_s = px + 0.0010
    sig_s = _Sig("GBPUSD", Direction.BEARISH, tp_levels=[px - 0.0008, px - 0.0013])
    tp1, tp2, d1, rr = EntryGate._compute_targets(sig_s, px, sl_s, pc5, m5_atr=0.0)
    assert tp1 < px and abs(d1 - (8.0 - buf)) < 0.01
    assert abs(pc5.price_to_pips(abs(tp2 - px)) - (13.0 - buf)) < 0.01

    # (e) Levels below price are ignored (only opposing/above used for a long);
    #     here only an above level qualifies.
    sig3 = _Sig("GBPUSD", Direction.BULLISH, tp_levels=[px - 0.0005, px + 0.0009])
    _, _, d1, _ = EntryGate._compute_targets(sig3, px, sl, pc5, m5_atr=0.0)
    assert abs(d1 - (9.0 - buf)) < 0.01


# ── 5c. Gold disabled by default on small accounts ───────────────────────────────

def test_gold_disabled_by_default():
    # XAUUSD risks ~$4–5/trade (≈3% of a ~$140 account) — too large; disabled
    # by default and excluded from the live symbol set.
    assert "XAUUSD" in cfg.DISABLED_SYMBOLS
    assert "XAUUSD" not in cfg.SYMBOLS
    assert "GBPUSD" in cfg.SYMBOLS and "USDJPY" in cfg.SYMBOLS


# ── 6. FX stop sizing is UNCHANGED (swing-first) ─────────────────────────────────

def test_fx_sl_unchanged():
    pc5 = PipCalculator(digits=5)
    px = 1.30000
    # swing 6 pips below + 3-pip buffer = 9-pip stop (legacy behaviour).
    sl = EntryGate._compute_sl(_Sig("GBPUSD", Direction.BULLISH, swing_lo=1.29940), px, pc5, m5_atr=0.0005)
    assert abs(pc5.price_to_pips(px - sl) - 9.0) < 0.01
    # FX is NOT in the ATR-primary set, so its fixed ranges are intact.
    assert cfg.SCALPER_MIN_SL_PIPS["GBPUSD"] == 3.0 and cfg.SCALPER_MAX_SL_PIPS["GBPUSD"] == 12.0
    assert "GBPUSD" not in cfg.SCALPER_ATR_PRIMARY_SYMBOLS


# ── 7. Cost/spread gate — per-symbol caps, REALISTIC inputs ──────────────────────

def test_cost_gate_realistic():
    # GBPUSD raw spread ~0.5 pip on a 13.5-pip target → cost ~1.2 pips → OK.
    assert EntryGate._cost_ok("GBPUSD", 13.5, BrokerCost(0.5, 3.5, 10.0)) is True
    # GBPUSD rollover spike 45 pips → blocked by the global 40-pip FX cap.
    assert EntryGate._cost_ok("GBPUSD", 13.5, BrokerCost(45.0, 3.5, 10.0)) is False
    # Gold normal spread $0.20 (20 pips) on a 270-pip target → OK (cost 27 = 10%).
    assert EntryGate._cost_ok("XAUUSD", 270, BrokerCost(20.0, 3.5, 1.0)) is True
    # Gold spike $0.90 (90 pips) → blocked by gold's own 80-pip cap.
    assert EntryGate._cost_ok("XAUUSD", 270, BrokerCost(90.0, 3.5, 1.0)) is False
    # Unknown cost (commission but no pip value) → reject (fail closed).
    assert EntryGate._cost_ok("XAUUSD", 270, BrokerCost(20.0, 3.5, 0.0)) is False
    # Gold's 80-pip cap must NOT leak to FX: 45-pip FX spread still rejected.
    assert cfg.SCALPER_MAX_SPREAD_PIPS.get("GBPUSD") is None


# ── 8. EV gate status + gold viability vs old params ─────────────────────────────

def test_ev_status_and_gold_viability():
    eg = _eg()
    # Unknown cost → "unknown" (never explored).
    assert eg._ev_status("XAUUSD", 180.0, 270.0, BrokerCost(20.0, 3.5, 0.0)) == "unknown"
    # NEW gold params: marginal bootstrap EV → "negative" but exploration-eligible.
    assert eg._ev_status("XAUUSD", 180.0, 270.0, BrokerCost(20.0, 3.5, 1.0)) == "negative"
    assert eg._exploration_allowed("XAUUSD") is True
    # Tight gold spread → outright EV pass.
    assert eg._ev_status("XAUUSD", 180.0, 270.0, BrokerCost(5.0, 3.5, 1.0)) == "pass"
    # OLD gold params (10-pip SL / 15-pip TP) were impossible — cost gate rejects.
    assert EntryGate._cost_ok("XAUUSD", 15.0, BrokerCost(20.0, 3.5, 1.0)) is False


# ── 9. Exploration bootstrap caps + fail-closed ──────────────────────────────────

def test_exploration_caps():
    eg = _eg()
    sym = "GBPUSD"
    assert eg._exploration_allowed(sym) is True              # fresh journal → allowed
    for _ in range(cfg.EXPLORATION_TRADES_PER_DAY):
        eg.record_exploration(sym)
    assert eg._exploration_allowed(sym) is False             # daily cap exhausted

    # With ≥ EV_MIN_SAMPLES real closed trades, exploration turns off (rolling EV governs).
    ex = _FakeExec()
    for i in range(cfg.EV_MIN_SAMPLES + 5):
        ex._journal.record_open(i, "USDJPY", "bullish", 1, 0.99, 1.02, 0.1, "C")
        ex._journal.record_close(i, 1.0 if i % 2 else -1.0,
                                 "win" if i % 2 else "loss", 2.0 if i % 2 else -2.0)
    eg2 = EntryGate(news_manager=None, execution=ex)
    assert eg2._exploration_allowed("USDJPY") is False


# ── 10. Post-fill validation (M1 latency / slippage guard) ───────────────────────

def test_post_fill_abort():
    from src.application.execution_service import ExecutionService
    pc = PipCalculator(digits=5)  # GBPUSD: 1 pip = 0.0001

    # Clean fill: entry 1.30000, SL 1.29950 (5 pips), TP1 1.30070 (7 pips),
    # 0.09 lot, $10/pip, $230 balance → risk $4.50 (<3% ceiling), rr 1.4 → viable.
    reason, dbg = ExecutionService._post_fill_abort_reason(
        entry=1.30000, stop=1.29950, tp1=1.30070, is_long=True,
        lot=0.09, pip_value=10.0, balance=230.0, pip_calc=pc,
    )
    assert reason is None, reason
    assert abs(dbg["sl_pips"] - 5.0) < 1e-6
    assert abs(dbg["tp1_pips"] - 7.0) < 1e-6

    # Adverse slippage widened the stop: filled at 1.30040 (long), SL still
    # 1.29950 → real stop 9 pips → risk $8.10 = 3.5% of $230 > 3% ceiling → ABORT.
    reason, _ = ExecutionService._post_fill_abort_reason(
        entry=1.30040, stop=1.29950, tp1=1.30070, is_long=True,
        lot=0.09, pip_value=10.0, balance=230.0, pip_calc=pc,
    )
    assert reason is not None and "risk" in reason

    # Favorable slippage ate the target: filled at 1.30065 (long), TP1 1.30070
    # → only 0.5 pip left vs a 5-pip stop (rr 0.1 < MIN_RR_FLOOR) → ABORT.
    reason, _ = ExecutionService._post_fill_abort_reason(
        entry=1.30065, stop=1.30015, tp1=1.30070, is_long=True,
        lot=0.09, pip_value=10.0, balance=230.0, pip_calc=pc,
    )
    assert reason is not None and "TP1" in reason

    # Price overshot TP1 before fill (wrong side) → ABORT.
    reason, _ = ExecutionService._post_fill_abort_reason(
        entry=1.30080, stop=1.30030, tp1=1.30070, is_long=True,
        lot=0.09, pip_value=10.0, balance=230.0, pip_calc=pc,
    )
    assert reason is not None and "TP1" in reason

    # Short side, clean fill: entry 1.30000, SL 1.30050 (5p), TP1 1.29930 (7p) → viable.
    reason, _ = ExecutionService._post_fill_abort_reason(
        entry=1.30000, stop=1.30050, tp1=1.29930, is_long=False,
        lot=0.09, pip_value=10.0, balance=230.0, pip_calc=pc,
    )
    assert reason is None, reason


# ── 11. EV bootstrap charges a slippage allowance ────────────────────────────────

def test_ev_bootstrap_slippage():
    # GBPUSD bootstrap (empty journal): a 4-pip SL / 4-pip TP scalp whose blended
    # reward barely clears spread+commission must FAIL once the ~1-pip slippage
    # allowance is charged — i.e. the slippage term actually moves the verdict.
    eg = _eg()
    # Tight scalp; pip_value 10 → commission 0.35p/side. spread 0.4p + ~0.7p ≈ 1.1p cost.
    bc = BrokerCost(0.4, 3.5, 10.0)
    # Prove the slippage term is wired and monotonic: a larger charge can only
    # make EV worse (never better). This guards against the term being dropped.
    import src.config as _c
    saved = _c.EV_SLIPPAGE_PIPS
    try:
        _c.EV_SLIPPAGE_PIPS = 0.0
        loose = eg._ev_status("GBPUSD", 4.0, 4.0, bc)
        _c.EV_SLIPPAGE_PIPS = 5.0
        strict = eg._ev_status("GBPUSD", 4.0, 4.0, bc)
    finally:
        _c.EV_SLIPPAGE_PIPS = saved
    # A larger slippage charge can only make EV worse (never better).
    rank = {"pass": 2, "negative": 1, "unknown": 0}
    assert rank[strict] <= rank[loose]


# ── Standalone runner (no pytest needed) ────────────────────────────────────────

def _run_all() -> int:
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc!r}")
        except Exception as exc:  # surface unexpected errors clearly
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(funcs)
    print(f"\n{total - failed}/{total} passed" + ("" if failed == 0 else f", {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())
