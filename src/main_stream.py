"""
main_stream.py — Event-driven entry point (ZeroMQ streaming mode, M1 scalper only).

CONTAINER ARCHITECTURE
Both the MT5 EA (ZoneBotBridge.mq5) and this Python bot run inside the
same Wine prefix in the same Docker container.  Communication is pure
Wine loopback TCP (127.0.0.1:5556) — no Docker networking involved.

EVENT LOOP
  MT5 EA (ZoneBotBridge.mq5)
      │  PUB tcp://127.0.0.1:5556 (Wine loopback)
      ▼
  ZmqFeed (background daemon thread)
      │  pushes events onto event_queue
      ▼
  Main loop (this file — runs on main thread)
      │  TICK          → run_monitoring_only()
      │  M1 close      → _run_strategy_evaluation()
      │  Other closes  → strategy.on_candle() (structure state only)
      │  TRADE         → run_monitoring_only()
      │  HEARTBEAT     → debug log
      ▼
  ExecutionService (uses StreamingMarketDataRepo)

HOW TO RUN:
    python -m src.main_stream
"""
from __future__ import annotations

import logging
import queue
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import src.config as config

from src.infrastructure                              import mt5_time
from src.infrastructure.mt5_bridge.mt5_gateway      import MT5Gateway
from src.infrastructure.market_data_repo             import MT5MarketDataRepository, BrokerClock
from src.infrastructure.trade_repo                   import MT5TradeRepository
from src.infrastructure.news_client                  import ForexNewsClient
from src.infrastructure.mt5_news_client              import MT5NewsClient
from src.infrastructure.composite_news_client        import CompositeNewsClient
from src.infrastructure.cache_store                  import JsonCacheStore
from src.infrastructure.spread_calculator            import SpreadCalculator
from src.infrastructure.cleanup_service              import CleanupService
from src.infrastructure.checkpoint_service           import CheckpointService
from src.infrastructure.trade_journal                import TradeJournal
from src.application.news_manager                   import NewsManager
from src.application.execution_service              import ExecutionService
from src.application.scheduler                      import Scheduler
from src.application.email_service                  import EmailService
from src.infrastructure.stream.candle_builder       import CandleBuilder, TRACKED_TIMEFRAMES
from src.infrastructure.stream.zmq_feed             import ZmqFeed, FEED_STOPPED
from src.infrastructure.stream.streaming_market_data_repo import StreamingMarketDataRepo
from src.domain.entities                            import Timeframe
from src.strategy.strategy_manager                  import StrategyManager
from src.strategy.entry_gate                        import EntryGate

logger = logging.getLogger("main_stream")

ZMQ_ENDPOINT = getattr(config, "ZMQ_ENDPOINT", "tcp://127.0.0.1:5556")

# Seed only M1 and M5 — M5 needed for scalper alignment context filter
_SEED_COUNTS: Dict[Timeframe, int] = {
    Timeframe.M1: 120,
    Timeframe.M5: 30,
}


# ── Inline helpers (previously in src/main.py) ────────────────────────────────

def _setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Rotate at 50 MB, keep 5 archives (~300 MB ceiling total).
            # The previous plain FileHandler grew one file unbounded
            # (reached 144 MB in 5 weeks). Same filename, so tailing
            # tooling keeps working across rotation.
            RotatingFileHandler(
                "logs/trading_bot.log",
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )


def _send_mt5_failure_email() -> None:
    try:
        svc = EmailService()
        subject = "ZONE BOT ALERT: MT5 connection failed — action required"
        body = (
            f"The trading bot could not connect to MT5 after all attempts.\n\n"
            f"Account : {config.TRADING_ID}\n"
            f"Server  : {config.MT5_SERVER}\n\n"
            f"ACTION REQUIRED:\n"
            f"  1. Create a new demo account at your broker's website.\n"
            f"  2. Update TRADING_ID, MT5_PASSWORD, MT5_SERVER in .env\n"
            f"  3. Restart: docker compose restart\n"
        )
        if svc.send_report(subject, body):
            logger.info("MT5 failure alert sent to %s", config.EMAIL_RECIPIENT)
        else:
            logger.warning("MT5 failure alert could not be sent — check EMAIL_* config in .env")
    except Exception as exc:
        logger.warning("Failed to send MT5 failure email: %s", exc)


def connect_mt5() -> MT5Gateway:
    gw = MT5Gateway(
        login=    config.TRADING_ID,
        password= config.MT5_PASSWORD,
        server=   config.MT5_SERVER,
        mt5_path= config.MT5_PATH,
    )
    if not gw.connect(retries=10, delay=15.0):
        logger.critical(
            "MT5 connection failed.\n"
            "  1. MT5 open and charts loaded\n"
            "  2. Tools > Options > Expert Advisors > Allow Algorithmic Trading\n"
            "  3. Correct TRADING_ID / MT5_PASSWORD / MT5_SERVER"
        )
        _send_mt5_failure_email()
        sys.exit(0)
    return gw


def _commission_in_account_ccy(usd: float) -> float:
    rates = {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79,
        "KES": config.KES_PER_USD,
        "NGN": 1550.0, "ZAR": 18.5,
    }
    return usd * rates.get(config.ACCOUNT_CURRENCY.upper(), 1.0)


def _build_commission_map() -> dict:
    acct_type = getattr(config, "BROKER_ACCOUNT_TYPE", "ZERO_SPREAD").upper()
    logger.info("Broker/account type: %s", acct_type)

    if acct_type in ("PREMIUM", "PRO"):
        logger.info("Commission model: $0 (spread-based account)")
        return {sym: 0.0 for sym in config.SYMBOLS}

    if acct_type == "IC_MARKETS_RAW":
        comm_usd_map = getattr(config, "IC_COMMISSION_USD", {})
        default_usd  = getattr(config, "IC_COMMISSION_USD_DEFAULT", 3.50)
        result = {}
        for sym in config.SYMBOLS:
            usd_rate = comm_usd_map.get(sym, default_usd)
            result[sym] = _commission_in_account_ccy(usd_rate)
            logger.info("  IC Markets commission %s: $%.2f USD → %.2f %s/lot",
                        sym, usd_rate, result[sym], config.ACCOUNT_CURRENCY)
        return result

    # Default: HFM Zero Spread
    comm_usd_map = getattr(config, "HFM_COMMISSION_USD", {})
    default_usd  = getattr(config, "HFM_COMMISSION_USD_DEFAULT", 3.0)
    result = {}
    for sym in config.SYMBOLS:
        usd_rate = comm_usd_map.get(sym, default_usd)
        result[sym] = _commission_in_account_ccy(usd_rate)
        logger.info("  HFM commission %s: $%.2f USD → %.2f %s/lot",
                    sym, usd_rate, result[sym], config.ACCOUNT_CURRENCY)
    return result


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _seed_builders(
    builders:    Dict[str, CandleBuilder],
    mt5_repo:    MT5MarketDataRepository,
) -> None:
    for sym, builder in builders.items():
        for tf, count in _SEED_COUNTS.items():
            try:
                candles = mt5_repo.get_candles(sym, tf, count)
                if candles:
                    builder.seed_from_history(tf, candles)
                    logger.info("Seeded %s %s: %d bars", sym, tf.value, len(candles))
                else:
                    logger.warning("Seed empty for %s %s", sym, tf.value)
            except Exception as exc:
                logger.warning("Seed failed %s %s: %s", sym, tf.value, exc)


# ── Main entry ────────────────────────────────────────────────────────────────

def run_stream() -> None:
    gw = connect_mt5()

    # Calibrate the broker-to-real-world-hour offset ONCE, here, before
    # anything reads MT5 time. This is the only place the host clock is
    # read at all (detect_utc_offset() compares one MT5 tick against
    # time.time() to measure the offset) — the result is a static constant
    # (config.BROKER_UTC_OFFSET_HOURS) that mt5_time.mt5_epoch_to_datetime
    # applies to every MT5 timestamp from here on. The host clock is never
    # consulted again after this one measurement.
    _detected_offset = gw.detect_utc_offset()
    _offset, _offset_source = mt5_time.resolve_startup_offset(_detected_offset)
    config.BROKER_UTC_OFFSET_HOURS = _offset
    logger.info(
        "MT5-to-real-world-hour offset: UTC%+d (%s; detected=%+d, applied once at startup)",
        config.BROKER_UTC_OFFSET_HOURS, _offset_source, _detected_offset,
    )

    clock   = BrokerClock(gw)   # MT5 server time, offset-corrected — no NTP, no host clock
    bot_now = clock.now()
    logger.info("Bot time at startup (MT5-derived): %s", bot_now.strftime("%Y-%m-%dT%H:%M:%S"))

    acc = gw.get_account_info()
    if acc:
        logger.info(
            "MT5  |  %s  |  balance=%.2f %s  |  server=%s",
            acc.get("name"), acc.get("balance", 0),
            acc.get("currency", "?"), acc.get("server"),
        )

    # ── Build one CandleBuilder per symbol ────────────────────────────
    event_queue: queue.Queue = queue.Queue()
    builders: Dict[str, CandleBuilder] = {
        sym: CandleBuilder(sym) for sym in config.SYMBOLS
    }

    # ── MT5 repo ───────────────────────────────────────────────────────
    mt5_market_data = MT5MarketDataRepository(gw)

    # ── Seed historical bars ────────────────────────────────────────────
    logger.info("Seeding historical candles from MT5...")
    _seed_builders(builders, mt5_market_data)

    # ── Strategy engine — seed M1 and M5 only ─────────────────────────
    strategy = StrategyManager(symbols=config.SYMBOLS)
    logger.info("Seeding StrategyManager with historical candles...")
    _strategy_seed_counts = {
        Timeframe.M5:  30,
        Timeframe.M1:  120,
    }
    for sym in config.SYMBOLS:
        for tf, cnt in _strategy_seed_counts.items():
            try:
                candles = mt5_market_data.get_candles(sym, tf, cnt)
                if candles:
                    strategy.seed(sym, tf, candles)
            except Exception as exc:
                logger.warning("Strategy seed failed %s %s: %s", sym, tf.value, exc)
    logger.info("StrategyManager ready.")

    # ── Streaming repo ─────────────────────────────────────────────────
    stream_repo = StreamingMarketDataRepo(
        builders=builders,
        fallback=mt5_market_data,
    )

    # ── Build services ─────────────────────────────────────────────────
    trade_repo   = MT5TradeRepository(gw)
    cache        = JsonCacheStore()
    mt5_news     = MT5NewsClient()
    _has_external_news = bool(config.NEWS_API_KEY)
    if _has_external_news:
        ff_news     = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
        news_client = CompositeNewsClient([ff_news, mt5_news])
    else:
        news_client = mt5_news
        logger.warning(
            "NEWS COVERAGE: no external NEWS_API_KEY configured — relying on MT5's "
            "built-in calendar only (best-effort). High-impact events may be missed. "
            "Set NEWS_API_KEY for full coverage, or NEWS_REQUIRE_EXTERNAL_FEED=true to "
            "block trading until an external feed is present."
        )
    news_manager    = NewsManager(news_client, has_external_feed=_has_external_news)
    commission      = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    commission_map  = _build_commission_map()
    spread_calc  = SpreadCalculator(
        market_data=       stream_repo,
        commission_per_lot=commission,
        commission_map=    commission_map,
    )
    execution = ExecutionService(
        market_data=       stream_repo,
        trade_repo=        trade_repo,
        news_manager=      news_manager,
        spread_calculator= spread_calc,
        symbols=           config.SYMBOLS,
        risk_pct=          config.RISK_PERCENT,
        commission=        commission,
        min_rr=            config.MIN_RR,
        account_currency=  config.ACCOUNT_CURRENCY,
        journal=           TradeJournal(),
    )

    # ── Entry gate ─────────────────────────────────────────────────────
    entry_gate = EntryGate(news_manager=news_manager, execution=execution)
    _startup_balance = (gw.get_account_info() or {}).get("balance", 0.0)

    cleanup    = CleanupService()
    checkpoint = CheckpointService(getattr(config, "CHECKPOINT_DIR", ".checkpoints"))
    email_svc  = EmailService()
    scheduler  = Scheduler(clock=clock)

    # ── Restore checkpoint ─────────────────────────────────────────────
    # ORDER MATTERS: restore the gate's persisted state BEFORE calling
    # on_new_day() below — its same-day guard then keeps the restored
    # drawdown baseline on an intraday restart instead of re-baselining
    # to current equity (which silently forgave the day's losses on every
    # restart — QA finding R2.1).
    cp = checkpoint.load()
    if cp:
        logger.info("Resumed from checkpoint (saved_at=%s).", cp.get("saved_at"))
        rt_state = cp.get("runtime_state")
        if rt_state:
            try:
                execution.load_runtime_state(rt_state)
            except Exception as exc:
                logger.warning("Runtime state restore failed: %s", exc)
        try:
            entry_gate.restore_state(cp.get("entry_gate_state"))
        except Exception as exc:
            logger.warning("EntryGate state restore failed: %s", exc)

    # No-op if the restored state is from today; re-baselines on a new day.
    entry_gate.on_new_day(_startup_balance, now=clock.now())

    # ── Dashboard ──────────────────────────────────────────────────────
    try:
        from src.api.server import load_config_overrides
        load_config_overrides()
    except Exception as exc:
        logger.warning("Config override load skipped: %s", exc)

    if getattr(config, "DASHBOARD_ENABLED", True):
        try:
            from src.api.server import DashboardServer
            journal = TradeJournal()
            DashboardServer(
                journal, checkpoint, None,
                balance_fn=lambda: (gw.get_account_info() or {}).get("balance"),
            ).start()
        except Exception as exc:
            logger.warning("Dashboard failed to start: %s", exc)

    # ── Tick analytics — initialise singleton (respects TICK_ANALYTICS_ENABLED) ─
    from src.application.tick_analytics import get_analytics as _get_analytics
    _tick_analytics = _get_analytics()
    _analytics_enabled  = getattr(config, "TICK_ANALYTICS_ENABLED",  True)
    _analytics_schedule = getattr(config, "TICK_ANALYTICS_SCHEDULE", True)

    # ── Scheduler ──────────────────────────────────────────────────────
    scheduler.add_daily("cleanup", cleanup.run_all, hour=2)
    scheduler.add_weekly("market_close_wipe", cleanup.market_close_wipe, weekday=4, hour=21)

    # Tick analytics periodic reports (only registered when scheduling is enabled)
    if _analytics_enabled and _analytics_schedule:
        for _hr in range(24):
            scheduler.add_daily(
                f"tick_analytics_hourly_{_hr:02d}",
                lambda _a=_tick_analytics, _clk=clock: _a.run_hourly(_clk.now()),
                hour=_hr,
            )
        scheduler.add_daily(
            "tick_analytics_daily",
            lambda _a=_tick_analytics, _clk=clock: _a.run_daily(_clk.now()),
            hour=22,
        )
        scheduler.add_weekly(
            "tick_analytics_weekly",
            lambda _a=_tick_analytics, _clk=clock: _a.run_weekly(_clk.now()),
            weekday=4,
            hour=21,
        )
        logger.info("TickAnalytics scheduled: hourly + daily (22:00 UTC) + weekly (Fri 21:00 UTC)")
    elif not _analytics_enabled:
        logger.info("TickAnalytics: disabled via TICK_ANALYTICS_ENABLED=false — no schedules registered")
    else:
        logger.info("TickAnalytics: scheduling disabled via TICK_ANALYTICS_SCHEDULE=false — reports must be triggered manually")

    last_checkpoint = clock.now()
    cp_interval     = getattr(config, "CHECKPOINT_INTERVAL_MIN", 1) * 60
    loop_n          = 0
    last_news_refresh = clock.now()

    # ── Start ZMQ feed ─────────────────────────────────────────────────
    feed = ZmqFeed(
        endpoint=    ZMQ_ENDPOINT,
        builders=    builders,
        event_queue= event_queue,
    )
    feed.start()

    running = [True]
    _halt_state = [None]  # tracks the last-logged TRADING_HALTED value so the
                           # transition (and only the transition) gets logged,
                           # not every tick -- see the TICK/CANDLE_CLOSED
                           # handlers below.

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received.")
        running[0] = False

    signal.signal(signal.SIGINT,  _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass

    logger.info(
        "Bot STREAM mode live. ZMQ=%s | Symbols: %s",
        ZMQ_ENDPOINT, " | ".join(config.SYMBOLS),
    )

    # ── Main event loop ────────────────────────────────────────────────
    while running[0]:
        try:
            event = event_queue.get(timeout=1.0)
        except queue.Empty:
            clock.invalidate()
            _now = clock.now()
            if (_now - last_checkpoint).total_seconds() >= cp_interval:
                _do_checkpoint(execution, checkpoint, _now, loop_n, entry_gate)
                last_checkpoint = _now
            continue

        if event is FEED_STOPPED:
            logger.warning("ZMQ feed thread stopped — restarting in 5s...")
            time.sleep(5.0)
            if running[0]:
                feed.start()
            continue

        etype = event.get("type", "")
        clock.invalidate()
        now = clock.now()

        # ── Full trade halt transition log (state-change only, not per-tick) ──
        if config.TRADING_HALTED != _halt_state[0]:
            if config.TRADING_HALTED:
                logger.warning(
                    "TRADING_HALTED engaged — ticks/candles will be received and "
                    "discarded (not recorded, not zone/structure-labeled), no "
                    "signals will be generated, no new trades will be opened. "
                    "Open positions keep tick-level TP1/TP2/time-based-exit "
                    "monitoring, but candle-level structural SL trailing and "
                    "reversal-exit checks are OFF for the duration of the halt."
                )
            else:
                logger.warning("TRADING_HALTED lifted — normal signal analysis resumed.")
            _halt_state[0] = config.TRADING_HALTED

        # ── Refresh news periodically ──────────────────────────────────
        if (now - last_news_refresh).total_seconds() >= 60:
            try:
                news_manager.refresh_if_needed(now)
            except Exception as exc:
                logger.warning("News refresh error: %s", exc)
            last_news_refresh = now

        # ── TICK ───────────────────────────────────────────────────────
        if etype == "TICK":
            sym = event.get("sym", "")
            if sym in config.SYMBOLS:
                # Tick-level position monitoring (TP1/TP2 partials, time-based
                # exit) stays active under TRADING_HALTED -- deliberately NOT
                # gated. This does not generate signals or record tick data
                # into any analysis structure, only manages what's already
                # open. NOTE: this is tick-level only -- candle-level
                # structural SL trailing / reversal-exit (run_trade_revaluation,
                # called from the M1-close path) is skipped while halted; see
                # the CANDLE_CLOSED handler below and the TRADING_HALTED
                # comment in config.py.
                try:
                    execution.run_monitoring_only(sym, now)
                except Exception as exc:
                    logger.exception("Monitoring [%s]: %s", sym, exc)
                # Tick-level analysis (zone/level tracking within the
                # forming bar) -- this is the gate: under TRADING_HALTED the
                # tick is received (so the queue never backs up) but simply
                # discarded here, never reaching the strategy engine at all.
                if not config.TRADING_HALTED:
                    try:
                        strategy.push_tick(
                            sym,
                            bid=event.get("bid", 0.0),
                            ask=event.get("ask", 0.0),
                            ts=float(event.get("time", 0)),
                        )
                    except Exception as exc:
                        logger.debug("Strategy tick push [%s]: %s", sym, exc)

        # ── CANDLE_CLOSED ──────────────────────────────────────────────
        # Sole source for candle-close events: CandleBuilder's _on_close
        # callback, fired exactly once per bar (either from the tick-driven
        # finalizer, or from the EA's BAR_CLOSE correction path when it's
        # genuinely a new bar — see CandleBuilder.on_bar_close_from_ea).
        elif etype == "CANDLE_CLOSED":
            candle = event.get("candle")
            if candle is None:
                continue
            # Full halt: the candle-closed event is received (drained off
            # the queue, so CandleBuilder/ZmqFeed never back up) but goes
            # no further -- no structure/zone labeling (strategy.on_candle),
            # no signal generation, no entry-gate evaluation. An early
            # continue rather than gating each call individually guarantees
            # nothing downstream of "a candle closed" can run while halted.
            # This ALSO skips run_trade_revaluation (structural SL trailing /
            # reversal-exit for already-open positions), since that call lives
            # inside _run_strategy_evaluation on this same M1-close path --
            # open positions keep only the tick-level monitoring above while
            # halted, not this. See the TRADING_HALTED comment in config.py.
            if config.TRADING_HALTED:
                continue
            sym = candle.symbol
            tf  = candle.timeframe
            try:
                strategy.on_candle(candle)
            except Exception as exc:
                logger.debug("Strategy on_candle [%s/%s]: %s", sym, tf.value, exc)

            if sym not in config.SYMBOLS:
                continue

            # M1 close → strategy evaluation
            if tf == Timeframe.M1:
                loop_n += 1
                try:
                    _run_strategy_evaluation(
                        strategy, stream_repo, sym, builders,
                        entry_gate, mt5_market_data, gw, now,
                    )
                except Exception as exc:
                    logger.exception("Strategy eval [%s]: %s", sym, exc)
            # All other timeframes: on_candle already called above (keeps structure state updated)

        # ── TRADE event ────────────────────────────────────────────────
        elif etype == "TRADE":
            sym = event.get("sym", "")
            logger.info(
                "EA TRADE event | %s %s ticket=%s price=%.5f",
                event.get("event", "?"),
                sym,
                event.get("ticket", "?"),
                event.get("price", 0.0),
            )
            if sym in config.SYMBOLS:
                try:
                    execution.run_monitoring_only(sym, now)
                except Exception as exc:
                    logger.exception("Post-trade monitoring [%s]: %s", sym, exc)

        elif etype == "HEARTBEAT":
            logger.debug("Heartbeat: broker_time=%s", event.get("time"))

        # ── Periodic checkpoint ─────────────────────────────────────────
        if (now - last_checkpoint).total_seconds() >= cp_interval:
            _do_checkpoint(execution, checkpoint, now, loop_n, entry_gate)
            last_checkpoint = now

    # ── Shutdown ───────────────────────────────────────────────────────
    feed.stop()
    scheduler.stop()
    clock.invalidate()
    _do_checkpoint(execution, checkpoint, clock.now(), loop_n, entry_gate)
    gw.disconnect()
    logger.info("Bot stopped cleanly (stream mode).")


# ── Helpers ────────────────────────────────────────────────────────────────

_last_gate_day: Dict[str, str] = {}


def _run_strategy_evaluation(
    strategy:    "StrategyManager",
    stream_repo: "StreamingMarketDataRepo",
    symbol:      str,
    builders:    "Dict[str, CandleBuilder]",
    entry_gate:  "Optional[EntryGate]" = None,
    mt5_repo:    "Optional[MT5MarketDataRepository]" = None,
    gw:          "Optional[MT5Gateway]" = None,
    now:         "Optional[datetime]" = None,
) -> None:
    from src.domain.entities import Timeframe as TF

    builder = builders.get(symbol)
    if builder is None:
        return

    if now is None:
        logger.error(
            "_run_strategy_evaluation [%s] called without `now` — refusing to fall back "
            "to the host clock; skipping this evaluation cycle.", symbol,
        )
        return

    m1_candles = builder.get_closed_candles(TF.M1, 60)
    # Pass M5 candles in the h1_candles slot — ScalperAlignmentEngine uses M5 context
    m5_candles = builder.get_closed_candles(TF.M5, 30)
    forming_m1 = builder.get_forming_bar(TF.M1)

    if not m1_candles or not m5_candles:
        return

    if forming_m1 is not None:
        elapsed = max(0.0, min(60.0, (now - forming_m1.time).total_seconds()))
    else:
        elapsed = 30.0

    try:
        current_price = stream_repo.get_current_price(symbol)
    except Exception:
        current_price = m1_candles[-1].close if m1_candles else 0.0

    signal = strategy.evaluate(
        symbol=symbol,
        forming_m1=forming_m1,
        m1_candles=m1_candles,
        h1_candles=m5_candles,   # M5 candles in h1 slot
        current_price=current_price,
        elapsed_m1_secs=elapsed,
    )

    # Re-evaluate open trades on this symbol using fresh market context.
    # Called unconditionally — signal=None is valid input (means "no setup").
    if entry_gate is not None:
        _structure = getattr(strategy, "_structure", {}).get(symbol)
        try:
            entry_gate._exec.run_trade_revaluation(
                symbol=symbol,
                fresh_signal=signal,
                structure=_structure,
                m1_candles=m1_candles,
                m5_candles=m5_candles,
                now=now,
            )
        except Exception as exc:
            logger.exception("Trade revaluation [%s]: %s", symbol, exc)

    if not signal:
        struct = strategy.structure_summary(symbol)
        logger.info(
            "M1 EVAL [%s] NO_SIGNAL | M1=%s M5=%s | spread=%s | price=%.5f",
            symbol,
            struct.get("M1", "?"),
            struct.get("M5", "?"),
            strategy.spread_state(symbol).value,
            current_price,
        )
        return

    logger.info(
        "M1 SIGNAL ✓ [%s] %s | conf=%.2f | m1_score=%.2f m5_score=%.2f | "
        "tick=%.2f | candle=%.2f | regime=%s | price=%.5f",
        symbol,
        signal.direction.value.upper(),
        signal.confidence,
        signal.alignment.m1_score,
        signal.alignment.m5_score,
        signal.tick_analysis.score,
        abs(signal.candle_score.score),
        signal.alignment.regime.value,
        current_price,
    )

    if entry_gate is None or not getattr(config, "STRATEGY_GATE_ENABLED", False):
        return

    today = now.date().isoformat()
    if _last_gate_day.get(symbol) != today:
        _last_gate_day[symbol] = today
        try:
            bal = (gw.get_account_info() or {}).get("balance", 0.0) if gw else 0.0
            entry_gate.on_new_day(bal, now=now)
        except Exception:
            pass

    from src.domain.value_objects import PipCalculator

    # Symbol metadata + CORRECT per-pip value (account currency). pip_value=0
    # signals bad/unavailable tick data → entry gate will fail closed.
    try:
        digits    = mt5_repo.get_symbol_digits(symbol) if mt5_repo else 5
        pip_value = mt5_repo.get_pip_value(symbol)     if mt5_repo else 0.0
    except Exception as exc:
        logger.error("Symbol meta/pip_value fetch failed for %s: %s", symbol, exc)
        digits, pip_value = 5, 0.0

    pip_calc = PipCalculator(digits=digits)

    # Real broker cost (live spread + per-symbol, currency-converted commission +
    # correct pip value) from the SpreadCalculator used by ExecutionService.
    try:
        broker_cost = entry_gate._exec._spread.get_broker_cost(symbol)
    except Exception as exc:
        logger.error("Broker cost fetch failed for %s: %s — skipping entry", symbol, exc)
        return

    # M5 ATR for SL computation
    h1_atr = 0.0
    if m5_candles and len(m5_candles) >= 14:
        h1_atr = sum(c.high - c.low for c in m5_candles[-14:]) / 14

    balance = 0.0
    try:
        balance = (gw.get_account_info() or {}).get("balance", 0.0) if gw else 0.0
    except Exception:
        pass

    candidate = entry_gate.evaluate(
        signal=        signal,
        builder=       builder,
        current_price= current_price,
        now=           now,
        balance=       balance,
        pip_calc=      pip_calc,
        pip_value=     pip_value,
        digits=        digits,
        h1_atr=        h1_atr,
        broker_cost=   broker_cost,
    )

    if candidate is None:
        return

    exec_svc = entry_gate._exec
    try:
        trade = exec_svc.execute_entry_candidate(candidate, now)
        if trade:
            entry_gate.record_trade(now=now)
            entry_gate.record_fill(symbol, signal.direction, now=now)
            if getattr(candidate, "_exploration", False):
                entry_gate.record_exploration(symbol)
            logger.info(
                "STRATEGY_TRADE [%s] %s | conf=%.2f | %.2f lots | "
                "entry=%.5f SL=%.5f TP1=%.5f | daily_total=%d",
                symbol,
                signal.direction.value.upper(),
                signal.confidence,
                trade.lot_size,
                trade.entry_price,
                trade.stop_loss,
                trade.take_profit,
                entry_gate.daily_trade_count(now=now),
            )
    except Exception as exc:
        logger.exception("Strategy trade execution [%s]: %s", symbol, exc)


def _do_checkpoint(
    execution:   ExecutionService,
    checkpoint:  CheckpointService,
    now:         datetime,
    loop_n:      int,
    entry_gate:  Optional[EntryGate] = None,
) -> None:
    try:
        rt_state = execution.save_runtime_state()
    except Exception as exc:
        logger.warning("Runtime state serialisation failed: %s", exc)
        rt_state = {}
    try:
        gate_state = entry_gate.get_state() if entry_gate is not None else None
    except Exception as exc:
        logger.warning("EntryGate state serialisation failed: %s", exc)
        gate_state = None
    checkpoint.save({
        "loop_n":        loop_n,
        "symbols":       config.SYMBOLS,
        "risk_pct":      config.RISK_PERCENT,
        "min_rr":        config.MIN_RR,
        "runtime_state": rt_state,
        "entry_gate_state": gate_state,
        "last_loop_ts":  now.isoformat(),
    }, now=now)


if __name__ == "__main__":
    _setup_logging()

    # Full trade halt: don't connect to MT5, don't attach to the tick feed,
    # don't do ANY of the run_stream() setup -- exit immediately. This is a
    # static operator switch (.env, requires a restart to change), so there
    # is nothing to poll for here; the container-level launcher checks the
    # same TRADING_HALTED env var before ever spawning this process (see
    # override_start.sh) so this process exiting does not trigger a restart
    # loop -- it's a belt-and-suspenders guard for direct invocation.
    if config.TRADING_HALTED:
        logger.warning(
            "TRADING_HALTED=true — not connecting to MT5, not attaching to "
            "the tick feed. Exiting immediately; no reconnect/restart will "
            "be attempted. Set TRADING_HALTED=false and restart to resume."
        )
        sys.exit(0)

    # Refuse to start trading if config violates edge-first minimums.
    # Only enforced when STRATEGY_GATE_ENABLED=true (observe mode still starts).
    if getattr(config, "STRATEGY_GATE_ENABLED", False):
        from src.edge_floors import validate_edge_floors
        validate_edge_floors()

    logger.info("-" * 50)
    logger.info("  Scalper Bot (STREAM MODE)  |  account=%s  |  server=%s",
                config.TRADING_ID, config.MT5_SERVER)
    logger.info("  Symbols : %s", ", ".join(config.SYMBOLS))
    logger.info("  Risk    : %.1f%%  |  MinRR: %.1f", config.RISK_PERCENT, config.MIN_RR)
    logger.info("  ZMQ     : %s", ZMQ_ENDPOINT)
    logger.info("-" * 50)
    logger.info("Mode: LIVE STREAM")
    run_stream()
