# ══════════════════════════════════════════════════════════════════
#  config.py  —  ALL settings live here. One file. Nothing else.
#
#  Sensitive values (passwords, API keys) are read from environment
#  variables first, falling back to the defaults below.
#  In Docker: set them in docker-compose.yml / .env (already done).
#  Locally:   export them in your shell, or keep the defaults for dev.
# ══════════════════════════════════════════════════════════════════

import os

# ── MT5 Account Credentials ────────────────────────────────────────
_trading_id_raw = os.environ.get("TRADING_ID")
if not _trading_id_raw:
    raise RuntimeError("TRADING_ID environment variable is not set. Set it in your .env or docker-compose.yml.")
TRADING_ID = int(_trading_id_raw)

MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
if not MT5_PASSWORD:
    raise RuntimeError("MT5_PASSWORD environment variable is not set.")

MT5_SERVER = os.environ.get("MT5_SERVER")
if not MT5_SERVER:
    raise RuntimeError("MT5_SERVER environment variable is not set.")
ACCOUNT_NICKNAME = "Demojose"
ACCOUNT_CURRENCY = os.environ.get("ACCOUNT_CURRENCY", "KES")
ACCOUNT_BALANCE  = float(os.environ.get("ACCOUNT_BALANCE", "30000"))
LEVERAGE         = "1:400"

MT5_PATH = ""

# ── News API ───────────────────────────────────────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
NEWS_API_URL = os.environ.get("NEWS_API_URL", "http://192.168.1.100:8087")
# ── Dashboard Auth ─────────────────────────────────────────────────
DASHBOARD_PASSWORD          = os.environ.get("DASHBOARD_PASSWORD", "")
JWT_ACCESS_EXPIRES_MINUTES  = int(os.environ.get("JWT_ACCESS_EXPIRES_MINUTES", "15"))
JWT_REFRESH_EXPIRES_DAYS    = int(os.environ.get("JWT_REFRESH_EXPIRES_DAYS", "7"))

# ── Symbols to Trade ───────────────────────────────────────────────
SYMBOLS = ["GBPUSD", "XAUUSD", "USDJPY", "AUDUSD", "USDCHF"]

# ── Broker Time Zone ───────────────────────────────────────────────
BROKER_UTC_OFFSET_HOURS: int = 2

# ── Broker / Account Type ─────────────────────────────────────────
# IC_MARKETS_RAW : IC Markets Raw Spread — raw ECN spread + $3.50/lot/side commission
# ZERO_SPREAD    : HFM Zero Spread — raw spread + commission
# PREMIUM / PRO  : HFM spread-based, no commission
BROKER_ACCOUNT_TYPE: str = (
    os.environ.get("BROKER_ACCOUNT_TYPE")
    or os.environ.get("HFM_ACCOUNT_TYPE")
    or "ZERO_SPREAD"
).upper()
HFM_ACCOUNT_TYPE: str = BROKER_ACCOUNT_TYPE

# ── IC Markets Raw Spread Account ─────────────────────────────────
IC_COMMISSION_PER_LOT_USD: float = float(os.environ.get("IC_COMMISSION_PER_LOT_USD", "3.50"))
IC_COMMISSION_USD: dict[str, float] = {
    "EURUSD": 3.50, "GBPUSD": 3.50, "USDJPY": 3.50,
    "AUDUSD": 3.50, "USDCHF": 3.50, "NZDUSD": 3.50,
    "EURGBP": 3.50, "USDCAD": 3.50, "EURJPY": 3.50, "GBPJPY": 3.50,
    "XAUUSD": 3.50, "XAGUSD": 3.50,
}
IC_COMMISSION_USD_DEFAULT: float = 3.50

# ── HFM Zero Spread commission (kept for backward compat in build_commission_map) ──
HFM_COMMISSION_USD: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "USDJPY": 3.0,
    "AUDUSD": 3.0, "USDCHF": 3.0, "NZDUSD": 3.0,
    "EURGBP": 3.0, "USDCAD": 3.0, "EURJPY": 3.0, "GBPJPY": 3.0,
    "XAUUSD": 5.0, "XAGUSD": 5.0,
}
HFM_COMMISSION_USD_DEFAULT: float = 3.0

# ── Risk Management ────────────────────────────────────────────────
RISK_PERCENT        = 2.0
COMMISSION_PER_LOT  = 3.0  # USD/lot (per side) — default for major pairs
MIN_RR              = 1.5
# Maximum unique symbols that may have open trades simultaneously.
# A new symbol can only open a trade if active_symbols < MAX_OPEN_SYMBOLS.
# The final free slot is reserved for A-grade entries.
MAX_OPEN_SYMBOLS: int = int(os.environ.get("MAX_OPEN_SYMBOLS", "5"))

# Per-symbol trade caps.
# A/A+ trades: up to MAX_TRADES_PER_SYMBOL_A  (default 3)
# B/C/D trades: 1 additional slot beyond the A-grade trades (total cap 4)
# Hard per-symbol ceiling regardless of grade.
MAX_TRADES_PER_SYMBOL_A:     int = int(os.environ.get("MAX_TRADES_PER_SYMBOL_A",   "3"))
MAX_TRADES_PER_SYMBOL:       int = int(os.environ.get("MAX_TRADES_PER_SYMBOL",     "4"))

MAX_LOT_SIZE: float = float(os.environ.get("MAX_LOT_SIZE", "10.0"))

# Absolute money-risk ceiling per trade as a fraction of balance.
# Hard cap enforced AFTER lot sizing: if the realised SL risk of even the
# broker-minimum lot exceeds this, the trade is REJECTED (fail closed).
MAX_TRADE_RISK_PCT: float = float(os.environ.get("MAX_TRADE_RISK_PCT", "3.0"))

# ── Margin Safety ────────────────────────────────────────────────
# NOTE: single source of truth — defined once here (was previously duplicated
# lower in this file, which silently overrode this value).
MARGIN_SAFETY_FACTOR:    float = float(os.environ.get("MARGIN_SAFETY_FACTOR",   "1.5"))
# Minimum margin level (%) required before a new trade is allowed.
# MT5 issues a margin call at 100%; we block well above that.
MIN_MARGIN_LEVEL_PCT:    float = float(os.environ.get("MIN_MARGIN_LEVEL_PCT",    "200.0"))

# ── Portfolio Allocation ──────────────────────────────────────────
# Always keep this fraction of equity reserved (free margin) for future setups.
MARGIN_RESERVE_PCT:    float = float(os.environ.get("MARGIN_RESERVE_PCT",     "0.25"))
# Maximum total open exposure as fraction of equity.
MAX_EQUITY_EXPOSURE:   float = float(os.environ.get("MAX_EQUITY_EXPOSURE",    "0.60"))
# Maximum correlated-currency exposure (trades sharing the same currency leg).
MAX_CURRENCY_EXPOSURE: int   = int(os.environ.get("MAX_CURRENCY_EXPOSURE",    "3"))

# ── Daily drawdown circuit breaker ────────────────────────────────
# If equity drops this % below the day's opening equity, all new entries
# are blocked for the rest of the UTC day. Open trades keep being managed.
DAILY_DRAWDOWN_LIMIT_PCT: float = float(os.environ.get("DAILY_DRAWDOWN_LIMIT_PCT", "6.0"))

# ── SL buffer (pips) ─────────────────────────────────────────────
EXTRA_PIPS_SL_DEFAULT = 3.0

# ── News Guardrail ────────────────────────────────────────────────
HIGH_IMPACT_BUFFER_MINS    = 30
MEDIUM_IMPACT_BUFFER_MINS  = 0
NEWS_BUFFER_MINUTES        = 15
STRICT_NEWS_FILTER         = True

ATR_COST_THRESHOLD         = 0.25

NEWS_REFRESH_INTERVAL_MINUTES = 10

# ZeroMQ endpoint
ZMQ_ENDPOINT: str = os.environ.get("ZMQ_ENDPOINT", "tcp://127.0.0.1:5556")

# ── Symbol Metadata ───────────────────────────────────────────────
SYMBOL_DIGITS: dict[str, int] = {
    "EURUSD": 5, "GBPUSD": 5, "AUDUSD": 5, "USDCAD": 5,
    "USDCHF": 5, "NZDUSD": 5, "EURGBP": 5,
    "USDJPY": 3, "EURJPY": 3, "GBPJPY": 3,
    "XAUUSD": 2, "XAGUSD": 3,
}

TICK_VALUE_USD: dict[str, float] = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0,
    "USDCAD":  7.7, "USDCHF": 11.2, "NZDUSD": 10.0,
    "USDJPY":  9.1, "EURJPY":  9.1, "GBPJPY":  9.1,
    "XAUUSD": 10.0, "XAGUSD":  5.0,
}

KES_PER_USD = float(os.environ.get("KES_PER_USD", "130.0"))

SYMBOL_CURRENCIES: dict[str, list[str]] = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "XAUUSD": ["USD"],
    "USDJPY": ["USD", "JPY"],
    "AUDUSD": ["AUD", "USD"],
    "USDCHF": ["USD", "CHF"],
}

# ── Trade Monitor ─────────────────────────────────────────────────
MONITOR_ENABLED             = True
MONITOR_ACT_ON_SIGNALS      = True
MONITOR_CLOSE_CONVICTION    = 2

BREAKEVEN_RR_TRIGGER        = 1.5

# Loss streak: tracked for analytics only — no trading pause enforced.
# Set LOSS_STREAK_PAUSE_HOURS = 0 to disable pause (default).
MAX_CONSECUTIVE_LOSSES      = int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "5"))   # streak length before pause
LOSS_STREAK_PAUSE_HOURS     = float(os.environ.get("LOSS_STREAK_PAUSE_HOURS", "2"))  # per-symbol cooldown; set 0 to disable

# ── Multi-level Take Profit ────────────────────────────────────────
TIERED_TP_ENABLED        = True
TIERED_TP1_RATIO         = 1.5
TIERED_TP1_CLOSE_PCT          = 0.60
TIERED_TP1_CLOSE_PCT_A_PLUS   = 0.40
TIERED_TP1_CLOSE_PCT_B        = 0.50
TIERED_TP2_RATIO         = 2.0
TIERED_TP2_CLOSE_PCT     = 0.25
FINAL_RUNNER_PCT         = 0.15

# ── TP1 PROFIT-LOCK ───────────────────────────────────────────────
TP1_PROFIT_LOCK_R = 0.3

# ── MONITOR TRAILING ──────────────────────────────────────────────
MONITOR_TRAIL_ATR_BUFFER = 0.5

# ── TRADE QUALITY SCORING ─────────────────────────────────────────
TRADE_SCORE_THRESHOLD   = 2

# ── DYNAMIC POSITION SIZING ───────────────────────────────────────
# Quality-weighted risk per grade (now wired via MarginManager):
#   A+  conf ≥ 0.85 AND tq ≥ 0.75  →  RISK_HIGH_CONVICTION
#   A   conf ≥ 0.70 OR  tq ≥ 0.65  →  RISK_MEDIUM_CONVICTION
#   B   conf ≥ 0.60 OR  tq ≥ 0.50  →  RISK_LOW_CONVICTION
#   C   otherwise                   →  RISK_MIN_CONVICTION
RISK_HIGH_CONVICTION    = float(os.environ.get("RISK_HIGH_CONVICTION",    "2.5"))
RISK_MEDIUM_CONVICTION  = float(os.environ.get("RISK_MEDIUM_CONVICTION",  "1.8"))
RISK_LOW_CONVICTION     = float(os.environ.get("RISK_LOW_CONVICTION",     "1.0"))
RISK_MIN_CONVICTION     = float(os.environ.get("RISK_MIN_CONVICTION",     "0.5"))

# ── TIME-BASED EXIT ───────────────────────────────────────────────
MAX_TRADE_DURATION_MINUTES = 600

# ── Minimum SL distance ───────────────────────────────────────────
MIN_SL_PIPS: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 5.0, "EURJPY": 5.0, "GBPJPY": 5.0,
    "XAUUSD": 150.0,   # GOLD: kept consistent with SCALPER_MIN_SL_PIPS ($1.50)
    "XAGUSD": 20.0,
    "USDCHF": 3.0,
}
MIN_SL_PIPS_DEFAULT = 3.0

# ── Candle counts for scalper ─────────────────────────────────────
M1_CANDLE_COUNT  = 60
M5_CANDLE_COUNT  = 60

# ── Retention Policies ────────────────────────────────────────────
LOG_RETENTION_DAYS   = 3
CACHE_RETENTION_DAYS = 2

# ── Checkpoint ────────────────────────────────────────────────────
CHECKPOINT_DIR           = ".checkpoints"
CHECKPOINT_INTERVAL_MIN  = 5
CHECKPOINT_RETAIN_DAYS   = 7

# ── Dashboard ─────────────────────────────────────────────────────
DASHBOARD_ENABLED = True
DASHBOARD_PORT    = 8080

# Informational only — STREAM mode is event-driven (no fixed poll interval).
# Surfaced/editable on the dashboard config tab; not used by the live loop.
LOOP_INTERVAL_SECONDS: int = int(os.environ.get("LOOP_INTERVAL_SECONDS", "1"))

# Initial balance used by the (optional) backtester and the dashboard's
# backtest tab. Defaults to the configured account balance fallback.
BACKTEST_INITIAL_BALANCE: float = float(
    os.environ.get("BACKTEST_INITIAL_BALANCE", str(ACCOUNT_BALANCE))
)

# ── Email ─────────────────────────────────────────────────────────
EMAIL_ENABLED   = True
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", 587))
EMAIL_USER      = os.environ.get("EMAIL_USER",      "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD",  "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")

# ══════════════════════════════════════════════════════════════════
#  SCALPER MODE — tighter 4–8 pip targeting on M1/M5 momentum
# ══════════════════════════════════════════════════════════════════

SCALPER_MODE: bool = os.environ.get("SCALPER_MODE", "false").lower() == "true"

# Legacy flat session window — kept for backward compat but overridden by
# SCALPER_SYMBOL_SESSIONS below when per-symbol windows are defined.
SCALPER_SESSION_START_UTC: int = int(os.environ.get("SCALPER_SESSION_START_UTC", "6"))
SCALPER_SESSION_END_UTC:   int = int(os.environ.get("SCALPER_SESSION_END_UTC",   "16"))

# ── Per-symbol session windows (UTC hour start, UTC hour end) ─────────────
# Rationale per symbol:
#   GBPUSD  06-16  Pure London/NY pair — illiquid and choppy outside these hours
#   XAUUSD  06-20  Gold: London + NY + EU afterhours (genuine institutional flow)
#   USDJPY  00-16  Active Asian session (Tokyo) + London + NY
#   AUDUSD  00-12  Active Asian session (Sydney/Tokyo) + London open
#   USDCHF  06-16  European pair — follows GBPUSD pattern, dead in Asian hours
#
# Override any symbol via env var: SCALPER_SESSION_GBPUSD_START=8
# (not yet wired — edit here directly if you need to adjust)
SCALPER_SYMBOL_SESSIONS: dict = {
    "GBPUSD": (5,  19),   # Pre-London open → NY afternoon (extended from 17 — NY active until ~19 UTC)
    "XAUUSD": (12, 21),   # GOLD: London-PM + NY/COMEX core only (cleanest gold flow)
    "USDJPY": (0,  19),   # Full Asian + London + NY afternoon (extended from 17)
    "AUDUSD": (22, 13),   # Sydney open (wraps midnight) → London midday
    "USDCHF": (5,  19),   # European pair — extended to match GBPUSD NY afternoon
}

# ── Tick velocity gate ────────────────────────────────────────────────────
# Minimum ticks/second required before an entry is allowed.
# Measured over the last 5 seconds by TickFeatureCalculator.tick_velocity().
# Blocks dead/illiquid markets without hardcoding session hours.
#
# Calibration reference (IC Markets RAW, GBPUSD):
#   Active session (London/NY):     4–12 ticks/sec
#   Thin but tradeable (EU close):  1.5–4 ticks/sec
#   Low liquidity (Asian for GBP):  0.2–1.0 ticks/sec
#   Dead market (weekend gap):      0–0.1 ticks/sec
#
# 1.5 allows thin-but-moving markets (e.g. USDJPY at 01:00 UTC) while
# blocking genuinely dead price action.  Raise to 2.5 if you see too
# many low-quality Asian session signals during live observation.
SCALPER_MIN_TICK_VELOCITY: float = float(os.environ.get("SCALPER_MIN_TICK_VELOCITY", "1.0"))  # ticks/sec floor — blocks dead/illiquid markets where scalp fills are unreliable

SCALPER_MIN_SL_PIPS: dict = {
    "GBPUSD": 3.0,
    "XAUUSD": 150.0,   # GOLD: $1.50 floor — survives normal M1 noise (a "pip" is $0.01)
    "USDJPY": 3.0,
    "AUDUSD": 3.0,
    "USDCHF": 3.0,
    "EURUSD": 3.0,
}
SCALPER_MIN_SL_PIPS_DEFAULT: float = 3.0

SCALPER_MAX_SL_PIPS: dict = {
    "GBPUSD": 12.0,
    "XAUUSD": 500.0,   # GOLD: $5.00 ceiling — reject setups needing a wider stop
    "USDJPY": 12.0,
    "AUDUSD": 12.0,
    "USDCHF": 12.0,
    "EURUSD": 12.0,
}
SCALPER_MAX_SL_PIPS_DEFAULT: float = 12.0

# ══════════════════════════════════════════════════════════════════
#  GOLD (XAUUSD) — DEDICATED ADAPTIVE PROFILE
#  Gold's "pip" is $0.01, so FX-calibrated fixed pip stops/targets are
#  economically tiny and get destroyed by noise + cost. Gold therefore sizes
#  its stop from its OWN M5 ATR (volatility-adaptive), clamped to absolute
#  bounds, and carries its own spread cap, session, and hold limit. These
#  settings affect ONLY symbols listed in SCALPER_ATR_PRIMARY_SYMBOLS — every
#  other symbol keeps the unchanged swing-first / fixed-pip behaviour.
# ══════════════════════════════════════════════════════════════════
# Symbols that size their stop from ATR as the PRIMARY method (not just fallback).
SCALPER_ATR_PRIMARY_SYMBOLS: set = {"XAUUSD"}
# ATR multipliers for the adaptive stop (applied to M5 ATR):
#   floor  = ATR × MIN_MULT   (stop never tighter than this — survive noise)
#   ceiling= ATR × MAX_MULT   (stop never wider than this — cap risk)
#   default= ATR × DEFAULT_MULT (used when no structural swing is available)
# The result is ALSO clamped to the absolute SCALPER_MIN/MAX_SL_PIPS guards above.
SCALPER_ATR_SL_MIN_MULT:     float = float(os.environ.get("SCALPER_ATR_SL_MIN_MULT",     "0.7"))
SCALPER_ATR_SL_MAX_MULT:     float = float(os.environ.get("SCALPER_ATR_SL_MAX_MULT",     "1.5"))
SCALPER_ATR_SL_DEFAULT_MULT: float = float(os.environ.get("SCALPER_ATR_SL_DEFAULT_MULT", "1.0"))

# Per-symbol absolute spread cap (pips). Falls back to the global MAX_SPREAD_PIPS.
# Gold spreads are naturally wide in pip terms ($0.01 pips), so it needs its own.
SCALPER_MAX_SPREAD_PIPS: dict = {
    "XAUUSD": 80.0,   # $0.80 — gold raw spread is wide in 0.01-pip terms
}

# Per-symbol maximum hold minutes. Falls back to SCALPER_MAX_HOLD_MINUTES.
SCALPER_MAX_HOLD_MINUTES_PER_SYMBOL: dict = {
    "XAUUSD": 20,     # gold moves fast — shorter scalp window
}

SCALPER_TP1_RR: float = float(os.environ.get("SCALPER_TP1_RR", "1.5"))
SCALPER_TP2_RR: float = float(os.environ.get("SCALPER_TP2_RR", "2.0"))

SCALPER_MAX_HOLD_MINUTES: int = int(os.environ.get("SCALPER_MAX_HOLD_MINUTES", "30"))

# No hard daily trade cap — frequency is bounded by margin + EV gate only.
# Set SCALPER_MAX_DAILY_TRADES = 0 to disable the cap entirely.
SCALPER_MAX_DAILY_TRADES: int = int(os.environ.get("SCALPER_MAX_DAILY_TRADES", "0"))

# Kept for legacy env-var compatibility — no longer used by MarginManager.
# Symbol and per-symbol limits are now controlled by MAX_OPEN_SYMBOLS,
# MAX_TRADES_PER_SYMBOL, and MAX_TRADES_PER_SYMBOL_A defined above.
SCALPER_MAX_OPEN_TRADES: int = int(os.environ.get("SCALPER_MAX_OPEN_TRADES", "20"))

# Signal-quality gate thresholds. These are the floors that make the entry
# gates MEANINGFUL — they must agree with the documented design. The entry
# context scorer may relax them slightly within bounded floors (see
# entry_context.py) but can never push them to noise level.
SCALPER_MIN_ALIGNMENT_SCORE: float = float(os.environ.get("SCALPER_MIN_ALIGNMENT_SCORE", "0.55"))
SCALPER_MIN_TICK_SCORE:      float = float(os.environ.get("SCALPER_MIN_TICK_SCORE",      "0.50"))
SCALPER_MIN_CANDLE_SCORE:    float = float(os.environ.get("SCALPER_MIN_CANDLE_SCORE",    "0.30"))

# Entry context scorer — thresholds for autonomous gate adaptation
ENTRY_TICK_CONFIRMS_MIN_SCORE: float = float(os.environ.get("ENTRY_TICK_CONFIRMS_MIN_SCORE", "0.25"))
ENTRY_TICK_CONFIRMS_MIN_BIAS:  float = float(os.environ.get("ENTRY_TICK_CONFIRMS_MIN_BIAS",  "0.20"))
ENTRY_BURST_MOVE_THRESHOLD:    float = float(os.environ.get("ENTRY_BURST_MOVE_THRESHOLD",    "0.40"))

SCALPER_M5_EMA_PERIOD: int = int(os.environ.get("SCALPER_M5_EMA_PERIOD", "10"))

SCALPER_M1_CONSENSUS_MIN: int = int(os.environ.get("SCALPER_M1_CONSENSUS_MIN", "3"))

SCALPER_ATR_SL_FRACTION: float = float(os.environ.get("SCALPER_ATR_SL_FRACTION", "0.4"))

# ── TP ratios (for entry_gate.py) ─────────────────────────────────
TP1_RR_RATIO: float = SCALPER_TP1_RR
TP2_RR_RATIO: float = SCALPER_TP2_RR

# ── Entry gate strategy flag ──────────────────────────────────────
# SAFE DEFAULT: observation mode. The bot evaluates and logs every signal but
# places NO trades until STRATEGY_GATE_ENABLED=true is set explicitly in .env.
# A losing/uncertain system must earn the right to trade live.
STRATEGY_GATE_ENABLED: bool = os.environ.get("STRATEGY_GATE_ENABLED", "false").lower() == "true"

# ── Tick Analytics ────────────────────────────────────────────────
# Set any of these to "false" in .env to disable that subsystem.
# All default to true so the feature is on out of the box.
TICK_ANALYTICS_ENABLED:   bool = os.environ.get("TICK_ANALYTICS_ENABLED",   "true").lower() == "true"
TICK_ANALYTICS_SAVE:      bool = os.environ.get("TICK_ANALYTICS_SAVE",      "true").lower() == "true"
TICK_ANALYTICS_SCHEDULE:  bool = os.environ.get("TICK_ANALYTICS_SCHEDULE",  "true").lower() == "true"

# ── EV (Expected Value) gate ──────────────────────────────────────
# Minimum expected value in pips for a trade to be taken.
# EV = (estimated_win_rate × avg_win_pips) − ((1 − win_rate) × avg_loss_pips)
# Uses rolling win rate from tick analytics if available; falls back to
# ASSUMED_WIN_RATE. A positive EV confirms the edge is real after costs.
# ASSUMED_WIN_RATE is ONLY used before enough real closed trades exist. Once
# EV_MIN_SAMPLES broker-truth outcomes are recorded for a symbol, the EV gate
# switches to the measured rolling win rate / avg win / avg loss instead.
# During the bootstrap phase a conservative haircut is applied so the bot does
# not assume an edge it has not demonstrated.
ASSUMED_WIN_RATE:        float = float(os.environ.get("ASSUMED_WIN_RATE",        "0.50"))
ASSUMED_WIN_RATE_HAIRCUT: float = float(os.environ.get("ASSUMED_WIN_RATE_HAIRCUT", "0.05"))
EV_MIN_PIPS:             float = float(os.environ.get("EV_MIN_PIPS",             "0.10"))
EV_ROLLING_WINDOW:       int   = int(os.environ.get("EV_ROLLING_WINDOW",         "50"))
EV_MIN_SAMPLES:          int   = int(os.environ.get("EV_MIN_SAMPLES",            "30"))
# Expected exit R used to model the tiered TP plan in the EV gate's avg_win.
# Conservative default for the runner = TP1 RR (assume the runner gives back to TP1).
RUNNER_EXIT_RR:          float = float(os.environ.get("RUNNER_EXIT_RR",          str(SCALPER_TP1_RR)))

# ── Exploration bootstrap ─────────────────────────────────────────
# The EV gate refuses negative-EV trades, but a brand-new system has no
# broker-truth samples to compute EV from (cold-start). To break that deadlock
# WITHOUT weakening any other protection, allow a small, capped number of
# minimum-risk "exploration" trades per symbol per day that bypass ONLY the EV
# edge requirement (every other gate — news, spread/cost, margin, R:R, session,
# velocity — still applies). These gather real outcomes so the rolling EV can
# take over. Exploration trades are forced to the lowest risk grade.
EXPLORATION_ENABLED:        bool = os.environ.get("EXPLORATION_ENABLED", "true").lower() == "true"
EXPLORATION_TRADES_PER_DAY: int  = int(os.environ.get("EXPLORATION_TRADES_PER_DAY", "8"))  # per symbol

# ── Reversal re-entry cooldown ────────────────────────────────────
# After a reversal/structural-reversal exit, block ALL new entries on that
# symbol for this many seconds to avoid whipsaw churn in chop.
REVERSAL_REENTRY_COOLDOWN_SEC: int = int(os.environ.get("REVERSAL_REENTRY_COOLDOWN_SEC", "120"))

# ── Ranging-regime suppression ────────────────────────────────────
# In a detected ranging regime, require a higher alignment score (add this to
# the effective threshold). Ranging M1 chop is the main source of false signals.
RANGING_ALIGN_PENALTY: float = float(os.environ.get("RANGING_ALIGN_PENALTY", "0.10"))

# ── Spread / cost gate ────────────────────────────────────────────
# Round-trip cost (spread + commission) must be no more than this fraction of
# the TP1 target distance, otherwise the trade is rejected — a scalp whose
# target is eaten by cost has no edge. Also rejects on missing/insane spread.
COST_MAX_FRACTION_OF_TARGET: float = float(os.environ.get("COST_MAX_FRACTION_OF_TARGET", "0.40"))
MAX_SPREAD_PIPS: float = float(os.environ.get("MAX_SPREAD_PIPS", "40.0"))

# ── News data freshness (fail-closed) ─────────────────────────────
# If the news cache has not refreshed successfully within this many minutes,
# the news gate blocks ALL entries (we cannot prove we are clear of news).
NEWS_MAX_STALENESS_MIN: float = float(os.environ.get("NEWS_MAX_STALENESS_MIN", "30"))
# If True, REQUIRE a real external news feed (NEWS_API_KEY). When no external
# feed is configured the only source is MT5's built-in calendar, whose coverage
# is best-effort; setting this True makes the news gate block all entries until
# an external feed is present (explicit fail-closed). Default False (the startup
# logs a prominent warning so the operator knows coverage is best-effort).
NEWS_REQUIRE_EXTERNAL_FEED: bool = os.environ.get("NEWS_REQUIRE_EXTERNAL_FEED", "false").lower() == "true"

# ── Duplicate-trade guard ─────────────────────────────────────────
# Prevents re-entering the same symbol in the same direction within
# ANTI_DUPE_SECONDS seconds of the last fill.  Blocks runaway loops
# without capping total daily volume.
ANTI_DUPE_SECONDS:       int   = int(os.environ.get("ANTI_DUPE_SECONDS",         "60"))

# ── Trade Guardian (continuous re-evaluation) ─────────────────────
# cont_score = M1_structure*0.40 + signal_alignment*0.35 + pnl_ratio*0.25
#
# Score bands:
#   >= REEVAL_HOLD_THRESHOLD      → thesis intact, hold (clear defensive mode)
#   >= REEVAL_DEFENSIVE_THRESHOLD → weakening, enter defensive mode, tighten SL
#   >= REEVAL_EXIT_THRESHOLD      → partial defensive exit when in significant profit
#   <  REEVAL_EXIT_THRESHOLD      → thesis invalidated, close trade
#
# Reversal: opposing signal with confidence >= REEVAL_REVERSAL_CONF closes trade.
REEVAL_HOLD_THRESHOLD:      float = float(os.environ.get("REEVAL_HOLD_THRESHOLD",      "0.55"))
REEVAL_DEFENSIVE_THRESHOLD: float = float(os.environ.get("REEVAL_DEFENSIVE_THRESHOLD", "0.40"))
REEVAL_EXIT_THRESHOLD:      float = float(os.environ.get("REEVAL_EXIT_THRESHOLD",      "0.25"))
REEVAL_REVERSAL_CONF:       float = float(os.environ.get("REEVAL_REVERSAL_CONF",       "0.70"))

# Buffer pips below/above swing point when computing a new structural SL.
TRAIL_SL_BUFFER_PIPS: float = float(os.environ.get("TRAIL_SL_BUFFER_PIPS", "1.5"))
