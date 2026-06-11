# Scalper Prime — Forex Trading Bot

A production-grade M1 scalping bot built on MetaTrader 5, Python, and Docker.
It ingests real-time ticks from MT5 via a custom EA bridge, analyses multi-timeframe
market structure and tick momentum, and executes trades automatically with tiered
take-profit and dynamic stop-loss management.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Architecture Overview](#2-architecture-overview)
3. [Quick Start](#3-quick-start)
4. [First-Run Walkthrough](#4-first-run-walkthrough)
5. [Dashboard](#5-dashboard)
6. [Environment Variables (.env) — Complete Reference](#6-environment-variables-env--complete-reference)
7. [Config File (src/config.py) — Complete Reference](#7-config-file-srcconfigpy--complete-reference)
8. [Trading Strategy Explained](#8-trading-strategy-explained)
9. [Trade Guardian — Continuous Re-Evaluation](#9-trade-guardian--continuous-re-evaluation)
10. [Tick Analytics & Velocity Reports](#10-tick-analytics--velocity-reports)
11. [Troubleshooting](#11-troubleshooting)
12. [Upgrading & Maintenance](#12-upgrading--maintenance)

---

## 1. System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| OS | Linux (x86_64) | Ubuntu 22.04 LTS |
| Docker | 24.x | Latest stable |
| Docker Compose | v2.x | Latest stable |
| RAM | 2 GB | 4 GB |
| Disk | 5 GB free | 20 GB free |
| Network | Stable broadband | VPS with < 20ms to broker |
| MT5 Account | Demo at any supported broker | IC Markets Raw or HFM Zero Spread |

The bot runs entirely inside Docker. You do **not** need MetaTrader 5 installed
on your host machine — the container handles Wine + MT5 automatically.

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  Docker Container (scalper-prime)                                 │
│                                                                  │
│  ┌─────────────┐   TCP :5556   ┌──────────────────────────────┐ │
│  │  MT5 + Wine │ ────────────► │  ZmqFeed (tick receiver)     │ │
│  │  + EA Bridge│               │         │                    │ │
│  └─────────────┘               │         ▼                    │ │
│                                │  CandleBuilder (M1/M5/H1…)  │ │
│  ┌──────────────────────────┐  │         │                    │ │
│  │  VNC Browser UI :3000    │  │         ▼                    │ │
│  └──────────────────────────┘  │  StrategyManager             │ │
│                                │  ├─ StructureState (BOS)     │ │
│  ┌──────────────────────────┐  │  ├─ TickAnalyzer             │ │
│  │  Flask Dashboard :8080   │  │  └─ ScalperAlignment         │ │
│  └──────────────────────────┘  │         │                    │ │
│                                │         ▼                    │ │
│                                │  EntryGate (9 gates)         │ │
│                                │         │                    │ │
│                                │         ▼                    │ │
│                                │  ExecutionService            │ │
│                                │  ├─ place_trade()            │ │
│                                │  ├─ TP1/TP2 partial closes   │ │
│                                │  └─ SL trailing              │ │
│                                └──────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

**Data flow on every M1 candle close:**
1. EA sends tick/candle event over TCP to `ZmqFeed`
2. `CandleBuilder` assembles OHLCV candles for all timeframes
3. `StrategyManager` evaluates 6 signal gates (alignment, spread, burst, tick score, candle score)
4. If signal passes → `EntryGate` checks 9 execution gates (news, session, velocity, drawdown, margin, EV, anti-dupe, min SL, min R:R)
5. If all gates pass → `ExecutionService` calculates lot size, places order via MT5

---

## 2a. Execution Correctness & Safety Guarantees

The trading core enforces the following invariants. These are not aspirational —
they are implemented and unit-tested in the live execution path.

- **Correct position sizing.** Lot size is derived from the true *per-pip* money
  value, `pip_value = trade_tick_value × (pip_size / trade_tick_size)`, in the
  account currency. The bot never assumes MT5's `trade_tick_value` (a *per-point*
  figure on 5-/3-digit symbols) is per-pip. If tick data is missing the trade is
  **rejected** (it is never sized off a guess).
- **Absolute risk ceiling.** After sizing and margin-fitting, a trade whose
  realised SL risk would exceed `MAX_TRADE_RISK_PCT` of balance is rejected, even
  at the broker-minimum lot.
- **Real, currency-correct margin.** Margin and equity-reserve checks use live MT5
  figures (`free_margin`, `order_check` margin, margin level). No leverage proxy.
- **Gates fail closed.** News, spread/cost, expected-value, margin, trade-quality
  and loss-streak gates **reject** when their inputs are missing, stale or
  invalid — they never wave a trade through on error.
- **Functional news shield.** Trading is blocked around high-impact events *and*
  whenever the news cache is stale beyond `NEWS_MAX_STALENESS_MIN` (cannot prove
  the market is clear → no entry).
- **Real expected-value gate.** EV uses the **broker-truth rolling win rate / avg
  win / avg loss** once `EV_MIN_SAMPLES` closed trades exist; before that it uses a
  haircut assumed win rate. Round-trip cost (spread + commission-in-pips) must be
  below `COST_MAX_FRACTION_OF_TARGET` of the TP1 target.
- **Tiered take-profit that actually works.** The broker order's SL is the stop
  and its **TP is set to TP2** (the final target / hard backstop). The software
  manages the TP1 partial, profit-lock and structural trailing *before* TP2, so
  the tiered exit is not pre-empted by the broker closing the whole position at TP1.
- **Broker-truth analytics.** Every close is reconciled against MT5 deal history
  (realised net P&L, including swap/commission). Win/loss is classified by the
  **sign of realised money** — never reconstructed from maximum-favourable
  excursion. If the bot loses money, the analytics show losses.
- **Fill-anchored levels.** After the order fills, the tracked entry, SL distance,
  TP1/TP2 and MFE are anchored to the **actual fill price** (not the stale
  signal-time price), so risk and R:R reflect what was really executed.
- **EV reflects the real exit plan.** The EV gate's expected win is the
  **blended** value of the TP1 partial + TP2 + runner (weighted by close
  fractions), not a naive "100% at TP1". The runner is modelled conservatively
  at `RUNNER_EXIT_RR`.
- **Controlled exploration bootstrap.** Because a fresh system has no broker-truth
  samples to compute EV from, up to `EXPLORATION_TRADES_PER_DAY` minimum-risk
  probe trades per symbol may bypass **only** the EV edge requirement (every
  other gate still applies) until `EV_MIN_SAMPLES` real outcomes exist. After
  that, the rolling broker-truth EV governs and exploration stops. Exploration
  never bypasses a fail-closed ("unknown cost") condition.
- **Reversal re-entry cooldown.** After a reversal/structural-reversal exit, new
  entries on that symbol are blocked for `REVERSAL_REENTRY_COOLDOWN_SEC` to stop
  whipsaw churn.
- **Ranging suppression.** In a detected ranging regime the alignment threshold
  is raised by `RANGING_ALIGN_PENALTY`, so M1 chop must clear a higher bar.
- **News coverage is explicit.** With no external `NEWS_API_KEY`, the bot logs a
  prominent startup warning that coverage is MT5-best-effort; set
  `NEWS_REQUIRE_EXTERNAL_FEED=true` to fail closed (block all trading) until a
  real external feed is configured.
- **Gold (XAUUSD) runs a dedicated, volatility-adaptive profile.** A gold "pip"
  is $0.01, so FX-calibrated fixed pip stops are economically tiny and get
  destroyed by noise + cost (a 10-pip stop = $0.10, smaller than one gold tick,
  while round-trip cost ≈ $0.20–0.40 — guaranteed negative). Gold therefore
  sizes its stop from its **own M5 ATR** (`SCALPER_ATR_PRIMARY_SYMBOLS`):
  `SL = clamp(structure, 0.7×ATR, 1.5×ATR)` then clamped to absolute
  `150–500` pips ($1.50–$5.00). The TP1/TP2 R-multiples are unchanged so targets
  scale to ~$2–$10 automatically. Gold also has its own session
  (`12:00–21:00 UTC`, London-PM + NY/COMEX), spread cap (`80` pips = $0.80), and
  hold limit (`20` min). **Only XAUUSD is affected — every FX symbol keeps its
  unchanged swing-first, fixed-pip behaviour.** Gold is still gated by the same
  EV/cost/exploration logic and must prove positive EV on its own data.

---

## 3. Quick Start

### Step 1 — Clone and create your .env

```bash
git clone https://github.com/Josewathome/Scraper-forex-bot.git
cd Scraper-forex-bot
cp .env.example .env
```

Open `.env` and fill in **at minimum** these three required values:

```env
TRADING_ID=123456
MT5_PASSWORD=YourMT5Password
MT5_SERVER=YourBroker-ServerName
```

### Step 2 — Create required host directories

```bash
mkdir -p analytics logs
```

These are bind-mounted into the container. Without them Docker will create them
as root and the bot will fail to write logs and analytics.

### Step 3 — Start the bot

```bash
docker compose up -d
```

### Step 4 — Watch the startup sequence

```bash
docker compose logs -f scalper-prime
```

You will see stages:
```
[Stage 1] KasmVNC performance tuning
[Stage 2] Wine machine GUID pinning
[Stage 3] Mono .NET installation
[Stage 4] MetaTrader 5 installation & login
[Stage 5] ZMQ library & EA compilation
[Stage 6] Bot Python launch
```

Full startup takes **3–8 minutes** on first run (MT5 installation).
Subsequent starts take **30–60 seconds**.

### Step 5 — Verify it is running

```bash
# See live log stream
docker compose logs -f scalper-prime

# Check bot is receiving ticks
docker exec scalper-prime grep "TICK\|ALIGN\|SIGNAL" /bot/logs/trading_bot.log | tail -20

# Open the dashboard
# Navigate to http://localhost:8080 in your browser
```

### Stop the bot

```bash
docker compose down
```

---

## 4. First-Run Walkthrough

### Day 1: Observation mode (STRATEGY_GATE_ENABLED=false)

By default the bot runs in **observation mode** — it evaluates every M1 candle
and logs signals but places no trades. This is intentional. Use this time to:

1. **Watch the logs** for `STRATEGY SIGNAL` lines. Each line shows the symbol,
   direction, confidence score, and why each gate passed or blocked.

2. **Open the dashboard** at `http://localhost:8080`. Log in with your
   `DASHBOARD_PASSWORD` (or the auto-generated one printed in the logs).
   Go to the **Velocity Report** tab to see how many signals are being generated.

3. **Check the MT5 UI** via VNC at `http://localhost:3000`. Verify the EA
   (`ZoneBotBridge`) is attached to the chart and the "Algo Trading" button
   is green in MT5.

4. After **at least 2–3 days** of observation, review signal quality in the logs.
   If signals look reasonable (direction matches obvious price action), enable trading.

### Enabling live trading

```env
# In your .env file:
STRATEGY_GATE_ENABLED=true
```

Then restart:

```bash
docker compose down && docker compose up -d
```

---

## 5. Dashboard

Access the dashboard at `http://your-server-ip:8080`

| Tab | What it shows |
|---|---|
| **Analytics** | Win rate, total P&L, drawdown, per-symbol breakdown, recent trades |
| **Pairs** | Active trading symbols — add or remove without restarting |
| **Configuration** | Live risk settings, TP ratios, balance management |
| **Backtest** | Run and view backtest reports |
| **Velocity Report** | Tick analytics — daily/hourly/weekly signal pass rates, win rates by velocity band, threshold recommendations |
| **Logs** | Last 200 lines of the trading log with colour-coded severity |

### Dashboard endpoints (for scripting / monitoring)

All endpoints require `Authorization: Bearer <token>` except auth routes.

```
POST /api/auth/login              { "password": "..." }
GET  /api/status                  Bot uptime, server time, symbols
GET  /api/stats?days=30           Trade analytics (period filter optional)
GET  /api/trades?n=50             Last N trades
GET  /api/logs?n=200              Last N log lines
GET  /api/velocity-report         Full tick analytics data (JSON)
POST /api/velocity-report/refresh Trigger immediate daily report
PATCH /api/config                 Live config update (no restart needed)
```

---

## 6. Environment Variables (.env) — Complete Reference

Copy `.env.example` to `.env` and edit. Variables marked **[REQUIRED]** will
cause the bot to refuse to start if missing. All others have sensible defaults.

---

### MT5 Credentials — REQUIRED

```env
TRADING_ID=123456
```
Your MT5 login number. Shown in the top-left corner of the MT5 terminal.
Must be an integer. The bot will not start without this.

```env
MT5_PASSWORD=YourBrokerPassword
```
Your MT5 account password. Used to authenticate with your broker's server.

```env
MT5_SERVER=YourBroker-ServerName
```
Your broker's MT5 server name exactly as shown in the MT5 login dialog.
Examples: `HFMarketsKE-Demo2`, `HFMarketsKE-Live01`, `ICMarkets-Demo`, `ICMarkets-Live01`

---

### Broker / Account Type — REQUIRED

```env
BROKER_ACCOUNT_TYPE=PREMIUM
```
Controls how the bot calculates spread cost and commission for every trade.
Must match the account type you logged into above.

| Value | Account Type | Cost Model |
|---|---|---|
| `PREMIUM` | HFM Premium | Spread only (~1.4+ pips GBPUSD). No commission. |
| `PRO` | HFM Pro | Spread only (~0.5+ pips GBPUSD). No commission. |
| `ZERO_SPREAD` | HFM Zero Spread | Near-zero spread + $3/lot commission (majors), $5/lot (Gold) |
| `IC_MARKETS_RAW` | IC Markets Raw | ECN spread (~0.1 pip) + $3.50/lot/side commission |

**Which to choose:** For scalping, `ZERO_SPREAD` or `IC_MARKETS_RAW` are best
because raw-spread accounts have predictable, low execution costs. Premium/Pro
accounts have variable spread that widens significantly during news events.

```env
IC_COMMISSION_PER_LOT_USD=3.50
```
Override IC Markets commission if their fees change. Default is $3.50/lot/side.
Only relevant when `BROKER_ACCOUNT_TYPE=IC_MARKETS_RAW`.

---

### VNC Access

```env
VNC_USER=admin
VNC_PASSWORD=change_me_strong_password
```
Credentials for the browser-based MT5 UI at `http://localhost:3000`.
Use a strong password — this port is exposed on your host.
You can watch MT5 executing trades in real time through this interface.

---

### News API

```env
NEWS_API_KEY=
```
API key for an external forex news feed (ForexFactory/MyFxBook via a
companion `forex-api` container). Leave blank to use only MT5's built-in
news feed. The built-in feed is sufficient for most use cases.

```env
NEWS_API_URL=http://forex-api:8000
```
URL of the external forex-api service. Uses the Docker service name so it
resolves inside the shared network automatically. Only change this if you run
the news API on a different host or port.

---

### Dashboard

```env
DASHBOARD_PASSWORD=
```
Password for the web dashboard at `http://localhost:8080`. If left blank, a
random 16-character password is generated and printed to the container log at
startup. Set a permanent value so you don't need to check logs every time.

```env
JWT_ACCESS_EXPIRES_MINUTES=15
```
How long a login session token stays valid before requiring silent refresh.
Default 15 minutes is secure for most setups. Increase if you find yourself
getting logged out too often.

```env
JWT_REFRESH_EXPIRES_DAYS=7
```
How long the "remember me" refresh token lasts. After 7 days you must log in
again. Increase for convenience, decrease for tighter security.

---

### Email Alerts

```env
EMAIL_USER=youraddress@gmail.com
EMAIL_PASSWORD=your-16-char-app-password
EMAIL_RECIPIENT=recipient@example.com
```
Gmail SMTP credentials for trade alert emails. Use a **Gmail App Password**,
not your regular Gmail password. Generate one at:
`Google Account → Security → 2-Step Verification → App Passwords`

The bot sends emails on: MT5 connection failure (requires immediate action).

```env
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
```
SMTP server settings. Defaults work for Gmail. Change only if using another
email provider (e.g. Outlook: `smtp-mail.outlook.com:587`).

---

### Account Context

```env
ACCOUNT_CURRENCY=KES
```
Your MT5 account's base currency. Used for P&L display and converting
commission values into the correct currency for position sizing.
Common values: `USD`, `EUR`, `GBP`, `KES`, `NGN`, `ZAR`

```env
ACCOUNT_BALANCE=30000
```
Fallback balance used only if MT5 is unreachable at startup. The bot reads
your live balance from MT5 in real time — this value is rarely used. Set it
to approximately your real account balance as a safety fallback.

```env
KES_PER_USD=130.0
```
Current KES/USD conversion rate. Used when `ACCOUNT_CURRENCY=KES` to convert
USD-denominated commissions into KES for accurate position sizing.
Update periodically when the rate drifts significantly (every few weeks).

---

### Strategy Gate

```env
STRATEGY_GATE_ENABLED=false
```
Master switch for trade execution.

- `false` — **Observation mode.** The bot analyses every candle and logs all
  signals, but places zero trades. Use this for initial validation.
- `true` — **Live mode.** Validated signals pass through all gates and are
  executed via MT5.

**Always start with `false` on a new account or after major config changes.**
Watch signals for 2–3 days before enabling.

---

### Tick Analytics

```env
TICK_ANALYTICS_ENABLED=true
```
Master kill-switch for the entire tick analytics subsystem.
- `true` (default) — evaluations are recorded, files are written, schedules run
- `false` — nothing is recorded, no files written, no scheduler jobs registered.
  Use when you want zero analytics overhead.

```env
TICK_ANALYTICS_SAVE=true
```
Controls whether analytics data is written to disk.
- `true` (default) — writes JSONL files and the text report to `/bot/analytics/`
- `false` — in-memory recording continues (data exists for the session) but
  **nothing is written to disk**. Use when the analytics volume isn't mounted
  or you want to reduce disk I/O.

```env
TICK_ANALYTICS_SCHEDULE=true
```
Controls whether hourly/daily/weekly cron jobs are registered at startup.
- `true` (default) — reports auto-generate every hour (hourly), at 22:00 UTC
  (daily), and every Friday at 21:00 UTC (weekly)
- `false` — no automatic reports. You can still trigger them manually from
  the dashboard **⚡ Regenerate Now** button or via `POST /api/velocity-report/refresh`

---

### Daily Drawdown Limit

```env
DAILY_DRAWDOWN_LIMIT_PCT=6.0
```
Circuit breaker. If the account equity drops this percentage below the opening
equity of the current trading day, the bot stops placing new trades for the
rest of that day. Existing open trades continue to be managed.

Example: `6.0` means the bot stops after losing 6% of the day's starting equity.
This protects against runaway losing days. The counter resets at UTC midnight.

---

### Take-Profit Ratios

```env
TP1_RR_RATIO=1.5
```
TP1 (first partial close) is placed at `entry ± (SL_distance × 1.5)` and is managed
in software. At TP1, a grade-dependent fraction of the position is closed (60% grade
C / 50% B / 40% A+) and the stop-loss moves to lock in 0.3R profit. Note: the broker
order's hard take-profit is set to **TP2** (the final backstop), not TP1, so the TP1
partial and the trailing runner are not pre-empted by the broker.

```env
TP2_RR_RATIO=2.5
```
TP2 (second partial close) is placed at `entry ± (SL_distance × 2.5)`.
At TP2, 25% of the remaining position is closed. The final 15% runs as a
"runner" with a trailing stop.

**Relationship:** `TP2_RR_RATIO` must always be greater than `TP1_RR_RATIO`.
Increasing these values means higher profit targets but lower hit rate.
Decreasing them means faster profit-taking but smaller wins.

---

### Scalper Mode

```env
SCALPER_MODE=false
```
Enables the M1/M5 momentum scalper engine (4–12 pip targets on 1-minute charts).
Requires a low-spread account type (`ZERO_SPREAD` or `IC_MARKETS_RAW`).
Default `false` uses the standard intraday mode.

```env
SCALPER_MAX_HOLD_MINUTES=30
```
Maximum time a scalper trade can remain open before being force-closed.
If the trade is in profit at expiry it is closed for the current profit.
If it is at more than -0.5R loss it is cut. If it is between 0 and -0.5R
it remains open until SL or TP is hit.

```env
SCALPER_TP1_RR=1.5
SCALPER_TP2_RR=2.0
```
Scalper-specific TP ratios. These override `TP1_RR_RATIO` / `TP2_RR_RATIO`
when `SCALPER_MODE=true`. Kept tighter than intraday mode because M1 targets
are smaller.

```env
SCALPER_MAX_DAILY_TRADES=0
```
Hard cap on trades per calendar day. `0` = no cap (recommended). The margin
validation and drawdown circuit breaker handle exposure control without
needing an arbitrary trade counter.

```env
SCALPER_MAX_OPEN_TRADES=0
```
Hard cap on simultaneous open positions. `0` = no cap (recommended). Margin
validation blocks new trades when free margin is insufficient.

---

### Signal Quality Thresholds

```env
SCALPER_MIN_ALIGNMENT_SCORE=0.55
```
Minimum combined M1+M5 structural alignment score (0.0–1.0) for a signal to
proceed. Higher = fewer but higher-confidence signals. Lower = more signals
but noisier entries. Range: `0.45` (aggressive) to `0.75` (conservative).

```env
SCALPER_MIN_TICK_SCORE=0.50
```
Minimum tick composite score (0.0–1.0) covering velocity, acceleration,
directional imbalance, and price displacement. Signals with spread above 1.5×
baseline receive a penalty that can push this score below threshold.
Range: `0.45` to `0.70`.

```env
SCALPER_MIN_CANDLE_SCORE=0.30
```
Minimum absolute candle progress score (0.0–1.0) for the forming M1 bar.
Measures whether the current candle body, wicks, and tick bias agree with the
signal direction. Range: `0.25` (accept weak candle confirmation) to `0.50`
(require strong candle momentum).

```env
SCALPER_MIN_TICK_VELOCITY=1.0
```
Minimum tick rate (ticks per second, measured over the last 5 seconds) before
any entry is allowed. Guards against entering during dead, illiquid markets.

| Velocity | Market Condition |
|---|---|
| 0–0.2 ticks/s | Weekend / market closed |
| 0.2–1.0 ticks/s | Thin / Asian session (for EUR/GBP pairs) |
| 1.0–4.0 ticks/s | Moderate — tradeable with caution |
| 4.0–12.0 ticks/s | Active London / NY session |

Default `1.0` allows moderate-liquidity sessions. Raise to `2.0` if you see
too many false signals during quiet periods.

---

### EV Gate

```env
ASSUMED_WIN_RATE=0.52
```
The win rate used by the Expected Value gate when no live analytics data is
available yet. The EV gate formula is:
`EV = (win_rate × avg_win_pips) − ((1 − win_rate) × avg_loss_pips) − costs`
A trade is rejected if EV falls below `EV_MIN_PIPS`. This assumes 52%
win rate on a 1.5R target means the edge is slightly positive after costs.
Adjust downward (`0.48`) to allow more trades or upward (`0.55`) to be
more selective.

```env
EV_MIN_PIPS=0.10
```
The minimum expected value in pips for a trade to be taken. Trades where the
math produces a negative or near-zero EV (because the SL is too small relative
to spread + commission) are rejected before placement.

---

### Anti-Duplicate Guard

```env
ANTI_DUPE_SECONDS=60
```
Prevents re-entering the same symbol in the same direction within this many
seconds of the last fill. Blocks runaway signal loops (where a formation
candle keeps generating the same signal on every tick) without imposing a
blanket frequency cap.

Set to `0` to disable (not recommended). Set to `120` or higher for more
conservative duplicate prevention.

---

### Margin Safety

```env
MARGIN_SAFETY_FACTOR=1.5
```
Required free margin multiplier. A trade is rejected if:
`required_margin × MARGIN_SAFETY_FACTOR > free_margin`

At `1.5`, you must have 1.5× the required margin available before a trade
is placed. This provides a buffer so one trade opening doesn't immediately
trigger a margin call for the next. Increase to `2.0` for a larger buffer,
decrease to `1.2` if you want tighter capital utilisation.

```env
MIN_MARGIN_LEVEL_PCT=200.0
```
The minimum margin level percentage (equity / used_margin × 100%) the account
must maintain. MT5 issues a margin call at 100%. This setting blocks new trades
at 200%, giving a 2× safety buffer above the margin call threshold.

---

### Trade Guardian Thresholds

The Trade Guardian re-evaluates every open trade at each M1 close and asks:
*"If I had no position right now, would I still enter this trade?"*

The answer is a **continuation score** (0.0–1.0) computed from three factors:
- **40%** — M1 market structure still in the trade direction
- **35%** — Fresh signal alignment (confirming vs opposing)
- **25%** — Current P&L relative to risk (in-profit trades get holding latitude)

The score falls into one of four bands:

```env
REEVAL_HOLD_THRESHOLD=0.55
```
Score at or above this → thesis intact, continue holding. Clears defensive mode
if it was previously set.

```env
REEVAL_DEFENSIVE_THRESHOLD=0.40
```
Score between `REEVAL_DEFENSIVE_THRESHOLD` and `REEVAL_HOLD_THRESHOLD` → thesis
is weakening. The bot enters **defensive mode**: the stop-loss is immediately
tightened to the nearest confirmed M1 swing point (falls back to breakeven +
1 pip if no structural swing is available within range).

```env
REEVAL_EXIT_THRESHOLD=0.25
```
Score between `REEVAL_EXIT_THRESHOLD` and `REEVAL_DEFENSIVE_THRESHOLD` → thesis
degraded. If the trade is already ≥ 0.8R in profit and TP1 has not yet fired,
the bot takes a **50% partial exit** to lock in profit before conditions
deteriorate further.

Score below `REEVAL_EXIT_THRESHOLD` → thesis invalidated. The position is closed
immediately at market regardless of where TP or SL are placed.

```env
REEVAL_REVERSAL_CONF=0.70
```
If a fresh signal in the **opposite direction** has a confidence score at or
above this value, the existing position is closed. The entry gate then evaluates
the reversal signal as a brand-new trade on the next tick cycle. This is how
the bot changes direction when the market reverses.

```env
TRAIL_SL_BUFFER_PIPS=1.5
```
When trailing the stop-loss to a structural swing point, this many pips of
breathing room are added beyond the swing high/low. Prevents the stop from
being placed exactly at the swing level where liquidity often rests.

**How the structural SL trail works:**
At each M1 close, if the trade is in profit, the bot checks whether there is a
more recent confirmed swing point that is closer to current price than the
existing SL. If yes, and if the new SL would still be at least 2 pips from
current price, the SL is moved. This only ever tightens the stop — it never
moves the SL against the trade and never moves it by a fixed pip distance.

---

### Advanced / Rarely Changed

```env
MAX_LOT_SIZE=10.0
```
Absolute hard cap on any single trade's lot size. Prevents runaway position
sizing from a bug or extreme risk calculation. `10.0` lots is $100,000
notional on GBPUSD — appropriate for most retail accounts.

```env
ZMQ_ENDPOINT=tcp://127.0.0.1:5556
```
TCP endpoint the MT5 EA (`ZoneBotBridge.mq5`) uses to send ticks to the
Python bot. Both processes run inside the same container so this is loopback.
Only change if you move the EA to a different port or machine.

```env
SCALPER_M5_EMA_PERIOD=10
```
Period of the M5 EMA used by the alignment engine for trend context.
Shorter periods (e.g. `7`) respond faster but are noisier. Longer periods
(e.g. `20`) are smoother but lag more. Default `10` balances responsiveness
with stability on a 5-minute chart.

```env
SCALPER_ATR_SL_FRACTION=0.4
```
When no valid swing high/low is within the max SL distance, the bot falls back
to an ATR-based stop loss. The SL distance is set to `M5_ATR × this_fraction`.
`0.4` = 40% of the current M5 ATR. Increase toward `0.6` for wider stops in
volatile conditions; decrease toward `0.25` for tighter scalper stops.

---

## 7. Config File (src/config.py) — Complete Reference

`config.py` is the **single source of truth** for all bot settings. Every
value reads from an environment variable first and falls back to the hardcoded
default. You should not need to edit `config.py` directly — all meaningful
settings have a corresponding `.env` variable. This section explains every
setting in the file so you understand what it controls.

---

### MT5 Credentials
| Setting | Default | Notes |
|---|---|---|
| `TRADING_ID` | — (required) | MT5 login integer. Bot exits if missing. |
| `MT5_PASSWORD` | — (required) | MT5 account password. |
| `MT5_SERVER` | — (required) | Broker MT5 server name. |
| `ACCOUNT_CURRENCY` | `"KES"` | Display currency for P&L and sizing. |
| `ACCOUNT_BALANCE` | `30000` | Fallback balance when MT5 unreachable at start. |
| `LEVERAGE` | `"1:400"` | Informational only — actual leverage set by broker. |
| `MT5_PATH` | `""` | Path to MT5 terminal executable. Empty = auto-detect. |

---

### Broker / Commission

| Setting | Default | Notes |
|---|---|---|
| `BROKER_ACCOUNT_TYPE` | `"ZERO_SPREAD"` | Controls commission model. See .env section. |
| `IC_COMMISSION_PER_LOT_USD` | `3.50` | IC Markets commission per lot per side (USD). |
| `IC_COMMISSION_USD` | dict | Per-symbol IC commission rates. |
| `HFM_COMMISSION_USD` | dict | Per-symbol HFM Zero Spread commission rates. |
| `COMMISSION_PER_LOT` | `3.0` | Generic fallback commission (USD/lot). Used when broker type is unknown. |

---

### Risk Management

| Setting | Default | Notes |
|---|---|---|
| `RISK_PERCENT` | `2.0` | Percentage of account balance risked per trade. `2.0` = 2%. |
| `MIN_RR` | `1.5` | Minimum reward:risk ratio. Trades where TP1 is less than 1.5× the SL distance are rejected. |
| `MAX_OPEN_TRADES` | `20` | Soft reference value. Not enforced as a hard gate — margin validation handles exposure. |
| `MAX_TRADES_PER_SYMBOL` | `10` | Soft reference. Not enforced as a hard gate — anti-duplicate handles same-symbol frequency. |
| `MAX_LOT_SIZE` | `10.0` | Hard cap per trade in lots. Prevents runaway sizing. |

**How position sizing works:**
```
risk_usd    = account_balance × (RISK_PERCENT / 100)
sl_pips     = distance from entry to stop-loss in pips
lot_size    = risk_usd / (sl_pips × pip_value_per_lot)
lot_size    = min(lot_size, MAX_LOT_SIZE)
lot_size    = max(lot_size, 0.01)  # MT5 minimum
```

---

### Margin Safety

| Setting | Default | Notes |
|---|---|---|
| `MARGIN_SAFETY_FACTOR` | `1.5` | Free margin must be ≥ required_margin × this factor. |
| `MIN_MARGIN_LEVEL_PCT` | `200.0` | Minimum margin level % before new trades blocked. |

---

### News Filter

| Setting | Default | Notes |
|---|---|---|
| `HIGH_IMPACT_BUFFER_MINS` | `30` | Minutes before a high-impact news event to stop trading. |
| `MEDIUM_IMPACT_BUFFER_MINS` | `0` | Minutes before medium-impact events. `0` = no block. |
| `NEWS_BUFFER_MINUTES` | `15` | Post-event block duration (minutes). |
| `STRICT_NEWS_FILTER` | `True` | If `True`, also blocks during low-impact events. |
| `ATR_COST_THRESHOLD` | `0.25` | Block new trades if spread cost > 25% of current ATR. Prevents trading when spread is unusually wide relative to volatility. |
| `NEWS_REFRESH_INTERVAL_MINUTES` | `10` | How often the news cache is refreshed from the API. |

---

### Trade Monitor

| Setting | Default | Notes |
|---|---|---|
| `MONITOR_ENABLED` | `True` | Whether open trade monitoring (TP/SL management) is active. Disabling stops all automated trade management. |
| `MONITOR_ACT_ON_SIGNALS` | `True` | Whether the monitor takes action (close/modify) or only logs. |
| `BREAKEVEN_RR_TRIGGER` | `1.5` | Move SL to breakeven when trade reaches this R multiple. |
| `MAX_CONSECUTIVE_LOSSES` | `5` | Number of consecutive losses before a warning is logged. |
| `LOSS_STREAK_PAUSE_HOURS` | `0` | Hours to pause trading after `MAX_CONSECUTIVE_LOSSES`. `0` = no pause (analytics-only tracking). Set to `4` to re-enable pausing. |

---

### Tiered Take Profit

The bot uses a three-tier exit system to lock in profits progressively:

| Setting | Default | Notes |
|---|---|---|
| `TIERED_TP_ENABLED` | `True` | Enable/disable tiered exits. If `False`, the full position closes at TP1. |
| `TIERED_TP1_RATIO` | `1.5` | TP1 price = entry ± (SL_dist × 1.5). |
| `TIERED_TP1_CLOSE_PCT` | `0.60` | Close 60% of position at TP1 (grade C trades). |
| `TIERED_TP1_CLOSE_PCT_B` | `0.50` | Close 50% at TP1 for grade B trades (slightly more left for TP2). |
| `TIERED_TP1_CLOSE_PCT_A_PLUS` | `0.40` | Close 40% at TP1 for A+ grade trades (more left to run). |
| `TIERED_TP2_RATIO` | `2.0` | TP2 price = entry ± (SL_dist × 2.0). |
| `TIERED_TP2_CLOSE_PCT` | `0.25` | Close 25% of remaining at TP2. |
| `FINAL_RUNNER_PCT` | `0.15` | Remaining 15% stays open as a runner with trailing stop. |
| `TP1_PROFIT_LOCK_R` | `0.3` | After TP1 hit, SL moves to lock in 0.3R profit (above entry for longs). |

**Example trade at TP stages (1.0 lot initial):**
```
Entry:   1.28000  SL: 1.27900  (10 pips)
TP1:     1.28150  (1.5R = 15 pips) → close 0.60 lots, SL moves to 1.28003
TP2:     1.28200  (2.0R = 20 pips) → close 0.25 lots
Runner:  0.15 lots remains with trailing stop
```

---

### Minimum SL Distances

| Setting | Default Values |
|---|---|
| `MIN_SL_PIPS` | EURUSD/GBPUSD/AUDUSD/USDCHF: 3.0, USDJPY: 5.0, XAUUSD: 30.0 |
| `MIN_SL_PIPS_DEFAULT` | `3.0` (fallback for unlisted symbols) |

Trades where the SL is closer than this minimum are rejected. This prevents
entries where the stop is so tight it would be triggered by normal bid-ask
spread noise rather than real adverse movement.

---

### Scalper Signal Thresholds

| Setting | Default | Notes |
|---|---|---|
| `SCALPER_MIN_ALIGNMENT_SCORE` | `0.55` | M1+M5 structural alignment required. |
| `SCALPER_MIN_TICK_SCORE` | `0.50` | Tick momentum composite required. |
| `SCALPER_MIN_CANDLE_SCORE` | `0.30` | Forming candle direction strength required. |
| `SCALPER_MIN_TICK_VELOCITY` | `1.0` | Minimum ticks/second (measured over 5s window). |
| `SCALPER_M5_EMA_PERIOD` | `10` | EMA period for M5 trend context. |
| `SCALPER_M1_CONSENSUS_MIN` | `3` | Minimum M1 bars agreeing with direction (out of last 5). |
| `SCALPER_ATR_SL_FRACTION` | `0.4` | ATR fraction used for fallback SL when no swing available. |

---

### Scalper SL Ranges

| Setting | Values |
|---|---|
| `SCALPER_MIN_SL_PIPS` | GBPUSD/USDJPY/AUDUSD/USDCHF/EURUSD: 3.0, XAUUSD: 10.0 |
| `SCALPER_MAX_SL_PIPS` | GBPUSD/USDJPY/AUDUSD/USDCHF/EURUSD: 12.0, XAUUSD: 40.0 |

If the structure-derived SL is wider than `SCALPER_MAX_SL_PIPS`, the bot
falls back to an ATR-based SL. If the ATR SL is also too wide, the trade
is rejected.

---

### Session Windows

The bot only trades each symbol during its active session. All times are UTC.

| Symbol | Window | Rationale |
|---|---|---|
| `GBPUSD` | 05:00–17:00 | Pre-London open through NY close |
| `XAUUSD` | 05:00–22:00 | London + NY + EU afterhours institutional flow |
| `USDJPY` | 00:00–17:00 | Tokyo + London + NY |
| `AUDUSD` | 22:00–13:00 | Sydney open (wraps midnight) + London midday |
| `USDCHF` | 05:00–17:00 | European pair, matches GBPUSD |

These are defined in `SCALPER_SYMBOL_SESSIONS` in `config.py`. Edit directly
in the file to change them (env overrides not yet wired for per-symbol windows).

---

### EV & Anti-Duplicate

| Setting | Default | Notes |
|---|---|---|
| `ASSUMED_WIN_RATE` | `0.52` | Assumed win rate for EV calculation before live data available. |
| `EV_MIN_PIPS` | `0.10` | Minimum expected value (pips) for a trade to be accepted. |
| `ANTI_DUPE_SECONDS` | `60` | Cooldown (seconds) before same symbol+direction can be re-entered. |

---

### Candle & History Settings

| Setting | Default | Notes |
|---|---|---|
| `M1_CANDLE_COUNT` | `60` | Number of M1 candles fetched from MT5 at startup for seeding. |
| `M5_CANDLE_COUNT` | `60` | Number of M5 candles fetched at startup. |

---

### Retention & Checkpoints

| Setting | Default | Notes |
|---|---|---|
| `LOG_RETENTION_DAYS` | `3` | Log files older than this are deleted. |
| `CACHE_RETENTION_DAYS` | `2` | News cache files older than this are deleted. |
| `CHECKPOINT_DIR` | `".checkpoints"` | Directory where runtime state is saved. |
| `CHECKPOINT_INTERVAL_MIN` | `5` | How often (minutes) the bot saves its state. If it crashes, it resumes from the last checkpoint. |
| `CHECKPOINT_RETAIN_DAYS` | `7` | Checkpoint files older than this are deleted. |

---

### Dashboard Settings

| Setting | Default | Notes |
|---|---|---|
| `DASHBOARD_ENABLED` | `True` | Set to `False` to disable the web dashboard entirely. |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on inside the container (mapped to host in docker-compose.yml). |

---

### Tick Analytics

| Setting | Default | Notes |
|---|---|---|
| `TICK_ANALYTICS_ENABLED` | `True` | Master switch. |
| `TICK_ANALYTICS_SAVE` | `True` | Write files to `/bot/analytics/`. |
| `TICK_ANALYTICS_SCHEDULE` | `True` | Register automatic hourly/daily/weekly jobs. |

---

## 8. Trading Strategy Explained

The bot implements a **multi-confirmation M1 scalper** based on Smart Money
Concepts (SMC). It requires three independent systems to agree before placing
a trade.

### The three confirmation layers

**Layer 1 — Market Structure (M5 + M1)**
The `ScalperAlignmentEngine` scores the current structural bias:
- M5 EMA(10) must be sloping in the trade direction (price above rising EMA = bullish)
- M1 structure must show a Break of Structure (BOS) or Change of Character (CHoCH)
  in the same direction (determined by `StructureStateManager`)
- Composite score (M1 weight 65%, M5 weight 35%) must exceed `SCALPER_MIN_ALIGNMENT_SCORE`

**Layer 2 — Tick Momentum**
The `TickAnalyzer` scores current order flow:
- Tick velocity (ticks/second) must exceed `SCALPER_MIN_TICK_VELOCITY`
- Directional imbalance (up-ticks vs down-ticks over last 20 ticks) must agree with direction
- Spread must not be in "DANGEROUS" state (>2× baseline)
- Composite tick score must exceed `SCALPER_MIN_TICK_SCORE`

**Layer 3 — Candle Confirmation**
The `LiveCandleAnalyzer` scores the forming M1 candle:
- Body direction must agree with the signal
- Wick pressure must not contradict (no large rejection wick against the direction)
- Time-weighted score (early bars weighted more on ticks, late bars weighted on body) must exceed `SCALPER_MIN_CANDLE_SCORE`

### Entry gate chain (all 9 must pass)

| Gate | What it checks |
|---|---|
| 1. News | No high-impact event within 30 min pre / 10 min post |
| 2. Session | Current UTC hour is within the symbol's trading window |
| 3. Tick velocity | ≥ 1.0 ticks/sec in the last 5 seconds |
| 4. Drawdown | Daily equity loss < `DAILY_DRAWDOWN_LIMIT_PCT` |
| 5. Margin | Free margin ≥ required_margin × `MARGIN_SAFETY_FACTOR` |
| 6. EV | Expected value > `EV_MIN_PIPS` after spread + commission |
| 7. Anti-duplicate | Same symbol+direction not filled within `ANTI_DUPE_SECONDS` |
| 8. Min SL | SL distance ≥ symbol minimum pips |
| 9. Min R:R | TP1 distance ≥ SL distance × `MIN_RR` |

### Trade grades

| Grade | Confidence | TP1 Close % |
|---|---|---|
| A | ≥ 85% | 40% closed at TP1 (more left to run) |
| B | 70–84% | 50% closed at TP1 |
| C | < 70% | 60% closed at TP1 (take profit quickly) |

---

## 9. Trade Guardian — Continuous Re-Evaluation

Once a trade is open, the bot does not simply wait for TP or SL to be hit.
Every M1 close triggers a full re-evaluation of every open position, using
the same market analysis pipeline as the entry system.

### The core question

At each M1 close, for every open trade:

> *"If I had no position open right now, would I still enter this exact trade?"*

If the answer is clearly yes → hold.
If the answer is uncertain → enter defensive mode, tighten the stop.
If the answer is no → exit before TP or SL is reached.

### Continuation score

The bot computes a **continuation score** (0.0–1.0) from three independent factors:

| Factor | Weight | What it measures |
|---|---|---|
| M1 structural state | 40% | Is the market structure still in the trade direction? |
| Fresh signal alignment | 35% | Does new analysis confirm or oppose the trade? |
| P&L ratio | 25% | Is the trade in profit? (in-profit = benefit of the doubt) |

The three factors are deliberately independent. No single factor can override
the others — a trade must remain healthy across all three dimensions.

**Structure scoring (40% factor):**

| M1 State | Long trade | Short trade |
|---|---|---|
| BULLISH_TREND | 1.00 (fully intact) | 0.00 (structure against trade) |
| STRUCTURE_BREAK (bullish BOS) | 0.80 | 0.10 |
| MITIGATION_ZONE | 0.65 | 0.65 |
| RANGING | 0.35 | 0.35 |
| STRUCTURE_BREAK (bearish BOS) | 0.10 | 0.80 |
| BEARISH_TREND | 0.00 (structure against trade) | 1.00 (fully intact) |

**Signal scoring (35% factor):**
- Same-direction fresh signal → `max(0.55, fresh_signal.confidence)` (confirmed)
- No fresh signal (no setup at this bar) → 0.50 (neutral)
- Opposing fresh signal → `max(0.0, 1.0 − conf × 1.5)` (a 0.70 confidence opposing signal scores 0.0)

**P&L scoring (25% factor):**
- At −1R: 0.25 — trade at a loss gets less benefit of the doubt
- At 0R (breakeven): 0.40
- At +1R: 0.55
- At +2R: 0.70 (capped at 0.75)

### Decision bands

```
Score ≥ 0.55  (HOLD_THRESHOLD)
```
Thesis intact. Continue holding. If defensive mode was active, it is cleared.

```
0.40 ≤ Score < 0.55  (DEFENSIVE zone)
```
Thesis weakening. Enter **defensive mode**:
1. Stop-loss is moved to the nearest confirmed M1 swing low (for longs) or
   swing high (for shorts) that is closer to price than the current SL.
2. If no structural swing is available within range, SL moves to breakeven + 1 pip.
3. SL is never moved further than 2 pips from current price to avoid instant trigger.

```
0.25 ≤ Score < 0.40  (PARTIAL EXIT zone)
```
Thesis degraded. If the trade is already ≥ 0.8R in profit and TP1 has not
fired yet, the bot takes a **50% partial exit** to lock profit before the
situation deteriorates further.

```
Score < 0.25  (EXIT zone)
```
Thesis invalidated. Position is closed at market immediately. This happens
regardless of where TP or SL are set — the bot does not remain in a trade
simply because those levels have not been reached.

### Structural SL trail

Independently of the thesis score, at each M1 close the bot checks whether the
stop-loss can be moved to a better structural location:

1. Identify all confirmed M1 swing lows (for longs) or swing highs (for shorts)
2. For each swing point, compute `proposed_SL = swing_price − buffer_pips`
3. Accept the proposal only when ALL are true:
   - It is tighter than the current SL (genuine risk reduction)
   - It is on the correct side of current price (below price for longs)
   - It is at least 2 pips from current price (no instant trigger risk)
4. Move to the tightest qualifying proposal

The buffer (`TRAIL_SL_BUFFER_PIPS`, default 1.5) places the stop slightly
behind the swing point rather than at the exact level where liquidity often sits.

**What the trail never does:**
- Never moves SL by a fixed pip count
- Never uses ATR multiples
- Never trails when the trade is at breakeven or in a loss
- Never moves SL against the trade direction
- Never moves SL in the absence of a fresh confirmed structural swing

### Reversal detection

If the entry analysis produces a fresh signal in the **opposite direction** from
an open trade, and that signal's confidence is ≥ `REEVAL_REVERSAL_CONF`
(default 0.70), the open trade is closed immediately.

The entry gate then evaluates the reversal signal on the same tick cycle.
If it passes all 9 entry gates, a new trade is opened in the opposite direction.
There is no manual bias — the market determines direction at every bar.

### Monitoring cadence

| Method | Called when | What it does |
|---|---|---|
| `run_monitoring_only()` | Every tick | TP1/TP2 price-level checks, time exit, MFE tracking |
| `run_trade_revaluation()` | Every M1 close | Structural SL trail, thesis re-evaluation, reversal |

TP1 and TP2 are price-level events checked on every tick because they can be
hit between M1 closes. The structural analysis only runs at M1 close because
it uses bar data, not individual ticks.

### Observing Trade Guardian decisions in the log

```
REEVAL [GBPUSD] ticket=12345 LONG | cont=0.72 | in_profit=True | pr=+0.85R | mfe=6.2pips | defensive=False | #7
```
Shows symbol, ticket, direction, continuation score, profit status, P&L ratio,
max favourable excursion, whether defensive mode is active, and reeval count.

```
STRUCTURAL_TRAIL [GBPUSD] ticket=12345 LONG | SL 1.29900 → 1.30020 (swing-based)
```
SL moved to behind a confirmed M1 swing low.

```
REEVAL_DEFENSIVE [GBPUSD] ticket=12345 — cont=0.43 entering defensive mode
DEFENSIVE_SL [GBPUSD] ticket=12345 | SL 1.30020 → 1.30045 (tightened)
```
Thesis weakening — defensive mode entered and SL tightened to nearest swing.

```
REVERSAL_EXIT [GBPUSD] ticket=12345 | opposing BEARISH signal conf=0.78 ≥ 0.70 — closed pips=+3.1
```
Strong opposing signal detected — trade closed, reversal eligible for entry.

```
REEVAL_EXIT [GBPUSD] ticket=12345 LONG — thesis invalidated (cont=0.18) | pips=-2.4 | reeval_count=12
```
Thesis scored below EXIT_THRESHOLD — closed before SL was hit.

### Trade summary at close

Every trade close (by any mechanism) logs a full summary:

```
TRADE SUMMARY | GBPUSD LONG | entry_conf=0.73 | SL=5.2pips | MFE=8.1pips | PnL=4.3pips | eff=53% | TP1=True TP2=False | reeval=14 | last_cont=0.48
```

- `entry_conf` — signal confidence at the time of entry
- `MFE` — maximum favourable excursion (best price the trade ever reached)
- `eff` — capture efficiency = PnL / MFE (how much of the best-case move was captured)
- `reeval` — number of M1-close re-evaluations performed during the trade's life
- `last_cont` — continuation score at the moment of close

---

## 10. Tick Analytics & Velocity Reports

The tick analytics system records every signal evaluation and links it to
the eventual trade outcome. It answers the question: "Is my velocity threshold
set correctly, or am I filtering out good trades?"

### Output files (inside container at `/bot/analytics/`)

| File | Updated | Content |
|---|---|---|
| `tick_velocity_raw.jsonl` | Every evaluation | Raw record per signal: velocity, spread, outcome |
| `tick_velocity_hourly.jsonl` | Every hour | Aggregated stats per hour |
| `tick_velocity_daily.jsonl` | 22:00 UTC daily | Daily summary + win rate by velocity band |
| `tick_velocity_weekly.jsonl` | Friday 21:00 UTC | Weekly summary + threshold recommendation |
| `tick_velocity_report.txt` | Daily + weekly | Human-readable report with recommended changes |

### How to read the report

```bash
docker exec scalper-prime cat /bot/analytics/tick_velocity_report.txt
```

The report groups evaluations into three velocity bands:
- **BLOCKED** — below threshold (never executed)
- **LOW** — just above threshold (executed but potentially low quality)
- **BORDER** — near threshold (mixed quality)
- **HIGH** — well above threshold (highest quality)

The recommendation (`RAISE` / `LOWER` / `OK`) tells you whether to adjust
`SCALPER_MIN_TICK_VELOCITY` based on win rate differences between bands.

### Accessing via dashboard

Navigate to the **Velocity Report** tab. Use the view toggles (Daily / Hourly /
Weekly / Raw Report) to switch between perspectives. The **⚡ Regenerate Now**
button forces an immediate report without waiting for the scheduled time.

---

## 11. Troubleshooting

### Bot starts but no trades are placed

1. Check `STRATEGY_GATE_ENABLED=true` in `.env`
2. Watch logs: `docker compose logs -f scalper-prime | grep GATE`
3. Common gate blocks:
   - `outside_session_window` — outside trading hours for that symbol
   - `tick_velocity_too_low` — market too thin, raise `SCALPER_MIN_TICK_VELOCITY` or wait for London open
   - `news_proximity` — high-impact event nearby
   - `daily_drawdown_limit_hit` — already hit 6% loss today

### MT5 login fails

```
[ERROR] MT5 connection failed
```

1. Open VNC at `http://localhost:3000`
2. Check MT5 is open and logged in
3. Verify `TRADING_ID`, `MT5_PASSWORD`, `MT5_SERVER` in `.env`
4. Ensure "Algorithmic Trading" is enabled in MT5 (Tools → Options → Expert Advisors)

### "ZMQ feed thread stopped"

The TCP connection between the MT5 EA and Python broke. The bot will auto-reconnect.
If it keeps happening:
1. Check VNC — is MT5 still open and the EA still attached to the chart?
2. Look for "ZoneBotBridge" in the MT5 Experts tab (bottom of chart)
3. Restart: `docker compose restart scalper-prime`

### Analytics directory not found

```
TickAnalytics: directory does not exist: /bot/analytics
```

The `analytics` directory must exist on the host before starting:
```bash
mkdir -p analytics
docker compose down && docker compose up -d
```

### Dashboard shows no data

1. Confirm the bot is running: `docker compose ps`
2. Check the dashboard password matches `DASHBOARD_PASSWORD` in `.env`
3. Try force-refreshing the page (Ctrl+F5)

---

## 12. Upgrading & Maintenance

### Update the bot

```bash
git pull origin main
docker compose down
docker compose build   # only needed if Dockerfile changed
docker compose up -d
```

### View analytics report after a trading day

```bash
docker exec scalper-prime cat /bot/analytics/tick_velocity_report.txt
```

### Manually trigger an analytics report

```bash
# Via dashboard: Velocity Report tab → ⚡ Regenerate Now

# Via API:
curl -X POST http://localhost:8080/api/velocity-report/refresh \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

### Back up state and journals

```bash
# Trade journal (CSV + JSON of all trades)
docker cp scalper-prime:/bot/.checkpoints ./backup-checkpoints
docker cp scalper-prime:/bot/analytics    ./backup-analytics
```

### Reset analytics counters

To start fresh analytics tracking (e.g. after a strategy parameter change):
1. Stop the bot: `docker compose down`
2. Clear analytics: `rm analytics/*.jsonl analytics/*.txt`
3. Start: `docker compose up -d`

### Adjust a setting without restarting

Most risk and TP settings can be changed live via the dashboard **Configuration** tab.
Changes take effect on the next trade evaluation. Credentials and broker type
require a container restart.

---

## File Structure

```
.
├── .env                          ← Your credentials (gitignored)
├── .env.example                  ← Template — copy this to .env
├── docker-compose.yml            ← Container orchestration
├── analytics/                    ← Tick velocity reports (bind-mounted)
├── logs/                         ← Trading logs (bind-mounted)
├── src/
│   ├── config.py                 ← ALL settings with defaults
│   ├── main_stream.py            ← Entry point (event loop)
│   ├── domain/
│   │   ├── entities.py           ← Core data structures (Trade, Candle, etc.)
│   │   ├── value_objects.py      ← PipCalculator, RiskParameters, BrokerCost
│   │   └── repositories.py      ← Abstract interfaces for data access
│   ├── infrastructure/
│   │   ├── mt5_bridge/           ← MT5 Python API wrapper
│   │   ├── stream/               ← ZMQ feed, candle builder, streaming repo
│   │   ├── trade_repo.py         ← MT5 order execution
│   │   ├── market_data_repo.py   ← MT5 market data + BrokerClock
│   │   ├── spread_calculator.py  ← Live spread with 4-level fallback
│   │   ├── trade_journal.py      ← Persistent trade log (CSV + JSON)
│   │   └── checkpoint_service.py ← State save/resume on restart
│   ├── strategy/
│   │   ├── structure_state.py    ← Multi-TF swing detection, BOS/CHoCH
│   │   ├── tick_engine.py        ← Tick velocity, imbalance, spread state
│   │   ├── candle_analyzer.py    ← Forming candle progress scoring
│   │   ├── scalper_alignment.py  ← M1/M5 momentum composite score
│   │   ├── strategy_manager.py   ← Signal evaluation coordinator
│   │   └── entry_gate.py         ← 9-gate entry filter → EntryCandidate
│   ├── application/
│   │   ├── execution_service.py  ← Trade placement + open trade management
│   │   ├── news_manager.py       ← News event blocking
│   │   ├── tick_analytics.py     ← Velocity evaluation recording + reports
│   │   ├── email_service.py      ← SMTP alert delivery
│   │   └── scheduler.py          ← Daily/weekly background jobs
│   └── api/
│       └── server.py             ← Flask dashboard API + JWT auth
└── src/dashboard/
    └── index.html                ← Single-page dashboard UI
```
