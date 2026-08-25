//+------------------------------------------------------------------+
//| GoldTrendRider_Oil.mq5 - CRUDE OIL LEG (attach to an XTIUSD chart) |
//| Standalone, deployable version of the one finding this session    |
//| that survived scrutiny: buy only on the day the H1 trend bias      |
//| flips up, hold the position for as long as H1 keeps supporting it |
//| (exit the instant that support breaks - not a fixed R multiple),  |
//| protected by a structural stop for gap/crash risk.                |
//| No Asian box, no breakout/sweep/fade legacy - just this mechanism. |
//|                                                                    |
//| Symbol-agnostic: validated on XAUUSD, XAGUSD, and XTIUSD as three  |
//| independent legs. Deploy as three chart attachments (one per      |
//| symbol) with distinct magic numbers - see GoldTrendRider_Gold.mq5, |
//| GoldTrendRider_Silver.mq5, GoldTrendRider_Oil.mq5.                 |
//+------------------------------------------------------------------+
#property copyright "Jose Fx"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

#define EA_TAG "[GoldTrendRider_Oil]"

input group "=== Trend filter (the validated edge) ==="
input ENUM_TIMEFRAMES  InpTrendTF              = PERIOD_H1;
input int              InpTrendMAPeriod        = 50;
input int              InpDailyCheckHourGMT    = 8;      // MA-flip mode only: once-per-day sampling acts as a noise filter - checking every bar caught whipsaws and wrecked quality (tested)
input int              InpBrokerGMTOffsetHours = 2;      // broker server time minus GMT (adjust for DST)

input group "=== Direction / entry timing ==="
input bool             InpLongOnly             = true;   // shorts showed no validated edge this session - off by default
input bool             InpFlipOnly             = true;   // MA-flip mode only: enter the day the bias flips, not every day it already holds
input bool             InpRideTrend            = true;   // hold until H1 support breaks instead of a fixed TP
input double           InpRewardRiskRatio      = 2.0;    // only used if InpRideTrend = false

input group "=== Entry style ==="
input bool             InpUseBreakoutEntry     = false;  // trade a genuine break of a recent swing high/low instead of an MA cross - symmetric test for both directions
input int              InpBreakoutLookback     = 20;     // H1 bars used to define the swing high/low that must be broken
input bool             InpBreakoutTouchMode    = false;  // true = react the instant price touches the level intrabar; false = wait for the H1 candle to close beyond it

input group "=== Protective stop (a backstop - RideTrend exits most trades before this is ever hit) ==="
input int              InpSwingLookback        = 20;     // H1 bars used to find the recent swing low/high for the stop
input int              InpSLBufferPoints       = 50;

input group "=== Filters ==="
input int              InpMaxSpreadPoints      = 50;
input bool             InpDisableSunday        = true;
input bool             InpUseADXFilter         = true;   // skip fresh entries when H1 isn't actually trending (chop protection)
input int              InpADXPeriod            = 14;
input double           InpMinADX               = 25.0;   // below this, treat the flip as chop/noise, not a real trend

input group "=== Risk ==="
input double           InpRiskPercent          = 1.0;
input int              InpMagicNumber          = 20260824;

input group "=== Dual-ticket partial lock (RideTrend mode only) ==="
input bool             InpUseDualTicketPartialLock = true;   // open 2 full-risk tickets per signal instead of 1 (2x total risk)
input double           InpPartialLockBonusPercent  = 0.5;    // ticket #1's lock-profit target = (2 x InpRiskPercent + this) % of account balance

input group "=== Safety ==="
input string           InpExpectedSymbol       = "XTIUSD";  // warns (does not block) if attached to the wrong chart by mistake

//--- globals
CTrade         trade;
CPositionInfo  posInfo;
int            g_trendMAHandle = INVALID_HANDLE;
int            g_adxHandle     = INVALID_HANDLE;
int            g_prevDayBias   = 0;
datetime       g_lastH1BarTime = 0;
datetime       g_currentDay    = 0;
bool           g_checkedToday  = false;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetTypeFillingBySymbol(_Symbol);

   if(InpExpectedSymbol != "" && _Symbol != InpExpectedSymbol)
   {
      PrintFormat("%s WARNING: this EA expects %s but is attached to %s. " +
                  "Check you haven't mixed up which leg is on which chart (wrong magic number risk).",
                  EA_TAG, InpExpectedSymbol, _Symbol);
      Comment(StringFormat("WARNING: expected %s, running on %s", InpExpectedSymbol, _Symbol));
   }

   g_trendMAHandle = iMA(_Symbol, InpTrendTF, InpTrendMAPeriod, 0, MODE_SMA, PRICE_CLOSE);
   if(g_trendMAHandle == INVALID_HANDLE)
   {
      Print(EA_TAG, " failed to create trend MA handle");
      return(INIT_FAILED);
   }

   if(InpUseADXFilter)
   {
      g_adxHandle = iADX(_Symbol, InpTrendTF, InpADXPeriod);
      if(g_adxHandle == INVALID_HANDLE)
      {
         Print(EA_TAG, " failed to create ADX handle");
         return(INIT_FAILED);
      }
   }
   PrintFormat("%s initialized on %s %s | magic=%d | mode=%s | ride=%s | risk%%=%.2f | dualTicketLock=%s (bonus%%=%.2f)",
               EA_TAG, _Symbol, EnumToString(InpTrendTF), InpMagicNumber,
               (InpUseBreakoutEntry ? "breakout" : "MA-flip"),
               (InpRideTrend ? "true" : "false"), InpRiskPercent,
               ((InpRideTrend && InpUseDualTicketPartialLock) ? "true" : "false"), InpPartialLockBonusPercent);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print(EA_TAG, " deinit, reason=", reason);
   if(g_trendMAHandle != INVALID_HANDLE) IndicatorRelease(g_trendMAHandle);
   if(g_adxHandle     != INVALID_HANDLE) IndicatorRelease(g_adxHandle);
}

//+------------------------------------------------------------------+
bool IsTrendingEnough()
{
   if(!InpUseADXFilter) return true;
   double adxBuf[];
   ArraySetAsSeries(adxBuf, true);
   if(CopyBuffer(g_adxHandle, 0, 0, 1, adxBuf) <= 0) return true;
   return adxBuf[0] >= InpMinADX;
}

//+------------------------------------------------------------------+
// Live/demo: TimeGMT() reflects the terminal's own live server-to-GMT offset,
// so it self-adjusts across broker DST shifts automatically. Inside the
// Strategy Tester TimeGMT() is pinned to TimeCurrent() (a documented MQL5
// limitation), so that path falls back to the manual InpBrokerGMTOffsetHours.
datetime GMTTime()
{
   if(MQLInfoInteger(MQL_TESTER))
      return TimeCurrent() - InpBrokerGMTOffsetHours * 3600;
   return TimeGMT();
}

//+------------------------------------------------------------------+
datetime DayFloor(datetime t)
{
   MqlDateTime s;
   TimeToStruct(t, s);
   s.hour = 0; s.min = 0; s.sec = 0;
   return StructToTime(s);
}

//+------------------------------------------------------------------+
double NormalizePrice(double price)
{
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) tickSize = _Point;
   return MathRound(price / tickSize) * tickSize;
}

//+------------------------------------------------------------------+
//| 1 = price above the H1 trend MA, -1 = below, 0 = exactly on it    |
//+------------------------------------------------------------------+
int GetTrendBias()
{
   double maBuf[];
   ArraySetAsSeries(maBuf, true);
   if(CopyBuffer(g_trendMAHandle, 0, 0, 1, maBuf) <= 0) return 0;
   double price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(price > maBuf[0]) return 1;
   if(price < maBuf[0]) return -1;
   return 0;
}

//+------------------------------------------------------------------+
double CalcLotSize(double slDistancePoints)
{
   if(slDistancePoints <= 0) return 0.0;
   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
   double tickValue  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double point      = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(tickSize <= 0 || tickValue <= 0) return 0.0;

   double lossPerLot = (slDistancePoints * point / tickSize) * tickValue;
   if(lossPerLot <= 0) return 0.0;

   double lots = riskMoney / lossPerLot;
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(lotStep <= 0) lotStep = minLot;

   lots = MathFloor(lots / lotStep) * lotStep;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return lots;
}

//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Symbol() == _Symbol && posInfo.Magic() == InpMagicNumber)
            return true;
   return false;
}

//+------------------------------------------------------------------+
void CheckTrendSupportExit()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol || posInfo.Magic() != InpMagicNumber) continue;

      int bias = GetTrendBias();
      bool isLong = (posInfo.PositionType() == POSITION_TYPE_BUY);
      if((isLong && bias <= 0) || (!isLong && bias >= 0))
      {
         ulong ticket = posInfo.Ticket();
         double profit = posInfo.Profit();
         if(trade.PositionClose(ticket))
            Print(EA_TAG, " trend support broke -- closed ticket=", ticket, " profit=", profit);
         else
            Print(EA_TAG, " trend-exit close FAILED ticket=", ticket, ": ", trade.ResultRetcodeDescription());
      }
   }
}

//+------------------------------------------------------------------+
//| A genuine structural event: the last CLOSED H1 bar closed beyond  |
//| the high/low of the N bars before it. Symmetric by construction - |
//| no long/short asymmetry baked into the trigger itself.            |
//+------------------------------------------------------------------+
int CheckBreakoutDirection()
{
   double close1 = iClose(_Symbol, InpTrendTF, 1);
   int hiShift = iHighest(_Symbol, InpTrendTF, MODE_HIGH, InpBreakoutLookback, 2);
   int loShift = iLowest(_Symbol, InpTrendTF, MODE_LOW, InpBreakoutLookback, 2);
   double recentHigh = iHigh(_Symbol, InpTrendTF, hiShift);
   double recentLow  = iLow(_Symbol, InpTrendTF, loShift);

   if(close1 > recentHigh) return 1;
   if(close1 < recentLow) return -1;
   return 0;
}

//+------------------------------------------------------------------+
//| Same idea, but reacts the instant price touches the level         |
//| intrabar - doesn't wait for the H1 candle to actually close       |
//| beyond it. Faster, but catches wicks that don't hold too.         |
//+------------------------------------------------------------------+
int CheckBreakoutDirectionTouch()
{
   int hiShift = iHighest(_Symbol, InpTrendTF, MODE_HIGH, InpBreakoutLookback, 1);
   int loShift = iLowest(_Symbol, InpTrendTF, MODE_LOW, InpBreakoutLookback, 1);
   double recentHigh = iHigh(_Symbol, InpTrendTF, hiShift);
   double recentLow  = iLow(_Symbol, InpTrendTF, loShift);

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(ask > recentHigh) return 1;
   if(bid < recentLow) return -1;
   return 0;
}

//+------------------------------------------------------------------+
int GetEntryDirection()
{
   if(!InpUseBreakoutEntry) return GetTrendBias();
   return InpBreakoutTouchMode ? CheckBreakoutDirectionTouch() : CheckBreakoutDirection();
}

//+------------------------------------------------------------------+
double GetSwingStop(bool isBuy)
{
   double point = _Point;
   if(isBuy)
   {
      int shift = iLowest(_Symbol, InpTrendTF, MODE_LOW, InpSwingLookback, 1);
      return iLow(_Symbol, InpTrendTF, shift) - InpSLBufferPoints * point;
   }
   int shift = iHighest(_Symbol, InpTrendTF, MODE_HIGH, InpSwingLookback, 1);
   return iHigh(_Symbol, InpTrendTF, shift) + InpSLBufferPoints * point;
}

//+------------------------------------------------------------------+
void TryEnter()
{
   int bias = GetEntryDirection();
   if(bias == 0) return;

   if(!InpUseBreakoutEntry)
   {
      bool isFlip = (bias != g_prevDayBias);
      g_prevDayBias = bias;   // update every bar regardless of filters below, or flip memory goes stale
      if(InpFlipOnly && !isFlip) return;
   }

   if(InpLongOnly && bias < 0) return;
   if(!IsTrendingEnough()) return;
   if(HasOpenPosition()) return;

   bool isBuy = (bias > 0);
   double point = _Point;
   double refPrice = isBuy ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double sl = GetSwingStop(isBuy);
   double riskDist = MathAbs(refPrice - sl);
   if(riskDist <= 0) return;

   long stopsLevelPoints = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevelPoints > 0 && riskDist <= stopsLevelPoints * point)
   {
      Print(EA_TAG, " entry skipped: SL distance <= broker min stop level");
      return;
   }

   double tp = InpRideTrend ? 0.0
             : (isBuy ? refPrice + riskDist * InpRewardRiskRatio
                       : refPrice - riskDist * InpRewardRiskRatio);

   double lots = CalcLotSize(riskDist / point);
   if(lots <= 0) { Print(EA_TAG, " entry skipped: lot size"); return; }

   double slN = NormalizePrice(sl);

   if(InpRideTrend && InpUseDualTicketPartialLock)
   {
      // Ticket #1's lock-profit target, in dollars: (2 x InpRiskPercent + bonus%)
      // of account balance -- a plain percentage of balance, NOT derived from the
      // lot size or its rounding. The lot size only enters afterward, purely to
      // convert that dollar target into a price distance for ticket #1's TP.
      double targetDollars = AccountInfoDouble(ACCOUNT_BALANCE) *
                              (2.0 * InpRiskPercent + InpPartialLockBonusPercent) / 100.0;

      double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      double moneyPerPriceUnitPerLot = (tickSize > 0) ? tickValue / tickSize : 0.0;

      double tp1N = slN; // safe fallback if the conversion below can't be trusted
      if(moneyPerPriceUnitPerLot > 0 && lots > 0)
      {
         double priceDistance = targetDollars / (lots * moneyPerPriceUnitPerLot);
         double tp1 = isBuy ? refPrice + priceDistance : refPrice - priceDistance;
         tp1N = NormalizePrice(tp1);
      }

      string tag1 = isBuy ? "GoldTrendRider_Oil-Buy-1" : "GoldTrendRider_Oil-Sell-1";
      string tag2 = isBuy ? "GoldTrendRider_Oil-Buy-2" : "GoldTrendRider_Oil-Sell-2";

      bool ok1 = isBuy ? trade.Buy(lots, _Symbol, 0.0, slN, tp1N, tag1)
                        : trade.Sell(lots, _Symbol, 0.0, slN, tp1N, tag1);
      if(ok1)
         Print(EA_TAG, " ticket#1 opened (partial-lock): ", (isBuy ? "LONG" : "SHORT"),
               " lots=", lots, " entry=", refPrice, " SL=", slN, " TP=", tp1N, " target$=", targetDollars);
      else
         Print(EA_TAG, " ticket#1 order FAILED: ", trade.ResultRetcodeDescription());

      bool ok2 = isBuy ? trade.Buy(lots, _Symbol, 0.0, slN, 0.0, tag2)
                        : trade.Sell(lots, _Symbol, 0.0, slN, 0.0, tag2);
      if(ok2)
         Print(EA_TAG, " ticket#2 opened (ride-trend, no TP): ", (isBuy ? "LONG" : "SHORT"),
               " lots=", lots, " entry=", refPrice, " SL=", slN);
      else
         Print(EA_TAG, " ticket#2 order FAILED: ", trade.ResultRetcodeDescription());

      return;
   }

   double tpN = NormalizePrice(tp);
   if(isBuy)
   {
      if(trade.Buy(lots, _Symbol, 0.0, slN, tpN, "GoldTrendRider_Oil-Buy"))
         Print(EA_TAG, " LONG opened: lots=", lots, " entry=", refPrice, " SL=", slN, " TP=", (InpRideTrend ? 0.0 : tpN));
      else
         Print(EA_TAG, " LONG order failed: ", trade.ResultRetcodeDescription());
   }
   else
   {
      if(trade.Sell(lots, _Symbol, 0.0, slN, tpN, "GoldTrendRider_Oil-Sell"))
         Print(EA_TAG, " SHORT opened: lots=", lots, " entry=", refPrice, " SL=", slN, " TP=", (InpRideTrend ? 0.0 : tpN));
      else
         Print(EA_TAG, " SHORT order failed: ", trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(InpRideTrend) CheckTrendSupportExit();

   datetime gmtNow = GMTTime();
   MqlDateTime dt;
   TimeToStruct(gmtNow, dt);

   if(InpDisableSunday && dt.day_of_week == 0) return;

   if(InpUseBreakoutEntry && InpBreakoutTouchMode)
   {
      // intrabar touch - no bar-close gate, evaluate every tick
   }
   else if(InpUseBreakoutEntry)
   {
      // confirmed-close breakout - a genuine structural event, check on every new H1 bar close
      datetime t0 = iTime(_Symbol, InpTrendTF, 0);
      if(t0 == g_lastH1BarTime) return;
      g_lastH1BarTime = t0;
   }
   else
   {
      // MA-flip mode - tested checking every H1 bar close and it was a regression, not a
      // fix: catches every intrabar MA whipsaw as a "new flip" and collapsed win rate on
      // every instrument, including gold's own previously-good result. Once-daily sampling
      // acts as an implicit noise filter (a flip must survive most of a day to register),
      // so it's kept even though it also means this mode reacts more slowly.
      datetime today = DayFloor(gmtNow);
      if(today != g_currentDay)
      {
         g_currentDay   = today;
         g_checkedToday = false;
      }
      if(g_checkedToday || dt.hour < InpDailyCheckHourGMT) return;
      g_checkedToday = true;
   }

   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpreadPoints) return;

   TryEnter();
}
//+------------------------------------------------------------------+
