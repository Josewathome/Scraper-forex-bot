# Scalper Prime — Edge-First Architectural Redesign

**Status:** Design specification (not yet implemented)
**Author:** Quantitative architecture review
**Account context:** ~$230 USD / 30,000 KES, IC Markets Raw, MT5 via ZMQ bridge
**Mandate:** Transform a frequency-first experimental scalper into a professional
edge-first execution scalper. Target: 15–22 high-quality trades/day with
demonstrable positive expectancy and long-term survivability.

> This document is the single source of truth for the system's trading
> philosophy. Any change that contradicts the **Philosophy Lock** (§1) is a
> regression, regardless of how it is justified in the moment.

---

## 0. The core diagnosis (why we are redesigning, not patching)

The execution-safety fixes (post-fill SL revalidation, TP geometry check,
slippage-aware EV) were correct and are kept. But the *tuning of the decision
layer* is still frequency-first. Three structural facts drive the entire
redesign:

1. **The edge was never killed by frequency alone — it was killed by geometry.**
   Tight targets (1.5R), a 60%-at-TP1 close, and a sub-1R `MIN_RR_FLOOR=0.8`
   mean winners are capped while losers run full. No achievable win rate fixes
   bad geometry. Widening targets to *real structure* and letting runners work
   is what flips EV positive (proof in §5).

2. **Retail latency forbids tick-triggered scalping.** The infrastructure
   (Wine MT5 → ZMQ → Python → rpyc → MT5) carries 100–250 ms round-trip. The
   system's own `tick_engine.py` states tick-triggered entries are unreliable
   at this latency. Therefore the architecture must be **structure-triggered**,
   not momentum-chasing.

3. **Every adaptive mechanism in the system relaxes downward.** The
   `EntryContextScorer` can only *lower* thresholds. There is no force that
   raises the bar in poor conditions. Edge-first requires the opposite default.

---

## 1. PHILOSOPHY REDESIGN — locking edge-first permanently

### 1.1 The one question the system must answer

The decision layer's job is **not** "can we find a trade?" It is:

> **"Is this a high-quality opportunity worth risking 2% of a small account on,
> given that I will be filled late and pay ~2.2 pips of friction?"**

If the answer is not a clear yes, the answer is **no trade**. No-trade is the
default state, not the exception.

### 1.2 Hidden frequency incentives to be eliminated

| # | Incentive | Where | Disposition |
|---|---|---|---|
| 1 | Context scorer relaxes thresholds during burst/early-candle/post-BOS | `entry_context.py` | Replace with tighten-only logic (§1.4) |
| 2 | Permissive fallback defaults (0.43/0.15/0.10) | `entry_context.py:143` | Replace with floors *above* config |
| 3 | `MIN_RR_FLOOR=0.8` accepts sub-1R | `config.py` | Hard floor 1.5 |
| 4 | `TQ_MINIMUM=0.45` admits ranging/neutral | `trade_quality.py:64` | Raise to 0.60 |
| 5 | Exploration: 3/symbol/day × 4 = 12 EV-bypassed trades | `config.py` | Off by default; opt-in only |
| 6 | 60% close at TP1 caps winners | `config.py` | 50%, with TP1 at real structure |
| 7 | `COST_MAX_FRACTION_OF_TARGET=0.40` | `config.py` | 0.25 |
| 8 | `MAX_TRADES_PER_SYMBOL=4`, `MAX_CURRENCY_EXPOSURE=3` | `config.py` | 2 and 2 |
| 9 | DOJI forgiveness `× 1` waves through conflicts | `strategy_manager.py:348` | `× 0.5` |
| 10 | `SCALPER_MIN_TICK_VELOCITY=1.0` allows thin markets | `config.py` | 1.5 |

### 1.3 The Philosophy Lock (embed it in code)

Create `src/edge_floors.py` — a module of **hard minimums** that the rest of the
system reads and that a startup validator enforces. The system **refuses to
boot** if live config violates a floor.

```python
# src/edge_floors.py — IMMUTABLE edge-first floors. Never relax these.
# Any config value looser than a floor is a philosophy regression and the
# bot will refuse to start. Tightening above a floor is always allowed.

EDGE_FLOORS = {
    "MIN_RR_FLOOR":               1.5,    # never accept sub-1.5R reachable reward
    "SCALPER_MIN_ALIGNMENT_SCORE":0.55,   # structural bias is mandatory
    "SCALPER_MIN_TICK_SCORE":     0.50,
    "SCALPER_MIN_CANDLE_SCORE":   0.30,
    "SCALPER_MIN_TICK_VELOCITY":  1.5,    # block thin/illiquid markets
    "COST_MAX_FRACTION_OF_TARGET":0.25,   # cost may never exceed 25% of target
    "TQ_MIN_BASE":                0.60,   # trade-quality floor
    "MAX_TRADES_PER_SYMBOL":      2,      # ceiling, not floor — see validator
    "MAX_CURRENCY_EXPOSURE":      2,      # ceiling
}

# Keys where a LOWER live value is the violation (quality thresholds):
_FLOOR_KEYS  = {"MIN_RR_FLOOR","SCALPER_MIN_ALIGNMENT_SCORE","SCALPER_MIN_TICK_SCORE",
                "SCALPER_MIN_CANDLE_SCORE","SCALPER_MIN_TICK_VELOCITY","TQ_MIN_BASE"}
# Keys where a HIGHER live value is the violation (exposure ceilings):
_CEIL_KEYS   = {"COST_MAX_FRACTION_OF_TARGET","MAX_TRADES_PER_SYMBOL","MAX_CURRENCY_EXPOSURE"}

def validate(config) -> list[str]:
    """Return a list of violation strings; empty = OK. Called at startup."""
    bad = []
    for k in _FLOOR_KEYS:
        v = getattr(config, k, None)
        if v is not None and v < EDGE_FLOORS[k]:
            bad.append(f"{k}={v} < edge floor {EDGE_FLOORS[k]} (too loose)")
    for k in _CEIL_KEYS:
        v = getattr(config, k, None)
        if v is not None and v > EDGE_FLOORS[k]:
            bad.append(f"{k}={v} > edge ceiling {EDGE_FLOORS[k]} (too permissive)")
    return bad
```

Startup (`main_stream.py`) calls `edge_floors.validate(config)`; if non-empty,
log every violation and **exit**. This makes philosophy drift impossible to
deploy silently — someone has to consciously edit `edge_floors.py`, which is a
reviewable, deliberate act.

### 1.4 Tighten-only adaptation (invert the context scorer)

The `EntryContextScorer` is redesigned so flags can only **raise** thresholds:

| Condition | Old behavior | New behavior |
|---|---|---|
| Ranging regime | align +0.10 (kept) | align +0.15, candle +0.10, **tick velocity +0.5** |
| Volatility burst | candle relaxed −10% | **REJECT** (don't enter chasing a burst) |
| Spread elevated (1.3–2.0× baseline) | allowed | align +0.10 (demand stronger setup) |
| Early candle (<55%) | candle relaxed | **defer** — wait for the retest (§4), don't relax |
| M5 opposes M1 | align relaxed if "weakening" | align +0.10 (conflict = lower quality) |

The only permissible downward adjustment is removed entirely. Adaptation
becomes a **risk surcharge**, never a discount.

### 1.5 Fail-closed everywhere it matters

Already fail-closed: news, cost, margin, EV-unknown, trade-quality exception.
Add fail-closed for: missing structure data (no swing levels → no trade),
stale tick buffer (< N ticks in window → no trade), and config-floor violation
(refuse to boot).

---

## 2. EDGE-FIRST ENTRY PIPELINE

### 2.1 Definition of a "good trade" (all must be true)

1. **Structural event present** — a confirmed BOS or CHoCH on M1, OR price at a
   validated M5 swing level. No structural event → no trade.
2. **Entry is a retest, not a chase** — price has pulled back to the broken
   level / order block, not extended away from it (§4).
3. **HTF agreement** — M5 EMA(10) slope agrees with the trade direction, OR M5
   is demonstrably ranging and we are fading a clean range boundary.
4. **Momentum confirms, does not lead** — tick imbalance and velocity confirm
   the retest is holding (filter role only).
5. **Reachable target ≥ 1.5R to real structure** — the next opposing swing is
   far enough that, after the buffer and ~2.2 pips friction, reward ≥ 1.5×
   risk.
6. **Cost ≤ 25% of target** and spread NORMAL (not elevated/dangerous).
7. **Positive EV** — rolling broker-truth once available; conservative
   slippage-charged bootstrap before that.

### 2.2 Immediate-reject conditions (any one → no trade)

- Volatility burst in progress (entering the spike)
- Spread elevated or dangerous
- Price extended > 1.0×ATR from the structural level (missed the retest)
- M1 and M5 directions conflict with no ranging context
- Reachable RR < 1.5
- No structural target ahead (would fall back to a blind R-multiple)
- Within news pre/post window, or news data stale
- Reversal cooldown active, loss-streak cooldown active, or anti-dupe active
- Trade-quality score < 0.60

### 2.3 Opportunity ranking

When multiple symbols qualify in the same bar, rank by a single
**Edge Score** and take the best first (subject to exposure limits):

```
edge_score = 0.35*tq_structural + 0.25*alignment + 0.20*reachable_RR_norm
           + 0.15*momentum_confirm + 0.05*cost_headroom
```

Lower-ranked opportunities only fill if exposure budget remains. This replaces
"fire everything that passes" with "spend the risk budget on the best setups."

### 2.4 Component redesign summary

| Component | Edge-first redesign |
|---|---|
| **BOS** | Require displacement: breaking bar range ≥ 1.0×ATR. A weak break is noise. |
| **CHoCH** | Only trade CHoCH in the direction of M5 trend (counter-trend CHoCH is a reversal scalp = lower probability; defer until live data justifies). |
| **EMA alignment** | M5 EMA(10) slope sign must match direction. Flat slope (\|slope\|<ε) = ranging path only. |
| **Tick velocity** | Filter only, floor 1.5/s. Never a trigger. |
| **Candle scoring** | Used to confirm the *retest bar* holds (rejection wick into level), not to chase the breakout bar. |
| **Trade quality** | Floor 0.60; structural component weighted highest. |
| **Structure-aware TP** | Primary and only target source. Fallback R-multiple allowed ONLY when a structural level exists beyond 1.5R but is unusually far — never as a blind default. |
| **EV filter** | Bootstrap slippage-charged; switch to rolling broker-truth at 30 samples. |
| **Anti-duplicate** | Per symbol AND per direction; plus a **per-symbol minimum spacing** of 1 bar regardless of direction. |
| **Cooldowns** | Reversal cooldown arms on BOTH Guardian reversal exits AND stop-outs (a stop-out is structural invalidation). |
| **Portfolio** | Max 2 concurrent per symbol, max 2 correlated-currency, equity exposure ≤ 50%. |

---

## 3. EXECUTION-PHYSICS REDESIGN

### 3.1 Realistic assumptions (what we design for)

| Quantity | Realistic value | Dangerous assumption (rejected) |
|---|---|---|
| Signal→fill latency | 100–250 ms | "instant fill at signal price" |
| Entry slippage (active session) | 0.5–2.0 pips | "fills at mid" |
| Stop-exit slippage (market stop) | 0.5–1.5 pips | "stop fills exactly at level" |
| Spread (GBPUSD raw, normal) | 0.1–0.5 pip | "zero spread" |
| Spread (news/rollover) | 3–20+ pips | "spread is constant" |
| Round-trip friction | **~2.2 pips** | "~0.7 pip commission only" |

### 3.2 Hard execution limits

- **Max acceptable latency:** if the bridge heartbeat / tick age exceeds
  **2 seconds**, refuse all entries (stale market view).
- **Max acceptable entry slippage:** **40% of the stop distance**. The
  post-fill check already aborts above this via the risk ceiling; formalize it
  as an explicit slippage cap too.
- **Refuse-to-trade conditions:** spread elevated/dangerous; volatility burst;
  tick buffer stale; structure data missing; within news window; bridge
  latency > 2 s; daily drawdown breaker tripped.

### 3.3 Post-fill pipeline (already partly built — complete it)

1. **Risk revalidation** (built): real SL distance × pip_value × lot vs
   `MAX_TRADE_RISK_PCT`; abort+close if breached.
2. **TP geometry check** (built): TP1 kept at structural price; abort if
   reachable RR < `MIN_RR_FLOOR` from actual fill.
3. **Slippage cap** (add): if \|fill − signal\| > 40% of stop, abort+close even
   if risk ceiling not breached — the entry premise is gone.
4. **Stale-signal guard** (add): if > 2 s elapsed between signal and the place
   call, re-fetch price; if it moved > 0.5×ATR, cancel before placing.

### 3.4 Stale structure / TP

Structure levels are computed at signal time. Between signal and fill they can
be breached. The TP geometry check (§3.3.2) catches a breached TP1. Add: if the
**stop** level itself has been breached by the fill (entry on wrong side of
intended stop), abort — this indicates the move ran through structure during
latency.

---

## 4. CANDLE-CLOSE TIMING — THE DECISION

**Decision: Architecture B — a selective post-BOS/CHoCH retest system using
candle-close logic correctly, with a narrow intra-bar retest-touch detector as
a Phase-2 refinement.** A true intra-bar tick scalper (Option A) is **rejected**
for this system.

### 4.1 Why B is superior here (decisive reasoning)

- **Latency makes A impossible to do well.** Tick-triggered entries need
  co-located, sub-10 ms execution. At 100–250 ms you are always late to the
  burst. The system's own code already concluded this.
- **Retest entries are latency-insensitive.** You pre-identify the level; you
  wait for price to come back to it. Being 200 ms late to a resting level barely
  matters — the level is not moving. This is the single biggest reason B fits
  retail infrastructure.
- **Account size demands selectivity, not speed.** $230 cannot absorb the
  variance of high-frequency tick scalping. Fewer, structurally-justified
  entries with tight, logical stops are survivable.
- **Maintainability.** A structure-state machine (waiting for BOS → waiting for
  retest → confirm → enter) is far easier to reason about, test, and keep
  edge-first than a real-time tick-trigger system.

### 4.2 Trade-offs introduced by B

- **You will miss runaway breakouts that never retest.** Accepted — those are
  lower-probability chases anyway, and missing a trade costs nothing.
- **Fewer trades** (feature, not bug — matches the 15–22/day goal).
- **Requires a small state machine** per symbol (below).

### 4.3 The entry state machine (per symbol)

```
IDLE
  └─ on M1 close: BOS/CHoCH with displacement ≥ 1.0×ATR, M5-aligned?
        └─ yes → record broken level L, direction D, arm WAIT_RETEST (expires in K bars)
WAIT_RETEST  (K = 3 bars default)
  └─ on M1 close (or intra-bar touch in Phase 2):
        price pulled back to L ± buffer AND
        rejection evidence (wick into L, tick imbalance flips back to D) AND
        NOT extended > 1.0×ATR beyond L?
        └─ yes → ENTER (D), SL behind retest swing, TP to next opposing structure
        └─ expired (K bars, no clean retest) → IDLE  (missed = no trade)
ENTERED → hand to Trade Guardian
```

### 4.4 Acceptable retest profile

- Pullback depth: 30–70% of the BOS impulse leg (Fibonacci-like, not hardcoded
  levels — measured from impulse).
- Retest bar shows **rejection**: wick into the level ≥ body, close back in
  trade direction, OR tick imbalance re-aligns to D on the touch.
- Stop sits just beyond the retest extreme (tight, logical) → typically 4–7
  pips on majors.

### 4.5 Rejection conditions at the retest

- Price closes *through* the level (failed retest → structure void → IDLE).
- Retest takes > K bars (momentum gone).
- Spread elevated or burst during the touch.
- Pullback exceeds 1.0×ATR (the break is being reversed, not retested).

### 4.6 Phase-2 intra-bar refinement (optional, after B is stable)

A **narrow** intra-bar mechanism — NOT a tick scalper. Only while in
`WAIT_RETEST`, evaluate on a 5–10 s timer whether price has *touched* L so the
retest entry isn't delayed a full bar. Guards: one entry per `(symbol, bar)`;
only active in WAIT_RETEST; structure trigger unchanged. This preserves
selectivity while shaving retest latency.

---

## 5. PROFITABILITY REDESIGN (with the math)

### 5.1 The geometry change that creates the edge

Old geometry (frequency-first): SL 5p, TP1 7p (1.4R), close 60% at TP1.
New geometry (edge-first): SL behind retest swing (~5p), **TP1 at first
structural level (~8p / 1.6R) close 50%, runner 50% to next structure (~14p /
2.8R) or trailed.**

Friction (realistic): **2.2 pips** round trip.

```
avg_win  = 0.5 × 8p  + 0.5 × 14p = 11.0 pips   (gross, before friction)
avg_loss = 5 pips
EV(p) = p·(11.0 − 2.2) − (1−p)·(5 + 2.2)
      = 8.8p − 7.2(1−p) = 16.0p − 7.2

Break-even:  p = 7.2 / 16.0 = 45.0%
At p = 0.55: EV = +1.6 pips/trade
At p = 0.50: EV = +0.8 pips/trade
```

Contrast the **old** geometry under the same 55% win rate:
```
avg_win ≈ 0.6·7 + 0.4·~9 ≈ 7.8 pips ; avg_loss 5
EV = 0.55·(7.8−2.2) − 0.45·(5+2.2) = 3.08 − 3.24 = −0.16 pips  (negative!)
```

**This is the central result: the edge does not come from frequency or from a
higher win rate. It comes from (a) widening targets to real structure and (b)
letting the runner work instead of closing 60% at 1.5R.** The same win rate is
negative under old geometry and clearly positive under new geometry.

### 5.2 Realistic expectations

| Metric | Realistic target |
|---|---|
| Win rate (retest setups, majors) | 50–58% |
| Blended RR (realized) | 1.8–2.2R |
| Trades/day | 12–20 (after exploration off) |
| EV/trade | +0.8 to +1.6 pips |
| Daily P&L (0.09 lot, 15 trades) | roughly +$1.1 to +$2.2/day net, high variance |
| Expected max drawdown | 8–15% of account over a losing cluster |
| Survivability horizon | needs ≥ 200 trades to confirm the edge statistically |

### 5.3 Is 60% partial close wrong? — Yes, for edge-first

60% at 1.5R caps the trade's best outcome to fund a modest win. For a *selective*
system that only takes high-quality retests, the runner is where the edge lives.
Recommended: **TP1 50% at the first structural level (lock breakeven on the
rest), runner 50% trailed behind M1 structure to the next major level.** This
raises blended RR without raising win-rate requirements.

### 5.4 Stop size vs target width

Do **not** widen stops (they are structurally correct behind the retest). **Widen
targets** to real structure and **extend the runner**. The stop is the one thing
the retest gives you for free — a tight, logical invalidation point. Keep it.

---

## 6. FINAL PROFESSIONAL DEPLOYMENT CONFIGURATION

### 6.1 Recommended config (the deployable set)

```
# Quality floors (also enforced by edge_floors.py)
SCALPER_MIN_ALIGNMENT_SCORE   = 0.55
SCALPER_MIN_TICK_SCORE        = 0.50
SCALPER_MIN_CANDLE_SCORE      = 0.30
SCALPER_MIN_TICK_VELOCITY     = 1.5
MIN_RR_FLOOR                  = 1.5
COST_MAX_FRACTION_OF_TARGET   = 0.25
TQ_MIN_BASE                   = 0.60

# Targets / exits (the edge)
TP_STRUCTURE_AWARE            = true
TIERED_TP1_CLOSE_PCT          = 0.50      # was 0.60
TIERED_TP1_RATIO              = 1.5       # TP1 at first structural level (min 1.5R)
TIERED_TP2_RATIO              = 2.5+      # runner to next structure, trailed
RUNNER_EXIT_RR                = 2.0       # model runner conservatively at 2.0R

# Exposure (small-account survivability)
MAX_TRADES_PER_SYMBOL         = 2
MAX_OPEN_SYMBOLS              = 4
MAX_CURRENCY_EXPOSURE         = 2
MAX_EQUITY_EXPOSURE           = 0.50
DAILY_DRAWDOWN_LIMIT_PCT      = 6.0
MAX_TRADE_RISK_PCT            = 3.0
RISK_PERCENT                  = 2.0

# Behavior locks
EXPLORATION_ENABLED           = false     # opt-in only; default off
STRATEGY_GATE_ENABLED         = false     # observe first, earn the right to trade
DISABLED_SYMBOLS              = XAUUSD     # until capital ≥ ~$1,000

# Execution physics
EV_SLIPPAGE_PIPS              = 1.0
MAX_BRIDGE_LATENCY_SEC        = 2.0       # NEW — refuse trades if stale
MAX_ENTRY_SLIPPAGE_FRAC       = 0.40      # NEW — abort fill if slip > 40% of stop
REVERSAL_REENTRY_COOLDOWN_SEC = 120
```

### 6.2 Final gate order (entry pipeline)

```
0.  Bridge latency OK? (NEW)            → else refuse
1.  Loss-streak / reversal cooldown
2.  News window + freshness
3.  Session window
4.  Spread NORMAL + tick velocity ≥1.5
5.  Volatility burst? (NEW: reject)
6.  Daily drawdown breaker
7.  Portfolio allocation (2/sym, 2/ccy, 50% equity)
8.  Structural event present (BOS/CHoCH + displacement)   ← NEW gate
9.  Valid retest profile (not extended)                   ← NEW gate
10. Alignment ≥ 0.55 (tighten-only context)
11. Trade quality ≥ 0.60
12. Structure-aware TP exists, reachable RR ≥ 1.5
13. Cost ≤ 25% of target
14. EV positive (rolling, or slippage-charged bootstrap)
15. Anti-duplicate + min spacing
→ rank by edge_score, take best within exposure budget
→ place → POST-FILL: risk revalidation, TP geometry, slippage cap, stale guard
```

---

## 7. IMPLEMENTATION ROADMAP

Ordered by priority and dependency. Each step is independently testable.

### Phase 0 — Lock the philosophy (do first; low risk)
1. `edge_floors.py` + startup validator (refuse boot on violation).
2. Config tightening: `MIN_RR_FLOOR 1.5`, `COST_MAX_FRACTION 0.25`,
   `MAX_TRADES_PER_SYMBOL 2`, `MAX_CURRENCY_EXPOSURE 2`, `TICK_VELOCITY 1.5`,
   `TIERED_TP1_CLOSE_PCT 0.50`, `EXPLORATION_ENABLED false`.
3. DOJI multiplier `× 1 → × 0.5`; TQ base `0.45 → 0.60`.
4. Stop-out arms reversal cooldown.
5. Context-scorer fallback defaults → above config (fail toward quality).
**Validation:** unit tests; run a day in observe mode; confirm fewer signals.

### Phase 1 — Invert adaptation (medium risk)
6. Rewrite `EntryContextScorer` to tighten-only (§1.4). Burst → reject.
**Validation:** replay day-12/15 logs; confirm previously-relaxed entries now blocked.

### Phase 2 — Execution physics (medium risk)
7. Bridge-latency gate + stale-signal guard + explicit slippage cap.
**Validation:** unit tests on the pure decision helpers; live latency logging.

### Phase 3 — The structural retest engine (high value, highest effort)
8. Per-symbol entry state machine (IDLE → WAIT_RETEST → ENTER) with BOS
   displacement and retest-profile validation (§4.3–4.5).
**Validation:** backtest/replay; measure win rate and blended RR vs old.

### Phase 4 — Exit geometry (the edge)
9. TP1 50% at first structural level; runner 50% trailed to next structure.
**Validation:** compare blended RR before/after on closed broker-truth trades.

### Phase 5 — Phase-2 intra-bar retest touch (optional)
10. Narrow 5–10 s retest-touch detector inside WAIT_RETEST only.

### Risk notes
- **Dangerous to rush:** Phase 3 (state machine) — a bug here means missed or
  wrong entries. Build behind a flag, A/B against current logic.
- **Requires live validation:** Phases 3 & 4 win-rate/RR assumptions. Do not
  trust the model until ≥ 200 broker-truth trades confirm it.
- **Safe immediately:** Phase 0.

---

## 8. LONG-TERM MONITORING (detect quality drift)

Track daily from broker-truth journal (schema v2 only):

| Metric | Healthy range | Drift alarm |
|---|---|---|
| Trades/day | 12–20 | > 25 → frequency creeping back |
| Win rate (rolling 50) | ≥ 52% | < 48% sustained → edge gone |
| Blended realized RR | ≥ 1.8 | < 1.4 → exits too tight |
| Avg cost / avg target | ≤ 0.25 | > 0.30 → taking marginal setups |
| Exploration trades/day | 0 (off) | > 0 unexpectedly → flag flipped |
| % entries that are retests | ~100% | < 90% → chasing returned |
| Post-fill aborts/day | low, stable | spike → latency/slippage problem |
| Max intraday drawdown | < 6% | breaker trips → re-evaluate |
| `edge_floors.validate()` | empty | non-empty → config regression |

**The single most important guardrail:** `edge_floors.validate()` must run at
every startup and the result must be logged. If it is ever non-empty, the
system has drifted and must not trade.
