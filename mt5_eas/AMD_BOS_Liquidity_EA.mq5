//+------------------------------------------------------------------+
//|                                    AMD_BOS_Liquidity_EA.mq5       |
//|                                                                    |
//| Strategy (ICT/SMC style, symmetric long & short):                  |
//|  1) Track swing highs/lows (fractals) -> these are liquidity pools |
//|  2) MANIPULATION: a bar wicks through a swing point then closes    |
//|     back on the other side of it (a liquidity sweep).              |
//|  3) DISTRIBUTION / BOS: a later close breaks the opposing swing    |
//|     point, confirming a Break Of Structure in the new direction.   |
//|  4) EFFICIENCY: after BOS, wait for a Fair Value Gap (3-bar        |
//|     imbalance) in the breakout direction and enter there.          |
//|  5) ACCUMULATION is used only to tag whether the sweep happened    |
//|     out of a contracted range (tighter ATR-relative range) - this  |
//|     is the classic "AMD" case - vs a plain BOS/Liquidity/Efficiency|
//|     continuation setup elsewhere. Both use the same entry engine,  |
//|     which is what makes the logic symmetric in both directions.    |
//+------------------------------------------------------------------+
#property copyright "Backtest EA"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

#define EA_TAG "[AMD_BOS_Liquidity]"

//--- Inputs
input int    InpSwingWing        = 6;       // Fractal wing bars (left/right) for swing detection
input int    InpRangeLookback     = 20;      // Bars evaluated for the accumulation-range tag
input double InpRangeMaxATRMult   = 1.5;     // Max range width (x ATR) to tag a sweep as "AMD"
input int    InpATRPeriod         = 14;
input double InpSweepMinPoints    = 30;      // Min wick-through beyond swing point (points, used if InpUseATRThresholds=false)
input int    InpBosTimeoutBars    = 12;      // Bars allowed for BOS to arrive after a sweep
input int    InpFVGTimeoutBars    = 8;       // Bars allowed for an FVG to arrive after BOS
input int    InpFVGOrderTimeout   = 6;       // Bars a pending FVG-fill limit order stays live
input double InpFVGMinSpreadMult  = 1.5;     // Min FVG size as a multiple of current spread
input double InpRiskPercent       = 1.0;     // Risk % of balance per trade
input double InpRR                = 2.0;     // Reward:Risk multiple for TP (fallback when no structure target)
input double InpSLBufferPoints    = 20;      // Extra buffer beyond the sweep extreme (points, used if InpUseATRThresholds=false)
input bool   InpUseATRThresholds  = false;   // Scale sweep/SL/FVG thresholds off ATR instead of fixed points
input double InpSweepATRMult      = 0.15;    // Sweep-through distance as a fraction of ATR
input double InpSLBufferATRMult   = 0.10;    // SL buffer beyond the sweep extreme as a fraction of ATR
input double InpFVGMinATRMult     = 0.08;    // Min FVG size as a fraction of ATR (in addition to the spread check)
input int    InpStartHour         = 8;       // Session filter start (server time, inclusive) - London open
input int    InpEndHour           = 21;      // Session filter end (server time, inclusive) - NY close
input int    InpMaxSpreadPoints   = 300;     // Max allowed spread in points
input ulong  InpMagic             = 20260814;
input int    InpTradesPerSignal   = 2;       // Number of full-size tickets opened together on each confirmed signal (2x normal per-signal risk)
input bool   InpOnePositionOnly   = true;
input bool   InpUseHTFFilter      = true;    // Only trade with the higher-timeframe trend
input ENUM_TIMEFRAMES InpHTFPeriod = PERIOD_H1; // Higher timeframe used for the bias filter
input int    InpHTFMAPeriod       = 50;      // EMA period on the higher timeframe
input bool   InpUseStructureTP    = true;    // Target the next opposing swing instead of a fixed R multiple
input double InpMinStructureRR    = 1.0;     // Minimum R multiple a structure target must offer to be used
input int    InpSwingBufferSize   = 20;      // How many recent swing highs/lows to remember for TP targeting

//--- swing point
struct SwingPoint
  {
   datetime time;
   double   price;
   bool     valid;
  };

SwingPoint g_swingHigh, g_swingLow;   // most recent confirmed swing high/low

//--- pending sweep (manipulation) state, one slot per direction
struct SweepState
  {
   bool     pending;      // sweep detected, waiting for BOS
   double   extreme;      // the wick extreme of the sweep (for SL placement)
   datetime time;
   int      barsSinceSweep;
   bool     isAMD;        // true if it happened out of a contracted (accumulation) range
  };

SweepState g_sweepBull; // sell-side liquidity swept -> expecting bullish BOS
SweepState g_sweepBear; // buy-side liquidity swept  -> expecting bearish BOS

//--- pending BOS (waiting for FVG) state
struct BosState
  {
   bool     pending;
   int      dir;          // +1 bull, -1 bear
   double   slLevel;       // sweep extreme to base SL on
   bool     isAMD;
   int      barsSinceBos;
  };

BosState g_bos;

//--- pending FVG entry order tracking
ulong    g_pendingOrderTickets[]; // one signal can place InpTradesPerSignal tickets at once

datetime g_lastBarTime = 0;
int      g_atrHandle   = INVALID_HANDLE;
int      g_htfMAHandle = INVALID_HANDLE;

//--- rolling history of recent swing points, most recent at index 0, used to
//--- find the next untouched opposing liquidity level for structure-based TPs
double   g_recentHighPrice[];
datetime g_recentHighTime[];
double   g_recentLowPrice[];
datetime g_recentLowTime[];

//+------------------------------------------------------------------+
int OnInit()
  {
   g_atrHandle = iATR(_Symbol, PERIOD_CURRENT, InpATRPeriod);
   if(g_atrHandle == INVALID_HANDLE)
      return INIT_FAILED;

   if(InpUseHTFFilter)
     {
      g_htfMAHandle = iMA(_Symbol, InpHTFPeriod, InpHTFMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
      if(g_htfMAHandle == INVALID_HANDLE)
         return INIT_FAILED;
     }

   ArrayResize(g_recentHighPrice, InpSwingBufferSize);
   ArrayResize(g_recentHighTime, InpSwingBufferSize);
   ArrayResize(g_recentLowPrice, InpSwingBufferSize);
   ArrayResize(g_recentLowTime, InpSwingBufferSize);
   ArrayInitialize(g_recentHighPrice, 0);
   ArrayInitialize(g_recentLowPrice, 0);

   g_swingHigh.valid = false;
   g_swingLow.valid  = false;
   g_sweepBull.pending = false;
   g_sweepBear.pending = false;
   g_bos.pending = false;

   trade.SetExpertMagicNumber(InpMagic);
   Print(EA_TAG, " initialized on ", _Symbol, " ", EnumToString(_Period),
         " | HTF filter=", EnumToString(InpHTFPeriod), " | magic=", InpMagic,
         " | tickets/signal=", InpTradesPerSignal, " | risk%/ticket=", InpRiskPercent);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   Print(EA_TAG, " deinit, reason=", reason);
   if(g_atrHandle != INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);
   if(g_htfMAHandle != INVALID_HANDLE)
      IndicatorRelease(g_htfMAHandle);
  }

//+------------------------------------------------------------------+
//| Only allow trades that agree with the higher-timeframe trend      |
//+------------------------------------------------------------------+
bool HTFBiasOK(int dir)
  {
   if(!InpUseHTFFilter)
      return true;

   double maBuf[], closeBuf[];
   ArraySetAsSeries(maBuf, true);
   ArraySetAsSeries(closeBuf, true);
   if(CopyBuffer(g_htfMAHandle, 0, 1, 1, maBuf) <= 0)
      return false;
   if(CopyClose(_Symbol, InpHTFPeriod, 1, 1, closeBuf) <= 0)
      return false;

   if(dir == 1)
      return closeBuf[0] > maBuf[0];
   return closeBuf[0] < maBuf[0];
  }

//+------------------------------------------------------------------+
void PushSwingHigh(double price, datetime time)
  {
   for(int i = InpSwingBufferSize - 1; i > 0; i--)
     {
      g_recentHighPrice[i] = g_recentHighPrice[i-1];
      g_recentHighTime[i]  = g_recentHighTime[i-1];
     }
   g_recentHighPrice[0] = price;
   g_recentHighTime[0]  = time;
  }

void PushSwingLow(double price, datetime time)
  {
   for(int i = InpSwingBufferSize - 1; i > 0; i--)
     {
      g_recentLowPrice[i] = g_recentLowPrice[i-1];
      g_recentLowTime[i]  = g_recentLowTime[i-1];
     }
   g_recentLowPrice[0] = price;
   g_recentLowTime[0]  = time;
  }

//+------------------------------------------------------------------+
//| Nearest untouched opposing swing beyond entry, for a structure TP.|
//| Falls back to the fixed R multiple if nothing qualifies.          |
//+------------------------------------------------------------------+
double FindStructureTP(int dir, double entry, double slDist)
  {
   double best = 0;
   if(dir == 1)
     {
      for(int i = 0; i < InpSwingBufferSize; i++)
        {
         double p = g_recentHighPrice[i];
         if(p <= 0) continue;
         if(p > entry && (best == 0 || p < best))
            best = p;
        }
      if(best > 0 && (best - entry) >= slDist * InpMinStructureRR)
         return best;
      return entry + slDist * InpRR;
     }
   else
     {
      for(int i = 0; i < InpSwingBufferSize; i++)
        {
         double p = g_recentLowPrice[i];
         if(p <= 0) continue;
         if(p < entry && (best == 0 || p > best))
            best = p;
        }
      if(best > 0 && (entry - best) >= slDist * InpMinStructureRR)
         return best;
      return entry - slDist * InpRR;
     }
  }

//+------------------------------------------------------------------+
bool IsNewBar()
  {
   datetime t = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(t != g_lastBarTime)
     {
      g_lastBarTime = t;
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
bool WithinSession()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int h = dt.hour;
   if(InpStartHour <= InpEndHour)
      return (h >= InpStartHour && h <= InpEndHour);
   return (h >= InpStartHour || h <= InpEndHour); // wraps midnight
  }

//+------------------------------------------------------------------+
bool SpreadOK()
  {
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   return spread <= InpMaxSpreadPoints;
  }

//+------------------------------------------------------------------+
//| Detect a newly confirmed fractal swing at shift = wing+1          |
//+------------------------------------------------------------------+
void UpdateSwings(const double &high[], const double &low[], const datetime &time[], int bars)
  {
   int wing = InpSwingWing;
   int shift = wing + 1;
   if(bars < 2*wing + 2)
      return;

   bool isHigh = true;
   bool isLow  = true;
   for(int i = 1; i <= wing; i++)
     {
      if(high[shift-i] > high[shift]) isHigh = false;
      if(high[shift+i] > high[shift]) isHigh = false;
      if(low[shift-i] < low[shift])   isLow  = false;
      if(low[shift+i] < low[shift])   isLow  = false;
     }

   if(isHigh)
     {
      g_swingHigh.time  = time[shift];
      g_swingHigh.price = high[shift];
      g_swingHigh.valid = true;
      PushSwingHigh(high[shift], time[shift]);
     }
   if(isLow)
     {
      g_swingLow.time  = time[shift];
      g_swingLow.price = low[shift];
      g_swingLow.valid = true;
      PushSwingLow(low[shift], time[shift]);
     }
  }

//+------------------------------------------------------------------+
//| Was the lookback range contracted enough to call it accumulation? |
//+------------------------------------------------------------------+
bool WasAccumulation(const double &high[], const double &low[], int fromShift)
  {
   double atrBuf[];
   ArraySetAsSeries(atrBuf, true);
   if(CopyBuffer(g_atrHandle, 0, fromShift, 1, atrBuf) <= 0)
      return false;
   double atr = atrBuf[0];
   if(atr <= 0)
      return false;

   double hi = -DBL_MAX, lo = DBL_MAX;
   for(int i = fromShift; i < fromShift + InpRangeLookback; i++)
     {
      if(high[i] > hi) hi = high[i];
      if(low[i]  < lo) lo = low[i];
     }
   double width = hi - lo;
   return (width <= atr * InpRangeMaxATRMult);
  }

//+------------------------------------------------------------------+
double PointsToPrice(double points) { return points * _Point; }

//+------------------------------------------------------------------+
double CurrentATR(int shift = 1)
  {
   double atrBuf[];
   ArraySetAsSeries(atrBuf, true);
   if(CopyBuffer(g_atrHandle, 0, shift, 1, atrBuf) <= 0)
      return 0;
   return atrBuf[0];
  }

//+------------------------------------------------------------------+
double SweepThreshold()
  {
   if(InpUseATRThresholds)
      return CurrentATR(1) * InpSweepATRMult;
   return PointsToPrice(InpSweepMinPoints);
  }

double SLBufferDistance()
  {
   if(InpUseATRThresholds)
      return CurrentATR(1) * InpSLBufferATRMult;
   return PointsToPrice(InpSLBufferPoints);
  }

//+------------------------------------------------------------------+
//| Detect a liquidity sweep on the just-closed bar (shift 1)         |
//+------------------------------------------------------------------+
void DetectSweep(const double &high[], const double &low[], const double &close[], const datetime &time[])
  {
   int s = 1; // last closed bar
   double thresh = SweepThreshold();
   if(thresh <= 0)
      return;

   // Sweep of a swing LOW (sell-side liquidity) -> sets up bullish BOS
   if(g_swingLow.valid && !g_sweepBull.pending)
     {
      double lvl = g_swingLow.price;
      if(low[s] < lvl - thresh && close[s] > lvl)
        {
         g_sweepBull.pending = true;
         g_sweepBull.extreme = low[s];
         g_sweepBull.time    = time[s];
         g_sweepBull.barsSinceSweep = 0;
         g_sweepBull.isAMD   = WasAccumulation(high, low, s+1);
        }
     }

   // Sweep of a swing HIGH (buy-side liquidity) -> sets up bearish BOS
   if(g_swingHigh.valid && !g_sweepBear.pending)
     {
      double lvl = g_swingHigh.price;
      if(high[s] > lvl + thresh && close[s] < lvl)
        {
         g_sweepBear.pending = true;
         g_sweepBear.extreme = high[s];
         g_sweepBear.time    = time[s];
         g_sweepBear.barsSinceSweep = 0;
         g_sweepBear.isAMD   = WasAccumulation(high, low, s+1);
        }
     }
  }

//+------------------------------------------------------------------+
//| Check for BOS confirmation given pending sweeps; expire timeouts  |
//+------------------------------------------------------------------+
void DetectBOS(const double &close[])
  {
   int s = 1;

   if(g_sweepBull.pending)
     {
      g_sweepBull.barsSinceSweep++;
      if(g_swingHigh.valid && close[s] > g_swingHigh.price)
        {
         g_bos.pending = true;
         g_bos.dir     = 1;
         g_bos.slLevel = g_sweepBull.extreme;
         g_bos.isAMD   = g_sweepBull.isAMD;
         g_bos.barsSinceBos = 0;
         g_sweepBull.pending = false;
        }
      else if(g_sweepBull.barsSinceSweep > InpBosTimeoutBars || close[s] < g_sweepBull.extreme)
        {
         g_sweepBull.pending = false; // invalidated or timed out
        }
     }

   if(g_sweepBear.pending)
     {
      g_sweepBear.barsSinceSweep++;
      if(g_swingLow.valid && close[s] < g_swingLow.price)
        {
         g_bos.pending = true;
         g_bos.dir     = -1;
         g_bos.slLevel = g_sweepBear.extreme;
         g_bos.isAMD   = g_sweepBear.isAMD;
         g_bos.barsSinceBos = 0;
         g_sweepBear.pending = false;
        }
      else if(g_sweepBear.barsSinceSweep > InpBosTimeoutBars || close[s] > g_sweepBear.extreme)
        {
         g_sweepBear.pending = false;
        }
     }
  }

//+------------------------------------------------------------------+
//| Look for a Fair Value Gap (3-bar imbalance) in the BOS direction  |
//| and place a limit order at the gap to catch the "efficiency" fill|
//+------------------------------------------------------------------+
void DetectFVGAndEnter(const double &high[], const double &low[], const double &close[])
  {
   if(!g_bos.pending)
      return;

   g_bos.barsSinceBos++;
   if(g_bos.barsSinceBos > InpFVGTimeoutBars)
     {
      g_bos.pending = false;
      return;
     }

   // 3-bar FVG using the last closed bar (s=1) as the most recent candle C,
   // s=2 as the middle impulse candle, s=3 as the older candle A.
   int a = 3, c = 1;
   double minGapSpread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) * _Point * InpFVGMinSpreadMult;
   double minGapATR    = InpUseATRThresholds ? CurrentATR(1) * InpFVGMinATRMult : 0;
   double minGap = MathMax(minGapSpread, minGapATR);

   if(g_bos.dir == 1)
     {
      // bullish FVG: A.high < C.low
      if(high[a] < low[c] && (low[c] - high[a]) >= minGap && HTFBiasOK(1))
        {
         double gapLow  = high[a];
         double gapHigh = low[c];
         double entry   = (gapLow + gapHigh) / 2.0;
         double sl      = g_bos.slLevel - SLBufferDistance();
         double risk    = entry - sl;
         if(risk > 0)
           {
            double tp = InpUseStructureTP ? FindStructureTP(1, entry, risk) : entry + risk * InpRR;
            PlaceEntry(1, entry, sl, tp, g_bos.isAMD ? "AMD-long" : "BOS-liq-eff-long");
            g_bos.pending = false;
           }
        }
     }
   else if(g_bos.dir == -1)
     {
      // bearish FVG: A.low > C.high
      if(low[a] > high[c] && (low[a] - high[c]) >= minGap && HTFBiasOK(-1))
        {
         double gapHigh = low[a];
         double gapLow  = high[c];
         double entry   = (gapLow + gapHigh) / 2.0;
         double sl      = g_bos.slLevel + SLBufferDistance();
         double risk    = sl - entry;
         if(risk > 0)
           {
            double tp = InpUseStructureTP ? FindStructureTP(-1, entry, risk) : entry - risk * InpRR;
            PlaceEntry(-1, entry, sl, tp, g_bos.isAMD ? "AMD-short" : "BOS-liq-eff-short");
            g_bos.pending = false;
           }
        }
     }
  }

//+------------------------------------------------------------------+
double CalcLots(double slDistance)
  {
   // Matches US30_ShortBBFade_EA's CalcLots exactly: folds in contractSize (matters on
   // some instruments where tickValue/tickSize alone under-states money-per-lot), and
   // SKIPS the trade (-1) rather than silently forcing minLot when the risk-based size
   // rounds below the broker minimum - the previous version's "lots = minLot" fallback
   // could silently risk far more than InpRiskPercent intends on a tight stop distance.
   if(slDistance <= 0)
      return -1;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double contractSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(tickValue <= 0 || tickSize <= 0)
      return -1;

   double moneyPerPriceUnitPerLot = tickValue / tickSize;
   if(contractSize > 0)
      moneyPerPriceUnitPerLot = MathMax(moneyPerPriceUnitPerLot, contractSize);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double moneyToRisk = balance * (InpRiskPercent / 100.0);
   double rawLots = moneyToRisk / (slDistance * moneyPerPriceUnitPerLot);

   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

   double lots = MathFloor(rawLots / step) * step;
   if(lots < minVol)
      return -1;
   if(lots > maxVol)
      lots = maxVol;
   return lots;
  }

//+------------------------------------------------------------------+
//| Reject orders whose SL/TP distance is inside the broker's own     |
//| minimum stops-level - avoids order-rejection surprises live.      |
//+------------------------------------------------------------------+
bool StopsLevelOk(double entryPrice, double slPrice, double tpPrice)
  {
   long stopsLevelPoints = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevelPoints <= 0)
      return true;
   double minDist = stopsLevelPoints * _Point;
   return MathAbs(entryPrice - slPrice) >= minDist && MathAbs(tpPrice - entryPrice) >= minDist;
  }

//+------------------------------------------------------------------+
//| Count only THIS EA's own open positions (by magic number) - the   |
//| previous PositionsTotal()>0 check counted every position on the    |
//| account, so this EA would never trade if anything else (another   |
//| EA, a leftover manual position) was already open.                  |
//+------------------------------------------------------------------+
bool HasOwnOpenPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == (long)InpMagic)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
void PlaceEntry(int dir, double entry, double sl, double tp, string tag)
  {
   if(InpOnePositionOnly && HasOwnOpenPosition())
      return;
   if(CountLivePendingOrders() > 0)
      return; // still have tickets pending from a previous signal
   if(!WithinSession() || !SpreadOK())
      return;
   if(!StopsLevelOk(entry, sl, tp))
     {
      Print(EA_TAG, " entry skipped, inside broker stops level. entry=", entry, " sl=", sl, " tp=", tp);
      return;
     }

   double slDist = MathAbs(entry - sl);
   double lots = CalcLots(slDist); // same lots for every ticket - InpTradesPerSignal full-size tickets, not one split N ways
   if(lots <= 0)
      return;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   datetime expiry = TimeCurrent() + PeriodSeconds(PERIOD_CURRENT) * InpFVGOrderTimeout;

   ArrayResize(g_pendingOrderTickets, InpTradesPerSignal);
   ArrayInitialize(g_pendingOrderTickets, 0);
   int opened = 0;

   for(int k = 0; k < InpTradesPerSignal; k++)
     {
      bool ok = false;
      if(dir == 1)
        {
         if(entry >= ask) // gap already filled/through -> enter at market instead
            ok = trade.Buy(lots, _Symbol, ask, sl, tp, tag);
         else
           {
            ok = trade.BuyLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_SPECIFIED, expiry, tag);
            if(ok) g_pendingOrderTickets[k] = trade.ResultOrder();
           }
        }
      else
        {
         if(entry <= bid)
            ok = trade.Sell(lots, _Symbol, bid, sl, tp, tag);
         else
           {
            ok = trade.SellLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_SPECIFIED, expiry, tag);
            if(ok) g_pendingOrderTickets[k] = trade.ResultOrder();
           }
        }
      if(ok)
         opened++;
      else
         Print(EA_TAG, " ticket ", k+1, "/", InpTradesPerSignal, " failed: ", trade.ResultRetcodeDescription());
     }
   Print(EA_TAG, " placed ", opened, "/", InpTradesPerSignal, " tickets (", lots, " lots each) for signal ", tag);
  }

//+------------------------------------------------------------------+
//| Counts still-live pending tickets from the current signal's batch |
//| and clears any that have since filled, cancelled, or expired.     |
//+------------------------------------------------------------------+
int CountLivePendingOrders()
  {
   int count = 0;
   for(int i = ArraySize(g_pendingOrderTickets) - 1; i >= 0; i--)
     {
      if(g_pendingOrderTickets[i] == 0)
         continue;
      if(OrderSelect(g_pendingOrderTickets[i]))
         count++;
      else
         g_pendingOrderTickets[i] = 0;
     }
   return count;
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   CountLivePendingOrders(); // clears stale tickets each tick, same as before

   if(!IsNewBar())
      return;

   int bars = Bars(_Symbol, PERIOD_CURRENT);
   int need = MathMax(2*InpSwingWing + 2, InpRangeLookback + InpSwingWing + 5) + 5;
   if(bars < need)
      return;

   double high[], low[], close[];
   datetime time[];
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);
   ArraySetAsSeries(close, true);
   ArraySetAsSeries(time, true);

   int toCopy = need;
   if(CopyHigh(_Symbol, PERIOD_CURRENT, 0, toCopy, high) <= 0) return;
   if(CopyLow(_Symbol, PERIOD_CURRENT, 0, toCopy, low) <= 0) return;
   if(CopyClose(_Symbol, PERIOD_CURRENT, 0, toCopy, close) <= 0) return;
   if(CopyTime(_Symbol, PERIOD_CURRENT, 0, toCopy, time) <= 0) return;

   UpdateSwings(high, low, time, toCopy);
   DetectSweep(high, low, close, time);
   DetectBOS(close);
   DetectFVGAndEnter(high, low, close);
  }
//+------------------------------------------------------------------+
