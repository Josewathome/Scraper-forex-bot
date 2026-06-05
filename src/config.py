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
MIN_RR              = 2.0
MAX_OPEN_TRADES     = 6
MAX_TRADES_PER_SYMBOL = 1
MAX_LOT_SIZE: float = float(os.environ.get("MAX_LOT_SIZE", "10.0"))

# ── Margin Safety ────────────────────────────────────────────────
MARGIN_SAFETY_FACTOR: float = 2.0

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

MAX_CONSECUTIVE_LOSSES      = 2
LOSS_STREAK_PAUSE_HOURS     = 4

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
RISK_HIGH_CONVICTION    = 1.5
RISK_MEDIUM_CONVICTION  = 1.0
RISK_LOW_CONVICTION     = 1.0

# ── TIME-BASED EXIT ───────────────────────────────────────────────
MAX_TRADE_DURATION_MINUTES = 600

# ── Minimum SL distance ───────────────────────────────────────────
MIN_SL_PIPS: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 5.0, "EURJPY": 5.0, "GBPJPY": 5.0,
    "XAUUSD": 30.0,
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
    "GBPUSD": (6,  16),
    "XAUUSD": (6,  20),
    "USDJPY": (0,  16),
    "AUDUSD": (0,  12),
    "USDCHF": (6,  16),
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
SCALPER_MIN_TICK_VELOCITY: float = float(os.environ.get("SCALPER_MIN_TICK_VELOCITY", "1.5"))

SCALPER_MIN_SL_PIPS: dict = {
    "GBPUSD": 3.0,
    "XAUUSD": 10.0,
    "USDJPY": 3.0,
    "AUDUSD": 3.0,
    "USDCHF": 3.0,
    "EURUSD": 3.0,
}
SCALPER_MIN_SL_PIPS_DEFAULT: float = 3.0

SCALPER_MAX_SL_PIPS: dict = {
    "GBPUSD": 8.0,
    "XAUUSD": 25.0,
    "USDJPY": 8.0,
    "AUDUSD": 8.0,
    "USDCHF": 8.0,
    "EURUSD": 8.0,
}
SCALPER_MAX_SL_PIPS_DEFAULT: float = 8.0

SCALPER_TP1_RR: float = float(os.environ.get("SCALPER_TP1_RR", "1.5"))
SCALPER_TP2_RR: float = float(os.environ.get("SCALPER_TP2_RR", "2.0"))

SCALPER_MAX_HOLD_MINUTES: int = int(os.environ.get("SCALPER_MAX_HOLD_MINUTES", "30"))

SCALPER_MAX_DAILY_TRADES: int = int(os.environ.get("SCALPER_MAX_DAILY_TRADES", "20"))

SCALPER_MAX_OPEN_TRADES: int = int(os.environ.get("SCALPER_MAX_OPEN_TRADES", "3"))

SCALPER_MIN_ALIGNMENT_SCORE: float = float(os.environ.get("SCALPER_MIN_ALIGNMENT_SCORE", "0.60"))
SCALPER_MIN_TICK_SCORE:      float = float(os.environ.get("SCALPER_MIN_TICK_SCORE",      "0.65"))
SCALPER_MIN_CANDLE_SCORE:    float = float(os.environ.get("SCALPER_MIN_CANDLE_SCORE",    "0.40"))

SCALPER_M5_EMA_PERIOD: int = int(os.environ.get("SCALPER_M5_EMA_PERIOD", "10"))

SCALPER_M1_CONSENSUS_MIN: int = int(os.environ.get("SCALPER_M1_CONSENSUS_MIN", "3"))

SCALPER_ATR_SL_FRACTION: float = float(os.environ.get("SCALPER_ATR_SL_FRACTION", "0.4"))

# ── TP ratios (for entry_gate.py) ─────────────────────────────────
TP1_RR_RATIO: float = SCALPER_TP1_RR
TP2_RR_RATIO: float = SCALPER_TP2_RR

# ── Entry gate strategy flag ──────────────────────────────────────
STRATEGY_GATE_ENABLED: bool = os.environ.get("STRATEGY_GATE_ENABLED", "true").lower() == "true"

# ── Daily drawdown limit ──────────────────────────────────────────
DAILY_DRAWDOWN_LIMIT_PCT: float = float(os.environ.get("DAILY_DRAWDOWN_LIMIT_PCT", "2.0"))
