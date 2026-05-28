"""
main.py — Entry Point & Dependency Wiring.

HOW TO RUN:
  Live trading:   python -m src.main
  Backtesting:    python -m src.main --backtest

New services wired in:
  - CleanupService    : daily log/cache/zone retention
  - CheckpointService : state save/resume on restart
  - TradeJournal      : persistent trade record for dashboard
  - ZoneFileStore     : per-symbol timestamped zone snapshots
  - DashboardServer   : Flask API at http://localhost:8080
  - Scheduler         : cleanup daily, backtest weekly
  - WeeklyBacktestRunner + EmailService
"""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import src.config as config

from src.infrastructure.mt5_bridge.mt5_gateway  import MT5Gateway
from src.infrastructure.market_data_repo         import MT5MarketDataRepository, BrokerClock
from src.infrastructure.trade_repo               import MT5TradeRepository
from src.infrastructure.news_client              import ForexNewsClient
from src.infrastructure.mt5_news_client          import MT5NewsClient
from src.infrastructure.composite_news_client    import CompositeNewsClient
from src.infrastructure.cache_store              import JsonCacheStore
from src.infrastructure.zone_repo                import InMemoryZoneRepository
from src.infrastructure.spread_calculator        import SpreadCalculator
from src.infrastructure.cleanup_service          import CleanupService
from src.infrastructure.checkpoint_service       import CheckpointService
from src.infrastructure.trade_journal            import TradeJournal
from src.infrastructure.zone_store               import ZoneFileStore
from src.application.news_manager                import NewsManager
from src.application.execution_service           import ExecutionService
from src.application.scheduler                   import Scheduler
from src.application.email_service               import EmailService
from src.application.weekly_backtest_runner      import WeeklyBacktestRunner


def _setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/trading_bot.log", encoding="utf-8"),
        ],
    )

logger = logging.getLogger("main")


def _send_mt5_failure_email() -> None:
    """Send a one-time alert email when all MT5 connection attempts fail."""
    try:
        from src.application.email_service import EmailService
        svc = EmailService()
        subject = "ZONE BOT ALERT: MT5 connection failed — action required"
        body = (
            f"The trading bot could not connect to MT5 after all attempts.\n\n"
            f"Account : {config.TRADING_ID}\n"
            f"Server  : {config.MT5_SERVER}\n\n"
            f"MOST LIKELY CAUSE:\n"
            f"  The demo account has expired or the credentials are wrong.\n"
            f"  MT5 log is showing 'Invalid account'.\n\n"
            f"ACTION REQUIRED:\n"
            f"  1. Create a new demo account at your broker's website.\n"
            f"  2. Update TRADING_ID, MT5_PASSWORD, MT5_SERVER in .env\n"
            f"  3. Restart: docker compose restart\n\n"
            f"If credentials are correct, MT5 may just be reconnecting.\n"
            f"In that case restart the container:\n"
            f"  docker compose restart\n"
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
        # Exit with 0 so Docker does NOT auto-restart the container.
        # Restarting immediately would just repeat the same failure in a
        # tight loop, wasting resources. The email tells the user to fix
        # the MT5 login via VNC and then manually restart.
        sys.exit(0)
    return gw


def build_services(gw: MT5Gateway, journal=None, clock=None):
    market_data  = MT5MarketDataRepository(gw)
    trade_repo   = MT5TradeRepository(gw)
    zone_repo    = InMemoryZoneRepository(persist=True)
    cache        = JsonCacheStore()
    mt5_news     = MT5NewsClient()
    if config.NEWS_API_KEY:
        ff_news     = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
        news_client = CompositeNewsClient([ff_news, mt5_news])
        logger.info("News guardian: ForexFactory (primary) + MT5 calendar (enrichment)")
    else:
        news_client = mt5_news
        logger.info("News guardian: MT5 built-in calendar (NEWS_API_KEY not set)")
    news_manager    = NewsManager(news_client)
    commission      = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    commission_map  = _build_commission_map()
    spread_calc  = SpreadCalculator(
        market_data=       market_data,
        commission_per_lot=commission,
        commission_map=    commission_map,
    )
    execution = ExecutionService(
        market_data=       market_data,
        trade_repo=        trade_repo,
        zone_repo=         zone_repo,
        news_manager=      news_manager,
        spread_calculator= spread_calc,
        symbols=           config.SYMBOLS,
        risk_pct=          config.RISK_PERCENT,
        commission=        commission,
        min_rr=            config.MIN_RR,
        account_currency=  config.ACCOUNT_CURRENCY,
        journal=           journal,
        clock=             clock,
    )
    return execution, news_manager, market_data, zone_repo


def _commission_in_account_ccy(usd: float) -> float:
    rates = {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79,
        "KES": config.KES_PER_USD,
        "NGN": 1550.0, "ZAR": 18.5,
    }
    return usd * rates.get(config.ACCOUNT_CURRENCY.upper(), 1.0)


def _build_commission_map() -> dict:
    """
    Build a per-symbol commission dict in account currency.

    HFM Zero Spread : $3/lot majors, $5/lot gold — commission only.
    HFM Premium/Pro : $0 commission — spread is the cost.
    IC Markets Raw  : $3.50/lot all instruments — spread + commission.
    """
    acct_type = getattr(config, "BROKER_ACCOUNT_TYPE", "ZERO_SPREAD").upper()
    logger.info("Broker/account type: %s", acct_type)

    if acct_type in ("PREMIUM", "PRO"):
        logger.info("Commission model: $0 (spread-based account — HFM Premium/Pro)")
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


class H1ClockWatcher:
    def __init__(self, broker_now: datetime) -> None:
        # Pre-seed with the current hour so the first loop tick does NOT fire.
        # Uses broker time so bar boundaries align with IC Markets candle closes.
        self._last = int(broker_now.replace(minute=0, second=0, microsecond=0).timestamp())

    def should_remap(self, now: datetime) -> bool:
        h = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        if self._last != h:
            self._last = h
            return True
        return False


class _BarClockWatcher:
    """
    Generic closed-bar detector for any timeframe with a fixed bar size.

    Fires once per bar close — i.e. when the current bar boundary
    (floor(now / bar_seconds) * bar_seconds) changes from the last seen value.
    Pre-seeded at construction so the first loop tick never fires spuriously.
    Always seeded from broker time, never the server's local clock.
    """
    def __init__(self, bar_seconds: int, broker_now: datetime) -> None:
        self._bar_seconds = bar_seconds
        self._last = int(broker_now.timestamp()) // bar_seconds * bar_seconds

    @classmethod
    def with_last_bar(cls, bar_seconds: int, last_bar_ts: int) -> "_BarClockWatcher":
        """
        Construct with a known last-bar timestamp from a checkpoint.
        Prevents the first live loop tick from spuriously firing a remap
        for a bar that was already processed before the restart.
        """
        obj = cls.__new__(cls)
        obj._bar_seconds = bar_seconds
        obj._last = last_bar_ts
        return obj

    @property
    def last_bar_ts(self) -> int:
        """Epoch-seconds of the last bar boundary that was processed."""
        return self._last

    def should_remap(self, now: datetime) -> bool:
        current = int(now.timestamp()) // self._bar_seconds * self._bar_seconds
        if self._last != current:
            self._last = current
            return True
        return False


# Convenience factories — pass broker_now so bar boundaries use IC Markets time.
def M30ClockWatcher(broker_now: datetime) -> _BarClockWatcher:
    return _BarClockWatcher(30 * 60, broker_now)

def M5ClockWatcher(broker_now: datetime) -> _BarClockWatcher:
    return _BarClockWatcher(5 * 60, broker_now)


def run_live() -> None:
    gw  = connect_mt5()

    # ── Broker clock — single source of truth for "now" ───────────────
    # All time-sensitive decisions use IC Markets server time, not the
    # Linux host clock.  One instance is created here and passed to every
    # component that needs the current time.
    clock = BrokerClock(gw)
    broker_now = clock.now()
    logger.info("Broker time at startup: %s UTC", broker_now.strftime("%Y-%m-%dT%H:%M:%S"))

    # Auto-detect broker server UTC offset (informational only — used to
    # correct trade open_time display, not for any trading decisions).
    _detected_offset = gw.detect_utc_offset()
    if _detected_offset != 0:
        config.BROKER_UTC_OFFSET_HOURS = _detected_offset
    logger.info("Broker UTC offset: UTC%+d (detected=%+d)", config.BROKER_UTC_OFFSET_HOURS, _detected_offset)

    acc = gw.get_account_info()
    if acc:
        logger.info("MT5  |  %s  |  balance=%.2f %s  |  server=%s",
                    acc.get("name"), acc.get("balance",0),
                    acc.get("currency","?"), acc.get("server"))

    journal    = TradeJournal()
    execution, news_manager, market_data, zone_repo = build_services(gw, journal, clock=clock)

    cleanup    = CleanupService()
    checkpoint = CheckpointService(getattr(config,"CHECKPOINT_DIR",".checkpoints"))
    zone_store = ZoneFileStore()
    email_svc  = EmailService()
    scheduler  = Scheduler()
    weekly_bt  = WeeklyBacktestRunner(market_data, email_svc)

    cp = checkpoint.load()
    if cp:
        logger.info("Resumed from checkpoint (saved_at=%s).", cp.get("saved_at"))
        rt_state = cp.get("runtime_state")
        if rt_state:
            try:
                execution.load_runtime_state(rt_state)
            except Exception as _rt_exc:
                logger.warning("Runtime state restore failed — starting fresh: %s", _rt_exc)

    # ── Gap recovery — reconstruct missed candles / zones since last checkpoint ──
    # Must run AFTER load_runtime_state (so open trade states exist) and
    # BEFORE initial zone mapping (gap recovery may do zone mapping itself).
    from src.application.gap_recovery_service import GapRecoveryService
    _gap_recovery      = GapRecoveryService(
        execution=   execution,
        market_data= market_data,
        zone_repo=   zone_repo,
        zone_store=  zone_store,
        trade_repo=  gw,          # GapRecoveryService uses MT5Gateway for open positions
        symbols=     config.SYMBOLS,
        email_svc=   email_svc,
        clock=       clock,
    )
    _gap_result        = None
    _gap_zones_fresh   = False
    if cp:
        try:
            _gap_result      = _gap_recovery.run(cp)
            # zones_remapped is True whenever _remap_all_zones() ran — including the
            # >3h skip path, which still remaps zones even though candle replay is skipped.
            _gap_zones_fresh = _gap_result.zones_remapped
            if _gap_result.warning_messages:
                for _w in _gap_result.warning_messages:
                    logger.warning("GAP RECOVERY: %s", _w)
        except Exception as _gap_exc:
            logger.exception("Gap recovery failed — continuing with fresh zone mapping: %s", _gap_exc)
            _gap_zones_fresh = False

    # Re-apply any config values changed via the dashboard before last shutdown.
    # Must run AFTER checkpoint load and BEFORE building services that read config.
    try:
        from src.api.server import load_config_overrides
        load_config_overrides()
    except Exception as _e:
        logger.warning("Config override load skipped: %s", _e)

    if getattr(config, "DASHBOARD_ENABLED", True):
        try:
            from src.api.server import DashboardServer
            DashboardServer(
                journal, checkpoint, weekly_bt,
                balance_fn=lambda: (gw.get_account_info() or {}).get("balance"),
            ).start()
        except Exception as e:
            logger.warning("Dashboard failed to start: %s", e)

    scheduler.add_daily("cleanup", cleanup.run_all, hour=2)
    # Friday 21:00 UTC = New York session close. Wipe zones + cache so the bot
    # starts fresh on Monday without months of accumulated files.
    scheduler.add_weekly("market_close_wipe", cleanup.market_close_wipe, weekday=4, hour=21)
    if getattr(config, "WEEKLY_BACKTEST_ENABLED", True):
        scheduler.add_weekly(
            "weekly_backtest", weekly_bt.run,
            weekday=getattr(config,"WEEKLY_BACKTEST_WEEKDAY",5),
            hour=   getattr(config,"WEEKLY_BACKTEST_HOUR_UTC",6),
        )
    scheduler.start()

    running = [True]
    def _shutdown(sig, frame):
        logger.info("Shutdown signal.")
        running[0] = False
    signal.signal(signal.SIGINT, _shutdown)
    try: signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError): pass

    # Initial zone mapping — skip if gap recovery already produced fresh zones.
    if not _gap_zones_fresh:
        logger.info("Initial zone mapping...")
        for sym in config.SYMBOLS:
            try:
                execution.run_zone_mapping(sym)
                zone_store.save_snapshot(sym, zone_repo.get_active_zones(sym), zone_repo.get_liquidity_pool(sym))
            except Exception as exc:
                logger.exception("Zone mapping [%s]: %s", sym, exc)

    logger.info("Bot live. Account type: %s | Watching: %s",
                getattr(config, "HFM_ACCOUNT_TYPE", "ZERO_SPREAD"),
                " | ".join(config.SYMBOLS))

    # Seed bar-clock watchers from checkpoint timestamps when available,
    # otherwise from broker time so bar boundaries align with IC Markets.
    # This prevents the first loop tick from falsely triggering a zone remap
    # for a bar that was already processed before the last checkpoint.
    def _make_watcher(bar_secs: int, cp_key: str) -> _BarClockWatcher:
        if cp and cp.get(cp_key):
            try:
                return _BarClockWatcher.with_last_bar(bar_secs, int(cp[cp_key]))
            except Exception:
                pass
        return _BarClockWatcher(bar_secs, clock.now())

    h1_watcher      = _make_watcher(3600,      "last_h1_bar_ts")
    m30_watcher     = _make_watcher(30 * 60,   "last_m30_bar_ts")
    m5_watcher      = _make_watcher(5  * 60,   "last_m5_bar_ts")
    last_checkpoint = clock.now()
    cp_interval     = getattr(config,"CHECKPOINT_INTERVAL_MIN", 5) * 60
    loop_n          = 0

    while running[0]:
        # Invalidate clock cache at the start of each tick so we get a
        # fresh broker timestamp for this iteration.
        clock.invalidate()
        loop_start = clock.now()
        try:
            news_manager.refresh_if_needed(loop_start)

            # ── H1 zone remap (every H1 close) ────────────────────
            if h1_watcher.should_remap(loop_start):
                logger.info("H1 close - re-mapping H1 zones.")
                for sym in config.SYMBOLS:
                    try:
                        execution.run_zone_mapping(sym)
                        zone_store.save_snapshot(sym, zone_repo.get_active_zones(sym), zone_repo.get_liquidity_pool(sym))
                    except Exception as exc:
                        logger.exception("H1 zone remap [%s]: %s", sym, exc)

            # ── M30 zone remap (every M30 close) ──────────────────
            if getattr(config, "M30_ZONE_MAPPING_ENABLED", True) and m30_watcher.should_remap(loop_start):
                logger.info("M30 close - re-mapping M30 zones.")
                for sym in config.SYMBOLS:
                    try:
                        execution.run_m30_zone_mapping(sym)
                    except Exception as exc:
                        logger.exception("M30 zone remap [%s]: %s", sym, exc)

            # ── M5 zone remap (every M5 close) ────────────────────
            if getattr(config, "M5_ZONE_MAPPING_ENABLED", True) and m5_watcher.should_remap(loop_start):
                logger.debug("M5 close - re-mapping M5 zones.")
                for sym in config.SYMBOLS:
                    try:
                        execution.run_m5_zone_mapping(sym)
                    except Exception as exc:
                        logger.exception("M5 zone remap [%s]: %s", sym, exc)

            # ── Monitoring pass (all symbols first) ───────────────
            for sym in config.SYMBOLS:
                try:
                    execution.run_monitoring_only(sym)
                except Exception as exc:
                    logger.exception("Monitoring [%s]: %s", sym, exc)

            # ── Phase 1: collect → rank → execute ─────────────────
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
                    logger.debug("Phase 1 stopped: global cap reached (%d)", config.MAX_OPEN_TRADES)
                    break
                if execution.open_count_for_symbol(candidate.symbol) >= getattr(config, "MAX_TRADES_PER_SYMBOL", 2):
                    logger.debug("Phase 1 skip %s: per-symbol cap reached", candidate.symbol)
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

            # ── Phase 2: conviction re-entry (TP1-gated) ──────────
            if getattr(config, "PHASE2_ENABLED", True):
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
                        logger.debug("Phase 2 stopped: global cap reached (%d)", config.MAX_OPEN_TRADES)
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

            loop_n += 1
            if (loop_start - last_checkpoint).total_seconds() >= cp_interval:
                try:
                    rt_state = execution.save_runtime_state()
                except Exception as _cp_exc:
                    logger.warning("Runtime state serialisation failed: %s", _cp_exc)
                    rt_state = {}
                checkpoint.save({
                    "loop_n":          loop_n,
                    "symbols":         config.SYMBOLS,
                    "risk_pct":        config.RISK_PERCENT,
                    "min_rr":          config.MIN_RR,
                    "runtime_state":   rt_state,
                    # Gap-recovery anchors — record the last bar boundary seen for
                    # each timeframe so GapRecoveryService can detect missed bars.
                    "last_h1_bar_ts":  h1_watcher.last_bar_ts,
                    "last_m30_bar_ts": m30_watcher.last_bar_ts,
                    "last_m5_bar_ts":  m5_watcher.last_bar_ts,
                    "last_loop_ts":    loop_start.isoformat(),
                }, now=loop_start)   # broker time → saved_at aligned with last_loop_ts
                last_checkpoint = loop_start

        except Exception as exc:
            logger.exception("Loop error: %s", exc)

        # Use monotonic wall-clock for sleep timing only — this is pure
        # performance measurement (how long did the loop take?), not a
        # trading decision, so local clock is correct here.
        import time as _wall
        elapsed = (clock.now() - loop_start).total_seconds()
        time.sleep(max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed))

    scheduler.stop()
    checkpoint.save({"symbols": config.SYMBOLS, "shutdown": "clean"}, now=clock.now())
    gw.disconnect()
    logger.info("Bot stopped cleanly.")


def run_backtest(
    start_date:  str = "",
    end_date:    str = "",
    report_name: str = "",
) -> None:
    from src.application.backtester  import Backtester
    from src.application.diagnostics import DiagnosticsCollector
    from src.domain.entities         import Timeframe
    from datetime                    import timedelta

    _now      = datetime.now(tz=timezone.utc)
    _fallback_start = _now - timedelta(days=30 * config.BACKTEST_MONTHS_BACK)

    # Priority: CLI arg → config override → auto-computed from BACKTEST_MONTHS_BACK
    start_str = start_date or config.BACKTEST_START_DATE or _fallback_start.strftime("%Y-%m-%d")
    end_str   = end_date   or config.BACKTEST_END_DATE   or _now.strftime("%Y-%m-%d")

    # Align the MT5 fetch window to the resolved date range so that
    # explicit --start/--end CLI dates are actually fetched from MT5.
    start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)

    logger.info("Backtest: %s to %s", start_str, end_str)
    gw = connect_mt5()
    md = MT5MarketDataRepository(gw)

    h1_buf = timedelta(hours=150)
    h4_buf = timedelta(hours=config.H4_CANDLE_COUNT * 4 + 40)

    # Request M1 from the full backtest start date.
    # MT5 returns whatever it has cached — no error if older bars are missing,
    # it simply returns fewer bars.  The simulator already falls back to M5
    # for any gap.  Press Home on each symbol's M1 chart first to maximise
    # the history the terminal has cached locally (up to ~5 months on HFM).
    d1_buf = timedelta(days=config.D1_CANDLE_COUNT + 10)
    candle_data = {}
    m30_buf = timedelta(hours=60)  # 60 bars of M30 context before start for zone mapping
    for sym in config.SYMBOLS:
        h1  = md.get_candles_range(sym, Timeframe.H1,  start_dt-h1_buf,  end_dt)
        m5  = md.get_candles_range(sym, Timeframe.M5,  start_dt,         end_dt)
        m1  = md.get_candles_range(sym, Timeframe.M1,  start_dt,         end_dt)
        h4  = md.get_candles_range(sym, Timeframe.H4,  start_dt-h4_buf,  end_dt)
        d1  = md.get_candles_range(sym, Timeframe.D1,  start_dt-d1_buf,  end_dt)
        m30 = md.get_candles_range(sym, Timeframe.M30, start_dt-m30_buf, end_dt)
        if h1 and m5:
            candle_data[sym] = {
                Timeframe.H1: h1, Timeframe.M5: m5, Timeframe.M1: m1,
                Timeframe.H4: h4, Timeframe.D1: d1, Timeframe.M30: m30 or [],
            }
            m1_count  = len(m1)  if m1  else 0
            m5_count  = len(m5)  if m5  else 0
            m30_count = len(m30) if m30 else 0
            m1_coverage = round(m1_count / max(m5_count * 5, 1) * 100, 1)
            logger.info("  %s: H1=%d  M5=%d  M1=%d (%.1f%% coverage)  H4=%d  D1=%d  M30=%d",
                        sym, len(h1), m5_count, m1_count, m1_coverage,
                        len(h4) if h4 else 0, len(d1) if d1 else 0, m30_count)
        else:
            logger.warning("  %s: insufficient data (H1=%s M5=%s).",
                           sym, "ok" if h1 else "MISSING", "ok" if m5 else "MISSING")

    if not candle_data:
        logger.critical("No candle data.")
        gw.disconnect(); sys.exit(1)

    cache    = JsonCacheStore()
    bt_mt5   = MT5NewsClient()
    if config.NEWS_API_KEY:
        bt_ff   = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
        bt_news = CompositeNewsClient([bt_ff, bt_mt5])
    else:
        bt_news = bt_mt5
    comm  = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    bt_bal = getattr(config, "BACKTEST_INITIAL_BALANCE", config.ACCOUNT_BALANCE)
    bt    = Backtester(news_client=bt_news, account=bt_bal,
                       risk_pct=config.RISK_PERCENT, commission=comm,
                       currency=config.ACCOUNT_CURRENCY,
                       commission_map=_build_commission_map())
    diag    = DiagnosticsCollector()
    results = bt.run(candle_data=candle_data, start_date=start_str,
                     end_date=end_str, diag=diag)
    bt.analyze_trade_features(results)

    report_dir  = Path("src/backtest_results")
    report_dir.mkdir(parents=True, exist_ok=True)
    run_ts      = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Priority: CLI --name arg → config BACKTEST_REPORT_NAME → default "backtest"
    rname       = report_name or getattr(config, "BACKTEST_REPORT_NAME", "backtest")
    report_path = report_dir / f"{rname}_{start_str}_{end_str}_{run_ts}.txt"
    diag.write_report(str(report_path))
    logger.info("Report -> %s", report_path)
    gw.disconnect()


if __name__ == "__main__":
    _setup_logging()
    logger.info("-" * 50)
    logger.info("  Zone-Reactive Bot  |  account=%s  |  server=%s",
                config.TRADING_ID, config.MT5_SERVER)
    logger.info("  Symbols : %s", ", ".join(config.SYMBOLS))
    logger.info("  Risk    : %.1f%%  |  MinRR: %.1f", config.RISK_PERCENT, config.MIN_RR)
    logger.info("-" * 50)

    parser = argparse.ArgumentParser(description="Zone-Reactive Forex Bot")
    parser.add_argument("--backtest", action="store_true",
                        help="Run backtest instead of live trading")
    parser.add_argument("--start", default="",
                        help="Backtest start date YYYY-MM-DD (overrides config)")
    parser.add_argument("--end", default="",
                        help="Backtest end date YYYY-MM-DD (overrides config)")
    parser.add_argument("--name", default="",
                        help="Report filename label, e.g. 'story_fixes_v1' "
                             "(overrides config BACKTEST_REPORT_NAME)")
    args = parser.parse_args()

    if args.backtest:
        logger.info("Mode: BACKTEST | start=%s end=%s name=%s",
                    args.start or "auto", args.end or "auto", args.name or "auto")
        run_backtest(
            start_date=  args.start,
            end_date=    args.end,
            report_name= args.name,
        )
    else:
        logger.info("Mode: LIVE")
        run_live()