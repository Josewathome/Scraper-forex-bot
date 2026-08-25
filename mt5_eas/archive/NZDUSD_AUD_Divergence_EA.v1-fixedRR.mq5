//+------------------------------------------------------------------+
//|                              NZDUSD_AUD_Divergence_EA.mq5          |
//| LIVE. Cross-pair momentum "catch-up" signal: NZDUSD and AUDUSD are |
//| strongly positively correlated (measured +0.89 on H1 returns,      |
//| 2025-2026). When NZDUSD (the driver) makes an extreme momentum     |
//| move but AUDUSD (traded here) hasn't kept pace, buy/sell AUDUSD    |
//| to catch up. Direction matters: this only holds NZDUSD->AUDUSD;    |
//| the reverse (AUDUSD leading) was tested and does NOT work.         |
//|                                                                      |
//| VALIDATION (real MT5 Strategy Tester, H1, RR=1.5, 2026 data only    |
//| per explicit instruction that only 2026 history is trusted on this  |
//| broker):                                                             |
//|   Discovery  (2026.01.01-2026.05.01): 17 trades, +$359.94, PF 1.94, |
//|     Sharpe 4.07, shorts 58.3% won, longs 60.0% won                  |
//|   Validation (2026.05.01-2026.08.03): 34 trades, +$580.28, PF 1.65, |
//|     Sharpe 6.81, shorts 63.6% won, longs 52.2% won                  |
//|   Reverse direction (AUDUSD driving NZDUSD) tested and FAILED both  |
//|     halves (-$74.82 PF 0.79, -$222.02 PF 0.49) - confirms this is a |
//|     directional lead-lag relationship, not a symmetric one.         |
//| Caveats: single ~7-month regime (2025 data excluded per broker      |
//| history-quality concern), moderate sample (51 trades combined),     |
//| some signal clustering during sustained divergence episodes.        |
//| Treat as promising-but-unproven pending live forward monitoring.    |
//+------------------------------------------------------------------+
#property copyright "Live - deployed for forward monitoring"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

#define EA_TAG "[NZDUSD_AUD_Divergence]"

input string DriverSymbol       = "NZDUSD";
input int    InpMomWindow       = 48;
input int    InpZWindow         = 200;
input double InpZThreshold      = 1.5;
input double InpLagThreshold    = 0.0030;
input int    InpATRPeriod       = 14;
input double InpSL_ATR_Mult     = 1.5;
input double InpTP_RR           = 1.5;
input double InpRiskPercent     = 1.0;
input int    InpMaxTradesPerDay = 4;
input int    InpMaxSpreadPoints = 30;
input ulong  InpMagicNumber     = 55211801;

datetime g_currentDay = 0;
int      g_tradesToday = 0;
int      g_atrHandle = INVALID_HANDLE;
datetime g_lastBarTime = 0;

int OnInit()
{
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetExpertMagicNumber(InpMagicNumber);
   if(!SymbolSelect(DriverSymbol, true))
   {
      Print(EA_TAG, " failed to select driver symbol ", DriverSymbol);
      return(INIT_FAILED);
   }
   g_atrHandle = iATR(_Symbol, PERIOD_H1, InpATRPeriod);
   if(g_atrHandle == INVALID_HANDLE) { Print(EA_TAG, " ATR handle failed"); return(INIT_FAILED); }
   Print(EA_TAG, " initialized on ", _Symbol, " H1, driver=", DriverSymbol, " magic=", InpMagicNumber);
   return(INIT_SUCCEEDED);
}
void OnDeinit(const int reason)
{
   Print(EA_TAG, " deinit, reason=", reason);
   if(g_atrHandle != INVALID_HANDLE) IndicatorRelease(g_atrHandle);
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

// momentum series for a symbol: mom[k] = (close[k] - close[k+momWindow]) / close[k+momWindow]
// where index k=0 is the most recent CLOSED bar (shift=1 in MT5 terms), increasing k = further back
bool BuildMomSeries(string sym, int momWindow, int count, double &mom[])
{
   double closes[];
   ArraySetAsSeries(closes, true); // index 0 = most recent closed bar
   int need = count + momWindow + 1;
   if(CopyClose(sym, PERIOD_H1, 1, need, closes) < need) return false;
   ArrayResize(mom, count);
   for(int k = 0; k < count; k++)
   {
      double now = closes[k];
      double past = closes[k+momWindow];
      if(past == 0) return false;
      mom[k] = (now - past) / past;
   }
   return true;
}

void OnTick()
{
   ResetDailyIfNewDay();
   if(!SpreadOk()) return;

   datetime t[];
   if(CopyTime(_Symbol, PERIOD_H1, 0, 1, t) < 1) return;
   if(t[0] == g_lastBarTime) return;
   g_lastBarTime = t[0];

   if(HasOpenPosition()) return;
   if(g_tradesToday >= InpMaxTradesPerDay) return;

   double atrBuf[];
   if(CopyBuffer(g_atrHandle, 0, 1, 1, atrBuf) < 1) return;
   double atr = atrBuf[0];
   if(atr <= 0) return;

   double driverMom[];
   if(!BuildMomSeries(DriverSymbol, InpMomWindow, InpZWindow, driverMom)) return;
   double tradedMom[];
   if(!BuildMomSeries(_Symbol, InpMomWindow, 1, tradedMom)) return;

   double dNow = driverMom[0];
   double mean=0; for(int k=0;k<InpZWindow;k++) mean += driverMom[k]; mean /= InpZWindow;
   double var=0; for(int k=0;k<InpZWindow;k++) var += (driverMom[k]-mean)*(driverMom[k]-mean); var /= InpZWindow;
   double std = MathSqrt(var);
   if(std <= 0) return;
   double dz = (dNow - mean) / std;
   if(MathAbs(dz) < InpZThreshold) return;

   double tMom = tradedMom[0];
   int signal = 0;
   // NZDUSD and AUDUSD are POSITIVELY correlated (measured +0.89) - traded should move WITH driver
   if(dz > InpZThreshold && tMom < InpLagThreshold) signal = 1;        // driver up strongly, traded lagging -> long (catch up)
   else if(dz < -InpZThreshold && tMom > -InpLagThreshold) signal = -1; // driver down strongly, traded lagging -> short (catch down)
   if(signal == 0) return;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   if(signal == 1)
   {
      double slPrice = ask - atr*InpSL_ATR_Mult;
      double riskDistance = ask - slPrice;
      if(riskDistance <= 0) return;
      double tpPrice = ask + riskDistance*InpTP_RR;
      double lots = CalcLots(InpRiskPercent, riskDistance);
      if(lots <= 0) { Print(EA_TAG, " LONG skipped: lot size"); return; }
      if(trade.Buy(lots, _Symbol, ask, slPrice, tpPrice, "NZDAUDdiv"))
      {
         g_tradesToday++;
         Print(EA_TAG, " LONG opened: dz=", dz, " tMom=", tMom, " lots=", lots, " entry=", ask, " SL=", slPrice, " TP=", tpPrice);
      }
      else Print(EA_TAG, " LONG order failed: ", trade.ResultRetcodeDescription());
   }
   else if(signal == -1)
   {
      double slPrice = bid + atr*InpSL_ATR_Mult;
      double riskDistance = slPrice - bid;
      if(riskDistance <= 0) return;
      double tpPrice = bid - riskDistance*InpTP_RR;
      double lots = CalcLots(InpRiskPercent, riskDistance);
      if(lots <= 0) { Print(EA_TAG, " SHORT skipped: lot size"); return; }
      if(trade.Sell(lots, _Symbol, bid, slPrice, tpPrice, "NZDAUDdiv"))
      {
         g_tradesToday++;
         Print(EA_TAG, " SHORT opened: dz=", dz, " tMom=", tMom, " lots=", lots, " entry=", bid, " SL=", slPrice, " TP=", tpPrice);
      }
      else Print(EA_TAG, " SHORT order failed: ", trade.ResultRetcodeDescription());
   }
}
