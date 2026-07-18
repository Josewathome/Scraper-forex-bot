# Pre-Phase-3 Baseline — OFFICIAL RECORD
**Snapshot taken:** 2026-07-18T08:29:53Z · **Commit at snapshot:** `adb1a42` (Phase 2B) · **Journal archive:** `baselines/trade_journal_pre_phase3_2026-07-18.json` (sha256 `686c9bea5fd50a48059105fb2b452fb001cf1e47bef0b5780fb0b926aff92874`, 67 records, 26104 bytes)

This is the frozen reference point for evaluating whether the Phase 3 tick-score
correctness fix changes trading outcomes. Every scheduled review during the
observation period compares against these exact numbers. **Do not edit this
file or the archived journal.**

---

## 1. Trade statistics (all money-valid closed trades, full history to date)

| Metric | Value |
|---|---|
| Total journal records | 67 |
| Closed records | 67 |
| v2 money-valid records (basis for all stats below) | **30** |
| Wins | 9 |
| Losses | 21 |
| Breakeven | 0 |
| Win rate | **30.0%** |
| Win rate 95% CI (Wilson) | [16.7%, 47.9%] |
| Gross win | $4.54 |
| Gross loss | $13.66 |
| Net P&L | **−$9.12** |
| Profit factor | **0.33** |
| Expectancy per trade | **−$0.304** |
| Expectancy 95% CI (t-approx, df=29) | **[−$0.638, +$0.030]** |
| CI excludes zero? | **No** — point estimate is clearly negative but n=30 is not yet enough to claim statistical significance |

## 2. Symbol breakdown

| Symbol | n | Wins | Net P&L |
|---|---|---|---|
| USDJPY | 21 | 7 | −$2.41 |
| XAUUSD | 4 | 1 | −$5.33 |
| GBPUSD | 4 | 1 | −$1.14 |
| AUDUSD | 1 | 0 | −$0.24 |
| USDCHF | 0 | — | — |

70% of all samples are USDJPY — a direct consequence of the tick-score bug, not a deliberate sampling choice. This concentration is the single most important caveat on every number above: the sample barely tests 4 of 5 designed symbols at all.

## 3. Gate-blocker statistics

**Entry-gate block reasons** (current log window, 2026-07-11 → 2026-07-18, post rotation):
| Reason | Count |
|---|---|
| outside_session_window | 128 |
| spread_cost_excessive | 118 |
| expected_value_negative | 111 |
| rr_below_minimum | 109 |
| tick_velocity_too_low | 57 |
| news_proximity | 32 |
| net_rr_below_minimum | 10 |

**Gate 5 (tick-score, the bug under fix) — pass rate at the 0.40 threshold, BEFORE Phase 3:**

| Symbol | Window | Samples | Mean tick_score | Pass @ 0.40 | Pass rate |
|---|---|---|---|---|---|
| USDJPY | current (Jul 11–18) | 7,117 | 0.271 | 1,875 | **26.35%** |
| GBPUSD | current (Jul 11–18) | 7,117 | 0.164 | 197 | **2.77%** |
| USDJPY | full archive (Jun 5–Jul 11) | 42,851 | 0.251 | 10,071 | 23.50% |
| GBPUSD | full archive (Jun 5–Jul 11) | 43,250 | 0.156 | 1,024 | 2.37% |
| AUDUSD | full archive (Jun 5–Jul 11) | 22,678 | 0.149 | 785 | 3.46% |
| USDCHF | full archive (Jun 5–Jul 11) | 22,737 | 0.154 | 875 | 3.85% |
| XAUUSD | full archive (Jun 5–Jul 11) | 10,035 | 0.264 | 2,554 | 25.45% |

Both windows agree closely (USDJPY 23.5% vs 26.4%, GBPUSD 2.4% vs 2.8%) — the miscalibration is stable and has been unaffected by Phases 0–2B, which never touched `tick_engine.py`. This is the "before" side of the post-deployment pass-rate comparison required after the Phase 3 fix.

## 4. Root cause being corrected (unchanged from prior design review)

`tick_composite_score` (`src/strategy/tick_engine.py`): displacement term divides raw price by a fixed `0.0002`, which is ~2 pips for 5-digit pairs (GBPUSD/AUDUSD/USDCHF) but ~0.02 pips for USDJPY — free credit for JPY, a real 2-pip bar for everyone else. Velocity term divides by a fixed 10 ticks/sec, uncalibrated to this feed's observed 1–4 ticks/sec. This is the only mechanism Phase 3 touches.

## 5. What this baseline is for

Every 2-week scheduled review during the observation period reports the same metrics recomputed on the growing post-Phase-3 sample and states the delta against every number in §1–§3. The objective is a single yes/no: does the post-fix sample show a statistically credible positive expectancy, or does it remain negative / indistinguishable from zero. Per standing instruction, if the answer at threshold (75 trades, 100 trades, or 8 weeks — whichever comes first) is the latter, the recommendation is to abandon the strategy rather than continue tuning it.
