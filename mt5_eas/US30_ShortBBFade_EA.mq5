//+------------------------------------------------------------------+
//|                                          US30_ShortBBFade_EA.mq5    |
//|                                                                    |
//| DEPLOYED 2026-08-11 for a 2-month demo forward-monitoring period.   |
//| Mean-reversion, SHORT ONLY: fades an H1 close above the 20-period/   |
//| 2-deviation Bollinger Band (classic %B extreme). Found via a broad  |
//| multi-instrument, multi-strategy screen (this session tested 8      |
//| strategy families across 10 instruments; this was the only survivor |
//| of a real MT5 Strategy Tester chronological discovery/validation     |
//| split, with the validation half backed by 99% genuine broker tick    |
//| data, not synthetic history).                                        |
//|                                                                       |
//| Validated performance (real MT5 Strategy Tester, ICMarketsKE-Demo,    |
//| $5000 test balance, 2025.01.01-2026.08.01, short-only):               |
//|   Discovery (2025):   PF 1.70, +$371.70, max DD 3.27%, 24 trades      |
//|   Validation (2026):  PF 1.74, +$804.15, max DD 5.04%, 49 trades      |
//| Combined: +$1175.85 on $5000 (~23.5%), ~0.32R avg expectancy/trade,   |
//| ~1 trade/week combined rate. Long side was NOT included - it showed   |
//| a real but much weaker edge (27.8% win rate in validation vs 53.1%    |
//| for shorts) and was excluded per the confirmed short-only test.       |
//|                                                                        |
//| Caveats, disclosed not hidden: 73 total backtest trades is a real but |
//| modest sample (professional standard wants 200+ for full confidence). |
//| Discovery-period backtest data was 0% real ticks (no tick archive     |
//| exists that far back for US30) - only the validation half could be    |
//| independently confirmed as genuinely real-tick-backed. This is why    |
//| this EA is being run in a live demo forward-test rather than          |
//| deployed on real capital directly.                                     |
//+------------------------------------------------------------------+
#property copyright "Forward-test deployment - monitor for 2 months before considering real capital"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

#define EA_TAG "[US30_ShortBBFade]"

input int    InpBBPeriod        = 20;
input double InpBBDeviation     = 2.0;
input int    InpATRPeriod       = 14;
input double InpSL_ATR_Mult     = 1.0;
input double InpTP_RR           = 1.5;
input double InpRiskPercent     = 1.0;
input int    InpMaxTradesPerDay = 8;
input int    InpMaxSpreadPoints = 30;
input bool   InpEnableLongs     = false;  // excluded - confirmed weaker leg (27.8% WR in validation)
input bool   InpEnableShorts    = true;   // confirmed leg (53.1% WR in validation)
input ulong  InpMagicNumber     = 55211101;
input int    InpTicketsPerTrade = 4;      // Full-size tickets opened together per signal (4x normal per-signal risk)

datetime g_currentDay = 0;
int      g_tradesToday = 0;
int      g_atrHandle = INVALID_HANDLE;
int      g_bbHandle = INVALID_HANDLE;
datetime g_lastBarTime = 0;
#define LASTBAR_GVAR "US30_ShortBBFade_lastBar"

int OnInit()
{
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetExpertMagicNumber(InpMagicNumber);

   // Restore the last-acted-on bar across restarts (GlobalVariable persists in
   // the terminal, unlike a plain global which resets to 0 on every reinit).
   // Without this, a restart re-evaluates the still-most-recent closed bar as
   // if it were new, and re-fires a signal that already traded -- confirmed
   // live 2026-08-21: 4 container restarts in a row duplicated the same
   // EURUSD/GBPUSD signal on LowVolBigCandleReversion 3x each.
   if(GlobalVariableCheck(LASTBAR_GVAR))
      g_lastBarTime = (datetime)GlobalVariableGet(LASTBAR_GVAR);

   g_atrHandle = iATR(_Symbol, PERIOD_H1, InpATRPeriod);
   if(g_atrHandle == INVALID_HANDLE) { Print(EA_TAG, " ATR handle failed"); return(INIT_FAILED); }
   g_bbHandle = iBands(_Symbol, PERIOD_H1, InpBBPeriod, 0, InpBBDeviation, PRICE_CLOSE);
   if(g_bbHandle == INVALID_HANDLE) { Print(EA_TAG, " Bands handle failed"); return(INIT_FAILED); }

   Print(EA_TAG, " initialized on ", _Symbol, " ", EnumToString(PERIOD_H1),
         " | shorts=", InpEnableShorts, " longs=", InpEnableLongs,
         " | magic=", InpMagicNumber, " | risk%/ticket=", InpRiskPercent,
         " | tickets/signal=", InpTicketsPerTrade);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   Print(EA_TAG, " deinit, reason=", reason);
   if(g_atrHandle != INVALID_HANDLE) IndicatorRelease(g_atrHandle);
   if(g_bbHandle != INVALID_HANDLE) IndicatorRelease(g_bbHandle);
}

void ResetDailyIfNewDay()
{
   datetime todayStart = TimeCurrent() - (TimeCurrent() % 86400);
   if(todayStart != g_currentDay) { g_currentDay = todayStart; g_tradesToday = 0; }
}
bool SpreadOk() { return SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) <= InpMaxSpreadPoints; }

bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == (long)InpMagicNumber)
         return true;
   }
   return false;
}

double CalcLots(double riskPercent, double stopDistancePrice)
{
   if(stopDistancePrice <= 0) return -1;
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double contractSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(tickValue <= 0 || tickSize <= 0) return -1;
   double moneyPerPriceUnitPerLot = tickValue / tickSize;
   if(contractSize > 0) moneyPerPriceUnitPerLot = MathMax(moneyPerPriceUnitPerLot, contractSize);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double moneyToRisk = balance * (riskPercent/100.0);
   double rawLots = moneyToRisk / (stopDistancePrice*moneyPerPriceUnitPerLot);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lots = MathFloor(rawLots/step)*step;
   if(lots < minVol) return -1;
   if(lots > maxVol) lots = maxVol;
   return lots;
}

void OnTick()
{
   ResetDailyIfNewDay();
   if(!SpreadOk()) return;

   datetime t[];
   if(CopyTime(_Symbol, PERIOD_H1, 0, 1, t) < 1) return;
   if(t[0] == g_lastBarTime) return;
   g_lastBarTime = t[0];
   GlobalVariableSet(LASTBAR_GVAR, (double)t[0]);

   if(HasOpenPosition()) return;
   if(g_tradesToday >= InpMaxTradesPerDay) return;

   double atrBuf[];
   if(CopyBuffer(g_atrHandle, 0, 1, 1, atrBuf) < 1) return;
   double atr = atrBuf[0];
   if(atr <= 0) return;

   double upperBuf[], lowerBuf[];
   if(CopyBuffer(g_bbHandle, 1, 1, 1, upperBuf) < 1) return;
   if(CopyBuffer(g_bbHandle, 2, 1, 1, lowerBuf) < 1) return;
   double upper = upperBuf[0], lower = lowerBuf[0];

   double c[];
   if(CopyClose(_Symbol, PERIOD_H1, 1, 1, c) < 1) return;
   double lastClose = c[0];

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   if(lastClose > upper && InpEnableShorts)  // closed above upper band -> fade SHORT
   {
      double slPrice = bid + atr*InpSL_ATR_Mult;
      double riskDistance = slPrice - bid;
      if(riskDistance <= 0) { Print(EA_TAG, " SHORT skipped: bad risk distance"); return; }
      double tpPrice = bid - riskDistance*InpTP_RR;
      double lots = CalcLots(InpRiskPercent, riskDistance);
      if(lots <= 0) { Print(EA_TAG, " SHORT skipped: lot size"); return; }
      int opened = 0;
      for(int k = 0; k < InpTicketsPerTrade; k++)
      {
         if(trade.Sell(lots, _Symbol, bid, slPrice, tpPrice, "US30ShortBBFade"))
         {
            opened++;
            Print(EA_TAG, " SHORT ticket ", (k+1), "/", InpTicketsPerTrade, " opened: lots=", lots, " entry=", bid, " SL=", slPrice, " TP=", tpPrice);
         }
         else
         {
            Print(EA_TAG, " SHORT ticket ", (k+1), "/", InpTicketsPerTrade, " failed: ", trade.ResultRetcodeDescription());
         }
      }
      if(opened > 0)
      {
         g_tradesToday++;
         Print(EA_TAG, " SHORT signal placed ", opened, "/", InpTicketsPerTrade, " tickets (", lots, " lots each)");
      }
   }
   else if(lastClose < lower && InpEnableLongs)  // closed below lower band -> fade LONG
   {
      double slPrice = ask - atr*InpSL_ATR_Mult;
      double riskDistance = ask - slPrice;
      if(riskDistance <= 0) { Print(EA_TAG, " LONG skipped: bad risk distance"); return; }
      double tpPrice = ask + riskDistance*InpTP_RR;
      double lots = CalcLots(InpRiskPercent, riskDistance);
      if(lots <= 0) { Print(EA_TAG, " LONG skipped: lot size"); return; }
      int opened = 0;
      for(int k = 0; k < InpTicketsPerTrade; k++)
      {
         if(trade.Buy(lots, _Symbol, ask, slPrice, tpPrice, "US30ShortBBFade"))
         {
            opened++;
            Print(EA_TAG, " LONG ticket ", (k+1), "/", InpTicketsPerTrade, " opened: lots=", lots, " entry=", ask, " SL=", slPrice, " TP=", tpPrice);
         }
         else
         {
            Print(EA_TAG, " LONG ticket ", (k+1), "/", InpTicketsPerTrade, " failed: ", trade.ResultRetcodeDescription());
         }
      }
      if(opened > 0)
      {
         g_tradesToday++;
         Print(EA_TAG, " LONG signal placed ", opened, "/", InpTicketsPerTrade, " tickets (", lots, " lots each)");
      }
   }
}
