"""
main_stream.py — Event-driven entry point (ZeroMQ streaming mode).

CONTAINER ARCHITECTURE
Both the MT5 EA (ZoneBotBridge.mq5) and this Python bot run inside the
same Wine prefix in the same Docker container.  Communication is pure
Wine loopback TCP (127.0.0.1:5556) — no Docker networking involved.

Startup order (managed by override_start.sh):
  1. MT5 terminal launches  (wine terminal64.exe)
  2. ZoneBotBridge EA auto-loads on the GBPUSD chart
  3. EA binds ZMQ PUB on 127.0.0.1:5556
  4. Python bot launches  (wine python -m src.main_stream)
  5. ZmqFeed connects SUB to 127.0.0.1:5556
  6. Tick events flow — bot is live

Because the EA binds BEFORE the Python SUB connects, no messages are
lost at startup.  ZMQ PUB/SUB is a broadcast model — the SUB simply
starts receiving from the first message after connect().

EVENT LOOP
  MT5 EA (ZoneBotBridge.mq5)
      │  PUB tcp://127.0.0.1:5556 (Wine loopback)
      ▼
  ZmqFeed (background daemon thread)
      │  pushes events onto event_queue
      ▼
  Main loop (this file — runs on main thread)
      │  TICK       → run_monitoring_only() every tick (≈100ms)
      │  M1 close   → collect_entry_candidate() + execute
      │  M5 close   → run_m5_zone_mapping()
      │  M30 close  → run_m30_zone_mapping()
      │  H1 close   → run_zone_mapping() (H1 zones)
      ▼
  ExecutionService (unchanged — uses StreamingMarketDataRepo)
      │  get_candles() → CandleBuilder cache (no MT5 round-trip)
      ▼
  MT5Gateway (order placement only)

HOW TO RUN:
    python -m src.main_stream
    python -m src.main_stream --backtest   (delegates to original backtester)
"""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import src.config as config

from src.infrastructure.mt5_bridge.mt5_gateway      import MT5Gateway
from src.infrastructure.market_data_repo             import MT5MarketDataRepository, BrokerClock
from src.infrastructure.trade_repo                   import MT5TradeRepository
from src.infrastructure.news_client                  import ForexNewsClient
from src.infrastructure.mt5_news_client              import MT5NewsClient
from src.infrastructure.composite_news_client        import CompositeNewsClient
from src.infrastructure.cache_store                  import JsonCacheStore
from src.infrastructure.zone_repo                    import InMemoryZoneRepository
from src.infrastructure.spread_calculator            import SpreadCalculator
from src.infrastructure.cleanup_service              import CleanupService
from src.infrastructure.checkpoint_service           import CheckpointService
from src.infrastructure.trade_journal                import TradeJournal
from src.infrastructure.zone_store                   import ZoneFileStore
from src.application.news_manager                   import NewsManager
from src.application.execution_service              import ExecutionService
from src.application.scheduler                      import Scheduler
from src.application.email_service                  import EmailService
from src.application.weekly_backtest_runner         import WeeklyBacktestRunner
from src.infrastructure.stream.candle_builder       import CandleBuilder, TRACKED_TIMEFRAMES
from src.infrastructure.stream.zmq_feed             import ZmqFeed, FEED_STOPPED
from src.infrastructure.stream.streaming_market_data_repo import StreamingMarketDataRepo
from src.domain.entities                            import Timeframe
from src.strategy.strategy_manager                  import StrategyManager
from src.strategy.entry_gate                        import EntryGate

# Re-use helpers from main.py so nothing is duplicated.
from src.main import (
    connect_mt5,
    _setup_logging,
    _commission_in_account_ccy,
    _build_commission_map,
    _BarClockWatcher,
)

logger = logging.getLogger("main_stream")

# ZMQ endpoint the EA publishes on.  Must match PUB_ENDPOINT in ZoneBotBridge.mq5.
# Uses 127.0.0.1 explicitly — both EA and bot run inside the same Wine prefix.
ZMQ_ENDPOINT = getattr(config, "ZMQ_ENDPOINT", "tcp://127.0.0.1:5556")

# How many historical bars to seed per timeframe before the live feed starts.
_SEED_COUNTS: Dict[Timeframe, int] = {
    Timeframe.M1:  120,
    Timeframe.M5:  72,
    Timeframe.M30: 60,    # M30 story lookback needs 8 bars; 60 gives full history
    Timeframe.H1:  120,
    Timeframe.H4:  60,
}


def _seed_builders(
    builders:    Dict[str, CandleBuilder],
    mt5_repo:    MT5MarketDataRepository,
) -> None:
    """
    Pre-populate each CandleBuilder cache with MT5 historical bars at startup.

    This gives the signal finders a full window of candles from the very first
    event instead of needing to wait for live ticks to accumulate the history.
    """
    for sym, builder in builders.items():
        for tf, count in _SEED_COUNTS.items():
            try:
                candles = mt5_repo.get_candles(sym, tf, count)
                if candles:
                    builder.seed_from_history(tf, candles)
                    logger.info(
                        "Seeded %s %s: %d bars (need %d)",
                        sym, tf.value, len(candles), count,
                    )
                else:
                    logger.warning("Seed empty for %s %s", sym, tf.value)
            except Exception as exc:
                logger.warning("Seed failed %s %s: %s", sym, tf.value, exc)


def run_stream() -> None:
    gw = connect_mt5()

    clock      = BrokerClock(gw)
    broker_now = clock.now()
    logger.info("Broker time at startup: %s UTC", broker_now.strftime("%Y-%m-%dT%H:%M:%S"))

    _detected_offset = gw.detect_utc_offset()
    if _detected_offset != 0:
        config.BROKER_UTC_OFFSET_HOURS = _detected_offset
    logger.info(
        "Broker UTC offset: UTC%+d (detected=%+d)",
        config.BROKER_UTC_OFFSET_HOURS, _detected_offset,
    )

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

    # ── MT5 repo — still used for orders, seed data, and fallback ─────
    mt5_market_data = MT5MarketDataRepository(gw)

    # ── Seed historical bars before ZMQ feed starts ────────────────────
    logger.info("Seeding historical candles from MT5...")
    _seed_builders(builders, mt5_market_data)

    # ── Phase 1+2: Strategy engine — seeded from same historical data ──
    strategy = StrategyManager(symbols=config.SYMBOLS)
    logger.info("Seeding StrategyManager with historical candles...")
    _strategy_seed_counts = {
        Timeframe.H1:  120,
        Timeframe.M30: 60,
        Timeframe.M15: 60,
        Timeframe.M5:  72,
        Timeframe.M1:  120,
    }
    _strategy_seed_timeframes = list(_strategy_seed_counts.keys())
    for sym in config.SYMBOLS:
        for tf in _strategy_seed_timeframes:
            try:
                candles = mt5_market_data.get_candles(sym, tf, _strategy_seed_counts[tf])
                if candles:
                    strategy.seed(sym, tf, candles)
            except Exception as exc:
                logger.warning("Strategy seed failed %s %s: %s", sym, tf.value, exc)
    logger.info("StrategyManager ready.")

    # ── Streaming repo — wraps builders, falls back to MT5 ────────────
    stream_repo = StreamingMarketDataRepo(
        builders=builders,
        fallback=mt5_market_data,
    )

    # ── Build services — ExecutionService gets the streaming repo ──────
    trade_repo   = MT5TradeRepository(gw)
    zone_repo    = InMemoryZoneRepository(persist=True)
    cache        = JsonCacheStore()
    mt5_news     = MT5NewsClient()
    if config.NEWS_API_KEY:
        ff_news     = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
        news_client = CompositeNewsClient([ff_news, mt5_news])
    else:
        news_client = mt5_news
    news_manager    = NewsManager(news_client)
    commission      = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    commission_map  = _build_commission_map()
    spread_calc  = SpreadCalculator(
        market_data=       stream_repo,
        commission_per_lot=commission,
        commission_map=    commission_map,
    )
    execution = ExecutionService(
        market_data=       stream_repo,   # <-- streaming, not MT5 polling
        trade_repo=        trade_repo,
        zone_repo=         zone_repo,
        news_manager=      news_manager,
        spread_calculator= spread_calc,
        symbols=           config.SYMBOLS,
        risk_pct=          config.RISK_PERCENT,
        commission=        commission,
        min_rr=            config.MIN_RR,
        account_currency=  config.ACCOUNT_CURRENCY,
        journal=           TradeJournal(),
        clock=             clock,
    )

    # ── Phase 4: Entry gate — converts StrategySignals into EntryCandidate ──
    entry_gate = EntryGate(news_manager=news_manager, execution=execution)
    # Seed starting equity for daily drawdown tracking
    _startup_balance = (gw.get_account_info() or {}).get("balance", 0.0)
    entry_gate.on_new_day(_startup_balance)

    cleanup    = CleanupService()
    checkpoint = CheckpointService(getattr(config, "CHECKPOINT_DIR", ".checkpoints"))
    zone_store = ZoneFileStore()
    email_svc  = EmailService()
    scheduler  = Scheduler()
    weekly_bt  = WeeklyBacktestRunner(mt5_market_data, email_svc)

    # ── Restore checkpoint ─────────────────────────────────────────────
    cp = checkpoint.load()
    if cp:
        logger.info("Resumed from checkpoint (saved_at=%s).", cp.get("saved_at"))
        rt_state = cp.get("runtime_state")
        if rt_state:
            try:
                execution.load_runtime_state(rt_state)
            except Exception as exc:
                logger.warning("Runtime state restore failed: %s", exc)

    # ── Gap recovery ───────────────────────────────────────────────────
    from src.application.gap_recovery_service import GapRecoveryService
    gap_svc = GapRecoveryService(
        execution=   execution,
        market_data= mt5_market_data,   # gap recovery needs real MT5 historical data
        zone_repo=   zone_repo,
        zone_store=  zone_store,
        trade_repo=  gw,
        symbols=     config.SYMBOLS,
        email_svc=   email_svc,
        clock=       clock,
    )
    gap_zones_fresh = False
    if cp:
        try:
            gap_result      = gap_svc.run(cp)
            gap_zones_fresh = gap_result.zones_remapped
            for w in (gap_result.warning_messages or []):
                logger.warning("GAP RECOVERY: %s", w)
        except Exception as exc:
            logger.exception("Gap recovery failed: %s", exc)

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
                journal, checkpoint, weekly_bt,
                balance_fn=lambda: (gw.get_account_info() or {}).get("balance"),
            ).start()
        except Exception as exc:
            logger.warning("Dashboard failed to start: %s", exc)

    # ── Scheduler ──────────────────────────────────────────────────────
    scheduler.add_daily("cleanup", cleanup.run_all, hour=2)
    scheduler.add_weekly("market_close_wipe", cleanup.market_close_wipe, weekday=4, hour=21)
    if getattr(config, "WEEKLY_BACKTEST_ENABLED", True):
        scheduler.add_weekly(
            "weekly_backtest", weekly_bt.run,
            weekday=getattr(config, "WEEKLY_BACKTEST_WEEKDAY", 5),
            hour=   getattr(config, "WEEKLY_BACKTEST_HOUR_UTC", 6),
        )
    scheduler.start()

    # ── Initial zone mapping ───────────────────────────────────────────
    # Always run M30/M5 zone mapping at startup regardless of gap recovery.
    # GapRecoveryService only fills M30/M5 zones when gap > MIN_GAP_SECONDS (60s).
    # For sub-60s restarts (or first boot), _m30_zones/_m5_zones start empty and
    # would stay empty until the next bar close, causing signal evaluation to run
    # without M30/M5 context.  Running unconditionally here is safe — it's idempotent
    # and the data is available from the already-seeded CandleBuilder cache.
    logger.info("Initial M30/M5 zone mapping...")
    for sym in config.SYMBOLS:
        try:
            execution.run_m30_zone_mapping(sym)
        except Exception as exc:
            logger.exception("M30 zone mapping [%s]: %s", sym, exc)
        try:
            execution.run_m5_zone_mapping(sym)
        except Exception as exc:
            logger.exception("M5 zone mapping [%s]: %s", sym, exc)

    if not gap_zones_fresh:
        logger.info("Initial H1 zone mapping...")
        for sym in config.SYMBOLS:
            try:
                execution.run_zone_mapping(sym)
                zone_store.save_snapshot(
                    sym,
                    zone_repo.get_active_zones(sym),
                    zone_repo.get_liquidity_pool(sym),
                )
            except Exception as exc:
                logger.exception("Zone mapping [%s]: %s", sym, exc)

    # ── Bar-clock watchers (checkpoint-seeded) ─────────────────────────
    def _make_watcher(bar_secs: int, cp_key: str) -> _BarClockWatcher:
        if cp and cp.get(cp_key):
            try:
                return _BarClockWatcher.with_last_bar(bar_secs, int(cp[cp_key]))
            except Exception:
                pass
        return _BarClockWatcher(bar_secs, clock.now())

    h1_watcher  = _make_watcher(3600,    "last_h1_bar_ts")
    m30_watcher = _make_watcher(1800,    "last_m30_bar_ts")
    m5_watcher  = _make_watcher(300,     "last_m5_bar_ts")

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
            # Nothing arrived in the last second — checkpoint if due, and also
            # fire time-based zone remaps as a safety net in case the EA missed
            # a bar-close event (e.g. no tick arrived exactly on the boundary).
            clock.invalidate()
            _now = clock.now()
            if m5_watcher.should_remap(_now) and getattr(config, "M5_ZONE_MAPPING_ENABLED", True):
                for _sym in config.SYMBOLS:
                    try:
                        execution.run_m5_zone_mapping(_sym)
                    except Exception as exc:
                        logger.exception("M5 zone remap (timer) [%s]: %s", _sym, exc)
            if m30_watcher.should_remap(_now) and getattr(config, "M30_ZONE_MAPPING_ENABLED", True):
                for _sym in config.SYMBOLS:
                    try:
                        execution.run_m30_zone_mapping(_sym)
                    except Exception as exc:
                        logger.exception("M30 zone remap (timer) [%s]: %s", _sym, exc)
            if h1_watcher.should_remap(_now):
                for _sym in config.SYMBOLS:
                    try:
                        execution.run_zone_mapping(_sym)
                        zone_store.save_snapshot(
                            _sym,
                            zone_repo.get_active_zones(_sym),
                            zone_repo.get_liquidity_pool(_sym),
                        )
                    except Exception as exc:
                        logger.exception("H1 zone remap (timer) [%s]: %s", _sym, exc)
            if (_now - last_checkpoint).total_seconds() >= cp_interval:
                _do_checkpoint(
                    execution, checkpoint, _now, loop_n,
                    h1_watcher, m30_watcher, m5_watcher,
                )
                last_checkpoint = _now
            continue

        if event is FEED_STOPPED:
            # Feed thread exited — this is always a recoverable condition.
            # The EA may have restarted (MT5 terminal reload, container restart).
            # ZmqFeed.start() spawns a new thread; the existing ZMQ context
            # and all builder state are preserved — no data is lost.
            logger.warning("ZMQ feed thread stopped — restarting in 5s...")
            time.sleep(5.0)
            if running[0]:
                feed.start()
            continue

        etype = event.get("type", "")
        clock.invalidate()
        now = clock.now()

        # ── Refresh news periodically (independent of bar events) ──────
        if (now - last_news_refresh).total_seconds() >= 60:
            try:
                news_manager.refresh_if_needed(now)
            except Exception as exc:
                logger.warning("News refresh error: %s", exc)
            last_news_refresh = now

        # ── TICK: run monitoring on every tick ─────────────────────────
        if etype == "TICK":
            sym = event.get("sym", "")
            if sym in config.SYMBOLS:
                try:
                    execution.run_monitoring_only(sym)
                except Exception as exc:
                    logger.exception("Monitoring [%s]: %s", sym, exc)
                # Phase 2: push tick into strategy tick buffer
                try:
                    strategy.push_tick(
                        sym,
                        bid=event.get("bid", 0.0),
                        ask=event.get("ask", 0.0),
                        ts=float(event.get("time", 0)),
                    )
                except Exception as exc:
                    logger.debug("Strategy tick push [%s]: %s", sym, exc)

        # ── CANDLE_CLOSED (from CandleBuilder) or BAR_CLOSE (from EA) ──
        elif etype in ("CANDLE_CLOSED", "BAR_CLOSE"):
            if etype == "CANDLE_CLOSED":
                candle = event.get("candle")
                if candle is None:
                    continue
                sym = candle.symbol
                tf  = candle.timeframe
                # Phase 1: feed closed candle to structure engine
                try:
                    strategy.on_candle(candle)
                except Exception as exc:
                    logger.debug("Strategy on_candle [%s/%s]: %s", sym, tf.value, exc)
            else:
                # Raw BAR_CLOSE from EA — just use for remap triggers
                sym = event.get("sym", "")
                tf_s = event.get("tf", "")
                tf_map = {"M1": Timeframe.M1, "M5": Timeframe.M5, "M30": Timeframe.M30,
                          "H1": Timeframe.H1, "H4": Timeframe.H4}
                tf = tf_map.get(tf_s)
                if tf is None:
                    continue

            if sym not in config.SYMBOLS:
                continue

            # ── H1 close → remap H1 zones ──────────────────────────────
            if tf == Timeframe.H1:
                logger.info("H1 close [%s] — remapping H1 zones.", sym)
                try:
                    execution.run_zone_mapping(sym)
                    zone_store.save_snapshot(
                        sym,
                        zone_repo.get_active_zones(sym),
                        zone_repo.get_liquidity_pool(sym),
                    )
                except Exception as exc:
                    logger.exception("H1 zone remap [%s]: %s", sym, exc)
                # Also remap ALL other symbols — they all share the same H1 bar cadence.
                # Do NOT gate this with h1_watcher.should_remap() here; the watcher
                # is only used in the queue.Empty timer branch to fire when the EA
                # misses a bar-close event.  Calling should_remap() inside this loop
                # advances the internal timestamp on the first call, causing all
                # subsequent symbols in the loop to get False and be skipped.
                h1_watcher.should_remap(now)   # advance the watcher so the timer branch doesn't double-fire
                for other in config.SYMBOLS:
                    if other == sym:
                        continue
                    try:
                        execution.run_zone_mapping(other)
                        zone_store.save_snapshot(
                            other,
                            zone_repo.get_active_zones(other),
                            zone_repo.get_liquidity_pool(other),
                        )
                    except Exception as exc:
                        logger.exception("H1 zone remap [%s]: %s", other, exc)

            # ── M30 close → remap M30 zones for ALL symbols ────────────
            elif tf == Timeframe.M30:
                m30_watcher.should_remap(now)  # advance timer branch guard
                if getattr(config, "M30_ZONE_MAPPING_ENABLED", True):
                    for _s in config.SYMBOLS:
                        try:
                            execution.run_m30_zone_mapping(_s)
                        except Exception as exc:
                            logger.exception("M30 zone remap [%s]: %s", _s, exc)

            # ── M5 close → remap M5 zones for ALL symbols ──────────────
            elif tf == Timeframe.M5:
                m5_watcher.should_remap(now)   # advance timer branch guard
                if getattr(config, "M5_ZONE_MAPPING_ENABLED", True):
                    for _s in config.SYMBOLS:
                        try:
                            execution.run_m5_zone_mapping(_s)
                        except Exception as exc:
                            logger.exception("M5 zone remap [%s]: %s", _s, exc)

            # ── M1 close → evaluate entry candidates ───────────────────
            if tf == Timeframe.M1:
                loop_n += 1
                _run_entry_evaluation(execution, now)
                # Phase 1+2: run strategy signal evaluation for this symbol
                if sym in config.SYMBOLS:
                    try:
                        _run_strategy_evaluation(
                            strategy, stream_repo, sym, builders,
                            entry_gate, mt5_market_data, gw, now,
                        )
                    except Exception as exc:
                        logger.exception("Strategy eval [%s]: %s", sym, exc)

        # ── TRADE event from EA ─────────────────────────────────────────
        elif etype == "TRADE":
            sym = event.get("sym", "")
            logger.info(
                "EA TRADE event | %s %s ticket=%s price=%.5f",
                event.get("event", "?"),
                sym,
                event.get("ticket", "?"),
                event.get("price", 0.0),
            )
            # Refresh monitoring immediately after any trade state change
            if sym in config.SYMBOLS:
                try:
                    execution.run_monitoring_only(sym)
                except Exception as exc:
                    logger.exception("Post-trade monitoring [%s]: %s", sym, exc)

        elif etype == "HEARTBEAT":
            logger.debug("Heartbeat: broker_time=%s", event.get("time"))

        # ── Periodic checkpoint ─────────────────────────────────────────
        if (now - last_checkpoint).total_seconds() >= cp_interval:
            _do_checkpoint(
                execution, checkpoint, now, loop_n,
                h1_watcher, m30_watcher, m5_watcher,
            )
            last_checkpoint = now

    # ── Shutdown ───────────────────────────────────────────────────────
    feed.stop()
    scheduler.stop()
    clock.invalidate()
    # Save a full checkpoint on clean shutdown so gap recovery on next restart
    # has accurate last_loop_ts and runtime_state.  Writing only a minimal dict
    # here would overwrite trade states, bar timestamps, and streaks — causing
    # unnecessary state loss on every clean restart (SIGTERM / docker stop).
    _do_checkpoint(
        execution, checkpoint, clock.now(), loop_n,
        h1_watcher, m30_watcher, m5_watcher,
    )
    gw.disconnect()
    logger.info("Bot stopped cleanly (stream mode).")


# ── Helpers ────────────────────────────────────────────────────────────────

_last_gate_day: Dict[str, str] = {}   # symbol → last date string for new-day reset


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
    """
    Phase 1+2+4 strategy signal evaluation for one symbol on M1 close.

    When entry_gate is provided (Phase 4 mode), a validated signal is
    converted to an EntryCandidate and executed via ExecutionService.

    When entry_gate is None, runs in observation mode (logs only).
    """
    from src.domain.entities import Timeframe as TF

    builder = builders.get(symbol)
    if builder is None:
        return

    m1_candles = builder.get_closed_candles(TF.M1, 60)
    h1_candles = builder.get_closed_candles(TF.H1, 60)
    forming_m1 = builder.get_forming_bar(TF.M1)

    if not m1_candles or not h1_candles:
        return

    import time as _t
    elapsed = (_t.time() - forming_m1.time.timestamp()) if forming_m1 is not None else 30.0

    try:
        current_price = stream_repo.get_current_price(symbol)
    except Exception:
        current_price = m1_candles[-1].close if m1_candles else 0.0

    signal = strategy.evaluate(
        symbol=symbol,
        forming_m1=forming_m1,
        m1_candles=m1_candles,
        h1_candles=h1_candles,
        current_price=current_price,
        elapsed_m1_secs=elapsed,
    )

    if not signal:
        struct = strategy.structure_summary(symbol)
        logger.debug(
            "STRATEGY_NO_SIGNAL [%s] | structure=%s | spread=%s",
            symbol, struct, strategy.spread_state(symbol).value,
        )
        return

    logger.info(
        "STRATEGY_SIGNAL [%s] %s | conf=%.2f | align=%.2f | "
        "tick=%.2f | candle=%.2f | regime=%s | TF_states=%s",
        symbol,
        signal.direction.value.upper(),
        signal.confidence,
        signal.alignment.score,
        signal.tick_analysis.score,
        abs(signal.candle_score.score),
        signal.alignment.regime.value,
        signal.alignment.details,
    )

    # ── Phase 4: attempt entry via EntryGate ──────────────────────────
    if entry_gate is None or not getattr(config, "STRATEGY_GATE_ENABLED", False):
        return  # observation mode — log only

    eval_now = now or datetime.now(tz=timezone.utc)

    # New-day reset: reset daily counters when day changes
    today = eval_now.date().isoformat()
    if _last_gate_day.get(symbol) != today:
        _last_gate_day[symbol] = today
        try:
            bal = (gw.get_account_info() or {}).get("balance", 0.0) if gw else 0.0
            entry_gate.on_new_day(bal)
        except Exception:
            pass

    # Gather execution context
    try:
        digits     = mt5_repo.get_symbol_digits(symbol) if mt5_repo else 5
        tick_value = mt5_repo.get_tick_value(symbol)    if mt5_repo else 10.0
    except Exception:
        digits, tick_value = 5, 10.0

    from src.domain.value_objects import PipCalculator, BrokerCost
    pip_calc = PipCalculator(digits=digits)

    try:
        spread_pips = stream_repo.get_spread_pips(symbol)
    except Exception:
        spread_pips = 1.5

    broker_cost = BrokerCost(
        spread_pips=    spread_pips,
        commission_usd= getattr(config, "COMMISSION_PER_LOT", 0.0),
    )

    # H1 ATR — use last 14 H1 candles
    h1_atr = 0.0
    if len(h1_candles) >= 14:
        h1_atr = sum(c.high - c.low for c in h1_candles[-14:]) / 14

    balance = 0.0
    try:
        balance = (gw.get_account_info() or {}).get("balance", 0.0) if gw else 0.0
    except Exception:
        pass

    candidate = entry_gate.evaluate(
        signal=        signal,
        builder=       builder,
        current_price= current_price,
        now=           eval_now,
        balance=       balance,
        pip_calc=      pip_calc,
        tick_value=    tick_value,
        digits=        digits,
        h1_atr=        h1_atr,
        broker_cost=   broker_cost,
    )

    if candidate is None:
        return

    # Execute the trade
    from src.application.execution_service import ExecutionService as _ES
    exec_svc = entry_gate._exec
    try:
        trade = exec_svc.execute_entry_candidate(candidate)
        if trade:
            entry_gate.record_trade()
            logger.info(
                "STRATEGY_TRADE [%s] %s | conf=%.2f | %.2f lots | "
                "entry=%.5f SL=%.5f TP1=%.5f",
                symbol,
                signal.direction.value.upper(),
                signal.confidence,
                trade.lot_size,
                trade.entry_price,
                trade.stop_loss,
                trade.take_profit,
            )
    except Exception as exc:
        logger.exception("Strategy trade execution [%s]: %s", symbol, exc)


def _run_entry_evaluation(execution: ExecutionService, now: datetime) -> None:
    """Run Phase 1 + Phase 2 entry evaluation across all symbols."""
    p1_candidates = []
    for sym in config.SYMBOLS:
        try:
            c = execution.collect_entry_candidate(sym, is_phase2=False)
            if c:
                p1_candidates.append(c)
        except Exception as exc:
            logger.exception("Entry eval [%s]: %s", sym, exc)

    p1_candidates.sort(key=lambda c: c.trade_score, reverse=True)

    for candidate in p1_candidates:
        if len(execution._tr.get_open_positions()) >= config.MAX_OPEN_TRADES:
            break
        if execution.open_count_for_symbol(candidate.symbol) >= getattr(config, "MAX_TRADES_PER_SYMBOL", 2):
            continue
        try:
            trade = execution.execute_entry_candidate(candidate)
            if trade:
                logger.info(
                    "TRADE P1 | %s %s | score=%d | %.2f lots | "
                    "entry=%.5f SL=%.5f TP=%.5f",
                    trade.symbol, trade.direction.value.upper(),
                    candidate.trade_score, trade.lot_size,
                    trade.entry_price, trade.stop_loss, trade.take_profit,
                )
        except Exception as exc:
            logger.exception("Execute P1 [%s]: %s", candidate.symbol, exc)

    if not getattr(config, "PHASE2_ENABLED", True):
        return

    p2_min = getattr(config, "PHASE2_MIN_TRADE_SCORE", 5)
    p2_cap = getattr(config, "MAX_TRADES_PER_SYMBOL", 2)
    p2_candidates = []

    for sym in config.SYMBOLS:
        if execution.open_count_for_symbol(sym) >= p2_cap:
            continue
        if not execution.has_tp1_hit_for_symbol(sym):
            continue
        try:
            c = execution.collect_entry_candidate(sym, is_phase2=True)
            if c and c.trade_score >= p2_min:
                p2_candidates.append(c)
        except Exception as exc:
            logger.exception("Phase 2 eval [%s]: %s", sym, exc)

    p2_candidates.sort(key=lambda c: c.trade_score, reverse=True)

    for candidate in p2_candidates:
        if len(execution._tr.get_open_positions()) >= config.MAX_OPEN_TRADES:
            break
        if execution.open_count_for_symbol(candidate.symbol) >= p2_cap:
            continue
        try:
            trade = execution.execute_entry_candidate(candidate)
            if trade:
                logger.info(
                    "TRADE P2 | %s %s | score=%d | %.2f lots | "
                    "entry=%.5f SL=%.5f TP=%.5f",
                    trade.symbol, trade.direction.value.upper(),
                    candidate.trade_score, trade.lot_size,
                    trade.entry_price, trade.stop_loss, trade.take_profit,
                )
        except Exception as exc:
            logger.exception("Execute P2 [%s]: %s", candidate.symbol, exc)


def _do_checkpoint(
    execution:   ExecutionService,
    checkpoint:  CheckpointService,
    now:         datetime,
    loop_n:      int,
    h1_watcher:  _BarClockWatcher,
    m30_watcher: _BarClockWatcher,
    m5_watcher:  _BarClockWatcher,
) -> None:
    try:
        rt_state = execution.save_runtime_state()
    except Exception as exc:
        logger.warning("Runtime state serialisation failed: %s", exc)
        rt_state = {}
    checkpoint.save({
        "loop_n":          loop_n,
        "symbols":         config.SYMBOLS,
        "risk_pct":        config.RISK_PERCENT,
        "min_rr":          config.MIN_RR,
        "runtime_state":   rt_state,
        "last_h1_bar_ts":  h1_watcher.last_bar_ts,
        "last_m30_bar_ts": m30_watcher.last_bar_ts,
        "last_m5_bar_ts":  m5_watcher.last_bar_ts,
        "last_loop_ts":    now.isoformat(),
    }, now=now)




if __name__ == "__main__":
    _setup_logging()
    logger.info("-" * 50)
    logger.info("  Zone-Reactive Bot (STREAM MODE)  |  account=%s  |  server=%s",
                config.TRADING_ID, config.MT5_SERVER)
    logger.info("  Symbols : %s", ", ".join(config.SYMBOLS))
    logger.info("  Risk    : %.1f%%  |  MinRR: %.1f", config.RISK_PERCENT, config.MIN_RR)
    logger.info("  ZMQ     : %s", ZMQ_ENDPOINT)
    logger.info("-" * 50)

    parser = argparse.ArgumentParser(description="Zone-Reactive Forex Bot (stream mode)")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--start",   default="")
    parser.add_argument("--end",     default="")
    parser.add_argument("--name",    default="")
    args = parser.parse_args()

    if args.backtest:
        # Delegate to the original backtester — no streaming needed
        from src.main import run_backtest
        logger.info("Mode: BACKTEST | start=%s end=%s name=%s",
                    args.start or "auto", args.end or "auto", args.name or "auto")
        run_backtest(args.start, args.end, args.name)
    else:
        logger.info("Mode: LIVE STREAM")
        run_stream()
