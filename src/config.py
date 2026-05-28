# ══════════════════════════════════════════════════════════════════
#  config.py  —  ALL settings live here. One file. Nothing else.
#
#  Sensitive values (passwords, API keys) are read from environment
#  variables first, falling back to the defaults below.
#  In Docker: set them in docker-compose.yml / .env (already done).
#  Locally:   export them in your shell, or keep the defaults for dev.
#
#  FIX 4: config.py previously had all credentials hardcoded.
#  Now os.environ is checked first so docker-compose env vars take
#  effect without touching this file.
# ══════════════════════════════════════════════════════════════════

import os

# ── MT5 Account Credentials ────────────────────────────────────────
# IMPORTANT: Set these via environment variables or docker-compose.yml.
# No credentials should be hardcoded here — use the .env file locally.
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

# Starting balance used for every backtest simulation.
# Defaults to ACCOUNT_BALANCE but can be set independently so you can test
# at a different account size without touching your live balance figure.
BACKTEST_INITIAL_BALANCE = float(os.environ.get("BACKTEST_INITIAL_BALANCE", str(ACCOUNT_BALANCE)))

MT5_PATH = ""

# ── News API ───────────────────────────────────────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
NEWS_API_URL = os.environ.get("NEWS_API_URL", "http://192.168.1.100:8087")
# ── Dashboard Auth ─────────────────────────────────────────────────
# Password for the web dashboard. If empty, a random one is printed to
# the log at startup (check logs/trading_bot.log for the one-time password).
DASHBOARD_PASSWORD          = os.environ.get("DASHBOARD_PASSWORD", "")
JWT_ACCESS_EXPIRES_MINUTES  = int(os.environ.get("JWT_ACCESS_EXPIRES_MINUTES", "15"))
JWT_REFRESH_EXPIRES_DAYS    = int(os.environ.get("JWT_REFRESH_EXPIRES_DAYS", "7"))

# ── Symbols to Trade ───────────────────────────────────────────────
# EURUSD removed: only pair with negative net P&L in backtest (-98k on 40 trades).
# Re-add once entry quality improves (win rate was 7.5%, avg loss -17k).
SYMBOLS = ["GBPUSD", "XAUUSD", "USDJPY", "AUDUSD", "USDCHF"]


# ── Broker Time Zone ───────────────────────────────────────────────
# AUTO-DETECTED at startup: main.py calls gw.detect_utc_offset() and
# overwrites this value before any trades are processed.
# This default (2 = EET, common for ECN brokers) is only used as a
# fallback when detection fails — e.g. bot started while market is closed
# and no fresh EURUSD tick is available.  Change it if your broker is
# permanently on a different offset and you start the bot off-hours.
BROKER_UTC_OFFSET_HOURS: int = 2

# ── Broker / Account Type ─────────────────────────────────────────
# Controls commission and spread cost model used across the entire bot.
#
# HFM accounts (set BROKER_ACCOUNT_TYPE in .env):
#   "ZERO_SPREAD"      : HFM Zero Spread — raw spread ≈ 0 pips + $3–5/lot commission
#   "PREMIUM"          : HFM Premium     — spread-based (~1.4+ pips), no commission
#   "PRO"              : HFM Pro         — spread-based (~0.5+ pips), no commission
#
# IC Markets accounts:
#   "IC_MARKETS_RAW"   : IC Markets Raw Spread — raw ECN spread + $3.50/lot/side commission
#
# Set via .env:  BROKER_ACCOUNT_TYPE=ZERO_SPREAD  (or IC_MARKETS_RAW, PREMIUM, etc.)
# Legacy env var HFM_ACCOUNT_TYPE is still accepted for backwards compatibility.
BROKER_ACCOUNT_TYPE: str = (
    os.environ.get("BROKER_ACCOUNT_TYPE")
    or os.environ.get("HFM_ACCOUNT_TYPE")
    or "ZERO_SPREAD"
).upper()
# Keep HFM_ACCOUNT_TYPE as an alias so existing code referencing it still works.
HFM_ACCOUNT_TYPE: str = BROKER_ACCOUNT_TYPE

# Per-symbol opening commission in USD per standard lot — Zero Spread account only.
# $3.00 / side  ($6.00 round-trip) for major forex pairs.
# $5.00 / side  ($10.00 round-trip) for Gold (XAUUSD / XAGUSD).
# Values are ignored when HFM_ACCOUNT_TYPE is "PREMIUM" or "PRO".
HFM_COMMISSION_USD: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "USDJPY": 3.0,
    "AUDUSD": 3.0, "USDCHF": 3.0, "NZDUSD": 3.0,
    "EURGBP": 3.0, "USDCAD": 3.0, "EURJPY": 3.0, "GBPJPY": 3.0,
    "XAUUSD": 5.0, "XAGUSD": 5.0,
}
HFM_COMMISSION_USD_DEFAULT: float = 3.0  # fallback for symbols not in the map above

# ── IC Markets Raw Spread Account ─────────────────────────────────
# Broker: IC Markets (KE)
# Account Type: Raw Spread
# Commission: $3.50 / side ($7.00 round-trip) for all instruments.
# Spreads: raw ECN, ~0.1 pips on EURUSD during liquid sessions.
# Used when BROKER_ACCOUNT_TYPE=IC_MARKETS_RAW in .env
IC_COMMISSION_PER_LOT_USD: float = float(os.environ.get("IC_COMMISSION_PER_LOT_USD", "3.50"))
IC_COMMISSION_USD: dict[str, float] = {
    "EURUSD": 3.50, "GBPUSD": 3.50, "USDJPY": 3.50,
    "AUDUSD": 3.50, "USDCHF": 3.50, "NZDUSD": 3.50,
    "EURGBP": 3.50, "USDCAD": 3.50, "EURJPY": 3.50, "GBPJPY": 3.50,
    "XAUUSD": 3.50, "XAGUSD": 3.50,  # IC Markets charges same rate for metals
}
IC_COMMISSION_USD_DEFAULT: float = 3.50


# ── Risk Management ────────────────────────────────────────────────
RISK_PERCENT        = 2.0
COMMISSION_PER_LOT  = 3.0  # USD/lot (per side) — Zero Spread default for major pairs
MIN_RR              = 2.0   # was 3.0 — backtest: rr rejections eating valid signals; 2R still profitable
MAX_OPEN_TRADES     = 6
MAX_TRADES_PER_SYMBOL = 2    # max concurrent trades on a single symbol (Phase 1 cap)
MAX_LOT_SIZE: float = float(os.environ.get("MAX_LOT_SIZE", "10.0"))  # hard ceiling applied in _build_trade()

# ── Phase 2 — Conviction Re-Entry ─────────────────────────────────
# Phase 2 fires AFTER Phase 1 in the same cycle.
# A symbol is only eligible if it already has a trade with TP1 hit
# (proven winner) and the new signal scores at maximum conviction.
PHASE2_ENABLED         = True
PHASE2_MIN_TRADE_SCORE = 5   # minimum score out of 6 to qualify for re-entry

# ── Margin Safety ────────────────────────────────────────────────
# A new trade is skipped if the margin MT5 requires for it exceeds
# free_margin / MARGIN_SAFETY_FACTOR.  Factor=2 means the trade may
# consume at most half the remaining free margin, keeping a buffer for
# existing trades' floating losses and swap charges.
# Set to 1.0 to allow trades right up to the margin limit (not recommended).
MARGIN_SAFETY_FACTOR: float = 2.0

# ── UPGRADED: Dynamic ATR-Based SL Multipliers ─────────────────────
ATR_SL_MULTIPLIERS: dict[str, float] = {
    "EURUSD": 0.20, "GBPUSD": 0.20, "AUDUSD": 0.20,
    "USDJPY": 0.25, "EURJPY": 0.25, "GBPJPY": 0.25,
    "XAUUSD": 0.15,  # was 0.30 — gold H1 ATR ~120p; 0.30× = 36p SL buffer pushing
    "XAGUSD": 0.20,  # total SL to 50-80p, making MIN_RR=2 require 100-160p TP (50-80%
    "USDCHF": 0.20,  # of gold daily range).  0.15× = 18p buffer — achievable RR.
}
ATR_SL_MULTIPLIER_DEFAULT = 0.20

EXTRA_PIPS_SL: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 6.0, "EURJPY": 6.0, "GBPJPY": 6.0,
    "XAUUSD": 10.0, "XAGUSD": 10.0,
    "USDCHF": 3.0,
}
EXTRA_PIPS_SL_DEFAULT = 3.0


# ── Zone Detection ────────────────────────────────────────────────
OB_IMPULSE_CANDLES    = 3
OB_IMPULSE_MULTIPLIER = 2.0
LIQUIDITY_LOOKBACK    = 10
FVG_MIN_ATR_RATIO     = 0.3

# Max zones kept per H1 bar scan (newest + largest preferred).
# Without a cap, EURUSD was generating ~2.6 zones per bar = pure noise.
MAX_OBS_PER_BAR  = 10
MAX_FVGS_PER_BAR = 8

# ── H4 Zone Confluence ─────────────────────────────────────────────
# When an H1 zone overlaps with an H4 OB/FVG of the same direction,
# score_setup() awards an extra bonus.  Two institutional timeframes
# pointing at the same price level is a significantly stronger setup.
H4_ZONE_CONFLUENCE_BONUS: int   = 2    # extra score points when H1+H4 zones align
H4_ZONE_MIN_OVERLAP_PCT:  float = 0.2  # ≥20% of the smaller zone must overlap
H4_ZONE_MAX_OBS:          int   = 5    # cap H4 OBs so stale ones don't dominate
H4_ZONE_MAX_FVGS:         int   = 4    # cap H4 FVGs


# ── M5 ChoCh Entry Filters ────────────────────────────────────────
# Raised from 1.1 → 1.3 (volume) and 1.5 → 2.0 (body).
# The old OR gate with loose thresholds produced an 8.3% win rate.
# These tighter values still allow genuine institutional moves through.
MIN_DISPLACEMENT_VOLUME_RATIO = 1.3
CANDLE_SIZE_RATIO = 2.0

# ── Per-Symbol ChoCh Displacement Overrides ───────────────────────
# Gold (XAUUSD) M5 candles are 5-10× larger than forex M5 candles
# (~15-25p body vs ~1.5p on GBPUSD).  Applying the same 2.0× body
# ratio and 1.3× volume ratio to gold over-filters: a 20-pip gold
# displacement body is already exceptional, but the 3-bar average
# body on gold is also 10-15p — so 2.0× requires a 20-30p body which
# is rare.  Lowering thresholds specifically for gold lets genuine
# institutional gold moves through without degrading forex filtering.
SYMBOL_DISPLACEMENT_VOLUME_RATIO: dict[str, float] = {
    "XAUUSD": 1.1,   # gold volume spikes are smaller relative to avg
    "XAGUSD": 1.1,
}
SYMBOL_CANDLE_SIZE_RATIO: dict[str, float] = {
    "XAUUSD": 1.5,   # gold avg body is already large; 1.5× is sufficient
    "XAGUSD": 1.5,
}

# ── M1 Predictive Signal Filters (find_m1_predictive_signal) ─────
# M1 operates on much smaller candles (~0.8 pip range vs ~2.5 pip on M5).
# Thresholds are raised significantly to overcome M1 noise floor.
# These are separate from M5 thresholds so each timeframe is calibrated
# independently — changing M5 values does not affect M1 and vice versa.
M1_MIN_WAVE_CANDLES          = 5     # minimum M1 candles in retracement wave before ChoCh
                                      # (_is_swing_high needs 4-candle structure + 1 retracement)
M1_DISPLACEMENT_VOLUME_RATIO = 1.8   # M5 uses 1.3; M1 noise requires higher bar
M1_CANDLE_SIZE_RATIO         = 3.0   # M5 uses 2.0; 3× avg body filters micro-noise
M1_BODY_MIN_PRICE            = 0.00015  # absolute body floor = ~1.5 pips on 5-digit pairs
                                         # ensures displacement body ≥ typical spread
M1_MIN_ZONE_SIZE_PIPS        = 6.0   # zone must be ≥6 pips tall for M1 wave to have
                                      # meaningful internal structure before zone.low
# M1 SL uses zone.mid rather than zone.low so the stop survives normal
# liquidity sweeps inside the zone while still being 4× tighter than M5 ChoCh.
# After M5 candle confirms bullish, execution_service moves SL to zone.low - buffer.
M1_SL_USE_ZONE_MID           = True

# ── Legacy M1 LTF Signal Filters (find_ltf_signal / backtester) ───
REJECTION_WICK_RATIO = 0.6   # Lower wick must be >= 60% of candle range for bullish rejection
VOLUME_RATIO_MIN     = 1.0   # Volume must be >= 1× average to count as valid rejection candle


# ── Weighted Scoring ──────────────────────────────────────────────
SETUP_SCORE_THRESHOLD = 3
# Risk fractions returned by grade_setup() (A+/B/C grades).
# NOTE: execution_service overrides these with dynamic_risk() (score-based)
# after placing the trade, so only COUNTER_TREND_MIN_RR has a hard effect
# on live trades (it lowers the RR bar for C-grade counter-trend setups).
SETUP_RISK_A_PLUS     = 1.0
SETUP_RISK_B          = 0.5
SETUP_RISK_C          = 0.25
COUNTER_TREND_MIN_RR  = 1.5   # Min RR required for C-grade (counter-trend) setups

ZONE_ATR_BUFFER_PCT = 0.15

# ── Trend Filters (commission protection) ────────────────────────
# REQUIRE_H4_BIAS: False = allow trades when H4 is ranging, as long as the
# zone score still passes SYMBOL_MIN_SCORE (D1 alignment can compensate).
# True = hard-block all entries when H4 structure is ambiguous.
# Keep False — forex consolidates often; a ranging H4 + clear D1 trend
# is still a valid trade.  The scoring system already penalises no-trend
# setups (they lose the +3 H4 bonus and need D1+equilibrium to pass).
REQUIRE_H4_BIAS:      bool = False

# BLOCK_COUNTER_D1: False = soft gate (counter-D1 trades require COUNTER_D1_MIN_SCORE).
# True = hard-block ALL counter-D1 trades regardless of score.
# Soft gate is preferred: a genuine H4+H4-zone+equilibrium counter-D1 setup
# still scores 7+ and should be taken.  Pure counter-D1 with only equilibrium
# alignment (score 5) is blocked — those are the trades that lose in trending months.
BLOCK_COUNTER_D1:     bool = False

# Minimum setup score required for a counter-D1 trade to proceed.
# Score 7 = H4 bias (3) + H4 confluence (2) + equilibrium (2) — meaning
# institutions on H4 AND a confirmed H4 OB/FVG AND price in the correct
# premium/discount zone.  A trade missing any of these is rejected.
COUNTER_D1_MIN_SCORE: int  = 7

H4_CANDLE_COUNT  = 50
H1_CANDLE_COUNT  = 100
M30_CANDLE_COUNT = 60   # 30 bars = 15 hours of M30 context for zone mapping
M5_CANDLE_COUNT  = 60
M1_CANDLE_COUNT  = 60   # raised from 30: M1 predictive signal needs ~30 bars of context
                         # + 30 bars lookback for wave detection inside the current M5 period
D1_CANDLE_COUNT  = 30   # ~6 weeks of daily data for D1 trend detection

H4_EQUILIBRIUM_LOOKBACK = 20
D1_EQUILIBRIUM_LOOKBACK = 15  # daily swing pivot lookback

# ── Per-Symbol H4 Slope Thresholds ────────────────────────────────
# Minimum absolute H4 slope (in pips per bar) before a direction is
# classified as a valid trend.  Pairs with higher volatility need a
# higher slope to distinguish trending from ranging.
# Used by find_h4_bias() to gate the H4_BIAS contribution to score_setup().
H4_SLOPE_THRESHOLD: dict[str, float] = {
    "EURUSD": 1.5, "GBPUSD": 2.0, "AUDUSD": 1.5,
    "USDJPY": 2.0, "EURJPY": 2.5, "GBPJPY": 3.0,
    "XAUUSD": 15.0, "XAGUSD": 8.0,
    "USDCHF": 1.5, "NZDUSD": 1.5, "USDCAD": 1.5,
}
H4_SLOPE_THRESHOLD_DEFAULT: float = 2.0

# ── M5 Zone Story Reader ──────────────────────────────────────────
# read_m5_zone_story() reads the sub-bar narrative inside the H1 zone.
# MIN_ZONE_STORY_BARS: minimum M5 bars with bodies inside the zone before
#   we trust the story.  Below this the story is "neutral" (not enough data).
# ZONE_STORY_WINDOW: how many of those bars to use for the current story
#   (uses the most recent N to avoid stale early-arrival data dominating).
# ZONE_STORY_VOL_THRESH: volume multiplier to classify a candle as "strong".
# ZONE_STORY_DIST_MAJORITY: fraction of bars that must be against the zone
#   direction for the story to be called "distributing".
# ZONE_STORY_ABS_MAJORITY: fraction of bars that must support the zone for
#   the story to be called "absorbing".
MIN_ZONE_STORY_BARS       : int   = 3
ZONE_STORY_WINDOW         : int   = 6
ZONE_STORY_VOL_THRESH     : float = 1.2
ZONE_STORY_DIST_MAJORITY  : float = 0.6
ZONE_STORY_ABS_MAJORITY   : float = 0.5

# ── M30 Story Reader ──────────────────────────────────────────────
# Number of recent M30 bars to fetch when reading the M30 story.
# 8 bars = 4 hours of M30 context — captures the last two H4 bar's worth
# of M30 data so we know what the last meaningful M30 structure was.
M30_STORY_LOOKBACK        : int   = 8

# ── Zone Health Early Warning ─────────────────────────────────────
# Thresholds for evaluate_zone_health check 4 (M5 momentum against zone).
# Lowered from 4/4 + 2-strong to 3/4 + 1-strong to catch distribution
# EARLIER — the old 4/4 gate fired after 20+ pip adverse move.
ZONE_HEALTH_AGAINST_MIN   : int   = 3    # minimum M5 bars against zone (of last 4)
ZONE_HEALTH_STRONG_MIN    : int   = 1    # minimum volume-spike bars among them
ZONE_HEALTH_VOL_SPIKE     : float = 1.5  # volume ratio to classify as spike

# ── M5 Precision Entry Zone ───────────────────────────────────────
# find_m5_precision_entry() locates the M5 OB inside the H1 zone.
# M5_PRECISION_MIN_PIPS: the M5 OB must be at least this many pips tall
# to be meaningful — smaller OBs are noise inside the H1 zone.
M5_PRECISION_MIN_PIPS     : float = 2.0

# ── M1 Bar-Position Gate ──────────────────────────────────────────
# m1_is_early_in_m5_bar(): M1 ChoCh must fire within the first N minutes
# of the M5 bar to have predictive value.  After minute 3 the M5 bar is
# already 60%+ complete and the M1 signal describes history, not the future.
M1_EARLY_BAR_MINUTES      : int   = 3

# ── M1 ChoCh vs M5 Swing Validation ──────────────────────────────
# m1_choch_breaks_m5_swing(): the level broken by the M1 ChoCh must be
# within M1_M5_SWING_PROXIMITY_PIPS of an M5 swing high/low.
# M1_M5_SWING_LOOKBACK: how many M5 bars to scan for the matching swing.
# Set M1_REQUIRE_M5_SWING_BREAK=False to disable (not recommended).
M1_REQUIRE_M5_SWING_BREAK  : bool  = True
M1_M5_SWING_LOOKBACK        : int   = 12
M1_M5_SWING_PROXIMITY_PIPS  : float = 3.0

# ── M5 / M30 Intra-Hour Zone Mapping ─────────────────────────────
# M30 zones re-map every 30-minute close.  They sit between H1 and M5
# in the hierarchy — a price level confirmed on BOTH H1 and M30 is
# significantly stronger than one appearing only on H1.
# M5 zones re-map every 5-minute close.  They provide the tightest
# structural context for M1 entries.
#
# Confluence bonus: when the active H1 zone overlaps an M30 zone of
# the same direction, the setup score receives +1.  When it also
# overlaps an M5 zone, another +1 is awarded.  This is additive with
# the existing H4 zone confluence bonus — max total score rises from
# 9 to 11 but thresholds are not changed, so existing A+/B grades
# are not disrupted and no good trades are blocked.
#
# If NO M30/M5 zone overlaps, trade still proceeds as before — the
# confluence is a BONUS, never a hard block.  This is the key design
# principle: add information, never remove trade opportunities.
M30_ZONE_MAPPING_ENABLED  : bool  = True
M5_ZONE_MAPPING_ENABLED   : bool  = True

M30_ZONE_MAX_OBS          : int   = 4   # cap M30 OBs per scan
M30_ZONE_MAX_FVGS         : int   = 3   # cap M30 FVGs per scan
M5_ZONE_MAX_OBS           : int   = 3   # cap M5 OBs per scan
M5_ZONE_MAX_FVGS          : int   = 3   # cap M5 FVGs per scan

# Minimum overlap fraction for M30/M5 zone to count as confluent with H1.
# 20% overlap (same as H4 confluence) keeps the bar meaningful without
# being so strict that close-but-not-perfect zones are missed.
M30_ZONE_MIN_OVERLAP_PCT  : float = 0.2
M5_ZONE_MIN_OVERLAP_PCT   : float = 0.2

# Score bonuses awarded when M30/M5 zones overlap the active H1 zone.
# Kept at +1 each (vs +2 for H4) because lower-TF zones are less
# institutionally significant than H4 — they confirm, not lead.
M30_ZONE_CONFLUENCE_BONUS : int   = 1
M5_ZONE_CONFLUENCE_BONUS  : int   = 1

# ── News Guardrail ────────────────────────────────────────────────
HIGH_IMPACT_BUFFER_MINS    = 30
MEDIUM_IMPACT_BUFFER_MINS  = 0   # only HIGH events block trading
NEWS_BUFFER_MINUTES        = 15
STRICT_NEWS_FILTER         = True
"""
When using the HFM Premium Account, you will likely need to raise your
New Threshold: Set ATR_COST_THRESHOLD = 0.20. This allows a cost up to 20% of the H1 ATR, which accommodates the ~1.4 pip spread on an 8-pip H1 move.
Volatility Buffer: If trading specifically during the Iran conflict spikes, a threshold of 0.25 might be necessary to avoid being completely filtered out, though this increases your "cost of business" significantly.
"""

ATR_COST_THRESHOLD         = 0.25


# How often the news manager re-fetches events from the API.
# 10 min keeps the blind-spot window below the HIGH_IMPACT_BUFFER_MINS (30 min)
# so a late-added event is always caught before the bot enters within its window.
NEWS_REFRESH_INTERVAL_MINUTES = 10

# ── Trading Sessions ──────────────────────────────────────────────
TRADING_SESSIONS = {
    "LONDON":   {"start": 8,  "end": 16},
    "NEW_YORK":  {"start": 13, "end": 21},
}

# Minutes to wait after a session opens before placing trades, and
# minutes before a session closes to stop placing trades.
# Analysis and zone mapping continue unaffected during this buffer.
SESSION_BUFFER_MIN: int = int(os.environ.get("SESSION_BUFFER_MIN", "15"))

LOOP_INTERVAL_SECONDS = 60

# ZeroMQ endpoint the MT5 EA publishes on (ZoneBotBridge.mq5 PUB_ENDPOINT).
# Used by main_stream.py. Must match the EA's configured PUB_ENDPOINT.
# Use 127.0.0.1 explicitly — Wine's loopback resolver does not always
# resolve "localhost" to the correct IPv4 address.
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

SYMBOL_MIN_SCORE: dict[str, int] = {
    "EURUSD": 3,
    "GBPUSD": 3,
    # USDJPY: 2 — most profit comes from monitor exits on H4/D1-equilibrium
    # setups; score 3 killed too many valid entries in previous backtests.
    "USDJPY": 2,
    # XAUUSD lowered 5→3: backtest showed 1,620 score rejections vs 25 signals
    # (64.8 rejects per signal vs 24.3 for GBPUSD — gold was 2.7× over-filtered).
    # The 23 trades that did execute produced the highest avg MC profit (+146 KES)
    # of any symbol.  Score 3 (H4 aligned) is sufficient quality control.
    "XAUUSD": 3,
    "AUDUSD": 3,
    "USDCHF": 3,
}

# ── Trade Monitor ─────────────────────────────────────────────────
MONITOR_ENABLED             = True
MONITOR_ACT_ON_SIGNALS      = True

# 2 = close when any 2 distinct signal types agree within the confirm window.
# reverse_choch (20% FP alone) + mfe_stall (1.6% FP alone) together
# approach 0% combined FP — a reliable close trigger.
# 3 was too strict: monitor fired 322 events but took 0 close actions.
MONITOR_CLOSE_CONVICTION    = 2

BREAKEVEN_RR_TRIGGER        = 1.5
MONITOR_TRAIL_CONVICTION    = 1
MONITOR_CONFIRM_WINDOW_M5   = 3
MFE_STALL_BARS              = 6     # was 8 → lowered: 75/132 stall events took no action then went to loss
# After TP1 hits the position is 40-60% smaller and SL is at breakeven —
# no downside to giving the runner more room.  The standard 6-bar stall
# was killing runners in 30 min (one M30 candle) before TP2 was reached.
# Backtest showed 0 WIN_FULL in 145 trades; extending post-TP1 tolerance
# to 12 bars (60 min) gives the runner time to reach TP2 at 2.0R.
MFE_STALL_BARS_POST_TP1    = 12
OPPOSING_ZONE_WARN_ATR      = 1.0
MIN_PROFIT_RATIO_TO_CLOSE   = 0.1   # was 0.3 → 75 stall non-actions had MFE below the 0.3R gate; lower to catch them
# Absolute pip floor for mfe_stall exit — fires even when R-ratio not met.
# Without this, tiny-MFE stalls (2-5 pip MFE on 50-pip SL = 0.04-0.1R) are
# ignored and the trade then hits SL.  Any positive MFE ≥ this many pips closes.
MIN_STALL_MFE_PIPS          = 2.0

# Minimum MFE (as a multiple of SL distance) required before a reverse_choch
# signal is allowed to close the trade.  Without this, reverse_choch closes
# trades on 1-pip MFE producing +2/+6 KES outcomes.
# 0.5 = price must have moved at least 0.5R in our favour first.
REVERSE_CHOCH_MIN_MFE_R     = 0.15

# Consecutive loss streak protection: after this many real losses (net-negative)
# on a symbol in a row, skip new entries on that symbol for LOSS_STREAK_PAUSE_HOURS.
# GBPUSD had 3 consecutive losses of -114/-113/-112 in Nov; USDCHF had 4.
MAX_CONSECUTIVE_LOSSES      = 2
LOSS_STREAK_PAUSE_HOURS     = 4

# ── Multi‑level Take Profit ────────────────────────────────────────
TIERED_TP_ENABLED        = True
TIERED_TP1_RATIO         = 1.5   # was 2.0 — losers avg MFE 13.8p, SL ~30-50p → TP1 at 2R unreachable; 1.5R more realistic
# Grade-adaptive TP1 partial-close percentages.
# High-conviction (A+) setups keep 60% running; counter-trend (C) exits 60% defensively.
TIERED_TP1_CLOSE_PCT          = 0.60   # grade C / counter-trend / fallback
TIERED_TP1_CLOSE_PCT_A_PLUS   = 0.40   # A+ grade: close 40%, keep 60% running
TIERED_TP1_CLOSE_PCT_B        = 0.50   # B grade: close 50%, keep 50% running
# TP2 lowered 2.5→2.0R: backtest showed 0 WIN_FULL in 145 trades over 6 months.
# Root cause: MFE_STALL_BARS=6 (30 min = 1 M30 candle) was closing runners before
# TP2 at 35p (2.5×14p SL) was reached.  At 2.0R, TP2 = 28p — well within the
# monitor's avg 51.9p MFE at stall fire.  Combined with MFE_STALL_BARS_POST_TP1=12,
# TP2 hits should now occur regularly on the stronger setups.
TIERED_TP2_RATIO         = 2.0
TIERED_TP2_CLOSE_PCT     = 0.25
FINAL_RUNNER_PCT         = 0.15   # informational only — the runner fraction is 1 - TP1_PCT - TP2_PCT at runtime

# ── STALL EXIT RULE ───────────────────────────────────────────────
# False = exit on stall whenever profit ≥ MIN_PROFIT_RATIO_TO_CLOSE (1.5R),
# even if TP1 hasn't been hit yet.  249 stall events took no action because
# TP1 was not yet reached — those trades then reversed and hit SL.
STALL_EXIT_REQUIRES_TP1 = False

# ── TP1 PROFIT-LOCK ───────────────────────────────────────────────
# After TP1 hit, SL is moved to entry + (initial_risk × this multiplier).
# 0.3 = lock in 0.3R of profit before letting the runner continue.
TP1_PROFIT_LOCK_R = 0.3

# ── MONITOR TRAILING ──────────────────────────────────────────────
# ATR fraction used as buffer below/above swing pivots for SL trail.
MONITOR_TRAIL_ATR_BUFFER = 0.5

# ── TRADE QUALITY SCORING ─────────────────────────────────────────
TRADE_SCORE_THRESHOLD   = 2
SCORE_HTF_ALIGNMENT       = 2   # max contribution
SCORE_LIQUIDITY_SWEEP     = 1
SCORE_CLEAN_FVG           = 1
SCORE_STRONG_DISPLACEMENT = 1
SCORE_KILL_ZONE           = 0   # outside kill zones perform 2.2× better in backtest

# ── DYNAMIC POSITION SIZING ───────────────────────────────────────
RISK_HIGH_CONVICTION    = 1.5
RISK_MEDIUM_CONVICTION  = 1.0
RISK_LOW_CONVICTION     = 1.0   # was 0.5 — half-size entries at 0.5% produce sub-commission profit on small accounts

# ── TIME‑BASED EXIT ───────────────────────────────────────────────
MAX_TRADE_DURATION_MINUTES = 600

# ── SESSION FILTER (KILL ZONES) ───────────────────────────────────
KILL_ZONES = {
    "LONDON":   (8, 11),
    "NEW_YORK": (14, 17),
}

# ── Minimum SL distance ───────────────────────────────────────────
MIN_SL_PIPS: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 5.0, "EURJPY": 5.0, "GBPJPY": 5.0,
    "XAUUSD": 30.0,  # was 50.0 — combined with ATR buffer 0.15 (down from 0.30)
    "XAGUSD": 20.0,  # total gold SL now ~30-50p instead of 50-80p; RR gate passable
    "USDCHF": 3.0,
}
MIN_SL_PIPS_DEFAULT = 3.0

# ── Retention Policies ────────────────────────────────────────────
LOG_RETENTION_DAYS   = 3
CACHE_RETENTION_DAYS = 2
ZONE_RETENTION_DAYS  = 5
ZONES_DIR            = "zones"

# ── Checkpoint ────────────────────────────────────────────────────
CHECKPOINT_DIR           = ".checkpoints"
CHECKPOINT_INTERVAL_MIN  = 5       # save checkpoint every N minutes (worst-case gap on power loss)
CHECKPOINT_RETAIN_DAYS   = 7       # keep dated daily backups for this many days, then delete

# ── Dashboard ─────────────────────────────────────────────────────
DASHBOARD_ENABLED = True
DASHBOARD_PORT    = 8080

# ── Email / Weekly Backtest ───────────────────────────────────────
EMAIL_ENABLED   = True            # set True + fill fields below to activate
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", 587))
EMAIL_USER      = os.environ.get("EMAIL_USER",      "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD",  "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")

WEEKLY_BACKTEST_ENABLED  = True
WEEKLY_BACKTEST_WEEKDAY  = 5      # Saturday
WEEKLY_BACKTEST_HOUR_UTC = 6      # 06:00 UTC

# ── Backtest ──────────────────────────────────────────────────────
NEWS_SLIPPAGE_PIPS   = 5.0
MAX_FORWARD_M1_BARS  = 4_320
# HFM retains up to ~5 months of M1 history via copy_rates_range, BUT only
# if you have first scrolled back on each symbol's M1 chart in the terminal
# (press Home repeatedly until the chart stops loading).  Without that step
# the terminal only serves its local cache (~6 weeks), explaining the 31.5%
# M1 coverage seen in the previous report.
#
# BEFORE running the backtest:
#   Open each symbol's M1 chart in MT5 and press Home until bars stop loading.
#
# After doing that, set this to 4 for a full and accurate simulation.
# Leave at 2 if you have not yet scrolled back (safe default).
BACKTEST_MONTHS_BACK = 2
BACKTEST_START_DATE  = ""
BACKTEST_END_DATE    = ""
BACKTEST_REPORT_NAME ="Last_6_months"

# Keep False so the backtest matches live trading exactly.
# Live trading monitors on M1 (SL/TP/MFE precision); the backtest should too.
# MT5 retains M1 for ~60 days, so BACKTEST_MONTHS_BACK is set to 2 below —
# that keeps full M1 coverage for every simulated trade.
BACKTEST_USE_M5_ONLY = False