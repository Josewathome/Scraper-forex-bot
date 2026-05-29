from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple, TYPE_CHECKING

import src.config as config
from src.domain.entities import (Candle, Direction, LiquidityPool, SignalType,
                                   Timeframe, TradeSignal, Zone, ZoneType)
from src.domain.value_objects import PipCalculator
from src.application.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


def atr(candles: List[Candle], period: int) -> float:
    """Average True Range over `period` candles."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(max(1, len(candles) - period), len(candles)):
        h = candles[i].high
        l = candles[i].low
        pc = candles[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def calculate_zone_buffer(atr_price: float, buffer_pct: float) -> float:
    return atr_price * getattr(config, 'ZONE_ATR_BUFFER_PCT', 0.15)


def is_in_trading_session(dt: datetime) -> bool:
    hour = dt.hour
    for session in config.TRADING_SESSIONS.values():
        if session['start'] <= hour < session['end']:
            return True
    return False


def find_htf_zone_confluence(h1_zone: Zone, h4_zones: List[Zone]) -> bool:
    min_overlap_pct = getattr(config, 'H4_ZONE_MIN_OVERLAP_PCT', 0.2)
    for h4z in h4_zones:
        if h4z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  h4z.low)
        overlap_high = min(h1_zone.high, h4z.high)
        if overlap_high <= overlap_low:
            continue
        overlap_size  = overlap_high - overlap_low
        smaller_size  = min(h1_zone.high - h1_zone.low, h4z.high - h4z.low)
        if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
            return True
    return False


def find_m30_zone_confluence(h1_zone: Zone, m30_zones: List[Zone]) -> bool:
    min_overlap_pct = getattr(config, 'M30_ZONE_MIN_OVERLAP_PCT', 0.2)
    for z in m30_zones:
        if z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  z.low)
        overlap_high = min(h1_zone.high, z.high)
        if overlap_high <= overlap_low:
            continue
        overlap_size = overlap_high - overlap_low
        smaller_size = min(h1_zone.high - h1_zone.low, z.high - z.low)
        if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
            return True
    return False


def find_m5_zone_confluence(h1_zone: Zone, m5_zones: List[Zone]) -> bool:
    min_overlap_pct = getattr(config, 'M5_ZONE_MIN_OVERLAP_PCT', 0.2)
    for z in m5_zones:
        if z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  z.low)
        overlap_high = min(h1_zone.high, z.high)
        if overlap_high <= overlap_low:
            continue
        overlap_size = overlap_high - overlap_low
        smaller_size = min(h1_zone.high - h1_zone.low, z.high - z.low)
        if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
            return True
    return False


def score_setup(zone_direction: Direction, h4_bias: Optional[Direction],
                bar_price: float, h4_eq: Tuple[float, float],
                d1_bias: Optional[Direction], h4_zone_confluent: bool) -> int:
    score = 0
    # H4 trend alignment: +3
    if h4_bias == zone_direction:
        score += 3
    # H4 zone confluence: bonus
    if h4_zone_confluent:
        score += getattr(config, 'H4_ZONE_CONFLUENCE_BONUS', 2)
    # D1 alignment: +1
    if d1_bias == zone_direction:
        score += 1
    # H4 equilibrium: +2 if price is in the discount/premium half
    if h4_eq and h4_eq[0] < h4_eq[1]:
        eq_mid = (h4_eq[0] + h4_eq[1]) / 2
        if zone_direction == Direction.BULLISH and bar_price <= eq_mid:
            score += 2
        elif zone_direction == Direction.BEARISH and bar_price >= eq_mid:
            score += 2
    return score


def grade_setup(score: int) -> Tuple[str, float, float]:
    """Returns (grade, risk_fraction, min_rr)."""
    if score >= 5:
        return ("A+", config.SETUP_RISK_A_PLUS, config.MIN_RR)
    if score >= 3:
        return ("B",  config.SETUP_RISK_B,      config.MIN_RR)
    return ("C", config.SETUP_RISK_C, config.COUNTER_TREND_MIN_RR)


def _is_swing_high(candles: List[Candle], index: int) -> bool:
    if index < 3 or index >= len(candles) - 3:
        return False
    h = candles[index].high
    return all(candles[index + k].high < h for k in range(-3, 0)) and \
           all(candles[index + k].high < h for k in range(1, 4))


def _is_swing_low(candles: List[Candle], index: int) -> bool:
    if index < 3 or index >= len(candles) - 3:
        return False
    l = candles[index].low
    return all(candles[index + k].low > l for k in range(-3, 0)) and \
           all(candles[index + k].low > l for k in range(1, 4))


def _breaks_structure_up(candles: List[Candle], from_index: int, lookback: int = 20) -> bool:
    start = max(0, from_index - lookback)
    swing_highs = []
    for i in range(start, from_index):
        if _is_swing_high(candles, i):
            swing_highs.append(candles[i].high)
    if not swing_highs:
        return False
    impulse_window = candles[from_index : from_index + getattr(config, 'OB_IMPULSE_CANDLES', 3)]
    return any(c.close > max(swing_highs) for c in impulse_window)


def _breaks_structure_down(candles: List[Candle], from_index: int, lookback: int = 20) -> bool:
    start = max(0, from_index - lookback)
    swing_lows = []
    for i in range(start, from_index):
        if _is_swing_low(candles, i):
            swing_lows.append(candles[i].low)
    if not swing_lows:
        return False
    impulse_window = candles[from_index : from_index + getattr(config, 'OB_IMPULSE_CANDLES', 3)]
    return any(c.close < min(swing_lows) for c in impulse_window)


def _is_mitigated(candles: List[Candle], start_idx: int,
                  zone_low: float, zone_high: float, direction: Direction) -> bool:
    for i in range(start_idx, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.low <= zone_low:
            return True
        if direction == Direction.BEARISH and c.high >= zone_high:
            return True
    return False


def find_h4_bias(h4_candles: List[Candle]) -> Optional[Direction]:
    lookback = getattr(config, 'H4_EQUILIBRIUM_LOOKBACK', 20)
    if len(h4_candles) < lookback:
        return None
    window = h4_candles[-lookback:]
    swing_highs = []
    swing_lows  = []
    for i in range(len(window)):
        if _is_swing_high(window, i):
            swing_highs.append(window[i].high)
        if _is_swing_low(window, i):
            swing_lows.append(window[i].low)
    higher_highs = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
    higher_lows  = len(swing_lows)  >= 2 and swing_lows[-1]  > swing_lows[-2]
    lower_highs  = len(swing_highs) >= 2 and swing_highs[-1] < swing_highs[-2]
    lower_lows   = len(swing_lows)  >= 2 and swing_lows[-1]  < swing_lows[-2]
    mid = len(window) // 2
    first_half_close  = sum(c.close for c in window[:mid]) / max(1, mid)
    second_half_close = sum(c.close for c in window[mid:]) / max(1, len(window) - mid)
    threshold = 0.0015
    if (higher_highs and higher_lows) or (second_half_close - first_half_close > threshold):
        return Direction.BULLISH
    if (lower_highs and lower_lows) or (first_half_close - second_half_close > threshold):
        return Direction.BEARISH
    return None


def find_d1_bias(d1_candles: List[Candle]) -> Optional[Direction]:
    lookback = getattr(config, 'D1_EQUILIBRIUM_LOOKBACK', 15)
    if len(d1_candles) < lookback:
        return None
    window = d1_candles[-lookback:]
    swing_highs = []
    swing_lows  = []
    for i in range(len(window)):
        if _is_swing_high(window, i):
            swing_highs.append(window[i].high)
        if _is_swing_low(window, i):
            swing_lows.append(window[i].low)
    mid = len(window) // 2
    first_avg  = sum(c.close for c in window[:mid]) / max(1, mid)
    second_avg = sum(c.close for c in window[mid:]) / max(1, len(window) - mid)
    threshold  = 0.003
    higher_highs = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
    higher_lows  = len(swing_lows)  >= 2 and swing_lows[-1]  > swing_lows[-2]
    lower_highs  = len(swing_highs) >= 2 and swing_highs[-1] < swing_highs[-2]
    lower_lows   = len(swing_lows)  >= 2 and swing_lows[-1]  < swing_lows[-2]
    if (higher_highs and higher_lows) or (second_avg - first_avg > threshold):
        return Direction.BULLISH
    if (lower_highs and lower_lows) or (first_avg - second_avg > threshold):
        return Direction.BEARISH
    return None


def compute_h4_equilibrium(h4_candles: List[Candle]) -> Tuple[float, float]:
    lookback = getattr(config, 'H4_EQUILIBRIUM_LOOKBACK', 20)
    window = h4_candles[-lookback:] if len(h4_candles) >= lookback else h4_candles
    if not window:
        return (0.0, 0.0)
    return (min(c.low for c in window), max(c.high for c in window))


def find_order_blocks(candles: List[Candle], symbol: str, timeframe: Timeframe) -> List[Zone]:
    zones = []
    scan_range = getattr(config, 'OB_IMPULSE_CANDLES', 3)
    for i in range(scan_range, len(candles) - scan_range):
        c = candles[i]
        impulse_window = candles[i+1 : i+1+scan_range]
        if not impulse_window:
            continue
        avg_impulse = sum(x.body_size for x in candles[max(0,i-10):i]) / max(1, min(10, i))
        strong_impulse = avg_impulse * getattr(config, 'OB_IMPULSE_MULTIPLIER', 2.0)
        # Bullish OB: bearish candle before strong bullish impulse that breaks structure up
        if c.is_bearish and _breaks_structure_up(candles, i+1):
            if not _is_mitigated(candles, i+1, c.low, c.high, Direction.BULLISH):
                zones.append(Zone(
                    id=str(uuid.uuid4())[:8], symbol=symbol,
                    zone_type=ZoneType.ORDER_BLOCK, direction=Direction.BULLISH,
                    high=c.high, low=c.low, created_at=c.time,
                ))
        # Bearish OB: bullish candle before strong bearish impulse
        if c.is_bullish and _breaks_structure_down(candles, i+1):
            if not _is_mitigated(candles, i+1, c.low, c.high, Direction.BEARISH):
                zones.append(Zone(
                    id=str(uuid.uuid4())[:8], symbol=symbol,
                    zone_type=ZoneType.ORDER_BLOCK, direction=Direction.BEARISH,
                    high=c.high, low=c.low, created_at=c.time,
                ))
    return zones


def find_fair_value_gaps(candles: List[Candle], symbol: str,
                          timeframe: Timeframe, atr_value: float) -> List[Zone]:
    zones = []
    min_size = atr_value * getattr(config, 'FVG_MIN_ATR_RATIO', 0.3)
    for i in range(1, len(candles) - 1):
        prev = candles[i-1]
        curr = candles[i]
        nxt  = candles[i+1]
        # Bullish FVG: gap between prev high and next low
        fvg_low  = prev.high
        fvg_high = nxt.low
        if fvg_high > fvg_low and (fvg_high - fvg_low) >= min_size:
            if _breaks_structure_up(candles, i) and not _is_mitigated(candles, i+1, fvg_low, fvg_high, Direction.BULLISH):
                zones.append(Zone(
                    id=str(uuid.uuid4())[:8], symbol=symbol,
                    zone_type=ZoneType.FAIR_VALUE_GAP, direction=Direction.BULLISH,
                    high=fvg_high, low=fvg_low, created_at=curr.time,
                ))
        # Bearish FVG: gap between next high and prev low
        fvg_low  = nxt.high
        fvg_high = prev.low
        if fvg_low > fvg_high and (fvg_low - fvg_high) >= min_size:
            if _breaks_structure_down(candles, i) and not _is_mitigated(candles, i+1, fvg_high, fvg_low, Direction.BEARISH):
                zones.append(Zone(
                    id=str(uuid.uuid4())[:8], symbol=symbol,
                    zone_type=ZoneType.FAIR_VALUE_GAP, direction=Direction.BEARISH,
                    high=fvg_low, low=fvg_high, created_at=curr.time,
                ))
    return zones


def find_liquidity_pool(candles: List[Candle], symbol: str, timeframe: Timeframe) -> Optional[LiquidityPool]:
    window = candles[-getattr(config, 'LIQUIDITY_LOOKBACK', 10):]
    if not window:
        return None
    highs = [c.high for c in window]
    lows  = [c.low  for c in window]
    high_level = max(highs)
    low_level  = min(lows)
    last_close = window[-1].close
    mid = (high_level + low_level) / 2
    direction = Direction.BULLISH if last_close < mid else Direction.BEARISH
    price = high_level if direction == Direction.BULLISH else low_level
    return LiquidityPool(symbol=symbol, direction=direction, price=price,
                          created_at=datetime.now(tz=timezone.utc))


def compute_tp1_price(direction: Direction, entry: float, stop_loss: float) -> float:
    sl_dist = abs(entry - stop_loss)
    ratio   = getattr(config, 'TIERED_TP1_RATIO', 1.5) if getattr(config, 'TIERED_TP_ENABLED', True) else 5
    tp1_dist = sl_dist * ratio
    if direction == Direction.BULLISH:
        return round(entry + tp1_dist, 5)
    return round(entry - tp1_dist, 5)


def compute_tp2_price(direction: Direction, entry: float, stop_loss: float) -> float:
    sl_dist = abs(entry - stop_loss)
    ratio   = getattr(config, 'TIERED_TP2_RATIO', 2.0) if getattr(config, 'TIERED_TP_ENABLED', True) else 3.5
    tp2_dist = sl_dist * ratio
    if direction == Direction.BULLISH:
        return round(entry + tp2_dist, 5)
    return round(entry - tp2_dist, 5)


def get_initial_risk(entry: float, stop_loss: float) -> float:
    return abs(entry - stop_loss)


def check_min_sl_pips(symbol: str, sl_pips: float) -> bool:
    min_pips = config.MIN_SL_PIPS.get(symbol, config.MIN_SL_PIPS_DEFAULT)
    return sl_pips >= min_pips


def _compute_sl_buffer(symbol: str, atr_price: float, pip_calc: PipCalculator, spread_pips: float) -> float:
    multiplier  = config.ATR_SL_MULTIPLIERS.get(symbol, config.ATR_SL_MULTIPLIER_DEFAULT)
    sl_pips_static = config.EXTRA_PIPS_SL.get(symbol, config.EXTRA_PIPS_SL_DEFAULT)
    return atr_price * multiplier + pip_calc.pips_to_price(sl_pips_static)


def find_m5_choch_signal(
    m5_candles: List[Candle], zone: Zone, pip_calc: PipCalculator,
    spread_pips: float, liquidity_pool: Optional[LiquidityPool],
    atr_price: float, diag: Optional[DiagnosticsCollector] = None,
    zone_buffer: float = 0.0, min_rr_override: Optional[float] = None,
) -> Optional[TradeSignal]:
    if len(m5_candles) < 10:
        return None
    symbol   = zone.symbol
    avg_volume = sum(c.volume for c in m5_candles[-10:]) / 10
    sl_buffer  = _compute_sl_buffer(symbol, atr_price, pip_calc, spread_pips)
    min_rr     = min_rr_override if min_rr_override is not None else config.MIN_RR
    zone_tapped = False
    retracement_start_idx = None
    for i, c in enumerate(m5_candles):
        if zone.direction == Direction.BULLISH:
            if c.low <= zone.high and c.low >= zone.low - zone_buffer:
                zone_tapped = True
                retracement_start_idx = i
        else:
            if c.high >= zone.low and c.high <= zone.high + zone_buffer:
                zone_tapped = True
                retracement_start_idx = i
    if not zone_tapped or retracement_start_idx is None:
        if diag: diag.record_rejection("zone_not_tapped", symbol=symbol)
        return None
    wave = m5_candles[retracement_start_idx:]
    if len(wave) < 3:
        return None
    for i in range(3, len(wave)):
        c = wave[i]
        prior_avg_body = sum(x.body_size for x in wave[max(0,i-10):i]) / max(1, min(10, i))
        if zone.direction == Direction.BULLISH:
            wave_high = max(x.high for x in wave[:i])
            disp = c.close > wave_high
            volume_ok = c.volume >= avg_volume * config.MIN_DISPLACEMENT_VOLUME_RATIO
            body_ok   = c.body_size >= prior_avg_body * config.CANDLE_SIZE_RATIO
            if not (disp and volume_ok and body_ok):
                continue
            entry  = c.close
            sl     = zone.low - sl_buffer
            tp     = entry + (entry - sl) * min_rr
            wave_low = min(x.low for x in wave[:i])
            sl_dist  = abs(entry - sl)
            tp_dist  = abs(tp - entry)
            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            if rr < min_rr:
                if diag: diag.record_rejection("rr_too_low", rr=rr, min_rr=min_rr)
                continue
            if diag: diag.record_signal("M5_CHOCH_BULLISH", entry=entry, sl=sl, tp=tp, rr=rr)
            return TradeSignal(symbol=symbol, direction=Direction.BULLISH,
                               entry_price=entry, stop_loss=sl, take_profit=tp,
                               signal_type=SignalType.CHOCH, timeframe=Timeframe.M5, zone=zone)
        else:
            wave_low = min(x.low for x in wave[:i])
            disp = c.close < wave_low
            volume_ok = c.volume >= avg_volume * config.MIN_DISPLACEMENT_VOLUME_RATIO
            body_ok   = c.body_size >= prior_avg_body * config.CANDLE_SIZE_RATIO
            if not (disp and volume_ok and body_ok):
                continue
            entry  = c.close
            sl     = zone.high + sl_buffer
            tp     = entry - (sl - entry) * min_rr
            sl_dist = abs(sl - entry)
            tp_dist = abs(entry - tp)
            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            if rr < min_rr:
                if diag: diag.record_rejection("rr_too_low", rr=rr, min_rr=min_rr)
                continue
            if diag: diag.record_signal("M5_CHOCH_BEARISH", entry=entry, sl=sl, tp=tp, rr=rr)
            return TradeSignal(symbol=symbol, direction=Direction.BEARISH,
                               entry_price=entry, stop_loss=sl, take_profit=tp,
                               signal_type=SignalType.CHOCH, timeframe=Timeframe.M5, zone=zone)
    return None


def find_m1_predictive_signal(
    m1_candles: List[Candle], zone: Zone, pip_calc: PipCalculator,
    spread_pips: float, liquidity_pool: Optional[LiquidityPool],
    atr_price: float, diag: Optional[DiagnosticsCollector] = None,
) -> Optional[TradeSignal]:
    """M1 predictive signal — faster entry within an active zone using M1 ChoCh."""
    if len(m1_candles) < getattr(config, 'M1_MIN_WAVE_CANDLES', 5):
        return None
    symbol     = zone.symbol
    sl_buffer  = _compute_sl_buffer(symbol, atr_price, pip_calc, spread_pips)
    avg_volume = sum(c.volume for c in m1_candles[-10:]) / 10 if len(m1_candles) >= 10 else 1.0
    min_rr     = config.MIN_RR
    zone_tapped = False
    tap_idx = None
    for i, c in enumerate(m1_candles):
        if zone.direction == Direction.BULLISH:
            if c.low <= zone.high:
                zone_tapped = True
                tap_idx = i
        else:
            if c.high >= zone.low:
                zone_tapped = True
                tap_idx = i
    if not zone_tapped or tap_idx is None:
        return None
    wave = m1_candles[tap_idx:]
    for i in range(getattr(config, 'M1_MIN_WAVE_CANDLES', 5), len(wave)):
        c = wave[i]
        prior_avg_body = sum(x.body_size for x in wave[max(0,i-10):i]) / max(1, min(10, i))
        if prior_avg_body < getattr(config, 'M1_BODY_MIN_PRICE', 0.00015):
            continue
        if zone.direction == Direction.BULLISH:
            wave_high = max(x.high for x in wave[:i])
            disp = c.close > wave_high
            volume_ok = c.volume >= avg_volume * getattr(config, 'M1_DISPLACEMENT_VOLUME_RATIO', 1.8)
            body_ok   = c.body_size >= prior_avg_body * getattr(config, 'M1_CANDLE_SIZE_RATIO', 3.0)
            if not (disp and volume_ok and body_ok):
                continue
            entry = c.close
            sl    = (zone.high + zone.low) / 2 - sl_buffer if getattr(config, 'M1_SL_USE_ZONE_MID', True) else zone.low - sl_buffer
            tp    = entry + (entry - sl) * min_rr
            sl_dist = abs(entry - sl)
            if sl_dist <= 0 or (tp - entry) / sl_dist < min_rr:
                continue
            return TradeSignal(symbol=symbol, direction=Direction.BULLISH,
                               entry_price=entry, stop_loss=sl, take_profit=tp,
                               signal_type=SignalType.PREDICTIVE, timeframe=Timeframe.M1, zone=zone)
        else:
            wave_low = min(x.low for x in wave[:i])
            disp = c.close < wave_low
            volume_ok = c.volume >= avg_volume * getattr(config, 'M1_DISPLACEMENT_VOLUME_RATIO', 1.8)
            body_ok   = c.body_size >= prior_avg_body * getattr(config, 'M1_CANDLE_SIZE_RATIO', 3.0)
            if not (disp and volume_ok and body_ok):
                continue
            entry = c.close
            sl    = (zone.high + zone.low) / 2 + sl_buffer if getattr(config, 'M1_SL_USE_ZONE_MID', True) else zone.high + sl_buffer
            tp    = entry - (sl - entry) * min_rr
            sl_dist = abs(sl - entry)
            if sl_dist <= 0 or (entry - tp) / sl_dist < min_rr:
                continue
            return TradeSignal(symbol=symbol, direction=Direction.BEARISH,
                               entry_price=entry, stop_loss=sl, take_profit=tp,
                               signal_type=SignalType.PREDICTIVE, timeframe=Timeframe.M1, zone=zone)
    return None


def read_m5_zone_story(m5_candles: List[Candle], zone: Zone) -> str:
    """Read the price action narrative inside the zone from M5 bars. Returns 'absorbing'/'distributing'/'neutral'/'against'."""
    window = getattr(config, 'ZONE_STORY_WINDOW', 6)
    min_bars = getattr(config, 'MIN_ZONE_STORY_BARS', 3)
    dist_majority = getattr(config, 'ZONE_STORY_DIST_MAJORITY', 0.6)
    abs_majority  = getattr(config, 'ZONE_STORY_ABS_MAJORITY', 0.5)
    vol_thresh    = getattr(config, 'ZONE_STORY_VOL_THRESH', 1.2)
    inside = [c for c in m5_candles if c.low <= zone.high and c.high >= zone.low]
    if len(inside) < min_bars:
        return 'neutral'
    recent = inside[-window:]
    avg_vol = sum(c.volume for c in recent) / len(recent) if recent else 1.0
    if zone.direction == Direction.BULLISH:
        against = sum(1 for c in recent if c.is_bearish)
        supporting = sum(1 for c in recent if c.is_bullish)
    else:
        against = sum(1 for c in recent if c.is_bullish)
        supporting = sum(1 for c in recent if c.is_bearish)
    total = len(recent)
    if total == 0:
        return 'neutral'
    if against / total >= dist_majority:
        return 'distributing'
    if supporting / total >= abs_majority:
        return 'absorbing'
    return 'neutral'


def read_m30_zone_story(m30_candles: List[Candle], zone: Zone) -> str:
    """Read M30 narrative for the zone. Returns 'against'/'neutral'/'supporting'."""
    lookback = getattr(config, 'M30_STORY_LOOKBACK', 8)
    recent = m30_candles[-lookback:] if len(m30_candles) >= lookback else m30_candles
    if not recent:
        return 'neutral'
    if zone.direction == Direction.BULLISH:
        against_bars = sum(1 for c in recent if c.is_bearish)
    else:
        against_bars = sum(1 for c in recent if c.is_bullish)
    if against_bars >= len(recent) * 0.6:
        return 'against'
    return 'neutral'


def find_m5_precision_entry(m5_candles: List[Candle], h1_zone: Zone,
                             pip_calc: PipCalculator) -> Optional[Zone]:
    """Find a tight M5 OB inside the H1 zone for precision entry."""
    min_pips = getattr(config, 'M5_PRECISION_MIN_PIPS', 2.0)
    min_size = pip_calc.pips_to_price(min_pips)
    m5_obs = find_order_blocks(m5_candles, h1_zone.symbol, Timeframe.M5)
    for z in reversed(m5_obs):
        if z.direction != h1_zone.direction:
            continue
        if z.low >= h1_zone.low and z.high <= h1_zone.high:
            if (z.high - z.low) >= min_size:
                logger.debug("M5 PRECISION ZONE | %s | %.5f-%.5f inside H1 zone %.5f-%.5f",
                             h1_zone.symbol, z.low, z.high, h1_zone.low, h1_zone.high)
                return z
    return None


def get_current_m5_bar_open(m5_candles: List[Candle]) -> Optional[float]:
    """Return the open price of the most recent M5 candle."""
    if not m5_candles:
        return None
    return m5_candles[-1].open


def compute_dynamic_tp(direction: Direction, entry: float, stop_loss: float,
                        atr: float, baseline_atr: float) -> Tuple[float, float]:
    ratio = atr / baseline_atr if baseline_atr > 0 else 1.0
    ratio = max(0.8, min(1.4, ratio))
    tp1 = compute_tp1_price(direction, entry, stop_loss)
    tp2_ratio = getattr(config, 'TIERED_TP2_RATIO', 2.0) * ratio
    sl_dist = abs(entry - stop_loss)
    if direction == Direction.BULLISH:
        tp2 = round(entry + sl_dist * tp2_ratio, 5)
    else:
        tp2 = round(entry - sl_dist * tp2_ratio, 5)
    return (tp1, tp2)


def score_trade(htf_aligned: bool, liquidity_sweep: bool, fvg_clean: bool,
                strong_displacement: bool, in_kill_zone: bool) -> int:
    score = 0
    if htf_aligned:        score += config.SCORE_HTF_ALIGNMENT
    if liquidity_sweep:    score += config.SCORE_LIQUIDITY_SWEEP
    if fvg_clean:          score += config.SCORE_CLEAN_FVG
    if strong_displacement: score += config.SCORE_STRONG_DISPLACEMENT
    if in_kill_zone:       score += config.SCORE_KILL_ZONE
    return score


def dynamic_risk(score: int) -> float:
    if score >= 5:
        return config.RISK_HIGH_CONVICTION
    if score >= 3:
        return config.RISK_MEDIUM_CONVICTION
    return config.RISK_LOW_CONVICTION


def is_strong_trend(candles: List[Candle], direction: Direction) -> bool:
    if len(candles) < 4:
        return False
    last_three = candles[-3:]
    if direction == Direction.BULLISH:
        return all(c.close > candles[i-4].close for i, c in enumerate(last_three, start=4))
    return all(c.close < candles[i-4].close for i, c in enumerate(last_three, start=4))


def is_kill_zone(dt: datetime) -> bool:
    hour = dt.hour
    for start, end in config.KILL_ZONES.values():
        if start <= hour < end:
            return True
    return False


def evaluate_zone_health(zone: Zone, h1_candles: List[Candle],
                          m5_candles: List[Candle], h1_atr: float) -> bool:
    """Return True if the zone is still healthy (price not distributing against it)."""
    post_zone_h1 = [c for c in h1_candles if c.time > zone.created_at]
    if not post_zone_h1:
        return True
    # Check if zone has been deeply mitigated on H1
    for candle in post_zone_h1:
        if zone.direction == Direction.BULLISH and candle.low < zone.low - h1_atr * 0.3:
            return False
        if zone.direction == Direction.BEARISH and candle.high > zone.high + h1_atr * 0.3:
            return False
    # Check M5 momentum against zone
    last3 = m5_candles[-3:] if len(m5_candles) >= 3 else m5_candles
    last4 = m5_candles[-4:] if len(m5_candles) >= 4 else m5_candles
    if not last4:
        return True
    vol_sample = sum(c.volume for c in m5_candles[-5:]) / max(1, min(5, len(m5_candles)))
    against = 0
    strong  = 0
    for c in last4:
        if zone.direction == Direction.BULLISH and c.is_bearish:
            against += 1
            if c.volume >= vol_sample * getattr(config, 'ZONE_HEALTH_VOL_SPIKE', 1.5):
                strong += 1
        elif zone.direction == Direction.BEARISH and c.is_bullish:
            against += 1
            if c.volume >= vol_sample * getattr(config, 'ZONE_HEALTH_VOL_SPIKE', 1.5):
                strong += 1
    return not (against >= getattr(config, 'ZONE_HEALTH_AGAINST_MIN', 3) and
                strong  >= getattr(config, 'ZONE_HEALTH_STRONG_MIN', 1))


def find_opposing_opportunity(
    rejected_zone: Zone, m5_candles: List[Candle], pip_calc: PipCalculator,
    spread_pips: float, liquidity_pool: Optional[LiquidityPool],
    h1_atr: float, zone_buffer: float, min_rr: float,
    diag: Optional[DiagnosticsCollector] = None,
) -> Optional[TradeSignal]:
    opp_direction = Direction.BEARISH if rejected_zone.direction == Direction.BULLISH else Direction.BULLISH
    syn_high = round(max(c.high for c in m5_candles[-5:]) if m5_candles else rejected_zone.high, 5)
    syn_low  = round(min(c.low  for c in m5_candles[-5:]) if m5_candles else rejected_zone.low,  5)
    synthetic_zone = Zone(
        id=str(uuid.uuid4())[:8], symbol=rejected_zone.symbol,
        zone_type=ZoneType.ORDER_BLOCK, direction=opp_direction,
        high=syn_high, low=syn_low, created_at=m5_candles[-1].time if m5_candles else rejected_zone.created_at,
    )
    signal = find_m5_choch_signal(m5_candles, synthetic_zone, pip_calc, spread_pips,
                                   liquidity_pool, h1_atr, diag, zone_buffer, min_rr)
    if signal:
        logger.info("OPPOSING OPPORTUNITY | %s %s | entry=%.5f sl=%.5f tp=%.5f",
                    rejected_zone.symbol, opp_direction.value, signal.entry_price, signal.stop_loss, signal.take_profit)
    return signal


class ZoneMapper:
    """Maps H1/M30/M5 candles to Order Block and FVG zones for one symbol."""

    DEFAULT_TF = Timeframe.H1

    def map_zones(self, candles: List[Candle], symbol: str,
                  timeframe: Timeframe) -> Tuple[List[Zone], Optional[LiquidityPool]]:
        atr_val  = atr(candles, 14)
        obs      = find_order_blocks(candles, symbol, timeframe)
        fvgs     = find_fair_value_gaps(candles, symbol, timeframe, atr_val)
        pool     = find_liquidity_pool(candles, symbol, timeframe)
        newest_ts = candles[-1].time if candles else None

        def _score(z: Zone) -> float:
            age_norm  = 1.0 - min(1.0, (newest_ts - z.created_at).total_seconds() / 86400) if newest_ts else 0.5
            size_norm = min(1.0, (z.high - z.low) / atr_val) if atr_val > 0 else 0.5
            return age_norm * 0.6 + size_norm * 0.4

        if timeframe == Timeframe.H4:
            max_obs  = getattr(config, 'H4_ZONE_MAX_OBS',  5)
            max_fvgs = getattr(config, 'H4_ZONE_MAX_FVGS', 4)
        elif timeframe == Timeframe.M30:
            max_obs  = getattr(config, 'M30_ZONE_MAX_OBS',  4)
            max_fvgs = getattr(config, 'M30_ZONE_MAX_FVGS', 3)
        elif timeframe == Timeframe.M5:
            max_obs  = getattr(config, 'M5_ZONE_MAX_OBS',  3)
            max_fvgs = getattr(config, 'M5_ZONE_MAX_FVGS', 3)
        else:
            max_obs  = getattr(config, 'MAX_OBS_PER_BAR',  10)
            max_fvgs = getattr(config, 'MAX_FVGS_PER_BAR', 8)

        obs.sort(key=_score, reverse=True)
        fvgs.sort(key=_score, reverse=True)
        all_zones = obs[:max_obs] + fvgs[:max_fvgs]

        logger.info("M30 zones remapped | %s | %d zones" if timeframe == Timeframe.M30
                    else "M5 zones remapped | %s | %d zones" if timeframe == Timeframe.M5
                    else "%s zones: %d",
                    symbol, len(all_zones))

        return all_zones, pool
