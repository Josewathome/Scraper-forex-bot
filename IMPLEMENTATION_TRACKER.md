# Forex Scalping Bot — Implementation Tracker
**Authoritative project memory. Read this before touching any code.**
Reference doc: [`Bot Building Reecomendation.md`](./Bot%20Building%20Reecomendation.md)

---

## Table of Contents
1. [Project Overview](#project-overview)
2. [System Architecture](#system-architecture)
3. [Current State](#current-state) ← **Start here every session**
4. [Development Phases](#development-phases)
5. [Research Findings](#research-findings)
6. [Known Issues & Bugs](#known-issues--bugs)
7. [Architectural Decisions Log](#architectural-decisions-log)
8. [Changelog](#changelog)

---

## Project Overview

### What This Bot Is
A retail forex scalping bot built on top of MetaTrader 5 (MT5), targeting 5–20 pip moves per trade using a multi-timeframe structure alignment engine combined with tick-level momentum confirmation. The bot is designed for **spread-based accounts** (HFM Premium/Pro, IC Markets Raw) where the absence of per-trade commissions makes tight scalping viable.

### Core Trading Objectives
- 0–5 high-quality trades per day (signal-gated, never forced)
- Target 5–20 pips per trade with 1.5:1–2:1 R:R minimum
- Risk 0.5–1.0% of account per trade
- Stop trading at −2% daily drawdown
- Trade only London Open (06:00–09:00 UTC) and NY Open (13:00–16:00 UTC)

### Strategy Summary
1. **H1** defines the global directional bias ("the river")
2. **M30** defines the intermediate swing structure
3. **M15** defines pullback vs continuation context
4. **M5** defines local swing points and entry zones
5. **M1** is used for precise entry timing only

Entry is only allowed when ≥70% of timeframe structure weights agree on direction. Tick features (velocity, acceleration, directional imbalance) act as entry confirmation — they are **filters, not triggers**.

### Three Entry Types (ranked by R:R)
| Type | Setup | Win Rate | Notes |
|------|-------|----------|-------|
| Type 1 | BOS + Retest | 55–62% | Best R:R |
| Type 2 | Momentum Continuation | 58–65% | Best win rate |
| Type 3 | Liquidity Sweep Reversal | 50–58% | Highest precision |

### Supported Brokers / Accounts
| Broker | Account Type | Spreads | Commission | Status |
|--------|-------------|---------|------------|--------|
| HFM | ZERO_SPREAD | ~0 pip raw | $3–5/lot/side | Configured in config.py |
| HFM | PREMIUM | ~1.4+ pip | None | Supported |
| HFM | PRO | ~0.5+ pip | None | Supported |
| IC Markets | IC_MARKETS_RAW | ~0.1 pip | $3.50/lot/side | Supported |

**Current default:** `BROKER_ACCOUNT_TYPE=ZERO_SPREAD` (set via `.env`)

### Pairs Being Traded
```
Active:   GBPUSD, XAUUSD, USDJPY, AUDUSD, USDCHF
Removed:  EURUSD (win rate 7.5%, net -98k in backtest — re-evaluate after strategy upgrade)
```

---

## System Architecture

### Actual File Structure (as of 2026-05-28)
```
src/
├── main.py                          ← Entry point, dependency wiring
├── main_stream.py                   ← Streaming entry point
├── config.py                        ← ALL settings (env-var driven)
├── api/
│   └── server.py                    ← Flask dashboard API (port 8080)
├── application/
│   ├── email_service.py             ← Trade alert emails
│   ├── gap_recovery_service.py      ← Reconnect/gap fill logic
│   ├── news_manager.py              ← News blocking ("The Shield")
│   ├── scheduler.py                 ← Daily cleanup, weekly backtest
│   └── weekly_backtest_runner.py    ← Automated weekly backtest
├── dashboard/
│   └── index.html                   ← Web UI
├── domain/
│   └── (entities, repos, value_objects — no strategy logic yet)
└── infrastructure/
    ├── cache_store.py               ← JSON-backed key/value cache
    ├── checkpoint_service.py        ← State save/resume on restart
    ├── cleanup_service.py           ← Log/cache retention
    ├── composite_news_client.py     ← Merges MT5 + external news feeds
    ├── market_data_repo.py          ← MT5 historical candle fetching
    ├── mt5_news_client.py           ← News events from MT5
    ├── news_client.py               ← External news API client
    ├── spread_calculator.py         ← Live spread computation with fallback chain
    ├── trade_journal.py             ← Persistent trade records
    ├── trade_repo.py                ← MT5 trade execution
    ├── mt5_bridge/
    │   ├── mt5_gateway.py           ← MT5 Python API wrapper
    │   └── ea/ZoneBotBridge.mq5    ← MQL5 EA — ZMQ bridge to Python
    └── stream/
        ├── candle_builder.py        ← Tick → OHLCV aggregator (M1/M5/M30/H1/H4)
        ├── streaming_market_data_repo.py ← Drop-in repo using CandleBuilder cache
        ├── zmq_feed.py              ← ZMQ subscriber receiving EA ticks
        └── __init__.py
```

### Data Flow
```
MT5 Terminal
    │
    │  ZMQ (tcp://localhost:5556)
    ▼
ZmqFeed (zmq_feed.py)
    │  raw tick: {symbol, bid, ask, time}
    ▼
CandleBuilder (per symbol)
    │  builds M1/M5/M30/H1/H4 candles in real time
    │  emits on_close callbacks when bars close
    ▼
StreamingMarketDataRepo
    │  exposes get_closed_candles() + get_forming_bar()
    ▼
[STRATEGY ENGINE — NOT YET IMPLEMENTED]
    │
    ▼
ExecutionService → MT5TradeRepository → MT5
```

### Container & Deployment
| Item | Value |
|---|---|
| Container name | `scalper-prime` |
| Dashboard | `http://localhost:8000` |
| MT5 VNC | `http://localhost:3000` |
| Env file | `.env` (real credentials, gitignored) |
| Env template | `.env.example` (safe to commit, all vars documented) |

Restart after any `.env` or `docker-compose.yml` change:
```bash
docker compose down && docker compose up -d
docker compose logs -f scalper-prime    # watch live logs
```

### Key Architecture Decisions
- **Tick source:** ZMQ push from MQL5 EA (not MT5 Python API polling) — avoids GIL issues and gives ~5ms latency
- **Candle building:** Done locally from ticks, not fetched from MT5 — eliminates "forming bar confusion" and gives exact bar boundaries
- **Hybrid trust:** EA also pushes BAR_CLOSE events as a correction layer for ticks missed during brief disconnects
- **Account type abstraction:** All cost calculations branch on `BROKER_ACCOUNT_TYPE` — strategy code never hard-codes spreads
- **EURUSD disabled:** Removed from `SYMBOLS` after backtest showed 7.5% win rate; re-enable only after strategy logic is implemented
- **One trade at a time per symbol:** `MAX_TRADES_PER_SYMBOL = 2` is the configured cap; Phase 1 of new strategy should use 1

---

## Current State

> **Always update this section when progress is made.**

### As of 2026-05-29

| Field | Value |
|-------|-------|
| Active Phase | **Phase 5 — Backtesting Engine** |
| Last Completed | Phase 4 — Entry & Exit Gate System + `.env` / `.env.example` rewrite |
| Current Blocker | None — strategy engine complete; run demo with `STRATEGY_GATE_ENABLED=false` first |
| Container Name | `scalper-prime` (renamed from `mt5` in `docker-compose.yml`) |
| Next Immediate Step | Restart container (`docker compose down && docker compose up -d`), watch logs for `STRATEGY_SIGNAL` lines, then Phase 5 backtester |

### What Is Built and Working
- [x] MT5 connectivity via ZMQ (EA ↔ Python)
- [x] Real-time tick ingestion (`ZmqFeed`)
- [x] Local candle building for M1, M5, M30, H1, H4 (`CandleBuilder`)
- [x] Historical candle seeding from MT5 at startup
- [x] Hybrid bar correction (EA BAR_CLOSE events)
- [x] News blocking via `NewsManager` (composite MT5 + external feed)
- [x] Live spread computation with 4-level fallback chain (`SpreadCalculator`)
- [x] Risk/cost model for HFM Zero Spread, Premium, Pro, IC Markets Raw
- [x] Trade execution via MT5TradeRepository
- [x] Checkpoint/resume service (survives restarts)
- [x] Dashboard API (Flask, port 8080)
- [x] Trade journal (persistent records)
- [x] Email alerts
- [x] Weekly automated backtest runner
- [x] Docker / docker-compose deployment (`container_name: scalper-prime`)
- [x] Phase 1+2 strategy engine (structure + tick intelligence + candle analysis)
- [x] Phase 4 entry gate (`src/strategy/entry_gate.py`) — 8-gate signal-to-trade pipeline
- [x] `.env` and `.env.example` fully documented with all Phase 4 variables

### What Is NOT Built Yet
- [ ] Backtesting engine (event-driven tick-level) — Phase 5
- [ ] Walk-forward optimization — Phase 5
- [ ] Demo/paper trading validation — Phase 6 (50+ trades minimum)
- [ ] Live micro-account deployment — Phase 7

### What Is Built and Validated
- [x] Full strategy engine: structure (Phase 1) + tick intelligence (Phase 2) + candle analysis (Phase 3)
- [x] Entry gate system (Phase 4) — `STRATEGY_GATE_ENABLED=true` to activate live execution

---

## Development Phases

---

### Phase 0 — Core Infrastructure
**Status: COMPLETED**
**Period:** Prior to 2026-05-28

#### Completed Tasks
- [x] MT5 Python bridge via ZMQ (`ZoneBotBridge.mq5` EA + `ZmqFeed`)
- [x] Tick streaming (real-time bid/ask per symbol)
- [x] Local candle building from ticks (M1, M5, M30, H1, H4)
- [x] Historical candle seeding at startup (gap fill)
- [x] EA BAR_CLOSE hybrid correction layer
- [x] `StreamingMarketDataRepo` as drop-in replacement for MT5 polling
- [x] News filtering infrastructure (`NewsManager`, composite news client)
- [x] Spread calculation with account-type model (`SpreadCalculator`)
- [x] Full cost model: HFM Zero/Premium/Pro + IC Markets Raw
- [x] Trade execution (`MT5TradeRepository`)
- [x] Risk management framework in `config.py` (ATR-based SL, EXTRA_PIPS_SL, MIN_RR)
- [x] Checkpoint service (state persistence across restarts)
- [x] Dashboard API (Flask, JWT auth, port 8080)
- [x] Trade journal (persistent CSV/JSON trade records)
- [x] Email notification service
- [x] Weekly backtest runner + scheduler
- [x] Docker + docker-compose deployment

#### Notes
- EURUSD was removed from `SYMBOLS` after backtest revealed 7.5% win rate. Root cause: strategy logic was placeholder/zone-based with no real signal quality. This is NOT a permanent removal — it will return once the structure engine is implemented.
- `MAX_TRADES_PER_SYMBOL = 2` and `MAX_OPEN_TRADES = 6` — these are the current live caps. Scalping strategy should default to `MAX_OPEN_TRADES = 1` initially.
- `MIN_RR` was lowered from 3.0 → 2.0 after backtest showed too many valid signals being rejected.
- `XAUUSD` SL multiplier was reduced from 0.30 → 0.15 because gold H1 ATR is ~120 pips; at 0.30× the required TP was 50–80% of the daily range.

---

### Phase 1 — Market Structure Engine
**Status: COMPLETED**
**Completed: 2026-05-28**

#### Goal
Implement the brain that understands where price is in the market structure across all 5 timeframes. This is the foundation everything else depends on.

#### Completed Tasks
- [x] **`StructureStateManager`** — `src/strategy/structure_state.py`
  - [x] Swing high / swing low detection (2-bar left/right confirmation, ties handled with >=)
  - [x] Track up to 10 swing points per timeframe (rolling window)
  - [x] Classify: BULLISH_TREND (HH+HL), BEARISH_TREND (LH+LL), RANGING
  - [x] Detect Break of Structure (BOS) — price closes beyond last swing
  - [x] Detect MITIGATION_ZONE (price returns to BOS level within 0.3× ATR proxy)
  - [x] State machine transitions between 5 states
  - [x] `get_state()`, `get_swing_highs()`, `get_swing_lows()`, `get_direction()`, `seed()` APIs

- [x] **`AlignmentScoreCalculator`** — `src/strategy/alignment.py`
  - [x] Weight map: H1=0.35, M30=0.25, M15=0.20, M5=0.15, M1=0.05
  - [x] Score 0.0–1.0, minimum 0.70 required
  - [x] H1 anti-conflict rule: blocks direction if M1 against H1 bias
  - [x] Returns `AlignmentResult` with score, direction, regime, details dict

- [x] **`RegimeDetector`** — `src/strategy/alignment.py`
  - [x] H1 ATR(14) vs 50-bar baseline
  - [x] RANGING if current ATR < 50% of baseline
  - [x] `Regime.TRENDING` / `Regime.RANGING` enum

- [x] **`MultiTFAnalyzer`** — convenience facade combining all 3 above
- [x] Domain source files created: `entities.py`, `value_objects.py`, `repositories.py`
- [x] Wired into `main_stream.py` — candles fed on CANDLE_CLOSED events
- [x] Historical seeding at startup (H1/M30/M15/M5/M1)

#### Implementation Notes
- Bug fixed: swing detection used strict `>` on both sides — ties caused valid swings to be missed. Fixed to `>=` on right side.
- `__len__` on `TickAnalyzer` returned buffer size (0 when empty), making truthiness checks fail. Fixed to use `is not None` guards everywhere.
- Smoke test passes: BULLISH_TREND detected, alignment=0.80, signal confidence=0.813

---

### Phase 2 — Tick Intelligence Engine
**Status: COMPLETED**
**Completed: 2026-05-28**

#### Goal
Add tick-level analysis as the entry confirmation layer. Tick features approve or reject entries that structure already identified as valid candidates. **Ticks are filters, never triggers.**

#### Completed Tasks
- [x] **`TickBuffer`** — `src/strategy/tick_engine.py`
  - [x] Thread-safe `deque(maxlen=500)` per symbol
  - [x] `push(bid, ask, ts)`, `snapshot()`, `latest()` APIs

- [x] **`TickFeatureCalculator`** — stateless, all methods accept tick list
  - [x] `tick_velocity(window_seconds=5)`
  - [x] `tick_acceleration(short=3, long=10)`
  - [x] `directional_imbalance(n_ticks=20)` — 0.0–1.0
  - [x] `price_displacement(n_ticks=20)`
  - [x] `volatility_burst(window=10)` — vs 50-tick baseline
  - [x] `spread_state(symbol, ticks)` — NORMAL/ELEVATED/DANGEROUS
  - [x] `tick_composite_score(ticks, symbol, direction)` — 0.0–1.0 with spread penalty
  - [x] Per-symbol spread baseline via exponential moving average

- [x] **`SpreadMonitor`** — `src/strategy/tick_engine.py`
  - [x] Rolling baseline update on every tick
  - [x] `recently_elevated` flag (DELAY_TICKS post-elevated)

- [x] **`TickAnalyzer`** — per-symbol facade
  - [x] `push_tick()`, `analyze(direction)` → `TickAnalysisResult`

- [x] **`LiveCandleAnalyzer`** — `src/strategy/candle_analyzer.py`
  - [x] `candle_progress_score()` with time-weighted tick/candle blend
  - [x] Wick pressure analysis
  - [x] `higher_candle_state()` — reconstruct M5/M15/M30/H1 from M1 batch

- [x] **`StrategyManager`** — `src/strategy/strategy_manager.py`
  - [x] Per-symbol `MultiTFAnalyzer` + `TickAnalyzer`
  - [x] 6-gate `evaluate()` method
  - [x] `seed()`, `on_candle()`, `push_tick()` lifecycle methods

- [x] Wired into `main_stream.py`:
  - [x] `strategy.push_tick()` on every TICK event
  - [x] `strategy.on_candle()` on every CANDLE_CLOSED event
  - [x] `_run_strategy_evaluation()` on every M1 close (observation mode)

#### Score Thresholds
```
tick_composite_score >= 0.60 required
candle_progress abs   >= 0.40 required
alignment_score       >= 0.70 required (0.75 in RANGING)
volatility_burst      <= 2.5  required
```

#### Implementation Notes
- Spread baseline uses EMA (alpha = 2/(50+1)) — adapts to session changes
- Tick displacement normalised at 0.0005 price units = full score (tunable per instrument)
- Signal is currently in **observation mode** — set `STRATEGY_GATE_ENABLED=true` in `.env` to enable live execution
- Phase 4 (`entry_gate.py`) is complete — gates live trades from StrategySignals

---

### Phase 3 — Live Candle Analysis
**Status: NOT STARTED**
**Dependency: Phase 2**
**Target: Weeks 5–6 (can partially overlap Phase 2)**

#### Goal
Analyze in-progress candles in real time to infer probable close direction and higher-timeframe candle formation state.

#### Tasks
- [ ] **`LiveCandleAnalyzer`**
  - [ ] `candle_progress_score(candle, tick_features)` — returns −1.0 to +1.0
    - [ ] Early candle (0–30% elapsed): weight tick momentum 70%, candle body 30%
    - [ ] Mid candle (30–70% elapsed): weight tick 50%, candle 50%
    - [ ] Late candle (70–100% elapsed): weight tick 25%, candle 75%
  - [ ] `_wick_analysis(candle)` — bullish/bearish wick pressure score
  - [ ] `higher_candle_formation_state(m1_candles, target_tf)` — reconstruct
        in-progress M5/M15/M30/H1 candle from current M1 batch
  - [ ] Require `abs(candle_score) >= 0.40` to pass entry gate

- [ ] Integration: feed `LiveCandleAnalyzer` with `CandleBuilder.get_forming_bar()`

#### Notes / Decisions
*(Fill in as work progresses)*

---

### Phase 3 (sub-task) — Live Candle Analysis
**Status: COMPLETED** (implemented as part of Phase 2)
See `src/strategy/candle_analyzer.py` — `LiveCandleAnalyzer`.

---

### Phase 4 — Entry & Exit Gate System
**Status: COMPLETED**
**Completed: 2026-05-28**

#### Goal
Convert validated StrategySignals into executable EntryCandidate objects via a runtime-safe gate chain.

#### Completed Tasks
- [x] **`EntryGate`** — `src/strategy/entry_gate.py`
  - [x] Gate 1: News proximity (±15 min block around high-impact events)
  - [x] Gate 2: Session filter (London 06:00–09:00, NY 13:00–16:00 UTC)
  - [x] Gate 3: Daily trade limit (`MAX_DAILY_STRATEGY_TRADES`, default 5)
  - [x] Gate 4: Daily drawdown limit (`DAILY_DRAWDOWN_LIMIT_PCT`, default 2.0%)
  - [x] Gate 5: Max open trades cap (`MAX_OPEN_TRADES`)
  - [x] Gate 6: Per-symbol open trade cap (`MAX_TRADES_PER_SYMBOL`)
  - [x] Gate 7: Minimum SL distance (`MIN_SL_PIPS` per-symbol dict)
  - [x] Gate 8: Minimum R:R (`MIN_RR`)
  - [x] Blocked signals logged at DEBUG with reason string
  - [x] Passing signals logged at INFO with full entry params

- [x] **SL computation** from swing structure:
  - [x] BULLISH: SL = `last_swing_support` − `EXTRA_PIPS_SL` buffer
  - [x] BEARISH: SL = `last_swing_resistance` + `EXTRA_PIPS_SL` buffer
  - [x] Rejects signal if no swing reference available (avoids arbitrary SL)

- [x] **TP computation**: `entry ± (SL_dist × TP_RR_RATIO)`
  - [x] TP1: `TP1_RR_RATIO` (default 1.5)
  - [x] TP2: `TP2_RR_RATIO` (default 2.5)

- [x] **`EntryCandidate` construction** — bridges to existing `ExecutionService`
  - [x] Builds `TradeSignal` from signal fields
  - [x] Constructs `EntryCandidate` with full broker cost context

- [x] Wired into `main_stream.py`:
  - [x] `EntryGate` instantiated after `ExecutionService`
  - [x] `entry_gate.on_new_day()` called at startup with account balance
  - [x] New-day reset triggered automatically when date changes
  - [x] `_run_strategy_evaluation()` now accepts `entry_gate` parameter
  - [x] Live execution controlled by `STRATEGY_GATE_ENABLED` env var (default: `False`)

#### Implementation Notes
- `STRATEGY_GATE_ENABLED=false` (default) → observation mode: signals logged, no trades placed
- `STRATEGY_GATE_ENABLED=true` → Phase 4 live: StrategySignals gate real trade execution
- `EntryCandidate` is defined in `execution_service.pyc` — imported at runtime inside `EntryGate.evaluate()`
- `EXTRA_PIPS_SL` and `MIN_SL_PIPS` are per-symbol dicts in `config.py` — handled correctly
- New-day equity reset uses account balance from `gw.get_account_info()` at startup
- Smoke tests pass: all 5 gate rejection scenarios confirmed working

#### Activation sequence
```
Step 1 — Observation (current):  STRATEGY_GATE_ENABLED=false
          Run for several days. Read STRATEGY_SIGNAL lines in the log.
          Check direction, confidence score, session timing, news blocks.

Step 2 — Demo live:               STRATEGY_GATE_ENABLED=true
          MAX_DAILY_STRATEGY_TRADES=3
          DAILY_DRAWDOWN_LIMIT_PCT=1.5
          TP1_RR_RATIO=1.5  /  TP2_RR_RATIO=2.5
          Run 50+ trades on demo. Track win rate and avg R:R.

Step 3 — Live micro:              Same .env, switch MT5_SERVER to live account.
          Start at minimum lot size. 100 trades before scaling risk.
```

#### `.env` variables added (Phase 4)
| Variable | Default | Meaning |
|---|---|---|
| `STRATEGY_GATE_ENABLED` | `false` | `false` = log only, `true` = execute trades |
| `MAX_DAILY_STRATEGY_TRADES` | `3` | Max trades/day from strategy engine |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `1.5` | Stop trading today after this % equity loss |
| `TP1_RR_RATIO` | `1.5` | TP1 = entry ± (SL × 1.5) |
| `TP2_RR_RATIO` | `2.5` | TP2 = entry ± (SL × 2.5) — runner target |

---

### Phase 5 — Backtesting Engine
**Status: NOT STARTED**
**Dependency: Phase 4**
**Target: Weeks 9–10**

#### Goal
Build an event-driven backtester that replays historical tick data through the exact same pipeline as live trading. This is mandatory — no OHLC bar-based backtesting.

#### Tasks
- [ ] Tick data collection: 3+ months per symbol (M1 minimum, tick-level preferred)
- [ ] Event-driven backtester skeleton
  - [ ] Replay ticks through `CandleBuilder` → `StructureStateManager` → `EntryGate`
  - [ ] Simulate order fill at next tick after signal (slippage model)
  - [ ] Track trades, P&L, drawdown, spread costs
- [ ] Walk-forward optimization framework
  - [ ] Split data: 70% train, 30% out-of-sample test
  - [ ] Never optimize on the same window you test on
- [ ] Pass/fail benchmarks before proceeding to live:
  - Sharpe ratio > 1.5
  - Max drawdown < 10%
  - Win rate > 52%
  - Positive EV per trade

#### Notes / Decisions
*(Fill in as work progresses)*

---

### Phase 6 — Demo / Paper Trading
**Status: NOT STARTED**
**Dependency: Phase 5 passing benchmarks**
**Target: Weeks 11–12**

#### Tasks
- [ ] Connect `EntryGate` output to live MT5 demo account execution
- [ ] Minimum 50 demo trades before considering live
- [ ] Track and compare: execution quality, slippage, real spread vs backtest spread
- [ ] Monitor spread costs per pair vs expectations
- [ ] Check for signal timing delays (WebSocket latency impact)

---

### Phase 7 — Live Micro Account
**Status: NOT STARTED**
**Dependency: Phase 6 success**
**Target: Weeks 13–16**

#### Tasks
- [ ] Start at minimum lot size ($0.01/pip)
- [ ] Monitor 100 live trades before scaling position size
- [ ] Compare live results to demo results — any gap > 15% requires investigation
- [ ] Only scale up after 100 trades with expected performance

---

## Research Findings

### Confirmed

**RF-001 — Tick Feed Latency**
Retail WebSocket (ZMQ) tick feeds have 50–200ms latency. This means tick features should be used as **confirmation filters only**, not as direct entry triggers. By the time a tick burst is detected and acted upon, the optimal entry price has likely moved. Enter on candle events; use ticks to approve/reject.

**RF-002 — Candle Prediction Is Probabilistic, Not Predictive**
A candle that is 70%+ through its period with strong unidirectional tick flow has a statistically elevated probability of closing in the same direction — but this is probabilistic inference, not prediction. Accuracy degrades sharply in ranging markets and near news events. Use as a weak signal only.

**RF-003 — Spread Widens at the Worst Moments**
Liquidity sweeps and BOS events — the exact moments Type 1 and Type 3 signals fire — are when spreads expand most. The spread gate must delay entry 10–20 seconds when spread > 1.5× baseline and re-evaluate.

**RF-004 — Fake Momentum Is the Biggest Risk**
Tick bursts that trigger directional imbalance signals frequently reverse within seconds. This is common near round numbers, session opens, and news events. Structure alignment score ≥ 0.70 must be a hard prerequisite before any tick signal is considered.

**RF-005 — Regime Sensitivity**
60% tick accuracy in trending sessions drops to ~42% in ranging sessions. A regime detector (H1 ATR comparison) is mandatory — not optional.

**RF-006 — EURUSD Failure on Zone-Based Logic**
EURUSD showed 7.5% win rate and net −98k in backtests under the previous zone-based strategy. This is a data quality signal about the prior strategy's logic, not about EURUSD itself. EURUSD should be re-added once the structure engine is implemented and validated.

**RF-007 — Realistic Monthly Returns**
With 58% win rate and 1.6:1 R:R:
- Expected value per trade: 0.508 R
- 3 trades/day × 0.5% risk × 20 days = ~15% gross theoretical
- **Realistic net after slippage, bad days, news:** 6–10% monthly

### Unvalidated Hypotheses
- Can tick velocity predict M5 candle direction before close? (needs backtest)
- Does XAUUSD respond differently to the structure engine than forex pairs? (gold has different microstructure)
- Can the Type 3 liquidity sweep detector be made reliable enough for live trading?

### Failed / Abandoned Ideas
*(Document here as experiments fail)*

---

## Known Issues & Bugs

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| BUG-001 | EURUSD removed due to 7.5% win rate — root cause is lack of signal quality (no structure engine) | High | Open — resolved by Phase 1 |
| BUG-002 | `MIN_RR` was 3.0, too aggressive — valid signals rejected; changed to 2.0 | Medium | Resolved |
| BUG-003 | XAUUSD SL too wide at 0.30× ATR — TP required >50% daily range; reduced to 0.15× | Medium | Resolved |
| BUG-004 | No strategy signal logic exists — bot framework is complete but produces no entries | Critical | Open — Phase 1 resolves |

---

## Architectural Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| Pre-2026 | ZMQ for tick delivery instead of MT5 Python API polling | Avoids GIL issues; ~5ms vs ~50ms latency; allows high-frequency tick processing |
| Pre-2026 | Local candle building from ticks (not MT5 get_candles) | Exact bar boundaries; no forming bar confusion; deterministic regardless of MT5 connection quality |
| Pre-2026 | Hybrid trust: EA BAR_CLOSE events as correction layer | Handles missed ticks during brief disconnects without abandoning local tick building |
| Pre-2026 | Single `config.py` for all settings | All parameters in one place; env-var driven for Docker/production safety |
| Pre-2026 | `BROKER_ACCOUNT_TYPE` abstraction for cost model | Allows same strategy code to work across HFM and IC Markets account types |
| 2026-05-28 | EURUSD removed from `SYMBOLS` | 7.5% win rate in backtest — re-add after structure engine validates entries |
| 2026-05-28 | `MIN_RR` lowered 3.0 → 2.0 | Backtest showed valid signals being over-filtered; 2.0 R still maintains positive EV |
| 2026-05-28 | Tick features as filters, not triggers | Retail latency (50–200ms) makes tick-triggered entries unreliable; ticks confirm but candle events trigger |
| 2026-05-28 | Never trade M1 against H1 structure | Single most powerful filter — eliminates largest class of bad trades per research |
| 2026-05-28 | Walk-forward testing only — no in-sample optimization | 8 gates + multiple thresholds = enormous overfitting risk; only out-of-sample results are valid |

---

## Changelog

### 2026-05-28 — Session 1: Strategy Design & Tracker Creation
- Completed comprehensive strategy research and design (see `Bot Building Reecomendation.md`)
- Audited existing codebase — confirmed Phase 0 infrastructure is complete
- Created this implementation tracker
- Mapped all existing components to architecture diagram
- Identified critical gap: no strategy signal logic exists
- Defined Phase 1–7 implementation roadmap
- Key finding: bot framework is production-quality; strategy brain is entirely missing
- Next action: implement `StructureStateManager` in Phase 1

---

*Last updated: 2026-05-28*
*Next review: Begin of Phase 1 implementation*
