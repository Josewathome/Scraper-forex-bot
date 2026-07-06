# Time-Handling Refactor Plan — Scraper-forex-bot

**Status:** Planning document only. No code has been changed yet.
**Read first:** `../../TIME_HANDLING_REFACTOR_OVERVIEW.md` (shared decision: MT5 server time is the sole authoritative clock; no true-UTC alignment; exactly one conversion boundary survives, for external news timestamps).

This bot shares its entire time architecture with XAUUSD-tradding-bot (same `time_sync.py`, same `BrokerClock` pattern), so most of this plan mirrors `../../XAUUSD-tradding-bot/docs/TIME_HANDLING_REFACTOR_PLAN.md` file-for-file. This document lists the same changes against this repo's own file/line numbers, plus the issues specific to this bot.

---

## 1. Delete the NTP clock entirely

- **Delete** `src/infrastructure/time_sync.py` (`TrueTimeClock`).
- **Rewrite** `BrokerClock` in `src/infrastructure/market_data_repo.py` (lines 21-52) to drop `TrueTimeClock` and read MT5 tick time directly, following New-forex-bot's `BrokerClock` pattern (dead-reckoning via `time.monotonic()` delta on brief MT5 gaps, loud failure — not silent host-clock fallback — when there's never been a good reading).
- Fix `invalidate()` (currently `pass` followed by dead code) to either do something real or be removed.

## 2. Stop treating the auto-detected broker offset as load-bearing for internal time

- `mt5_gateway.py`'s `detect_utc_offset()` and `main_stream.py:198-200`'s startup mutation of `config.BROKER_UTC_OFFSET_HOURS` get removed from the live path **(resolved by the new model for every internal timestamp)**. Rename the constant `MT5_NEWS_OFFSET_HOURS`, make it static and human-verified, and keep it only for the news-ingestion boundary (§4). `detect_utc_offset()` may remain as a manual diagnostic script, not wired into startup.
- Note the existing internal inconsistency this closes: `config.py:51` currently defaults `BROKER_UTC_OFFSET_HOURS = 2` while `time_sync.py`'s docstring says `UTC+3` — both numbers become irrelevant to internal timestamps under the new model, and only one static, verified value survives for news conversion.

## 3. Candle, tick, and server-time timestamps: stop "fixing" them

- `market_data_repo.py`'s `get_candles()` (line 86), `get_candles_range()` (line 210), `get_server_time_utc()` (lines 168-176), and `candle_builder.py`'s bar-construction functions (lines 193, 228, 243) already return raw MT5 epoch time — **no change to the values**, they're already correct under the new model.
- Fix the false docstring at `market_data_repo.py:58-59` ("All timestamps are converted to UTC on the way in" — they aren't, and now they shouldn't be). Rename `get_server_time_utc()` → `get_server_time()`.
- Update the misleading in-file contradiction: `BrokerClock`'s own docstring (lines 26-28) warning that MT5 tick time is "broker-local... produces wrong UTC datetimes" is no longer a warning once nothing is claiming to be UTC — rewrite it to state plainly that MT5 server time is the bot's time, full stop.

## 4. News ingestion: the one place a conversion still happens

- `mt5_news_client.py:123`, `news_client.py`'s ForexFactory (`_parse_ff_time`) and MyFxBook (`_parse_mfxbook_time`) parsers, and `composite_news_client.py`'s dedup bucketing get the same treatment as XAUUSD-tradding-bot: normalize every source onto MT5-server-time basis at ingestion, using the single static `MT5_NEWS_OFFSET_HOURS`, before any deduplication or `NoTradeWindow` construction happens.
- Fix `news_client.py`'s two silent-assumption spots: `_parse_ff_time` asserts UTC with no field actually checked (add a warning log if the source format ever changes), and `_parse_mfxbook_time`'s regex-fallback-to-UTC on parse failure (same — log it, don't silently guess).

## 5. Session windows: redefine once, stop converting at runtime

- This bot's session logic lives in `strategy/entry_gate.py`'s `_in_session()` (lines 499-507) and `config.py`'s `SCALPER_SYMBOL_SESSIONS`/`SCALPER_SESSION_START_UTC`/`SCALPER_SESSION_END_UTC` (lines 269-270, 282-288) — comparison is already a direct `now.hour` check with no runtime offset arithmetic (this bot did this part right already). The only change needed: redefine the config hour values once, in MT5-server-time terms (add the current broker offset by hand, verified against the terminal), and update the comment to record that.
- No engine-level `_utc_hour` conversion helper exists in this bot's strategy files to remove (unlike XAUUSD's `eurusd_engine.py`/`xauusd_engine.py`) — this bot's session gating was simpler to begin with.

## 6. Fix the standalone bug unrelated to timezones

- **`main_stream.py:539-545`**: `elapsed = (stream_repo.now() - forming_m1.time).total_seconds()` always raises `AttributeError` because `StreamingMarketDataRepo` has no `now()` method — silently caught, `elapsed` is hard-coded to `30.0` on every M1 evaluation, permanently forcing `entry_context.py`'s `EARLY_CANDLE` flag `True` and defeating the intended time-weighted candle-progress scoring.
- **Fix:** add a real `now()` method to `StreamingMarketDataRepo` (`infrastructure/stream/streaming_market_data_repo.py`) that returns the shared MT5-time accessor's current value, so `elapsed` reflects genuine bar progress. This is independent of the timezone work but should ship in the same pass since it's in the exact code path being touched.

## 7. Wire the single clock into everything that currently bypasses it

- `execution_service.py`:
  - `self._clock` is stored (line 158) and never read anywhere else — fix every method below to actually use it.
  - `run_monitoring_only()` (line 212) — called on every tick with no `now` parameter; add one, thread `clock.now()` in from `main_stream.py` (currently `main_stream.py:428,489` pass nothing).
  - `_arm_reversal_cooldown()` (line 203), `_update_streak()` (lines 1104, 1113) — use `self._clock.now()` instead of `datetime.now(tz=timezone.utc)`. This fixes the "arm with host clock, check with MT5 clock" mismatch feeding `is_symbol_paused()`/`is_reversal_cooldown()`.
  - `execute_entry_candidate()` (line 581), `_build_trade()` (line 1383) — `created_at=` from `self._clock.now()`.
  - `is_symbol_paused()`/`is_reversal_cooldown()` (lines 180, 194) — remove `Optional[datetime] = None`; make `now` required.
- `entry_gate.py`: `on_new_day()`, `load_runtime_state()`, `record_trade()`, `record_fill()`, `daily_trade_count()` (lines 128-156) — add a required `now` parameter to each, sourced from the caller's `clock.now()`, fixing the anti-duplicate-cooldown clock mismatch at line 331 and the daily-drawdown day-boundary logic.
- `news_manager.py` — `refresh_if_needed()`/`is_blocked()` (lines 48, 66) — remove the `now or datetime.now(...)` fallback; make `now` required.
- `strategy_manager.py:490` — `StrategySignal.timestamp=datetime.now(tz=timezone.utc)` has no `now` parameter on `evaluate()` to source it from; add one and thread the clock through.
- `domain/entities.py` — `Trade`/`Zone`/`LiquidityPool.created_at` default factories (lines 101, 132, 148) — remove the host-clock default or route it through the shared clock.
- `application/scheduler.py` — same fix as XAUUSD: give `Scheduler` a `clock` parameter, use it in `_loop()` (line 74), and fix `_fire()` (lines 108-117) to actually pass the timestamp into fired callbacks so the `tick_analytics` hourly/daily/weekly lambdas' dead `_now or datetime.now(...)` fallback (`main_stream.py:339-356`) can be deleted rather than silently always-firing.
- `mt5_gateway.py:600-601` `get_position_realized_pnl()` — replace the fully naive `_dt.datetime.now()` (no tzinfo at all) history-select window with the shared MT5-time accessor.

## 8. Cosmetic / non-trading-critical

- `api/server.py:52,532` — `"server_time"` field: relabel or route through the shared MT5-time accessor.
- JWT/rate-limiter (`api/server.py:226,290-298,352,364`) — out of scope, host clock is fine here.
- `trade_journal.py`, `cache_store.py`, `cleanup_service.py`, `checkpoint_service.py`'s fallback branch — host clock acceptable; optionally route through the shared clock for full consistency once it's cheap to do.

## Verification checklist after implementing

- [ ] `grep -rn "TrueTimeClock\|time_sync" src/` → no hits.
- [ ] `grep -rn "datetime.now(tz=timezone.utc)\|time.time()\|_dt.datetime.now()" src/` → only hits remaining are `api/server.py`'s JWT/rate-limiter and (optionally) non-critical logging.
- [ ] `grep -rn "BROKER_UTC_OFFSET_HOURS" src/` → only the renamed news-ingestion constant.
- [ ] `stream_repo.now()` exists and `elapsed_m1_secs` is no longer hard-coded to 30.0 — confirm via a log line showing real elapsed values varying tick to tick.
- [ ] Every function gating a trade (session, news, entry, cooldown, exit) takes `now`/`clock` as a required parameter.
